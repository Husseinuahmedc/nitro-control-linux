"""Native, read-only-by-default Nitro Control interface.

ControllerWorker coordinates separate bounded command/heartbeat RPC lanes.
Read-only sensors are isolated from both. The window only stages edits;
Apply is the only path from a draft to a set request.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QMainWindow, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from .client import ClientError, DEFAULT_SOCKET, SocketClient, validate_request, validate_status
from .palette import apply_theme


STALE_SECONDS = 5.0
SENSOR_BUDGET_SECONDS = 2.0
SETUP_HELP = (
    "Fan-control setup requires the root-owned Nitro Control service and polkit policy, "
    "readable Acer pwm1/pwm2 and pwm1_enable/pwm2_enable interfaces, and explicit "
    "hardware-write enablement (--enable-control) and the root-protected "
    "/etc/nitro-control/hardware-approved marker. The reviewed acceptance record is "
    "/etc/nitro-control/hardware-verified.json; separately approved validation instead "
    "requires --validation-mode. These are backend gates, not actions performed by "
    "this window. A stock kernel with "
    "missing PWM interfaces cannot provide fan control. Follow the separately approved "
    "hardware acceptance procedure before enabling writes. This app does not install "
    "services, change module parameters, or bypass missing interfaces. See "
    "application/PROTOCOL.md and docs/HARDWARE-ACCEPTANCE.md."
)


def unavailable_status(reason: str) -> dict[str, Any]:
    channel = {
        "temperature_c": None, "rpm": None, "mode": None, "pwm": None,
        "percent": None, "requested_mode": None, "requested_percent": None,
        "confirmation": "unsupported", "rpm_response": "unverified",
    }
    return {
        "schema": 1,
        "backend": {"connected": False, "control_available": False,
                    "control_enabled": False, "hardware_verified": False, "reason": reason},
        "cpu": dict(channel), "gpu": dict(channel),
        "lease": {"active": False, "owned_by_client": False, "owner_pid": None, "remaining_seconds": 0},
        "recovery": {"state": "read_only", "error": None},
        "error": reason, "sample_age_seconds": 0, "events": [],
    }


def both_auto(status: dict[str, Any]) -> bool:
    return bool(
        status["backend"]["connected"] and status["sample_age_seconds"] <= STALE_SECONDS
        and all(status[name]["mode"] == "auto" and
                status[name]["confirmation"] == "firmware-confirmed" for name in ("cpu", "gpu"))
    )


def command_feedback(status: dict[str, Any], names: tuple[str, ...] | list[str]) -> str:
    confirmations = {status[name]["confirmation"] for name in names}
    if "failed" in confirmations:
        message = "Firmware confirmation failed"
    elif confirmations == {"firmware-confirmed"}:
        message = "Firmware accepted"
    elif "unsupported" in confirmations:
        message = "Firmware confirmation unavailable"
    else:
        message = "Request received; firmware readback pending"
    responses = {status[name]["rpm_response"] for name in names}
    response_text = next(iter(responses)).replace("-", " ") if len(responses) == 1 else ", ".join(
        f"{name.upper()} {status[name]['rpm_response'].replace('-', ' ')}" for name in names
    )
    return f"{message} / RPM response {response_text}."


def _telemetry_factory() -> Any:
    from .telemetry import Telemetry
    return Telemetry()


class _SensorContext:
    """One reusable provider, owned solely by a daemon with no Qt references."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self.factory = factory
        self.requests: queue.Queue = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="nitro-readonly-sensors", daemon=True)
        self.thread.start()

    def submit(self, results: queue.Queue) -> bool:
        if self.stopped.is_set():
            return False
        try:
            self.requests.put_nowait(results)
            return True
        except queue.Full:
            return False

    def close(self) -> None:
        self.stopped.set()
        try:
            self.requests.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        provider = None
        try:
            while not self.stopped.is_set():
                results = self.requests.get()
                if results is None or self.stopped.is_set():
                    break
                try:
                    if provider is None:
                        provider = self.factory()
                    if self.stopped.is_set():
                        break
                    measured = provider.snapshot()
                    validate_status(measured)
                    result = (measured, "", time.monotonic())
                except Exception as exc:
                    result = (None, f"{type(exc).__name__}: {exc}", time.monotonic())
                if not self.stopped.is_set():
                    results.put_nowait(result)
        finally:
            if provider is not None:
                try:
                    close = getattr(provider, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass


class ControllerWorker(QObject):
    status_ready = Signal(dict)
    activity = Signal(bool, str)
    command_result = Signal(str, bool, str)
    shutdown_result = Signal(bool, str)
    finished = Signal()
    _rpc_completed = Signal(str, object)

    def __init__(self, client: SocketClient, telemetry_factory: Callable[[], Any] = _telemetry_factory) -> None:
        super().__init__()
        self._home_thread = self.thread()
        self.client = client
        self.telemetry_factory = telemetry_factory
        self._token: str | None = None
        self._timer: QTimer | None = None
        self._sample_timer: QTimer | None = None
        self._sensor_context: _SensorContext | None = None
        self._sampler: threading.Thread | None = None
        self._sample_results: queue.Queue | None = None
        self._sample_pending = False
        self._sample_started = 0.0
        self._sample_epoch = 0
        self._sample_timed_out = False
        self._inflight: dict[str, dict[str, Any]] = {}
        self._epoch = 0
        self._status_fence = 0.0
        self._targets: dict[str, dict[str, Any]] = {}
        self._renew_suspended = False
        self._uncertain_since = 0.0
        self._fault_reason = ""
        self._stopping = False
        self._finished = False
        self._recovery_unconfirmed = False
        self._last_status = unavailable_status("Connecting to the local controller...")
        self._last_published = time.monotonic()
        self._rpc_completed.connect(self._on_rpc_completed, Qt.ConnectionType.QueuedConnection)

    @Slot()
    def start(self) -> None:
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self.poll)
        self._timer.start(2000)
        self._sample_timer = QTimer(self)
        self._sample_timer.setInterval(100)
        self._sample_timer.timeout.connect(self._consume_sample)
        QTimer.singleShot(0, self.poll)

    def _publish(self, status: dict[str, Any], source: str = "controller") -> None:
        status = copy.deepcopy(status)
        trusted = self.client.peer_uid == 0 and source == "controller"
        if self._recovery_unconfirmed and not status["recovery"].get("error"):
            cause = self._fault_reason or status.get("error") or "Ownership / Auto recovery unconfirmed"
            status["recovery"] = {
                "state": "fault",
                "error": f"{cause}. Ownership / Auto recovery unconfirmed. Heartbeats stopped; waiting for fresh, inactive-lease Auto readbacks.",
            }
        if source == "controller" and not trusted:
            status["backend"].update(
                control_available=False, control_enabled=False, hardware_verified=False,
                reason="Custom socket peer is not root. Monitoring only; control and hardware claims are disabled.",
            )
        status["_client"] = {
            "owns_lease": bool(self._token and not self._recovery_unconfirmed
                               and status["lease"]["active"] and status["lease"]["owned_by_client"]),
            "source": source, "trusted": trusted, "read_only": self.client.read_only,
            "peer_uid": self.client.peer_uid, "socket": self.client.path,
        }
        self._last_status = status
        self._last_published = time.monotonic()
        self.status_ready.emit(status)
        if self._timer:
            interval = 1000 if self._token or source == "controller" else 2000
            if self._timer.interval() != interval:
                self._timer.setInterval(interval)

    def _fallback(self, reason: str) -> None:
        # Publish failure before starting any sensor work. Reuse only fresh local
        # measurements to avoid a placeholder flash on every disconnected poll.
        status = unavailable_status(reason)
        age = self._last_status["sample_age_seconds"] + time.monotonic() - self._last_published
        if self._last_status.get("_client", {}).get("source") == "local sensors" and age <= STALE_SECONDS:
            for name in ("cpu", "gpu"):
                for key in ("temperature_c", "rpm"):
                    status[name][key] = self._last_status[name][key]
            status["sample_age_seconds"] = age
            if "telemetry_backend" in self._last_status:
                status["telemetry_backend"] = self._last_status["telemetry_backend"]
                status["backend"]["reason"] = self._last_status["backend"]["reason"]
        if self._recovery_unconfirmed:
            for name in ("cpu", "gpu"):
                status[name]["confirmation"] = "failed"
        if self._sample_timed_out and self._sample_pending:
            status["events"].append("Local sensor query remains blocked; one isolated sampler is in flight.")
        self._publish(status, "local sensors")
        if self._stopping:
            return
        if self._sample_pending:
            self._consume_sample()
            if self._sample_pending:
                return
        try:
            if self._sensor_context is None:
                self._sensor_context = _SensorContext(self.telemetry_factory)
                self._sampler = self._sensor_context.thread
        except RuntimeError as exc:
            status["events"].append(f"Read-only sampler could not start: {exc}")
            self._publish(status, "local sensors")
            return
        self._sample_results = queue.Queue(maxsize=1)
        self._sample_started = time.monotonic()
        self._sample_epoch = self._epoch
        self._sample_timed_out = False
        self._sample_pending = self._sensor_context.submit(self._sample_results)
        if not self._sample_pending:
            self._sample_results = None
            status["events"].append("Read-only sampler is stopping or already has a queued request")
            self._publish(status, "local sensors")
            return
        if self._sample_timer:
            self._sample_timer.start()

    @Slot()
    def _consume_sample(self) -> None:
        if self._stopping or not self._sample_pending or self._sample_results is None:
            return
        try:
            measured, error, completed = self._sample_results.get_nowait()
            self._sample_pending = False
            self._sample_results = None
        except queue.Empty:
            if self._sample_timed_out or time.monotonic() - self._sample_started < SENSOR_BUDGET_SECONDS:
                return
            measured, error, completed = None, "Local sensor query timed out; no replacement worker will be spawned while it is blocked.", time.monotonic()
            self._sample_timed_out = True
        if self._sample_timer:
            self._sample_timer.stop()
        if (self._sample_epoch != self._epoch or
                self._last_status.get("_client", {}).get("source") != "local sensors"):
            return
        status = copy.deepcopy(self._last_status)
        if measured is not None and completed - self._sample_started <= SENSOR_BUDGET_SECONDS:
            for name in ("cpu", "gpu"):
                for key in ("temperature_c", "rpm", "temperature_source", "rpm_source"):
                    if key in measured[name]:
                        status[name][key] = measured[name][key]
            status["sample_age_seconds"] = measured["sample_age_seconds"] + time.monotonic() - completed
            status["events"] = measured["events"]
            status["telemetry_backend"] = measured["backend"]
            status["backend"]["reason"] = "Controller socket unavailable. Local sensors only. " + (
                measured["backend"].get("reason") or "Fan control needs the supervised controller service."
            )
            if measured["recovery"].get("error"):
                status["recovery"] = measured["recovery"]
        else:
            status["sample_age_seconds"] += time.monotonic() - self._last_published
            status["events"].append(error or "Late local sensor result discarded")
        self._publish(status, "local sensors")

    def _launch_rpc(self, lane: str, op: str, params: dict[str, Any]) -> None:
        transport = self.client.clone()
        record = {"op": op, "params": copy.deepcopy(params), "epoch": self._epoch,
                  "started": time.monotonic()}
        self._inflight[lane] = record
        completed = self._rpc_completed

        def request() -> None:
            response, error = None, ""
            try:
                response = transport.request(op, **params)
                if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                    raise ClientError("Invalid controller response")
                validate_status(response.get("status"))
                if not response["ok"] and not isinstance(response.get("error"), str):
                    raise ClientError("Controller rejected the request without an explanation")
                if op == "acquire" and response["ok"] and (
                    not isinstance(response.get("token"), str) or not response["token"]
                ):
                    raise ClientError("Controller did not return a valid acquisition token")
            except Exception as exc:
                response, error = None, f"{type(exc).__name__}: {exc}"
            completed.emit(lane, {"response": response, "error": error,
                                  "peer_uid": transport.peer_uid, "finished": time.monotonic()})

        try:
            threading.Thread(target=request, name=f"nitro-{lane}-rpc", daemon=True).start()
        except RuntimeError:
            self._inflight.pop(lane)
            raise

    def _mark_uncertain(self) -> None:
        if not self._recovery_unconfirmed:
            self._uncertain_since = time.monotonic()
        self._recovery_unconfirmed = True
        self._renew_suspended = True
        self._epoch += 1
        self._targets.clear()

    def _release(self) -> None:
        if self._token is None or "command" in self._inflight:
            return
        self._targets.clear()
        self.activity.emit(True, "Returning both fans to firmware Auto; waiting for readbacks...")
        try:
            self._launch_rpc("command", "release", {"token": self._token})
        except Exception as exc:
            self._token = None
            self._mark_uncertain()
            self._fallback(f"Release RPC could not start: {exc}")
            self.command_result.emit("release", False, "Auto recovery is not confirmed; release RPC could not start.")
            self.activity.emit(False, "")

    def _fault(self, reason: str) -> None:
        self._fault_reason = reason
        self._mark_uncertain()
        self._fallback(reason)
        self.command_result.emit("recovery", False, f"{reason}. Auto is not confirmed; heartbeats have stopped.")
        # Serialize this client's recovery after its Apply RPC. The backend must
        # independently fence or quiesce hardware work that outlives a timeout.
        self._release()

    def _status_problem(self, status: dict[str, Any], *, command_reply: bool = False) -> str:
        if not status["lease"]["active"] or not status["lease"]["owned_by_client"]:
            return "The controller revoked or did not confirm this window's lease"
        if (status["sample_age_seconds"] > STALE_SECONDS or status.get("error")
                or status["recovery"].get("error") or not status["backend"]["control_available"]
                or not status["backend"]["control_enabled"]
                or any(status[name][key] is None for name in ("cpu", "gpu") for key in ("temperature_c", "rpm"))):
            return status.get("error") or status["recovery"].get("error") or "Unsafe, disabled or stale controller status"
        pending = self._inflight.get("command", {})
        ignored = set() if command_reply else (
            set(pending.get("params", {})) if pending.get("op") == "set" else
            {"cpu", "gpu"} if pending.get("op") in {"auto", "max", "release"} else set()
        )
        for name in ("cpu", "gpu"):
            if name in ignored:
                continue
            channel = status[name]
            if channel["confirmation"] == "failed":
                return f"{name.upper()} firmware confirmation failed"
            desired = self._targets.get(name)
            if desired and channel["confirmation"] == "firmware-confirmed" and (
                channel["mode"] != desired["mode"] or
                (desired["mode"] == "manual" and (channel["percent"] is None or
                 abs(channel["percent"] - desired["percent"]) > 1))
            ):
                return f"{name.upper()} firmware readback does not match the submitted mode/setpoint"
        return ""

    def _observe_recovery(self, status: dict[str, Any], record: dict[str, Any], observed: float) -> bool:
        if (self._recovery_unconfirmed and self.client.peer_uid == 0
                and record["started"] >= self._uncertain_since
                and observed - status["sample_age_seconds"] >= self._uncertain_since
                and both_auto(status) and not status["lease"]["active"] and not status["recovery"].get("error")):
            self._recovery_unconfirmed = False
            self._token = None
            self._epoch += 1
            return True
        return False

    @Slot(str, object)
    def _on_rpc_completed(self, lane: str, result: dict[str, Any]) -> None:
        record = self._inflight.pop(lane)
        op = record["op"]
        self.client.peer_uid = result["peer_uid"]
        response = result["response"]
        if response is not None:
            response = copy.deepcopy(response)
            response["status"]["sample_age_seconds"] += time.monotonic() - result["finished"]
        if lane != "command":
            if record["epoch"] != self._epoch or record["started"] < self._status_fence:
                self._advance_shutdown()
                return
            if result["error"] or not response["ok"]:
                reason = result["error"] or response["error"]
                release = self._inflight.get("command", {})
                releasing = release.get("op") == "release"
                status = response["status"] if response else None
                if (releasing and op == "heartbeat" and response and not response["ok"]
                        and self.client.peer_uid == 0 and self._token is not None
                        and record["params"].get("token") == release["params"].get("token") == self._token
                        and reason in {"No matching active lease for this process", "Control lease changed during heartbeat"}
                        and status["backend"]["connected"] and status["backend"]["control_enabled"]
                        and not status["lease"]["active"] and not status["lease"]["owned_by_client"]
                        and status["lease"]["owner_pid"] is None
                        and status["recovery"]["state"] in {"recovering", "auto"}
                        and not status["recovery"].get("error") and not status.get("error")):
                    # Release revokes the lease before Auto I/O. Its own reply,
                    # not a concurrent heartbeat, must confirm that recovery.
                    self._renew_suspended = True
                    self._publish(status)
                elif self._token and (not self._renew_suspended or releasing):
                    self._fault(f"Controller connection or heartbeat failed: {reason}")
                elif response:
                    status = copy.deepcopy(response["status"])
                    status["error"] = reason
                    self._publish(status)
                else:
                    self._fallback(f"Controller unavailable at {self.client.path}: {reason}")
            else:
                status = copy.deepcopy(response["status"])
                problem = self._status_problem(status) if self._token and not self._renew_suspended else ""
                if problem:
                    self._fault(problem)
                else:
                    recovered = self._observe_recovery(status, record, time.monotonic())
                    self._publish(status)
                    if recovered:
                        self.command_result.emit("recovery", True, "Recovery observed: fresh firmware Auto confirmed for both fans and lease inactive. No manual settings replayed.")
        else:
            self._status_fence = result["finished"]
            if op == "release":
                self._token = None
                self._epoch += 1
                self._renew_suspended = True
                confirmed = bool(response and response["ok"] and self.client.peer_uid == 0
                                 and both_auto(response["status"]) and not response["status"]["lease"]["active"]
                                 and not response["status"]["recovery"].get("error"))
                if confirmed:
                    self._recovery_unconfirmed = False
                    self._publish(response["status"])
                    message = "Lease released. Firmware Auto confirmed for both fans; motor response is separate."
                    self.command_result.emit("recovery" if self._fault_reason else op,
                                             not bool(self._fault_reason),
                                             f"{self._fault_reason}. {message}" if self._fault_reason else message)
                else:
                    reason = result["error"] or (response.get("error") if response else None) or "Both fresh firmware Auto readbacks and an inactive lease are required"
                    self._mark_uncertain()
                    self._fallback(f"Release failed: {reason}")
                    self.command_result.emit(op, False, f"Auto recovery is not confirmed. {reason}")
            elif result["error"]:
                if op == "acquire":
                    self._mark_uncertain()
                    self._fallback(f"Acquisition outcome unknown: {result['error']}")
                    self.command_result.emit(op, False, "Acquisition outcome unknown. A missing token does not prove that no lease was granted. Auto recovery is not confirmed.")
                else:
                    self._fault(f"Control request outcome unconfirmed: {result['error']}")
            elif not response["ok"]:
                if self._token:
                    self._fault(response["error"])
                else:
                    status = copy.deepcopy(response["status"])
                    status["error"] = response["error"]
                    if ((op == "acquire" and "outcome unconfirmed" in response["error"].casefold())
                            or (status["lease"]["active"] and status["lease"]["owned_by_client"])):
                        self._mark_uncertain()
                    self._publish(status)
                    self.command_result.emit(op, False, response["error"])
            else:
                if op == "acquire":
                    self._token = response["token"]
                    self._epoch += 1
                    self._renew_suspended = False
                status = response["status"]
                if self._recovery_unconfirmed and op != "acquire":
                    self._release()
                else:
                    problem = self._status_problem(status, command_reply=True)
                    if problem:
                        self._fault(problem)
                    else:
                        self._publish(status)
                        message = ("Control lease acquired. Choose fan modes, then Apply. No draft is applied automatically."
                                   if op == "acquire" else command_feedback(status, list(self._targets)))
                        self.command_result.emit(op, True, message)
                        if op == "acquire":
                            self.poll()
            if "command" not in self._inflight:
                self.activity.emit(False, "")
            if self._recovery_unconfirmed and self._token:
                self._release()
        self._advance_shutdown()

    @Slot()
    def poll(self) -> None:
        if self._finished:
            return
        if not self._stopping and QThread.currentThread().isInterruptionRequested():
            self.stop()
            return
        try:
            if self._token and not self._renew_suspended:
                if "heartbeat" not in self._inflight:
                    self._launch_rpc("heartbeat", "heartbeat", {"token": self._token})
            elif not self._stopping and "status" not in self._inflight:
                self._launch_rpc("status", "status", {})
        except Exception as exc:
            if self._token and not self._renew_suspended:
                self._fault(f"Heartbeat RPC could not start: {exc}")
            else:
                self._fallback(f"Status RPC could not start: {exc}")
        self._advance_shutdown()

    @Slot(str, dict)
    def perform(self, op: str, params: dict[str, Any]) -> None:
        if self._stopping or "command" in self._inflight or QThread.currentThread().isInterruptionRequested():
            return
        try:
            if op == "release":
                if self._token is None:
                    message = ("Ownership / Auto recovery is unconfirmed; no token is available for release."
                               if self._recovery_unconfirmed else "No control lease held by this window.")
                    self.command_result.emit(op, not self._recovery_unconfirmed, message)
                    self.activity.emit(False, "")
                else:
                    self._release()
                return
            if op == "acquire":
                if self._token:
                    raise ClientError("This window already owns a lease; authorization will not run again")
                if self._recovery_unconfirmed:
                    raise ClientError("Wait for fresh, inactive-lease firmware Auto readbacks before acquiring again")
                if self._last_status["lease"]["active"]:
                    raise ClientError("A lease is already active. Wait for its owner to release control")
                self._fault_reason = ""
            elif op not in {"set", "auto", "max"} or not self._token or self._renew_suspended:
                raise ClientError("Take control explicitly before applying fan changes")
            request = dict(params)
            if op != "acquire":
                request["token"] = self._token
            validate_request(op, request)
            if op == "set":
                self._targets.update(copy.deepcopy(params))
            elif op in {"auto", "max"}:
                self._targets = {name: {"mode": op} for name in ("cpu", "gpu")}
            self.activity.emit(True, "Authorization may be required. Waiting for the system prompt..." if op == "acquire" else "Waiting for controller readback...")
            self._launch_rpc("command", op, request)
        except Exception as exc:
            self.command_result.emit(op, False, str(exc))
            self.activity.emit(False, "")

    @Slot()
    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self._sensor_context:
            self._sensor_context.close()
        if self._sample_timer:
            self._sample_timer.stop()
        self._advance_shutdown()

    def _advance_shutdown(self) -> None:
        if not self._stopping or self._finished:
            return
        if "command" in self._inflight:
            return
        if self._token:
            self._release()
            return
        if self._inflight:
            return
        self._finished = True
        if self._timer:
            self._timer.stop()
        if self._sample_timer:
            self._sample_timer.stop()
        message = ("Ownership / Auto recovery unconfirmed. No session token is available; supervisor recovery must be checked."
                   if self._recovery_unconfirmed else "No lease remains owned by this window; any release has confirmed both firmware Auto modes.")
        # Dispose Qt objects on the GUI thread, not concurrently with widgets.
        # PySide's QObject destruction can otherwise invert its GIL/Qt locks.
        self.moveToThread(self._home_thread)
        self.shutdown_result.emit(not self._recovery_unconfirmed, message)
        self.finished.emit()


def label(text: str = "", role: str = "", *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setProperty("role", role)
    widget.setWordWrap(wrap)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    if wrap:
        widget.setMinimumWidth(0)
    return widget


def display_number(value: Any, decimals: int = 0) -> str:
    if type(value) not in (int, float) or not math.isfinite(value):
        return "--"
    return f"{value:,.{decimals}f}"


def mode_text(mode: str | None, percent: float | None) -> str:
    if mode is None:
        return "--"
    return mode.capitalize() + (f" / {display_number(percent)}%" if percent is not None else " / --%")


class FanPanel(QWidget):
    edited = Signal()

    def __init__(self, channel: str) -> None:
        super().__init__()
        self.channel = channel
        self.dirty = False
        self._editable = False
        self.setAccessibleName(f"{channel.upper()} fan control")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(6)
        heading = QHBoxLayout()
        heading.addWidget(label(f"{channel.upper()} fan", "section"))
        heading.addStretch()
        heading.addWidget(label(f"Channel {1 if channel == 'cpu' else 2}", "muted"))
        layout.addLayout(heading)

        readings = QHBoxLayout()
        rpm_column = QVBoxLayout()
        rpm_column.setSpacing(0)
        self.rpm = label("--", "rpm")
        self.rpm.setAccessibleDescription(f"{channel.upper()} measured fan revolutions per minute")
        rpm_column.addWidget(self.rpm)
        self.rpm_hint = label("RPM unavailable", "muted")
        rpm_column.addWidget(self.rpm_hint)
        readings.addLayout(rpm_column, 1)
        temperature_column = QVBoxLayout()
        temperature_column.setSpacing(2)
        self.temperature = label("-- C", "temperature")
        self.temperature.setAccessibleDescription(f"{channel.upper()} measured temperature in Celsius")
        self.temperature.setAlignment(Qt.AlignmentFlag.AlignRight)
        temperature_column.addWidget(self.temperature)
        temp_caption = label("CPU package / coretemp" if channel == "cpu" else "NVIDIA temperature", "muted")
        temp_caption.setAlignment(Qt.AlignmentFlag.AlignRight)
        temperature_column.addWidget(temp_caption)
        readings.addLayout(temperature_column)
        layout.addLayout(readings)

        modes = QHBoxLayout()
        modes.setSpacing(5)
        self.mode_group = QButtonGroup(self)
        self.mode_buttons: dict[str, QPushButton] = {}
        for mode in ("auto", "max", "manual"):
            button = QPushButton(mode.capitalize())
            button.setCheckable(True)
            button.setAccessibleName(f"{channel.upper()} fan {mode.capitalize()} mode, staged until Apply")
            button.setToolTip(f"Stage {mode.capitalize()} for the {channel.upper()} fan. Apply sends the request.")
            button.clicked.connect(self._edit)
            self.mode_group.addButton(button)
            self.mode_buttons[mode] = button
            modes.addWidget(button, 1)
        layout.addLayout(modes)
        manual = QHBoxLayout()
        manual.setSpacing(10)
        manual.addWidget(label("Manual draft", "muted"))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(50, 100)
        self.slider.setValue(70)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.slider.setAccessibleName(f"{channel.upper()} manual percentage, 50 to 100")
        self.slider.setToolTip("Manual setpoint, 50-100%. Editing does not send a fan request.")
        self.percent = QSpinBox()
        self.percent.setRange(50, 100)
        self.percent.setValue(70)
        self.percent.setSuffix("%")
        self.percent.setKeyboardTracking(False)
        self.percent.setAccessibleName(f"{channel.upper()} manual percentage numeric input")
        self.slider.valueChanged.connect(self.percent.setValue)
        self.percent.valueChanged.connect(self.slider.setValue)
        self.slider.valueChanged.connect(self._edit)
        manual.addWidget(self.slider, 1)
        manual.addWidget(self.percent)
        layout.addLayout(manual)
        self.draft_hint = label("Take control to enable editing.", "muted", wrap=True)
        layout.addWidget(self.draft_hint)

        states = QGridLayout()
        states.setHorizontalSpacing(14)
        states.setVerticalSpacing(3)
        self.requested = label("--")
        self.firmware = label("--")
        self.requested.setAccessibleDescription(f"{channel.upper()} controller-requested mode and percentage")
        self.firmware.setAccessibleDescription(f"{channel.upper()} firmware-reported mode and setpoint; not measured RPM")
        states.addWidget(label("Requested mode / %", "muted"), 0, 0)
        states.addWidget(self.requested, 0, 1)
        states.addWidget(label("Firmware setpoint", "muted"), 1, 0)
        states.addWidget(self.firmware, 1, 1)
        states.setColumnStretch(1, 1)
        layout.addLayout(states)
        self.confirmation = label("Mode: unsupported", "muted", wrap=True)
        self.rpm_response = label("RPM response: unverified", "muted", wrap=True)
        self.confirmation.setAccessibleDescription(f"{channel.upper()} firmware mode confirmation")
        self.rpm_response.setAccessibleDescription(f"{channel.upper()} measured motor response to the requested mode")
        feedback = QVBoxLayout()
        feedback.setSpacing(2)
        feedback.addWidget(self.confirmation)
        feedback.addWidget(self.rpm_response)
        layout.addLayout(feedback)
        layout.addStretch()
        self.set_editable(False)

    def draft(self) -> dict[str, Any] | None:
        mode = next((name for name, button in self.mode_buttons.items() if button.isChecked()), None)
        if mode is None:
            return None
        return {"mode": mode, **({"percent": self.percent.value()} if mode == "manual" else {})}

    def _edit(self, *_: Any) -> None:
        self.dirty = True
        self._update_draft_hint()
        self.edited.emit()

    def stage(self, mode: str) -> None:
        self.mode_buttons[mode].setChecked(True)
        self._edit()

    def _update_draft_hint(self) -> None:
        draft = self.draft()
        manual = bool(draft and draft["mode"] == "manual")
        self.slider.setEnabled(self._editable and manual)
        self.percent.setEnabled(self._editable and manual)
        if self.dirty and draft:
            value = draft["mode"].capitalize()
            if manual:
                value += f" at {self.percent.value()}%"
            self.draft_hint.setText(f"Not applied: {value}")
        elif self._editable:
            self.draft_hint.setText("Choose a mode, then Apply.")
        else:
            self.draft_hint.setText("Fan controls unavailable. See status above.")

    def set_editable(self, enabled: bool) -> None:
        self._editable = enabled
        for button in self.mode_buttons.values():
            button.setEnabled(enabled)
        self._update_draft_hint()

    def discard_draft(self) -> None:
        self.dirty = False

    def render(self, data: dict[str, Any], *, stale: bool, editable: bool, unavailable_reason: str) -> None:
        self.rpm.setText("--" if stale else display_number(data["rpm"]))
        self.rpm_hint.setText("Sample stale" if stale else ("Measured RPM" if data["rpm"] is not None else "RPM unavailable"))
        self.rpm.setAccessibleDescription(f"{self.channel.upper()} measured fan revolutions per minute. {self.rpm_hint.text()}")
        self.temperature.setText(("--" if stale else display_number(data["temperature_c"])) + " C")
        self.temperature.setToolTip(str(data.get("temperature_source") or (
            "CPU package sensor from coretemp" if self.channel == "cpu" else "NVIDIA temperature from the read-only sensor; unavailable during initialization"
        )))
        self.requested.setText(mode_text(data["requested_mode"], data["requested_percent"]))
        self.firmware.setText(("Stale: " if stale else "") + mode_text(data["mode"], data["percent"]))
        confirmations = {
            "unsupported": "Mode: unavailable", "pending": "Mode: readback pending",
            "firmware-confirmed": "Mode: firmware confirmed", "failed": "Mode: confirmation failed",
        }
        self.confirmation.setText("Mode: stale, not confirmed live" if stale else confirmations[data["confirmation"]])
        responses = {
            "unverified": "RPM response: unverified", "pending": "RPM response: pending",
            "observed": "RPM response: observed", "not-observed": "RPM response: not observed",
        }
        self.rpm_response.setText("RPM response: stale" if stale else responses[data["rpm_response"]])
        if not self.dirty:
            self.mode_group.setExclusive(False)
            for mode, button in self.mode_buttons.items():
                button.setChecked(not stale and data["mode"] == mode and data["confirmation"] == "firmware-confirmed")
            self.mode_group.setExclusive(True)
            if data["mode"] == "manual" and data["percent"] is not None:
                value = min(100, max(50, round(data["percent"])))
                self.slider.blockSignals(True)
                self.percent.blockSignals(True)
                self.slider.setValue(value)
                self.percent.setValue(value)
                self.slider.blockSignals(False)
                self.percent.blockSignals(False)
        self.set_editable(editable and data["confirmation"] not in {"unsupported", "failed"})
        if not self._editable:
            self.setToolTip(unavailable_reason)
        else:
            self.setToolTip("")


class NitroControlWindow(QMainWindow):
    command = Signal(str, dict)
    stop_worker = Signal()

    def __init__(
        self, *, socket_path: str = DEFAULT_SOCKET, read_only: bool = False,
        client: SocketClient | None = None, telemetry_factory: Callable[[], Any] = _telemetry_factory,
        start_worker: bool = True,
    ) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.read_only = read_only
        self.status = unavailable_status("Connecting to the local controller. No fan changes are made at startup.")
        self._last_update = time.monotonic()
        self._busy = False
        self._busy_text = ""
        self._message = ""
        self._message_error = False
        self._applied_channels: tuple[str, ...] = ()
        self._closing = False
        self._shutdown_failed = False
        self._allow_unconfirmed_exit = False
        self._thread_done = not start_worker
        self._stacked: bool | None = None
        self.setWindowTitle("Nitro Control")
        self.setAccessibleName("Nitro Control, Acer Nitro ANV15-51 cooling")
        self.resize(900, 680)
        self.setMinimumSize(640, 620)
        self._build_ui()
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(1000)
        self._age_timer.timeout.connect(self._render)
        self._age_timer.start()
        self._thread: QThread | None = None
        self._worker: ControllerWorker | None = None
        if start_worker:
            self._thread = QThread(self)
            self._thread.setObjectName("nitro-controller-io")
            self._worker = ControllerWorker(client or SocketClient(socket_path, read_only=read_only), telemetry_factory)
            self._worker.moveToThread(self._thread)
            self._thread.started.connect(self._worker.start)
            self.command.connect(self._worker.perform, Qt.ConnectionType.QueuedConnection)
            self.stop_worker.connect(self._worker.stop, Qt.ConnectionType.QueuedConnection)
            self._worker.status_ready.connect(self.update_status)
            self._worker.activity.connect(self._on_activity)
            self._worker.command_result.connect(self._on_result)
            self._worker.shutdown_result.connect(self._on_shutdown_result)
            self._worker.finished.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
            self._worker.finished.connect(self._worker.deleteLater)
            self._thread.finished.connect(self._on_thread_finished)
            self._thread.start()
        self._render()
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._apply)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self._exit_clicked)
        QShortcut(QKeySequence("Ctrl+D"), self, activated=self.diagnostics_toggle.click)

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(26, 16, 26, 12)
        title = QVBoxLayout()
        title.setSpacing(2)
        title.addWidget(label("Nitro Control", "heading"))
        title.addWidget(label("Acer Nitro ANV15-51  /  Cooling control", "muted"))
        header.addLayout(title)
        header.addStretch()
        self.verification = label("Hardware unverified", "warning")
        self.verification.setAccessibleDescription("Hardware verification status")
        self.verification.setToolTip("Firmware acceptance is not a hardware validation record.")
        header.addWidget(self.verification, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("scrollContent")
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(content)
        body.setContentsMargins(24, 0, 24, 14)
        body.setSpacing(8)
        status_area = QFrame()
        status_area.setObjectName("statusArea")
        status_layout = QHBoxLayout(status_area)
        status_layout.setContentsMargins(14, 10, 12, 10)
        status_text = QVBoxLayout()
        status_text.setSpacing(3)
        self.state_title = label("Connecting...")
        self.state_title.setObjectName("stateTitle")
        self.state_reason = label("", "muted", wrap=True)
        status_text.addWidget(self.state_title)
        status_text.addWidget(self.state_reason)
        status_layout.addLayout(status_text, 1)
        self.take_control = QPushButton("Take control...")
        self.take_control.setAccessibleName("Take control; system authorization may be required")
        self.take_control.setToolTip("Authorize a supervised lease and establish firmware Auto on both fans. May open a system authorization prompt.")
        self.take_control.clicked.connect(lambda: self._send("acquire", {}))
        status_layout.addWidget(self.take_control)
        body.addWidget(status_area)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(label("Both fans", "section"))
        toolbar.addStretch()
        self.all_auto = QPushButton("Stage all Auto")
        self.all_max = QPushButton("Stage all Max")
        self.all_auto.setAccessibleName("Stage firmware Auto for both fans, then Apply")
        self.all_max.setAccessibleName("Stage Max for both fans, then Apply")
        self.all_auto.clicked.connect(lambda: self._stage_all("auto"))
        self.all_max.clicked.connect(lambda: self._stage_all("max"))
        toolbar.addWidget(self.all_auto)
        toolbar.addWidget(self.all_max)
        body.addLayout(toolbar)

        self.hardware = QFrame()
        self.hardware.setObjectName("hardware")
        self.hardware_layout = QGridLayout(self.hardware)
        self.hardware_layout.setContentsMargins(0, 0, 0, 0)
        self.hardware_layout.setSpacing(0)
        self.panels = {name: FanPanel(name) for name in ("cpu", "gpu")}
        for panel in self.panels.values():
            panel.edited.connect(self._draft_changed)
        self.divider = QFrame()
        self.divider.setObjectName("divider")
        self._reflow()
        body.addWidget(self.hardware)
        self.policy = label("Manual range: 50-100% software policy, not a calibrated hardware limit. Editing never writes.", "muted", wrap=True)
        body.addWidget(self.policy)

        apply_row = QHBoxLayout()
        self.feedback = label("Monitoring only. No startup fan writes or authorization.", "muted", wrap=True)
        self.feedback.setAccessibleDescription("Command result and unapplied changes")
        apply_row.addWidget(self.feedback, 1)
        self.apply_button = QPushButton("Apply changes")
        self.apply_button.setProperty("primary", True)
        self.apply_button.setAccessibleName("Apply staged fan changes")
        self.apply_button.setToolTip("Send only edited channels. Keyboard shortcut: Ctrl+Enter.")
        self.apply_button.clicked.connect(self._apply)
        apply_row.addWidget(self.apply_button)
        body.addLayout(apply_row)

        self.diagnostics_toggle = QToolButton()
        self.diagnostics_toggle.setObjectName("diagnosticsToggle")
        self.diagnostics_toggle.setText("Diagnostics and setup")
        self.diagnostics_toggle.setCheckable(True)
        self.diagnostics_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.diagnostics_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.diagnostics_toggle.setAccessibleName("Expand diagnostics and fan-control setup requirements")
        self.diagnostics_toggle.toggled.connect(self._toggle_diagnostics)
        body.addWidget(self.diagnostics_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setAccessibleName("Controller diagnostics and setup instructions, read only")
        self.diagnostics.setMinimumHeight(180)
        self.diagnostics.setMaximumHeight(240)
        self.diagnostics.hide()
        body.addWidget(self.diagnostics)
        body.addStretch(1)
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll, 1)

        footer = QFrame()
        footer.setObjectName("footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(24, 10, 24, 10)
        ownership_column = QVBoxLayout()
        ownership_column.setSpacing(2)
        self.ownership = label("Read-only session", wrap=True)
        self.recovery = label("Control requires a supervised, expiring lease.", "muted", wrap=True)
        ownership_column.addWidget(self.ownership)
        ownership_column.addWidget(self.recovery)
        footer_layout.addLayout(ownership_column, 1)
        self.release_button = QPushButton("Release to Auto")
        self.release_button.setAccessibleName("Release control and restore both fans to firmware Auto")
        self.release_button.clicked.connect(lambda: self._send("release", {}))
        footer_layout.addWidget(self.release_button)
        self.exit_button = QPushButton("Exit / restore Auto")
        self.exit_button.setAccessibleName("Exit; release any owned lease and restore firmware Auto")
        self.exit_button.setToolTip("Ctrl+Q. Owned leases are released and both firmware Auto readbacks checked before exit.")
        self.exit_button.clicked.connect(self._exit_clicked)
        footer_layout.addWidget(self.exit_button)
        outer.addWidget(footer)
        self.setCentralWidget(central)

    def _reflow(self) -> None:
        stacked = self.width() < 780
        if self._stacked == stacked:
            return
        self._stacked = stacked
        for widget in (*self.panels.values(), self.divider):
            self.hardware_layout.removeWidget(widget)
        self.hardware_layout.setRowStretch(0, 0)
        self.hardware_layout.setRowStretch(2, 0)
        self.hardware_layout.setColumnStretch(0, 1)
        self.hardware_layout.setColumnStretch(2, 0 if stacked else 1)
        self.divider.setMinimumSize(0, 0)
        self.divider.setMaximumSize(16777215, 16777215)
        if stacked:
            self.divider.setFixedHeight(1)
            self.hardware_layout.addWidget(self.panels["cpu"], 0, 0)
            self.hardware_layout.addWidget(self.divider, 1, 0)
            self.hardware_layout.addWidget(self.panels["gpu"], 2, 0)
        else:
            self.divider.setFixedWidth(1)
            self.hardware_layout.addWidget(self.panels["cpu"], 0, 0)
            self.hardware_layout.addWidget(self.divider, 0, 1)
            self.hardware_layout.addWidget(self.panels["gpu"], 0, 2)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "hardware_layout"):
            self._reflow()

    @Slot(dict)
    def update_status(self, status: dict[str, Any]) -> None:
        self.status = copy.deepcopy(status)
        self._last_update = time.monotonic()
        self._render()

    def _current_age(self) -> float:
        return self.status["sample_age_seconds"] + time.monotonic() - self._last_update

    def _ready(self) -> bool:
        backend = self.status["backend"]
        metadata = self.status.get("_client", {})
        return bool(
            backend["connected"] and backend["control_available"] and backend["control_enabled"]
            and metadata.get("trusted") and not self.read_only and self._current_age() <= STALE_SECONDS
            and not self._closing and not self._thread_done and not self._busy
            and (not self.status.get("error") or (
                not self._owns() and not self.status["lease"]["active"] and both_auto(self.status)
                and self.status["recovery"]["state"] == "auto"
            ))
            and not self.status["recovery"].get("error")
            and all(self.status[name][key] is not None for name in self.panels for key in ("temperature_c", "rpm"))
        )

    def _owns(self) -> bool:
        return bool(self.status.get("_client", {}).get("owns_lease"))

    @Slot()
    def _render(self) -> None:
        backend = self.status["backend"]
        lease = self.status["lease"]
        metadata = self.status.get("_client", {})
        stale = self._current_age() > STALE_SECONDS
        ready = self._ready()
        owns = self._owns()
        fault = self.status["recovery"].get("error") or self._shutdown_failed
        confirmed_auto = bool(both_auto(self.status) and backend["control_available"] and not stale
                              and metadata.get("trusted", False) and not self.status.get("error") and not fault)
        if fault:
            title, reason = "Recovery requires attention", self.status["recovery"].get("error") or self._message
        elif self._closing:
            title, reason = "Closing safely", "Releasing this window's lease and checking both firmware Auto modes..."
        elif self._busy:
            title, reason = "Authorization pending" if "Authorization" in self._busy_text else "Request pending", self._busy_text
        elif stale:
            title, reason = "Telemetry stale", "Controls locked. Waiting for a fresh sample; previous readings are not shown as live."
        elif not backend["connected"]:
            title, reason = "Monitoring only / controller unavailable", backend.get("reason") or "The local controller is not connected."
        elif self.status.get("error"):
            title, reason = "Controller reported an error", self.status["error"]
        elif not backend["control_available"]:
            title, reason = "Monitoring only / fan interface unavailable", backend.get("reason") or "Required Acer PWM interfaces are unavailable."
        elif not backend["control_enabled"] or self.read_only:
            title = "Read-only session"
            reason = "Started with --read-only. No authorization or control requests can be sent." if self.read_only else (backend.get("reason") or "Hardware writes are disabled in the controller configuration.")
        elif any(self.status[name][key] is None for name in self.panels for key in ("temperature_c", "rpm")):
            title = "Telemetry incomplete / fan changes locked"
            missing = [f"{name.upper()} {'temperature' if key == 'temperature_c' else 'RPM'}"
                       for name in self.panels for key in ("temperature_c", "rpm") if self.status[name][key] is None]
            reason = "Required readings unavailable: " + ", ".join(missing) + ". Wait for a fresh sample."
        elif confirmed_auto and self._message_error:
            title, reason = "Request failed / firmware Auto confirmed", self._message
        elif confirmed_auto:
            title = "Firmware Auto confirmed / both fans"
            reason = "This confirms firmware modes, not fan-motor response or full hardware compatibility."
        elif owns:
            title, reason = "Control owned by this window", "Choose modes independently. Only Apply sends staged changes."
        elif lease["active"]:
            title, reason = "Control lease already active", f"Owner PID: {lease['owner_pid'] or '--'}. This window cannot change fans without its own session token."
        else:
            title, reason = "Ready / monitoring only", "Take control authorizes a lease and establishes both fans in Auto. No draft applies automatically."
        self.state_title.setText(title)
        self.state_title.setProperty("autoConfirmed", title == "Firmware Auto confirmed / both fans")
        self.state_title.style().unpolish(self.state_title)
        self.state_title.style().polish(self.state_title)
        self.state_reason.setText(reason)
        self.verification.setText("Hardware validation recorded" if backend["hardware_verified"] and metadata.get("trusted") else "Hardware unverified")
        self.verification.setProperty("role", "muted" if backend["hardware_verified"] and metadata.get("trusted") else "warning")
        self.verification.style().unpolish(self.verification)
        self.verification.style().polish(self.verification)
        self.take_control.setEnabled(ready and not lease["active"] and not owns)
        self.take_control.setVisible(not owns)
        for name, panel in self.panels.items():
            panel.render(self.status[name], stale=stale, editable=ready and owns, unavailable_reason=reason)
        pair_editable = ready and owns and all(panel._editable for panel in self.panels.values())
        self.all_auto.setEnabled(pair_editable)
        self.all_max.setEnabled(pair_editable)
        dirty = any(panel.dirty for panel in self.panels.values())
        self.apply_button.setEnabled(ready and owns and dirty and all(not panel.dirty or (panel._editable and panel.draft()) for panel in self.panels.values()))
        self.release_button.setEnabled(owns and not self._busy and not self._closing)
        self.exit_button.setEnabled(not self._closing)
        if fault:
            feedback = self._message if self._message_error else reason
        elif self._busy:
            feedback = self._busy_text
        elif dirty:
            feedback = "Unapplied changes. Review each fan, then Apply."
        elif self._applied_channels and owns and not self._message_error:
            feedback = command_feedback(self.status, self._applied_channels)
        elif self._message:
            feedback = self._message
        else:
            feedback = "Monitoring only. No startup fan writes or authorization."
        self.feedback.setText(feedback)
        self.feedback.setProperty("role", "warning" if fault or (self._message_error and not dirty) else "muted")
        self.feedback.style().unpolish(self.feedback)
        self.feedback.style().polish(self.feedback)
        if owns:
            self.ownership.setText(f"Lease owned by this window / PID {lease['owner_pid'] or '--'}")
            self.recovery.setText("Independent 1 s heartbeats. Closing checks Auto; lost clients depend on supervisor recovery.")
        elif fault:
            self.ownership.setText("Ownership / Auto recovery not confirmed")
            self.recovery.setText("Heartbeats stopped. Supervisor recovery required; do not assume fans are in Auto.")
        elif not backend["connected"]:
            self.ownership.setText("Read-only local sensors / controller ownership unavailable")
            self.recovery.setText("No fan commands sent. Controller installation and hardware approval are separate.")
        elif lease["active"]:
            self.ownership.setText(f"Active lease / owner PID {lease['owner_pid'] or '--'}")
            self.recovery.setText("This window has no usable session token. No automatic takeover.")
        else:
            self.ownership.setText("Monitoring / no lease held by this window")
            self.recovery.setText("Control requires a supervised lease. No manual settings are restored on restart.")
        if self.diagnostics.isVisible():
            self._update_diagnostics()

    @Slot(bool, str)
    def _on_activity(self, busy: bool, text: str) -> None:
        self._busy, self._busy_text = busy, text
        self._render()

    @Slot(str, bool, str)
    def _on_result(self, op: str, ok: bool, message: str) -> None:
        self._message, self._message_error = message, not ok
        if op in {"acquire", "release", "recovery"}:
            self._applied_channels = ()
        # A successful transport alone never clears a draft. The worker has
        # checked the returned status and labels readback/RPM pending distinctly.
        if ok and op in {"set", "auto", "max", "release", "acquire", "recovery"}:
            for panel in self.panels.values():
                panel.discard_draft()
        if not ok and op == "recovery":
            for panel in self.panels.values():
                panel.discard_draft()
        self._render()

    def _send(self, op: str, params: dict[str, Any]) -> None:
        if self._busy or self._closing:
            return
        if op == "acquire" and not self.take_control.isEnabled():
            return
        if op == "release" and not self.release_button.isEnabled():
            return
        self._busy = True
        if op == "set":
            self._applied_channels = tuple(params)
        self._busy_text = "Authorization may be required. Waiting for the system prompt..." if op == "acquire" else "Waiting for controller readback..."
        self._render()
        self.command.emit(op, params)

    def _stage_all(self, mode: str) -> None:
        if not self.all_auto.isEnabled():
            return
        for panel in self.panels.values():
            panel.stage(mode)

    @Slot()
    def _draft_changed(self) -> None:
        self._message = ""
        self._message_error = False
        self._render()

    @Slot()
    def _apply(self) -> None:
        # Commit an in-progress numeric edit before taking the explicit snapshot.
        for panel in self.panels.values():
            panel.percent.interpretText()
        if not self.apply_button.isEnabled():
            return
        params = {name: panel.draft() for name, panel in self.panels.items() if panel.dirty}
        self._send("set", params)

    @Slot(bool)
    def _toggle_diagnostics(self, expanded: bool) -> None:
        self.diagnostics.setVisible(expanded)
        self.diagnostics_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.diagnostics_toggle.setAccessibleName(("Collapse" if expanded else "Expand") + " diagnostics and setup")
        if expanded:
            self._update_diagnostics()
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(self.diagnostics))

    def _update_diagnostics(self) -> None:
        def scrub(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items() if key.lower() not in {"token", "authorization"}}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value
        text = (
            f"Socket: {self.socket_path}\nSample age: {self._current_age():.1f} s\n"
            f"Read-only flag: {self.read_only}\n\n{SETUP_HELP}\n\n"
            "Firmware confirmation is not motor verification. RPM is measured only; it is never calculated from percent.\n\n"
            + json.dumps(scrub(self.status), indent=2, ensure_ascii=True)
        )
        if self._message_error:
            text += f"\n\nLast request error: {self._message}"
        position = self.diagnostics.verticalScrollBar().value()
        self.diagnostics.setPlainText(text)
        self.diagnostics.verticalScrollBar().setValue(position)

    @Slot()
    def _exit_clicked(self) -> None:
        if self._shutdown_failed and self._thread_done:
            self._allow_unconfirmed_exit = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread_done:
            if self._shutdown_failed and not self._allow_unconfirmed_exit:
                event.ignore()
                return
            self._age_timer.stop()
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self._message = "Releasing any owned lease before exit..."
            self._render()
            if self._thread:
                self._thread.requestInterruption()
            self.stop_worker.emit()

    @Slot(bool, str)
    def _on_shutdown_result(self, ok: bool, message: str) -> None:
        self._shutdown_failed = not ok
        self._message, self._message_error = message, not ok

    @Slot()
    def _on_thread_finished(self) -> None:
        self._thread_done = True
        if self._shutdown_failed:
            self._closing = False
            self.exit_button.setText("Exit without Auto confirmation")
            self.exit_button.setAccessibleName("Exit without verified Auto recovery; supervisor recovery is required")
            self._render()
        elif self._closing:
            QTimer.singleShot(0, self.close)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Native Nitro Control cooling interface; no startup fan writes.")
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="Controller Unix socket path")
    parser.add_argument("--read-only", action="store_true", help="Never acquire a lease or send fan-control requests")
    parser.add_argument("--screenshot", type=Path, help="Save only this app's window, using actual available telemetry")
    parser.add_argument("--quit-after", type=float, metavar="SECONDS", help="Close safely after this many seconds")
    args = parser.parse_args(argv)
    if args.quit_after is not None and (not math.isfinite(args.quit_after) or args.quit_after <= 0):
        parser.error("--quit-after must be a positive finite number")
    if args.screenshot is not None and not args.screenshot.parent.is_dir():
        parser.error("--screenshot parent directory must already exist")
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("Nitro Control")
    app.setOrganizationName("Nitro Control")
    app.setDesktopFileName("org.nitrocontrol.NitroControl")
    icon = QIcon.fromTheme("org.nitrocontrol.NitroControl")
    if icon.isNull():
        candidate = Path(__file__).resolve().parents[2] / "data/org.nitrocontrol.NitroControl.svg"
        if candidate.is_file():
            icon = QIcon(str(candidate))
    app.setWindowIcon(icon)
    apply_theme(app)
    window = NitroControlWindow(socket_path=args.socket, read_only=args.read_only)
    window.show()
    screenshot_failed = False

    def screenshot() -> None:
        nonlocal screenshot_failed
        if args.screenshot is not None:
            if not window.grab().save(str(args.screenshot)):
                screenshot_failed = True
                print(f"Could not save window screenshot: {args.screenshot}", file=sys.stderr)

    if args.screenshot is not None:
        delay = min(6000, max(0, int(args.quit_after * 1000) - 250)) if args.quit_after else 6000
        QTimer.singleShot(delay, screenshot)
    if args.quit_after is not None:
        QTimer.singleShot(max(1, int(args.quit_after * 1000)), window.close)
    signal.signal(signal.SIGINT, lambda *_: window.close())
    signal.signal(signal.SIGTERM, lambda *_: window.close())
    # The window's low-rate age timer also lets Python handle POSIX signals.
    result = app.exec()
    if window._thread and window._thread.isRunning():
        window._thread.requestInterruption()
        window.stop_worker.emit()
        window._thread.wait(35000)
    return 1 if screenshot_failed else result


if __name__ == "__main__":
    raise SystemExit(main())
