"""Read-only telemetry shared by the controller and the disconnected GUI."""

import copy
from pathlib import Path
import subprocess
import threading
import time

from .hardware import CHANNELS, MAX_SAMPLE_AGE, Hardware, read_integer
from .gpu_sensor import GpuSensor


def empty_channel():
    return {"temperature_c": None, "rpm": None, "mode": None, "pwm": None,
            "percent": None, "requested_mode": None, "requested_percent": None,
            "confirmation": "unsupported", "rpm_response": "unverified"}


class Telemetry:
    def __init__(self, hardware=None, *, clock=time.monotonic, runner=None,
                 runtime_dir="/run/nitro-control", gpu_factory=None):
        self.hardware = hardware if hardware is not None else Hardware()
        self.clock = clock
        self.runner = runner
        self.runtime_dir = Path(runtime_dir)
        self.data = {channel: empty_channel() for channel in CHANNELS}
        self.times = {}
        self.sampled_at = None
        self.lock = threading.RLock()
        self._gpu_query_at = None
        self._gpu_value = None
        self._gpu_sensor = None
        self._gpu_factory = gpu_factory
        self._closed = threading.Event()

    def _cpu_temperature(self):
        values = []
        try:
            for node in (self.hardware.sys_root / "class/hwmon").glob("hwmon*"):
                if (node / "name").read_text().strip() != "coretemp":
                    continue
                for label in node.glob("temp*_label"):
                    if label.read_text().strip().startswith("Package id "):
                        number = read_integer(label.with_name(label.name.replace("_label", "_input")),
                                              1, 125000)
                        if number is not None:
                            values.append(number / 1000)
        except (OSError, UnicodeError):
            return None
        return max(values) if values else None

    def _gpu_temperature(self):
        if self._closed.is_set():
            return None
        if self.runner is None:
            with self.lock:
                if self._gpu_sensor is None:
                    self._gpu_sensor = (GpuSensor(clock=self.clock) if self._gpu_factory is None
                                        else self._gpu_factory())
                sensor = self._gpu_sensor
            if self._closed.is_set():
                sensor.close()
                return None
            self._gpu_value, self._gpu_query_at = sensor.sample()
            return self._gpu_value
        # Explicit runner injection is retained for offline tests only. The
        # production path never launches nvidia-smi or waits for a GPU query.
        now = self.clock()
        if self._gpu_query_at is not None and now - self._gpu_query_at < 1.0:
            return self._gpu_value
        self._gpu_query_at = now
        self._gpu_value = None
        try:
            result = self.runner(["/usr/bin/nvidia-smi", "--query-gpu=temperature.gpu",
                                  "--format=csv,noheader,nounits"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, encoding="ascii", timeout=1, check=True)
            lines = result.stdout.strip().splitlines()
            if not lines or len(lines) > 32:
                return None
            values = [int(line.strip()) for line in lines]
            self._gpu_value = max(values) if all(0 < value <= 125 for value in values) else None
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            pass
        return self._gpu_value

    def record_channel(self, channel, data, sampled_at):
        with self.lock:
            for key in ("mode", "pwm", "percent", "rpm"):
                self.data[channel][key] = data.get(key)
            self.times[channel + ".fan"] = sampled_at

    def refresh(self):
        sampled_at = self.clock()
        channels = {channel: empty_channel() for channel in CHANNELS}
        times = {}
        for channel in CHANNELS:
            times[channel + ".fan"] = self.clock()
            try:
                data = self.hardware.read_channel(channel)
            except Exception:
                data = {}
            for key in ("mode", "pwm", "percent", "rpm"):
                channels[channel][key] = data.get(key)
        for channel, read in (("cpu", self._cpu_temperature), ("gpu", self._gpu_temperature)):
            times[channel + ".temperature"] = self.clock()
            channels[channel]["temperature_c"] = read()
            if channel == "gpu" and self._gpu_query_at is not None:
                times[channel + ".temperature"] = self._gpu_query_at
            elif channel == "gpu" and self._gpu_sensor is not None:
                times.pop(channel + ".temperature", None)
        # Publish a completed sample only. Slow reads never hold a cache/state
        # lock or make a previous reading appear to have a new timestamp.
        with self.lock:
            if self._closed.is_set():
                channels["gpu"]["temperature_c"] = None
                times.pop("gpu.temperature", None)
            self.data = channels
            self.times = times
            self.sampled_at = sampled_at

    def close(self):
        """Read-only, nonblocking, idempotent helper cancellation. Do not reuse."""
        self._closed.set()
        with self.lock:
            sensor = self._gpu_sensor
            self.data["gpu"]["temperature_c"] = None
            self.times.pop("gpu.temperature", None)
        if sensor is not None:
            sensor.close()

    def age(self, key):
        with self.lock:
            sampled_at = self.times.get(key)
        return float("inf") if sampled_at is None else max(0.0, self.clock() - sampled_at)

    def cached_snapshot(self):
        with self.lock:
            channels = copy.deepcopy(self.data)
            times = dict(self.times)
            sampled_at = self.sampled_at
        now = self.clock()
        for channel in CHANNELS:
            if now - times.get(channel + ".temperature", float("-inf")) > MAX_SAMPLE_AGE:
                channels[channel]["temperature_c"] = None
            if now - times.get(channel + ".fan", float("-inf")) > MAX_SAMPLE_AGE:
                for key in ("rpm", "mode", "pwm", "percent"):
                    channels[channel][key] = None
        hardware = self.hardware
        return {"schema": 1,
                "backend": {"connected": False, "control_available": hardware.control_available,
                            "control_enabled": False, "hardware_verified": hardware.hardware_verified,
                            "reason": hardware.reason},
                **channels,
                "lease": {"active": False, "owned_by_client": False, "owner_pid": None,
                          "remaining_seconds": 0},
                "recovery": {"state": "read_only", "error": None}, "error": None,
                "sample_age_seconds": (0.0 if sampled_at is None else
                                       max(0.0, now - sampled_at)), "events": []}

    def snapshot(self):
        self.refresh()
        status = self.cached_snapshot()
        # This file contains no tokens and is readable even if the worker is down.
        from .recovery import read_state
        try:
            state = read_state(self.runtime_dir)
            if state:
                status["recovery"] = {"state": state["state"], "error": state.get("error")}
        except (OSError, ValueError) as exc:
            status["recovery"] = {"state": "fault", "error": f"Recovery record unavailable/untrusted: {exc}"[:500]}
        return status
