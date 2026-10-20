"""Process/socket integration against workspace files ONLY, never live sysfs.

The fixture entry points below deliberately exist only in the test suite.
There is no fake-device or arbitrary-device-path production CLI.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nitro_control.auth import process_identity
from nitro_control.controller import run_worker
from nitro_control.hardware import CHANNELS, expected_pwm
from nitro_control.recovery import (Runtime, connect_socket, read_state, receive_message, send_message,
                                    supervisor_request)
from nitro_control.supervisor import Supervisor

from test_backend import APP, FakeHardware, FakeTelemetry


class FileFakeHardware(FakeHardware):
    def __init__(self, root):
        super().__init__()
        self.root = Path(root)

    def _record(self, operation, channel, value):
        with (self.root / "writes.jsonl").open("a") as stream:
            stream.write(json.dumps({"monotonic": time.monotonic(), "pid": os.getpid(),
                                     "op": operation, "channel": channel, "value": value}) + "\n")
        (self.root / f"{channel}.{operation}").write_text(str(value))

    def read_channel(self, channel):
        pwm = int((self.root / f"{channel}.pwm").read_text())
        data = {"mode": (self.root / f"{channel}.mode").read_text(), "pwm": pwm,
                "percent": (pwm * 100 + 254) // 255,
                "rpm": int((self.root / f"{channel}.rpm").read_text())}
        if data["mode"] == "max" and (self.root / "hang-readback").exists():
            (self.root / "readback-active").write_text(str(time.monotonic()))
            while (self.root / "hang-readback").exists():
                time.sleep(0.02)
        return data

    def write_mode(self, channel, mode, *, before_write=None):
        if before_write is not None:
            before_write()
        self._record("mode", channel, mode)

    def write_pwm(self, channel, pwm, *, before_write=None):
        if before_write is not None:
            before_write()
        self._record("pwm", channel, expected_pwm(pwm * 100 // 255))


class FileTelemetry(FakeTelemetry):
    def _gpu_temperature(self):
        if (self.hardware.root / "slow-refresh").exists():
            (self.hardware.root / "refresh-active").write_text(str(time.monotonic()))
            time.sleep(1)
        return super()._gpu_temperature()


def fixture_entry(args):
    mode, root, directory = args[:3]
    if mode == "fixture-worker":
        runtime = Runtime(directory, create=False)
        hardware = FileFakeHardware(root)
        return run_worker(runtime, hardware, int(args[3]), process_identity(os.getppid()),
                          telemetry=FileTelemetry(hardware), authorize=lambda _peer: True)

    def factory(runtime_dir, heartbeat_fd):
        return subprocess.Popen([sys.executable, __file__, "fixture-worker", root,
                                 str(runtime_dir), str(heartbeat_fd)],
                                pass_fds=(heartbeat_fd,), start_new_session=True)

    return Supervisor(directory, hardware=FileFakeHardware(root), worker_factory=factory).run()


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".backend-process-", dir=APP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        for channel in CHANNELS:
            for key, value in (("mode", "auto"), ("pwm", 153), ("rpm", 2000)):
                (self.root / f"{channel}.{key}").write_text(str(value))
        self.process = None
        self.addCleanup(self.cleanup_process)

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.025)
        self.fail("Fixture condition timed out")

    def launch(self):
        self.process = subprocess.Popen([sys.executable, __file__, "fixture-supervisor",
                                         str(self.root), str(self.runtime)], start_new_session=True)
        self.wait_for(lambda: (self.runtime / "control.sock").exists())
        self.wait_for(lambda: self.call({"op": "status"})["status"]["cpu"]["rpm"] == 2000)

    def call(self, request=None, payload=None, *, timeout=3):
        with connect_socket(self.runtime, timeout=timeout) as connection:
            if payload is not None:
                connection.sendall(payload)
            else:
                send_message(connection, request)
            return receive_message(connection)

    def arm_max(self):
        result = self.call({"op": "acquire"})
        self.assertTrue(result["ok"], result)
        token = result["token"]
        result = self.call({"op": "max", "token": token})
        self.assertTrue(result["ok"], result)
        return token

    def cleanup_process(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        # A killed fixture supervisor can leave its fixture child briefly alive.
        try:
            state = read_state(self.runtime)
            if state and state.get("worker"):
                from nitro_control.recovery import quiesce
                quiesce(state["worker"])
        except (OSError, ValueError):
            pass

    def assert_auto(self):
        self.assertEqual((self.root / "cpu.mode").read_text(), "auto")
        self.assertEqual((self.root / "gpu.mode").read_text(), "auto")

    def test_socket_per_call_peer_lease_strict_frames_and_floor(self):
        self.launch()
        self.assertEqual((self.runtime / "control.sock").stat().st_mode & 0o777, 0o666)
        self.assertEqual((self.runtime / "supervisor.sock").stat().st_mode & 0o777, 0o600)
        result = self.call({"op": "acquire"})
        self.assertTrue(result["ok"], result)
        token = result["token"]
        result = self.call({"op": "set", "token": token, "cpu": {"mode": "manual", "percent": 70}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"]["cpu"]["pwm"], 178)
        self.assertEqual(result["status"]["gpu"]["mode"], "auto")
        self.assertTrue(self.call({"op": "heartbeat", "token": token})["ok"])
        status = self.call({"op": "status"})["status"]
        self.assertTrue(status["lease"]["owned_by_client"])
        self.assertNotIn(token, json.dumps(status))
        self.assertFalse(status["backend"]["hardware_verified"])
        for payload in (b'{"op":"status","uid":0}\n', b'{"op":"status","op":"acquire"}\n',
                        b"[]\n", b"{no}\n", b"x" * 16385,
                        b'{"op":"set","cpu":{"mode":"manual","percent":NaN}}\n'):
            self.assertFalse(self.call(payload=payload)["ok"])
        # A new process with the same UID and token is not the lease owner.
        program = ("import json,sys; from nitro_control.recovery import connect_socket,receive_message,send_message;"
                   "s=connect_socket(sys.argv[1]); send_message(s,{'op':'heartbeat','token':sys.argv[2]});"
                   "print(json.dumps(receive_message(s))); s.close()")
        child = subprocess.run([sys.executable, "-c", program, str(self.runtime), token],
                               capture_output=True, text=True, check=True, timeout=3)
        self.assertFalse(json.loads(child.stdout)["ok"])
        self.assertTrue(self.call({"op": "release", "token": token})["ok"])
        self.assert_auto()

    def test_suspend_resume_requires_fresh_lease_and_independent_crash_recovery(self):
        self.launch()
        old_token = self.arm_max()
        worker_pid = read_state(self.runtime)["worker"]["pid"]
        result = supervisor_request("suspend", self.runtime)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(process_identity(worker_pid))
        self.assert_auto()
        self.assertEqual(result["status"]["state"], "suspended")
        self.assertTrue(supervisor_request("resume", self.runtime)["ok"])
        self.wait_for(lambda: (self.runtime / "control.sock").exists())
        self.assertFalse(self.call({"op": "max", "token": old_token})["ok"])
        self.assert_auto()
        self.arm_max()
        worker_pid = read_state(self.runtime)["worker"]["pid"]
        os.kill(worker_pid, signal.SIGKILL)
        state = self.wait_for(lambda: (s if (s := read_state(self.runtime))["state"] == "fault" else None))
        self.assertIsNone(state["worker"])
        self.assertIn("exited", state["error"])
        self.assert_auto()
        # Recovery is the supervisor's write, not the killed worker's cached claim.
        log = [json.loads(line) for line in (self.root / "writes.jsonl").read_text().splitlines()]
        self.assertEqual([row["pid"] for row in log[-2:]], [self.process.pid, self.process.pid])

    def test_completed_loop_hang_watchdog_stops_before_recovery(self):
        self.launch()
        self.arm_max()
        worker = read_state(self.runtime)["worker"]
        os.kill(worker["pid"], signal.SIGSTOP)
        started = time.monotonic()
        state = self.wait_for(lambda: (s if (s := read_state(self.runtime))["state"] == "fault" else None),
                              timeout=10)
        self.assertGreater(time.monotonic() - started, 4)
        self.assertIn("watchdog", state["error"])
        self.assertIsNone(process_identity(worker["pid"]))
        self.assert_auto()
        self.assertFalse((self.runtime / "control.sock").exists() and
                         state["worker"] is not None)

    def test_supervisor_crash_worker_self_recovers_without_manual_replay(self):
        self.launch()
        self.arm_max()
        worker = read_state(self.runtime)["worker"]
        self.process.kill()
        self.process.wait(timeout=2)
        self.wait_for(lambda: process_identity(worker["pid"]) is None)
        self.assert_auto()
        # A fresh supervisor reads the old trusted process identity and starts in Auto.
        self.launch()
        self.assert_auto()
        self.assertFalse(self.call({"op": "status"})["status"]["lease"]["active"])

    def test_unrecorded_worker_startup_barrier_never_writes(self):
        runtime = Runtime(self.runtime)
        runtime.acquire()
        self.addCleanup(runtime.close)
        runtime.write_state({"schema": 1, "state": "starting", "worker": None,
                             "supervisor": vars(process_identity(os.getpid()))})
        read_fd, write_fd = os.pipe()
        try:
            self.process = subprocess.Popen([sys.executable, __file__, "fixture-worker", str(self.root),
                                             str(self.runtime), str(write_fd)],
                                            pass_fds=(write_fd,), start_new_session=True)
            self.assertEqual(self.process.wait(timeout=4), 1)
            self.assertFalse((self.root / "writes.jsonl").exists())
            self.assertFalse((self.runtime / "control.sock").exists())
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_one_second_refresh_keeps_cache_and_heartbeats_fast_and_queue_bounded(self):
        (self.root / "slow-refresh").write_text("fixture only")
        self.launch()
        acquired = self.call({"op": "acquire"}, timeout=4)
        self.assertTrue(acquired["ok"], acquired)
        token = acquired["token"]
        before = time.monotonic()
        self.wait_for(lambda: float((self.root / "refresh-active").read_text()) > before)
        result = {}

        def mutate():
            started = time.monotonic()
            try:
                result["reply"] = self.call({"op": "max", "token": token}, timeout=4)
            except Exception as exc:
                result["error"] = str(exc)
            result["elapsed"] = time.monotonic() - started

        mutation = threading.Thread(target=mutate)
        mutation.start()
        latencies = {"status": [], "heartbeat": []}
        try:
            while mutation.is_alive():
                for op in latencies:
                    started = time.monotonic()
                    response = self.call({"op": op, **({"token": token} if op == "heartbeat" else {})},
                                         timeout=0.8)
                    latencies[op].append(time.monotonic() - started)
                    self.assertTrue(response["ok"], response)
                time.sleep(0.04)
        finally:
            mutation.join(timeout=5)
        self.assertFalse(mutation.is_alive())
        self.assertNotIn("error", result)
        self.assertTrue(result["reply"]["ok"], result)
        self.assertGreater(result["elapsed"], 0.6)
        self.assertLess(result["elapsed"], 2.5)
        self.assertLess(max(latencies["status"]), 0.3)
        self.assertLess(max(latencies["heartbeat"]), 0.3)
        print(f"\nLATENCY slow_refresh=1.0s status_max={max(latencies['status']):.4f}s "
              f"heartbeat_max={max(latencies['heartbeat']):.4f}s "
              f"queued_mutation={result['elapsed']:.4f}s", flush=True)
        self.assertTrue(self.call({"op": "release", "token": token}, timeout=4)["ok"])
        self.assert_auto()

    def test_live_ipc_heartbeats_cannot_feed_watchdog_during_readback_hang(self):
        self.launch()
        acquired = self.call({"op": "acquire"})
        self.assertTrue(acquired["ok"], acquired)
        token = acquired["token"]
        worker = read_state(self.runtime)["worker"]
        (self.root / "hang-readback").write_text("fixture only")
        result = {}

        def mutate():
            started = time.monotonic()
            try:
                result["reply"] = self.call({"op": "max", "token": token}, timeout=8)
            except Exception as exc:
                result["error"] = str(exc)
            result["elapsed"] = time.monotonic() - started

        mutation = threading.Thread(target=mutate)
        mutation.start()
        self.wait_for(lambda: (self.root / "readback-active").exists())
        started = time.monotonic()
        latencies = {"status": [], "heartbeat": []}
        state = None
        try:
            while time.monotonic() - started < 10:
                state = read_state(self.runtime)
                if state["state"] == "fault":
                    break
                for op in latencies:
                    began = time.monotonic()
                    try:
                        reply = self.call({"op": op, **({"token": token} if op == "heartbeat" else {})},
                                          timeout=1)
                    except OSError:
                        continue
                    latencies[op].append(time.monotonic() - began)
                    self.assertTrue(reply["ok"], reply)
                    if op == "heartbeat":
                        self.assertTrue(reply["status"]["lease"]["active"])
                time.sleep(0.15)
        finally:
            mutation.join(timeout=9)
        self.assertFalse(mutation.is_alive())
        self.assertEqual(state["state"], "fault", state)
        self.assertIn("watchdog", state["error"])
        self.assertGreater(time.monotonic() - started, 4)
        self.assertIsNone(process_identity(worker["pid"]))
        self.assert_auto()
        self.assertNotIn("error", result)
        self.assertFalse(result["reply"]["ok"])
        self.assertIn("outcome unconfirmed", result["reply"]["error"])
        self.assertGreaterEqual(result["elapsed"], 5.9)
        self.assertLess(result["elapsed"], 6.8)
        self.assertLess(max(latencies["status"]), 0.3)
        self.assertLess(max(latencies["heartbeat"]), 0.3)
        print(f"\nLATENCY hung_readback status_max={max(latencies['status']):.4f}s "
              f"heartbeat_max={max(latencies['heartbeat']):.4f}s "
              f"request_timeout_reply={result['elapsed']:.4f}s", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith("fixture-"):
        raise SystemExit(fixture_entry(sys.argv[1:]))
    unittest.main()
