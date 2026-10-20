"""Persistent, read-only NVML temperature helper and bounded nonblocking reader.

The native library is loaded only in the helper process, never in the control
loop. Only init, count, handle lookup and temperature(sensor 0) are bound.
"""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from .auth import Peer, process_identity
from .hardware import Hardware, MAX_SAMPLE_AGE, trusted_path

MAX_FRAME = 256
READ_BUDGET = 16384
SYSTEM_PYTHON = Path("/usr/bin/python")
NVML_LIBRARY = Path("/usr/lib/libnvidia-ml.so.1")


class Nvml:
    def __init__(self, library=None):
        import ctypes

        self.ctypes = ctypes
        if library is None:
            path = NVML_LIBRARY.resolve(strict=True)
            if not trusted_path(path):
                raise OSError("NVML library is not system-owned/protected")
            library = ctypes.CDLL(str(path))
        self.library = library
        init = library.nvmlInit_v2
        count = library.nvmlDeviceGetCount_v2
        handle = library.nvmlDeviceGetHandleByIndex_v2
        self.temperature = library.nvmlDeviceGetTemperature
        for function, args in ((init, []), (count, [ctypes.POINTER(ctypes.c_uint)]),
                               (handle, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]),
                               (self.temperature, [ctypes.c_void_p, ctypes.c_uint,
                                                   ctypes.POINTER(ctypes.c_uint)])):
            function.argtypes = args
            function.restype = ctypes.c_int
        if init() != 0:
            raise OSError("NVML initialization unavailable")
        devices = ctypes.c_uint()
        if count(ctypes.byref(devices)) != 0 or not 1 <= devices.value <= 32:
            raise OSError("NVML device count unavailable")
        self.handles = []
        for index in range(devices.value):
            device = ctypes.c_void_p()
            if handle(index, ctypes.byref(device)) != 0 or not device.value:
                raise OSError("NVML handle unavailable")
            self.handles.append(device)

    def sample(self, clock=time.monotonic):
        values, timestamps = [], []
        for device in self.handles:
            value = self.ctypes.c_uint()
            timestamps.append(clock())  # Immediately before GetTemperature, never before init.
            result = self.temperature(device, 0, self.ctypes.byref(value))
            values.append(value.value if result == 0 and 0 < value.value <= 125 else None)
        sampled_at = min(timestamps)
        temperature = max(values) if all(value is not None for value in values) else None
        if clock() - sampled_at > MAX_SAMPLE_AGE:
            temperature = None
        return temperature, sampled_at


def protect_parent(parent):
    """A blocked native init/read must not outlive its creating process/thread."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        return False
    return os.getppid() == parent.pid and process_identity(parent.pid) == parent


def serve(*, factory=Nvml, clock=time.monotonic, sleep=time.sleep, write=os.write, output=1,
          alive=lambda: True, limit=None):
    """Read-only process loop; dependencies/iteration limit are for offline tests only."""
    try:
        nvml = factory()
    except (OSError, AttributeError, ValueError):
        nvml = None
    count = 0
    while alive() and (limit is None or count < limit):
        started = clock()
        temperature, sampled_at = nvml.sample(clock) if nvml is not None else (None, None)
        payload = json.dumps({"schema": 1, "sampled_at": sampled_at, "temperature_c": temperature},
                             allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
        try:
            write(output, payload)
        except BlockingIOError:
            pass  # Never block on a paused reader; its next fresh sample will follow.
        except (BrokenPipeError, OSError):
            return
        if nvml is None:
            return
        count += 1
        sleep(max(0.0, 1.0 - (clock() - started)))


def _spawn():
    package = Path(__file__).resolve().parent.parent
    interpreter = SYSTEM_PYTHON.resolve(strict=True)
    if os.geteuid() == 0 and (not Hardware._trusted_code() or not trusted_path(interpreter)):
        raise OSError("Root GPU helper requires protected installed code/interpreter")
    parent = process_identity(os.getpid())
    if parent is None:
        raise OSError("GPU helper parent identity unavailable")
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": str(package),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    # Inherit the worker's process group/session, not a new group. No shell or user code flags.
    return subprocess.Popen([str(interpreter), "-P", "-s", "-m", "nitro_control.gpu_sensor",
                             "--parent-pid", str(parent.pid), "--parent-start", str(parent.start_ticks)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=0, env=environment, cwd=str(package))


def _reap(process):
    try:
        process.wait(timeout=0.1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        # A D-state native operation cannot be forcibly reaped until the kernel
        # permits exit. This daemon owns the Popen reference, never the caller.
        process.wait()


class GpuSensor:
    def __init__(self, *, clock=time.monotonic, spawn=None):
        self.clock = clock
        self.spawn = _spawn if spawn is None else spawn
        self.process = None
        self.started = False
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.buffer = b""
        self.value = None
        self.sampled_at = None

    def _cancel(self):
        process, self.process = self.process, None
        self.value, self.sampled_at = None, None
        self.buffer = b""
        if process is not None:
            process.stdout.close()
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                threading.Thread(target=_reap, args=(process,), daemon=True,
                                 name="nitro-gpu-reaper").start()

    def _frame(self, line):
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate sensor key")
                result[key] = value
            return result

        if len(line) > MAX_FRAME:
            raise ValueError("Oversized sensor frame")
        data = json.loads(line, object_pairs_hook=pairs)
        if (type(data) is not dict or set(data) != {"schema", "sampled_at", "temperature_c"}
                or type(data["schema"]) is not int or data["schema"] != 1):
            raise ValueError("Invalid sensor frame")
        at, value = data["sampled_at"], data["temperature_c"]
        if at is None:
            if value is not None:
                raise ValueError("Temperature without query timestamp")
        elif (type(at) not in (int, float) or not math.isfinite(at) or at < 0 or at > self.clock()
              or (self.sampled_at is not None and at < self.sampled_at)):
            raise ValueError("Invalid sensor timestamp")
        if value is not None and (type(value) is not int or not 0 < value <= 125):
            raise ValueError("Invalid temperature")
        self.value, self.sampled_at = value, at

    def sample(self):
        if self.closed.is_set() or not self.lock.acquire(blocking=False):
            return None, None
        try:
            if not self.started:
                self.started = True
                self.process = self.spawn()
                os.set_blocking(self.process.stdout.fileno(), False)
            if self.closed.is_set() or self.process is None:
                return None, None
            remaining = READ_BUDGET
            while remaining > 0:
                try:
                    chunk = os.read(self.process.stdout.fileno(), min(4096, remaining))
                except BlockingIOError:
                    break
                if not chunk:
                    self._cancel()
                    return None, None
                remaining -= len(chunk)
                self.buffer += chunk
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    self._frame(line)
                if len(self.buffer) > MAX_FRAME:
                    raise ValueError("Unframed sensor output")
            if self.process.poll() is not None:
                self._cancel()
                return None, None
            value = self.value
            if self.sampled_at is None or self.clock() - self.sampled_at > MAX_SAMPLE_AGE:
                value = None
            return value, self.sampled_at
        except (OSError, ValueError, UnicodeError, RecursionError):
            self._cancel()
            return None, None
        finally:
            if self.closed.is_set():
                self._cancel()
            self.lock.release()

    def close(self):
        """Idempotent cancellation; never wait for initialization, native I/O or exit."""
        self.closed.set()
        if self.lock.acquire(blocking=False):
            try:
                self._cancel()
            finally:
                self.lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Internal read-only NVML temperature process")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-start", type=int, required=True)
    args = parser.parse_args(argv)
    parent = Peer(os.geteuid(), args.parent_pid, args.parent_start)
    if not protect_parent(parent):
        return 1
    os.set_blocking(1, False)
    serve(alive=lambda: os.getppid() == parent.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
