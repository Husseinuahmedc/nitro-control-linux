"""Independent owner/watchdog. Recovery writes happen only after worker exit."""

import argparse
import json
import math
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time

from .auth import process_identity, socket_peer
from .hardware import Hardware, reset_auto
from .recovery import (RUNTIME, Runtime, process_ref, quiesce, read_state,
                       receive_message, send_message)

WATCHDOG_SECONDS = 5.0


class Supervisor:
    def __init__(self, runtime_dir=RUNTIME, *, enable_control=False, validation_mode=False,
                 hardware=None, worker_factory=None):
        self.runtime = Runtime(runtime_dir, control=enable_control)
        self.hardware = hardware if hardware is not None else Hardware(
            enable_control=enable_control, validation_mode=validation_mode)
        self.enable_control = enable_control
        self.validation_mode = validation_mode
        self.worker_factory = worker_factory
        self.worker = None
        self.worker_ref = None
        self.pipe = None
        self.pipe_buffer = b""
        self.private = None
        self.last_heartbeat = None
        self.state = "starting"
        self.error = None
        self.stopping = False
        self.state_loaded = False

    def snapshot(self):
        return {"schema": 1, "state": self.state, "error": self.error,
                "supervisor": vars(process_identity(os.getpid())),
                "worker": self.worker_ref, "monotonic": time.monotonic()}

    def _record(self, state, error=None):
        self.state = state
        self.error = str(error)[:1000] if error else None
        self.runtime.write_state(self.snapshot())

    def _spawn(self):
        if self.worker_ref is not None:
            raise RuntimeError("Cannot start another worker before confirmed quiescence")
        self._record("starting")
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            if self.worker_factory is not None:
                self.worker = self.worker_factory(self.runtime.path, write_fd)
            else:
                command = [sys.executable, "-P", "-s", "-m", "nitro_control.controller", "--worker",
                           "--heartbeat-fd", str(write_fd), "--runtime-dir", str(self.runtime.path)]
                if self.enable_control:
                    command.append("--enable-control")
                if self.validation_mode:
                    command.append("--validation-mode")
                environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                               "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
                               "PYTHONDONTWRITEBYTECODE": "1"}
                self.worker = subprocess.Popen(command, pass_fds=(write_fd,), start_new_session=True,
                                               env=environment, stdin=subprocess.DEVNULL,
                                               cwd=str(Path(__file__).resolve().parent.parent))
            self.worker_ref = process_ref(self.worker.pid)
            self.pipe = read_fd
            self.pipe_buffer = b""
            self.last_heartbeat = time.monotonic()
            self._record("running" if self.hardware.control_enabled else "read_only")
        except BaseException:
            os.close(read_fd)
            # A spawned child must remain recorded even if process-group validation failed.
            if self.worker is not None and self.worker_ref is None:
                peer = process_identity(self.worker.pid)
                if peer:
                    self.worker_ref = {**vars(peer), "pgid": self.worker.pid}
            raise
        finally:
            os.close(write_fd)

    def _auto_without_worker(self):
        if self.runtime.lock_fd is None:
            raise RuntimeError("Auto recovery requires the exclusive owner lock")
        if self.worker_ref is not None:
            raise RuntimeError("Recovery cannot race a possibly live worker")
        if not self.hardware.recovery_enabled:
            return False, f"Auto unavailable: {self.hardware.reason}"
        reset = reset_auto(self.hardware)
        return reset.ok, "; ".join(reset.errors) or None

    def start(self):
        self.runtime.acquire()
        old = read_state(self.runtime)
        self.state_loaded = True
        self.worker_ref = old.get("worker") if old else None
        if self.enable_control and not self.hardware.recovery_enabled:
            raise ValueError(self.hardware.reason)
        self.private = self.runtime.socket("supervisor.sock", 0o600)
        self.private.settimeout(1)
        if self.worker_ref is not None:
            if not quiesce(self.worker_ref):
                self._record("fault", "Previous worker cannot stop; NO writes or new worker")
                return False
            self.worker_ref = None
        self._record("recovering")
        if self.hardware.recovery_enabled:
            ok, error = self._auto_without_worker()
            if not ok:
                self._record("fault", error)
                return False
        self._spawn()
        return True

    def stop_worker(self, target="auto", cause=None):
        self._record("quiescing", cause)
        try:
            stopped = quiesce(self.worker_ref)
        except (OSError, ValueError) as exc:
            self._record("fault", f"Cannot establish worker exit: {exc}; NO recovery writes/new worker")
            return False
        if not stopped:
            self._record("fault", "Worker uninterruptible/cannot stop; NO recovery writes or new worker")
            return False
        if self.worker is not None:
            try:
                self.worker.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                self._record("fault", "Worker exit not reaped/confirmed; NO recovery writes")
                return False
        self.worker = None
        self.worker_ref = None
        if self.pipe is not None:
            os.close(self.pipe)
            self.pipe = None
        self._record("recovering", cause)
        if not self.hardware.recovery_enabled:
            self._record("fault" if self.enable_control else target,
                         f"Auto unavailable: {self.hardware.reason}" if self.enable_control else
                         cause or "Read-only: no Auto write acknowledgement")
            return False
        ok, error = self._auto_without_worker()
        self._record(target if ok else "fault", error or cause)
        return ok

    def suspend(self):
        return self.stop_worker("suspended")

    def resume(self):
        requires_auto = self.enable_control or self.hardware.recovery_enabled
        if self.worker_ref is not None:
            if not self.stop_worker("auto") and (requires_auto or self.worker_ref is not None):
                return False
        elif requires_auto:
            self._record("recovering")
            ok, error = self._auto_without_worker()
            if not ok:
                self._record("fault", error)
                return False
        self._spawn()
        return True

    def _private_request(self):
        connection, _ = self.private.accept()
        with connection:
            connection.settimeout(1)
            try:
                if socket_peer(connection).uid != self.runtime.uid:
                    raise ValueError("Private supervisor access denied")
                request = receive_message(connection)
                if set(request) != {"op"} or request["op"] not in ("status", "suspend", "resume"):
                    raise ValueError("Unknown private operation or unexpected fields")
                op = request["op"]
                ok = True if op == "status" else self.suspend() if op == "suspend" else self.resume()
                result = {"ok": ok, "status": self.snapshot()}
                if not ok:
                    result["error"] = self.error or "Auto was not acknowledged"
            except (ValueError, OSError) as exc:
                result = {"ok": False, "error": str(exc)[:500], "status": self.snapshot()}
            send_message(connection, result)

    def check_worker(self):
        if self.worker is None or self.state == "fault":
            return
        if self.worker.poll() is not None:
            self.stop_worker("fault", "Worker exited unexpectedly; independent Auto recovery attempted")
        elif time.monotonic() - self.last_heartbeat >= WATCHDOG_SECONDS:
            self.stop_worker("fault", "Completed-loop watchdog expired (5 seconds)")

    def _read_heartbeats(self):
        data = os.read(self.pipe, 4096)
        if not data:
            self.stop_worker("fault", "Worker exited or heartbeat pipe closed")
            return
        self.pipe_buffer += data
        while len(self.pipe_buffer) >= 8:
            stamp = struct.unpack("!d", self.pipe_buffer[:8])[0]
            self.pipe_buffer = self.pipe_buffer[8:]
            if math.isfinite(stamp) and self.last_heartbeat <= stamp <= time.monotonic():
                self.last_heartbeat = stamp

    def run(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda _sig, _frame: setattr(self, "stopping", True))
        try:
            self.start()
            while not self.stopping:
                inputs = [item for item in (self.private, self.pipe) if item is not None]
                readable, _, _ = select.select(inputs, [], [], 0.1)
                # Consume the old pipe before a private resume can replace its FD.
                if self.pipe is not None and self.pipe in readable:
                    try:
                        self._read_heartbeats()
                    except (OSError, ValueError) as exc:
                        self.stop_worker("fault", f"Worker heartbeat pipe fault: {exc}")
                if self.private is not None and self.private in readable:
                    try:
                        self._private_request()
                    except (OSError, ValueError):
                        pass
                self.check_worker()
        except Exception as exc:
            if self.runtime.lock_fd is not None and self.state_loaded:
                self.state = "fault"
                self.error = f"Supervisor fault: {exc}"[:1000]
                try:
                    self.runtime.write_state(self.snapshot())
                except (OSError, ValueError):
                    pass
            raise
        finally:
            self.close()
        return 1 if self.state == "fault" else 0

    def close(self):
        try:
            if self.runtime.lock_fd is not None and self.state_loaded:
                self.stop_worker("fault" if self.state == "fault" else "stopped", self.error)
        finally:
            if self.private is not None:
                self.private.close()
                self.runtime.remove_socket("supervisor.sock")
                self.private = None
            self.runtime.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-control", action="store_true")
    parser.add_argument("--validation-mode", action="store_true")
    parser.add_argument("--runtime-dir", default=str(RUNTIME))
    args = parser.parse_args(argv)
    try:
        return Supervisor(args.runtime_dir, enable_control=args.enable_control,
                          validation_mode=args.validation_mode).run()
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
