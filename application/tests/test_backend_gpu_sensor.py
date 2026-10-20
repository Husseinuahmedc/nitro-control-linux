"""Offline NVML/pipe/process fixtures only. No native GPU library is loaded."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nitro_control.auth import process_identity
from nitro_control.controller import Controller
from nitro_control.gpu_sensor import (GpuSensor, MAX_FRAME, Nvml, _spawn, protect_parent, serve)
from nitro_control.telemetry import Telemetry
from test_backend import Clock, FakeHardware, WorkspaceTest


class Call:
    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class Library:
    def __init__(self, *, clock=None, init_delay=0, first_read_delay=0, temperatures=(55,)):
        self.clock = clock
        self.init_delay = init_delay
        self.first_read_delay = first_read_delay
        self.temperatures = temperatures
        self.calls = []
        self.nvmlInit_v2 = Call(self.init)
        self.nvmlDeviceGetCount_v2 = Call(self.count)
        self.nvmlDeviceGetHandleByIndex_v2 = Call(self.handle)
        self.nvmlDeviceGetTemperature = Call(self.temperature)

    def delay(self, amount):
        if self.clock is not None:
            self.clock.advance(amount)
        else:
            time.sleep(amount)

    def init(self):
        self.calls.append("init")
        self.delay(self.init_delay)
        return 0

    def count(self, pointer):
        self.calls.append("count")
        pointer._obj.value = len(self.temperatures)
        return 0

    def handle(self, index, pointer):
        self.calls.append("handle")
        pointer._obj.value = index + 1
        return 0

    def temperature(self, handle, sensor, pointer):
        self.calls.append("temperature")
        if sensor != 0:
            raise AssertionError("Only temperature sensor zero is allowed")
        delay, self.first_read_delay = self.first_read_delay, 0
        self.delay(delay)
        value = self.temperatures[handle.value - 1]
        if value is None:
            return 999
        pointer._obj.value = value
        return 0


class PipeProcess:
    """Popen-shaped in-memory fake with a real local nonblocking pipe."""
    def __init__(self, *, delayed_exit=False):
        read_fd, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.returncode = None
        self.delayed_exit = delayed_exit
        self.done = threading.Event()
        self.reaped = threading.Event()
        self.killed = False

    def send(self, **changes):
        data = {"schema": 1, "sampled_at": 100.0, "temperature_c": 55, **changes}
        os.write(self.write_fd, json.dumps(data).encode() + b"\n")

    def poll(self):
        return self.returncode

    def terminate(self):
        if not self.delayed_exit:
            self.kill()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.done.set()

    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired("offline-fixture", timeout)
        self.reaped.set()
        return self.returncode

    def close(self):
        self.kill()
        self.stdout.close()
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None


class NvmlTests(unittest.TestCase):
    def test_init_does_not_age_first_fast_temperature_and_only_readonly_apis_are_called(self):
        clock = Clock()
        library = Library(clock=clock, init_delay=2.1, temperatures=(54, 62))
        nvml = Nvml(library)
        value, sampled_at = nvml.sample(clock)
        self.assertEqual(value, 62)
        self.assertEqual(sampled_at, 102.1)
        self.assertEqual(library.calls, ["init", "count", "handle", "handle", "temperature", "temperature"])

    def test_slow_temperature_is_discarded_but_next_fast_read_is_fresh(self):
        clock = Clock()
        library = Library(clock=clock, first_read_delay=2.1)
        nvml = Nvml(library)
        self.assertEqual(nvml.sample(clock), (None, 100))
        self.assertEqual(nvml.sample(clock), (55, 102.1))

    def test_invalid_zero_and_failed_temperature_never_fall_back(self):
        for values in ((0,), (126,), (None,), (55, None)):
            with self.subTest(values=values):
                self.assertIsNone(Nvml(Library(temperatures=values)).sample()[0])

    def test_missing_library_emits_unavailable_and_broken_pipe_exits(self):
        records = []
        serve(factory=Mock(side_effect=OSError("unavailable")),
              write=lambda _fd, payload: records.append(json.loads(payload)))
        self.assertEqual(records, [{"schema": 1, "sampled_at": None, "temperature_c": None}])
        writer = Mock(side_effect=BrokenPipeError())
        serve(factory=lambda: Nvml(Library()), write=writer, limit=3)
        self.assertEqual(writer.call_count, 1)

    def test_helper_queries_one_hz_without_retimestamps_after_slow_init_or_read(self):
        clock = Clock()
        library = Library(clock=clock, init_delay=2.1, first_read_delay=2.2)
        records = []
        serve(factory=lambda: Nvml(library), clock=clock, sleep=clock.advance, limit=3,
              write=lambda _fd, payload: records.append(json.loads(payload)))
        self.assertIsNone(records[0]["temperature_c"])
        self.assertAlmostEqual(records[0]["sampled_at"], 102.1)
        self.assertAlmostEqual(records[1]["sampled_at"], 104.3)
        self.assertEqual(records[1]["temperature_c"], 55)
        self.assertAlmostEqual(records[2]["sampled_at"] - records[1]["sampled_at"], 1.0)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.process = PipeProcess()
        self.addCleanup(self.process.close)
        self.spawn = Mock(return_value=self.process)
        self.sensor = GpuSensor(clock=self.clock, spawn=self.spawn)
        self.addCleanup(self.sensor.close)

    def test_warmup_is_nonblocking_single_process_and_original_age_is_preserved(self):
        for _ in range(20):
            started = time.monotonic()
            self.assertEqual(self.sensor.sample(), (None, None))
            self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(self.spawn.call_count, 1)
        self.process.send()
        self.assertEqual(self.sensor.sample(), (55, 100))
        self.clock.advance(0.25)
        self.assertEqual(self.sensor.sample(), (55, 100))
        self.clock.advance(2)
        self.assertEqual(self.sensor.sample(), (None, 100))
        self.process.send(sampled_at=self.clock(), temperature_c=56)
        self.assertEqual(self.sensor.sample(), (56, self.clock()))
        self.assertEqual(self.spawn.call_count, 1)

    def test_partial_frame_and_explicit_unavailable_do_not_reuse_old_temperature(self):
        self.sensor.sample()
        frame = b'{"schema":1,"sampled_at":100,"temperature_c":55}\n'
        os.write(self.process.write_fd, frame[:25])
        self.assertEqual(self.sensor.sample(), (None, None))
        os.write(self.process.write_fd, frame[25:])
        self.assertEqual(self.sensor.sample(), (55, 100))
        self.process.send(temperature_c=None)
        self.assertEqual(self.sensor.sample(), (None, 100))

    def test_malformed_oversized_nonfinite_future_bool_and_zero_fail_closed_no_respawn(self):
        payloads = [b"bad\n", b"x" * (MAX_FRAME + 1),
                    b'{"schema":1,"sampled_at":NaN,"temperature_c":55}\n',
                    b'{"schema":1,"sampled_at":101,"temperature_c":55}\n',
                    b'{"schema":true,"sampled_at":100,"temperature_c":55}\n',
                    b'{"schema":1,"sampled_at":100,"temperature_c":0}\n',
                    b'{"schema":1,"sampled_at":100,"sampled_at":100,"temperature_c":55}\n']
        for payload in payloads:
            with self.subTest(payload=payload[:70]):
                process = PipeProcess()
                spawn = Mock(return_value=process)
                sensor = GpuSensor(clock=self.clock, spawn=spawn)
                try:
                    sensor.sample()
                    os.write(process.write_fd, payload)
                    self.assertEqual(sensor.sample(), (None, None))
                    self.assertEqual(sensor.sample(), (None, None))
                    self.assertEqual(spawn.call_count, 1)
                finally:
                    sensor.close()
                    process.close()

    def test_close_is_nonblocking_idempotent_and_reaps_blocked_child(self):
        self.process.delayed_exit = True
        self.sensor.sample()
        started = time.monotonic()
        self.sensor.close()
        self.sensor.close()
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertTrue(self.process.reaped.wait(0.5))
        self.assertTrue(self.process.killed)
        self.assertEqual(self.sensor.sample(), (None, None))
        self.assertEqual(self.spawn.call_count, 1)

    def test_close_racing_blocked_spawn_never_waits_or_leaves_an_extra_helper(self):
        entered, release = threading.Event(), threading.Event()

        def spawn():
            entered.set()
            release.wait(1)
            return self.process

        sensor = GpuSensor(spawn=spawn)
        thread = threading.Thread(target=sensor.sample)
        thread.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        sensor.close()
        self.assertLess(time.monotonic() - started, 0.05)
        release.set()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.process.reaped.wait(0.5))

    def test_cpu_and_fans_keep_refreshing_while_gpu_unavailable_and_stale_gpu_cancels_manual(self):
        hardware = FakeHardware()
        telemetry = Telemetry(hardware, clock=self.clock, gpu_factory=lambda: self.sensor)
        telemetry._cpu_temperature = Mock(return_value=64)
        controller = Controller(hardware, telemetry, clock=self.clock, alive=lambda _peer: True)
        self.addCleanup(controller.shutdown)
        controller.tick()
        self.assertEqual(controller.status()["cpu"]["temperature_c"], 64)
        self.assertIsNone(controller.status()["gpu"]["temperature_c"])
        self.process.send()
        controller.tick()
        peer = process_identity(os.getpid())
        token = controller.handle({"op": "acquire"}, peer, authorized=True)["token"]
        result = controller.handle({"op": "set", "token": token,
                                    "cpu": {"mode": "manual", "percent": 80}}, peer)
        self.assertTrue(result["ok"], result)
        self.clock.advance(2.1)
        controller.tick()
        status = controller.status()
        self.assertEqual(status["cpu"]["temperature_c"], 64)
        self.assertIsNone(status["gpu"]["temperature_c"])
        self.assertFalse(status["lease"]["active"])
        self.assertEqual(hardware.channels["cpu"]["mode"], "auto")
        self.assertEqual(hardware.channels["gpu"]["mode"], "auto")

    def test_telemetry_close_before_first_refresh_never_starts_provider(self):
        factory = Mock()
        telemetry = Telemetry(FakeHardware(), gpu_factory=factory)
        telemetry._cpu_temperature = lambda: 64
        telemetry.close()
        telemetry.close()
        telemetry.refresh()
        self.assertEqual(telemetry.cached_snapshot()["cpu"]["temperature_c"], 64)
        self.assertIsNone(telemetry.cached_snapshot()["gpu"]["temperature_c"])
        factory.assert_not_called()


class LaunchTests(unittest.TestCase):
    def test_protected_launch_sanitizes_environment_and_inherits_process_group(self):
        with (patch("nitro_control.gpu_sensor.subprocess.Popen") as spawn,
              patch.dict(os.environ, {"LD_PRELOAD": "untrusted", "PYTHONPATH": "/untrusted"})):
            _spawn()
        command = spawn.call_args.args[0]
        options = spawn.call_args.kwargs
        self.assertTrue(command[0].startswith("/usr/bin/"))
        self.assertEqual(command[1:5], ["-P", "-s", "-m", "nitro_control.gpu_sensor"])
        self.assertNotIn("LD_PRELOAD", options["env"])
        self.assertNotEqual(options["env"]["PYTHONPATH"], "/untrusted")
        self.assertNotIn("start_new_session", options)
        self.assertNotIn("shell", options)

    def test_root_cannot_launch_user_writable_helper_code(self):
        with (patch("nitro_control.gpu_sensor.os.geteuid", return_value=0),
              patch("nitro_control.gpu_sensor.Hardware._trusted_code", return_value=False),
              patch("nitro_control.gpu_sensor.subprocess.Popen") as spawn):
            with self.assertRaises(OSError):
                _spawn()
        spawn.assert_not_called()

    def test_real_fixture_process_warmup_does_not_delay_snapshots_and_close_reaps(self):
        parent = process_identity(os.getpid())
        processes = []

        def spawn():
            process = subprocess.Popen([sys.executable, __file__, "fixture-helper", str(parent.pid),
                                        str(parent.start_ticks)], stdout=subprocess.PIPE, bufsize=0)
            processes.append(process)
            return process

        sensor = GpuSensor(spawn=spawn)
        telemetry = Telemetry(FakeHardware(), gpu_factory=lambda: sensor)
        telemetry._cpu_temperature = lambda: 64
        telemetry.runtime_dir = Path(__file__).resolve().parent / ".no-recovery-fixture"
        try:
            started = time.monotonic()
            snapshot = telemetry.snapshot()
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(snapshot["cpu"]["temperature_c"], 64)
            self.assertIsNone(snapshot["gpu"]["temperature_c"])
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                began = time.monotonic()
                snapshot = telemetry.snapshot()
                self.assertLess(time.monotonic() - began, 0.1)
                if snapshot["gpu"]["temperature_c"] is not None:
                    break
                time.sleep(0.05)
            self.assertEqual(snapshot["gpu"]["temperature_c"], 55)
            self.assertEqual(len(processes), 1)
            self.assertEqual(os.getpgid(processes[0].pid), os.getpgrp())
        finally:
            began = time.monotonic()
            telemetry.close()
            self.assertLess(time.monotonic() - began, 0.1)
            for process in processes:
                process.wait(timeout=1)


class ProcessLifetimeTests(WorkspaceTest):
    def test_parent_death_kills_helper_even_while_native_init_is_blocked(self):
        marker = self.root / "helper.json"
        owner = subprocess.Popen([sys.executable, __file__, "fixture-owner", str(marker)],
                                 start_new_session=True)
        peer = None
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    data = json.loads(marker.read_text())
                    peer = process_identity(data["pid"])
                    if peer is not None:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.01)
            self.assertIsNotNone(peer)
            self.assertEqual(data["pgid"], owner.pid)
            owner.kill()
            owner.wait(timeout=1)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and process_identity(peer.pid) == peer:
                time.sleep(0.01)
            self.assertNotEqual(process_identity(peer.pid), peer)
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=1)
            if peer is not None and process_identity(peer.pid) == peer:
                os.kill(peer.pid, 9)  # Only this known, identity-checked offline fixture.


if __name__ == "__main__":
    if sys.argv[1:2] == ["fixture-owner"]:
        parent = process_identity(os.getpid())
        with subprocess.Popen([sys.executable, __file__, "fixture-slow-helper", str(parent.pid),
                               str(parent.start_ticks), sys.argv[2]], stdout=subprocess.PIPE) as child:
            child.wait()
    elif sys.argv[1:2] in (["fixture-helper"], ["fixture-slow-helper"]):
        from nitro_control.auth import Peer
        parent = Peer(os.geteuid(), int(sys.argv[2]), int(sys.argv[3]))
        if not protect_parent(parent):
            raise SystemExit(1)
        slow = sys.argv[1] == "fixture-slow-helper"
        if slow:
            Path(sys.argv[4]).write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}))
        os.set_blocking(1, False)
        serve(factory=lambda: Nvml(Library(init_delay=10 if slow else 2.1)),
              alive=lambda: os.getppid() == parent.pid)
    else:
        unittest.main()
