"""Single-loop controller and bounded public Unix IPC worker."""

import argparse
from collections import deque
from concurrent.futures import Future, TimeoutError
import hmac
import os
import queue
import secrets
import signal
import socket
import stat
import struct
import threading
import time

from . import auth
from .hardware import CHANNELS, Hardware, expected_pwm, percent_to_pwm, reset_auto
from .recovery import (RUNTIME, Runtime, OwnerBusy, read_state, receive_message,
                       send_message, process_ref)
from .telemetry import MAX_SAMPLE_AGE, Telemetry

LEASE_SECONDS = 5.0
RPM_RESPONSE_SECONDS = 3.0
GUARD_TEMPERATURES = {"cpu": 85, "gpu": 80}


class LeaseLost(RuntimeError):
    pass


def validate_request(request):
    if type(request) is not dict or type(request.get("op")) is not str:
        raise ValueError("Request requires an operation")
    op = request["op"]
    if op not in ("status", "acquire", "heartbeat", "set", "max", "auto", "release"):
        raise ValueError("Unknown operation")
    allowed = {"op"} if op in ("status", "acquire") else {"op", "token"}
    if op == "set":
        allowed |= set(CHANNELS)
    if set(request) - allowed or not ({"op"} if op in ("status", "acquire") else
                                      {"op", "token"}) <= set(request):
        raise ValueError("Unexpected or missing request fields")
    if "token" in allowed:
        token = request["token"]
        if type(token) is not str or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("Invalid lease token")
    if op == "set":
        if not any(channel in request for channel in CHANNELS):
            raise ValueError("Set requires a CPU or GPU channel")
        for channel in CHANNELS:
            if channel not in request:
                continue
            setting = request[channel]
            if type(setting) is not dict or type(setting.get("mode")) is not str:
                raise ValueError("Channel requires an explicit mode")
            mode = setting["mode"]
            if mode not in ("auto", "max", "manual"):
                raise ValueError("Invalid fan mode")
            if set(setting) != ({"mode", "percent"} if mode == "manual" else {"mode"}):
                raise ValueError("Unexpected or missing channel fields")
            if mode == "manual" and (type(setting["percent"]) is not int
                                     or not 50 <= setting["percent"] <= 100):
                raise ValueError("Manual percent must be an integer in 50..100 (software safety floor)")
    return request


class Controller:
    def __init__(self, hardware=None, telemetry=None, *, clock=time.monotonic, alive=auth.peer_alive):
        self.hardware = hardware if hardware is not None else Hardware()
        self.telemetry = telemetry if telemetry is not None else Telemetry(self.hardware, clock=clock)
        self.clock = clock
        self.alive = alive
        self.lock = threading.RLock()
        self.io_lock = threading.RLock()
        self.busy = False
        self.tasks = queue.Queue(maxsize=32)
        self.events = deque(maxlen=32)
        self.lease = None
        self.deadline = 0.0
        self.requested = {name: {"mode": "auto"} if self.hardware.control_enabled else None
                          for name in CHANNELS}
        self.baselines = {}
        self.responses = {name: "unverified" for name in CHANNELS}
        self.error = None
        self.recovery_state = "ready" if self.hardware.control_enabled else "read_only"
        self.recovery_error = None
        self.fatal = False
        self._sync_backend()

    def _sync_backend(self):
        with self.lock:
            self.backend = {"control_available": self.hardware.control_available,
                            "control_enabled": self.hardware.control_enabled,
                            "hardware_verified": self.hardware.hardware_verified,
                            "reason": self.hardware.reason}

    def _event(self, kind, message, **extra):
        with self.lock:
            self.events.append({"monotonic": self.clock(), "type": kind,
                                "message": str(message)[:240], **extra})

    @staticmethod
    def _matches(data, setting):
        return (data["mode"] == setting["mode"] and type(data["pwm"]) is int
                and 0 <= data["pwm"] <= 255
                and (setting["mode"] != "manual"
                     or data["pwm"] == expected_pwm(setting["percent"])))

    def status(self, peer=None):
        with self.lock:
            status = self.telemetry.cached_snapshot()
            status["backend"].update(self.backend, connected=True)
            status["backend"]["control_enabled"] &= not self.fatal
            if self.error:
                status["backend"]["reason"] = self.error
            for channel in CHANNELS:
                setting = self.requested[channel]
                data = status[channel]
                if setting is not None:
                    data["requested_mode"] = setting["mode"]
                    data["requested_percent"] = setting.get("percent")
                    data["confirmation"] = ("pending" if self.busy or self.telemetry.sampled_at is None else
                                            "firmware-confirmed" if self._matches(data, setting) else "failed")
                data["rpm_response"] = self.responses[channel]
            active = self.lease is not None and self.clock() < self.deadline
            status["lease"] = {"active": active,
                               "owned_by_client": active and peer == self.lease[0],
                               "owner_pid": self.lease[0].pid if active else None,
                               "remaining_seconds": max(0.0, self.deadline - self.clock()) if active else 0}
            status["recovery"] = {"state": self.recovery_state, "error": self.recovery_error}
            status["error"] = self.error
            status["events"] = list(self.events)
            return status

    def _reply(self, peer, error=None, **extra):
        return {"ok": error is None,
                **({"error": str(error)[:500] or "Controller request failed"} if error is not None else {}),
                "status": self.status(peer), **extra}

    def _reset(self, reason, *, clear_lease=True, fault=False):
        with self.io_lock:
            with self.lock:
                was_busy = self.busy
                self.busy = True
                self.recovery_state = "recovering"
                if clear_lease:
                    self.lease = None
                    self.deadline = 0
                self.baselines.clear()
                self.responses = {name: "unverified" for name in CHANNELS}
            result = reset_auto(self.hardware, self.clock)
            with self.lock:
                for channel in CHANNELS:
                    self.requested[channel] = {"mode": "auto"}
                    self.telemetry.record_channel(channel, result.channels.get(channel, {}),
                                                  result.sampled_at[channel])
                self.recovery_error = "; ".join(result.errors)[:500] or None
                self.recovery_state = "auto" if result.ok else "fault"
                self.fatal |= fault or not result.ok
                self._event("recovery", reason, success=result.ok)
                if not result.ok:
                    self.error = f"Auto recovery failed: {self.recovery_error}"[:500]
                self.busy = was_busy
                self._sync_backend()
            return result.ok

    def _manual_problem(self):
        data = self.telemetry.cached_snapshot()
        for channel, limit in GUARD_TEMPERATURES.items():
            temperature = data[channel]["temperature_c"]
            if temperature is None or self.telemetry.age(channel + ".temperature") > MAX_SAMPLE_AGE:
                return f"{channel} temperature missing/stale; manual cancelled"
            if temperature >= limit:
                return f"{channel} temperature reached conservative {limit} C guard; manual cancelled"
            if data[channel]["rpm"] is None:
                return f"{channel} actual RPM missing/stale; manual cancelled"
        return None

    def _lease_problem(self, lease):
        with self.lock:
            if lease is None or self.lease is not lease:
                return "No matching active lease for this process"
        alive = self.alive(lease[0])
        with self.lock:
            if self.lease is not lease:
                return "Control lease changed during operation"
            if not alive:
                return "Lease owner exited or changed process identity"
            # Check after the process identity read, not before a potentially slow read.
            if self.clock() >= self.deadline:
                return "Control lease expired without heartbeat"
        return None

    def _require_lease(self, lease):
        problem = self._lease_problem(lease)
        if problem:
            raise LeaseLost(problem)

    def _matching_lease(self, request, peer):
        with self.lock:
            lease = self.lease
            if (lease is None or lease[0] != peer
                    or not hmac.compare_digest(lease[1], request["token"])):
                raise ValueError("No matching active lease for this process")
            return lease

    def _heartbeat(self, request, peer):
        try:
            lease = self._matching_lease(request, peer)
            problem = self._lease_problem(lease)
            with self.lock:
                if self.lease is not lease:
                    raise ValueError("Control lease changed during heartbeat")
                if problem or self.clock() >= self.deadline:
                    problem = problem or "Control lease expired without heartbeat"
                    self.deadline = min(self.deadline, self.clock())
                    self.error = problem
                    raise ValueError(problem)
                if self.fatal or not self.backend["control_enabled"]:
                    raise ValueError("Controller unavailable for lease renewal")
                self.deadline = self.clock() + LEASE_SECONDS
                return self._reply(peer)
        except ValueError as exc:
            return self._reply(peer, exc)

    def _expire(self):
        with self.lock:
            lease = self.lease
        if lease is not None:
            reason = self._lease_problem(lease)
            if reason:
                with self.lock:
                    self.error = reason
                self._reset(reason)

    def tick(self):
        with self.io_lock:
            self._expire()
            # Only the I/O loop samples hardware. Neither cache access nor a
            # lease heartbeat waits on this operation or feeds the parent watchdog.
            self.telemetry.refresh()
            self._expire()
            with self.lock:
                if self.fatal or not self.hardware.control_enabled:
                    return
                manual = any(setting and setting["mode"] == "manual" for setting in self.requested.values())
                problem = self._manual_problem() if manual else None
                fault = False
                data = self.telemetry.cached_snapshot()
                for channel in CHANNELS:
                    if problem:
                        break
                    setting = self.requested[channel]
                    if setting and not self._matches(data[channel], setting):
                        problem = f"{channel} firmware state changed or readback failed"
                        fault = True
                        break
                    baseline = self.baselines.get(channel)
                    if baseline and self.responses[channel] == "pending":
                        rpm, sampled_at = baseline
                        current = data[channel]["rpm"]
                        if (rpm is not None and current is not None
                                and self.telemetry.times[channel + ".fan"] > sampled_at
                                and abs(current - rpm) >= 100):
                            self.responses[channel] = "observed"
                        elif self.clock() - sampled_at >= RPM_RESPONSE_SECONDS:
                            self.responses[channel] = "not-observed"
            if problem:
                with self.lock:
                    self.error = problem
                self._reset(problem, fault=fault)

    def _apply(self, settings, lease):
        future = {**self.requested, **settings}

        def before_write():
            if any(setting and setting["mode"] == "manual" for setting in future.values()):
                problem = self._manual_problem()
                if problem:
                    raise RuntimeError(problem)
            self._require_lease(lease)

        if all(setting["mode"] == "auto" for setting in settings.values()) and len(settings) == 2:
            if not self._reset("Explicit Auto", clear_lease=False):
                raise RuntimeError(self.error)
            self._require_lease(lease)
            return
        baselines = {}
        for channel, setting in settings.items():
            before = self.clock()
            prior = self.hardware.read_channel(channel)
            self._require_lease(lease)
            baselines[channel] = (prior["rpm"], before)
            if setting["mode"] == "manual":
                before_write()
                self.hardware.write_pwm(channel, percent_to_pwm(setting["percent"]), before_write=before_write)
                self._require_lease(lease)
            if setting["mode"] != "auto":
                before_write()
                self.hardware.write_mode(channel, setting["mode"], before_write=before_write)
            else:
                self.hardware.write_mode(channel, "auto")
            self._require_lease(lease)
        # Re-read both after all writes: a coupled firmware update must not pass
        # because the omitted/earlier channel still has cached confirmation.
        for channel in CHANNELS:
            sampled_at = self.clock()
            actual = self.hardware.read_channel(channel)
            self.telemetry.record_channel(channel, actual, sampled_at)
            self._require_lease(lease)
        confirmed = self.telemetry.cached_snapshot()
        for channel in CHANNELS:
            if future[channel] and not self._matches(confirmed[channel], future[channel]):
                raise RuntimeError(f"{channel} firmware mode/PWM readback did not match request")
        if any(setting and setting["mode"] == "manual" for setting in future.values()):
            problem = self._manual_problem()
            if problem:
                raise RuntimeError(problem)
        with self.lock:
            for channel, setting in settings.items():
                rpm, before = baselines[channel]
                self.requested[channel] = dict(setting)
                if setting["mode"] == "auto":
                    self.baselines.pop(channel, None)
                    self.responses[channel] = "unverified"
                else:
                    self.baselines[channel] = (rpm, before)
                    self.responses[channel] = "pending"
                self._event("command", f"{channel} {setting['mode']} firmware-confirmed",
                            channel=channel, pre_command_rpm=rpm, pre_command_monotonic=before)

    def handle(self, request, peer, *, authorized=False):
        try:
            validate_request(request)
        except ValueError as exc:
            return self._reply(peer, exc)
        op = request["op"]
        if op == "status":
            return self._reply(peer)
        if op == "heartbeat":
            return self._heartbeat(request, peer)
        with self.io_lock:
            with self.lock:
                self.busy = True
            error = None
            extra = {}
            lease = None
            try:
                self._expire()
                if not self.hardware.control_enabled:
                    raise ValueError(self.hardware.reason)
                if self.fatal:
                    raise ValueError("Controller fault; independent supervisor recovery required")
                if op == "acquire":
                    if not authorized:
                        raise ValueError("Authorization denied or unavailable")
                    if not self.alive(peer):
                        raise ValueError("Client process exited or changed identity")
                    if self.lease is not None:
                        raise ValueError("Another control lease is active")
                    if not self._reset("Auto before acquiring lease"):
                        raise ValueError(self.error)
                    if not self.alive(peer):
                        raise LeaseLost("Client process exited or changed identity during acquisition")
                    token = secrets.token_hex(32)
                    with self.lock:
                        lease = self.lease = (peer, token)
                        self.deadline = self.clock() + LEASE_SECONDS
                        self.error = None
                    self._event("lease", "Control lease acquired", owner_pid=peer.pid)
                    extra["token"] = token
                else:
                    lease = self._matching_lease(request, peer)
                    self._require_lease(lease)
                    if op == "release":
                        if not self._reset("Client released control"):
                            raise ValueError(self.error)
                    else:
                        settings = ({name: request[name] for name in CHANNELS if name in request}
                                    if op == "set" else {name: {"mode": op} for name in CHANNELS})
                        future = {**self.requested, **settings}
                        if any(setting and setting["mode"] == "manual" for setting in future.values()):
                            problem = self._manual_problem()
                            if problem:
                                with self.lock:
                                    self.error = problem
                                self._reset(problem)
                                raise ValueError(problem)
                        try:
                            self._apply(settings, lease)
                            with self.lock:
                                self.error = None
                        except LeaseLost:
                            raise
                        except Exception as exc:
                            original = f"Command failed: {exc}"[:500]
                            with self.lock:
                                self.error = original
                            self._reset(original, fault=True)
                            raise ValueError(original) from exc
                if op != "release":
                    self._require_lease(lease)
            except LeaseLost as exc:
                error = str(exc)
                with self.lock:
                    self.error = error
                self._reset(error, fault=True)
            except (ValueError, OSError, RuntimeError) as exc:
                error = str(exc)
            finally:
                with self.lock:
                    self.busy = False
                    self._sync_backend()
            if error is None and op != "release":
                try:
                    self._require_lease(lease)
                except LeaseLost as exc:
                    error = str(exc)
                    with self.lock:
                        self.error = error
                    self._reset(error, fault=True)
            return self._reply(peer, error, **(extra if error is None else {}))

    def submit(self, request, peer, authorized=False):
        future = Future()
        if type(request) is dict and request.get("op") in ("status", "heartbeat"):
            future.set_result(self.handle(request, peer, authorized=authorized))
            return future
        try:
            self.tasks.put_nowait((future, request, peer, authorized))
        except queue.Full:
            future.set_result(self._reply(peer, "Controller request queue full"))
        return future

    def process_one(self):
        try:
            future, request, peer, authorized = self.tasks.get_nowait()
        except queue.Empty:
            return
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(self.handle(request, peer, authorized=authorized))
            except Exception as exc:
                with self.lock:
                    self.fatal = True
                    self.error = f"Unexpected controller fault: {type(exc).__name__}"
                future.set_result(self._reply(peer, self.error))

    def shutdown(self):
        with self.io_lock:
            with self.lock:
                self.lease = None
                self.deadline = 0
            self.telemetry.close()
            if self.hardware.recovery_enabled:
                self._reset("Worker stopping; no lease persisted")


class IPCServer:
    def __init__(self, runtime, controller, *, authorize=auth.authorize):
        self.runtime = runtime
        self.controller = controller
        self.authorize = authorize
        self.clients = threading.BoundedSemaphore(16)
        self.auth_clients = threading.BoundedSemaphore(2)
        self.stopped = threading.Event()
        self.socket = runtime.socket("control.sock", 0o666)
        self.socket.settimeout(0.2)
        self.thread = threading.Thread(target=self._accept, daemon=True)

    def start(self):
        self.thread.start()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.clients.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def _client(self, connection):
        peer = None
        try:
            with connection:
                connection.settimeout(2)
                try:
                    peer = auth.socket_peer(connection)
                    request = validate_request(receive_message(connection))
                    if request["op"] in ("status", "heartbeat"):
                        response = self.controller.handle(request, peer)
                    else:
                        authorized = False
                        if request["op"] == "acquire" and self.controller.status(peer)["backend"]["control_enabled"]:
                            if not self.auth_clients.acquire(blocking=False):
                                raise ValueError("Authorization capacity exhausted")
                            try:
                                authorized = self.authorize(peer)
                            finally:
                                self.auth_clients.release()
                        future = self.controller.submit(request, peer, authorized)
                        try:
                            response = future.result(timeout=6)
                        except TimeoutError:
                            future.cancel()
                            raise ValueError("Controller did not complete request; outcome unconfirmed") from None
                except (ValueError, OSError) as exc:
                    response = self.controller._reply(peer, exc)
                send_message(connection, response)
        except (OSError, ValueError):
            pass
        finally:
            self.clients.release()

    def close(self):
        self.stopped.set()
        self.socket.close()
        self.thread.join(timeout=1)
        self.runtime.remove_socket("control.sock")


def run_worker(runtime, hardware, heartbeat_fd, parent, *, telemetry=None, authorize=auth.authorize):
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: stop.set())
    # Until the supervisor durably records this child, a crash must not leave an
    # unrecorded writer that a subsequent recovery helper cannot quiesce.
    own_ref = process_ref(os.getpid())
    deadline = time.monotonic() + 2
    ready = False
    while not stop.is_set():
        state = read_state(runtime)
        if not auth.peer_alive(parent):
            break
        if state and state.get("supervisor") == vars(parent) and state.get("worker") == own_ref:
            ready = True
            break
        if time.monotonic() >= deadline:
            break
        stop.wait(0.01)
    if not ready:
        os.close(heartbeat_fd)
        runtime.close()
        return 1
    os.set_blocking(heartbeat_fd, False)
    controller = Controller(hardware, telemetry)
    server = IPCServer(runtime, controller, authorize=authorize)
    server.start()
    failed = False
    try:
        while not stop.is_set():
            started = time.monotonic()
            if not auth.peer_alive(parent):
                with controller.lock:
                    controller.error = "Supervisor process disappeared"
                failed = True
                break
            controller.tick()
            controller.process_one()
            if controller.fatal:
                failed = True
                break
            # Only this completed control loop can feed the independent watchdog.
            try:
                os.write(heartbeat_fd, struct.pack("!d", time.monotonic()))
            except (BrokenPipeError, BlockingIOError):
                failed = True
                break
            stop.wait(max(0, 0.25 - (time.monotonic() - started)))
    finally:
        try:
            controller.shutdown()
        finally:
            server.close()
            os.close(heartbeat_fd)
            runtime.close()
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Internal supervised controller worker")
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--heartbeat-fd", type=int, required=True)
    parser.add_argument("--runtime-dir", default=str(RUNTIME))
    parser.add_argument("--enable-control", action="store_true")
    parser.add_argument("--validation-mode", action="store_true")
    args = parser.parse_args(argv)
    runtime = None
    try:
        runtime = Runtime(args.runtime_dir, control=args.enable_control, create=False)
        state = read_state(runtime)
        parent = auth.process_identity(os.getppid())
        if (parent is None or parent.uid != os.geteuid() or not state
                or state.get("supervisor") != vars(parent)
                or not stat.S_ISFIFO(os.fstat(args.heartbeat_fd).st_mode)):
            raise ValueError("Worker requires its live supervisor, trusted state and heartbeat pipe")
        try:
            runtime.acquire()
        except OwnerBusy:
            pass
        else:
            runtime.close()
            raise ValueError("Supervisor does not hold the owner lock")
        return run_worker(runtime, Hardware(enable_control=args.enable_control,
                                            validation_mode=args.validation_mode), args.heartbeat_fd, parent)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Worker refused: {exc}", flush=True)
        return 1
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
