"""Limited steady-state hardware evidence runner; default operation is read-only.

Execution requires a separately booted/read-only-gated test kernel, the existing
root controller, explicit --execute-approved, and an operator evidence directory.
This never installs policy, enables a controller, writes sysfs, or creates an
acceptance record. Already-issued firmware operations cannot be cancelled here.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import signal
import threading
import time

from . import __version__
from .client import (ACQUIRE_TIMEOUT, MUTATION_TIMEOUT, SocketClient, validate_status)
from .hardware import IDENTITY, MAX_SAMPLE_AGE, expected_pwm

TEST_KERNEL = "7.2.9-arch1-1-nitro-test"
VALIDATION_REASON = "Validation mode: hardware NOT verified"
RPC_SECONDS = 1.5
PHASE_SECONDS = 8.0
POLL_SECONDS = 0.5
PENDING_TESTS = ["GUI lease expiry/death", "worker crash/hang", "partial-write/readback failure",
                 "suspend/resume"]
AUTO = {"cpu": {"mode": "auto"}, "gpu": {"mode": "auto"}}
PHASES = [
    ("both_max", {"cpu": {"mode": "max"}, "gpu": {"mode": "max"}}, ("cpu", "gpu")),
    ("cpu_manual_80", {"cpu": {"mode": "manual", "percent": 80}, "gpu": {"mode": "auto"}}, ("cpu",)),
    ("gpu_manual_80", {"cpu": {"mode": "auto"}, "gpu": {"mode": "manual", "percent": 80}}, ("gpu",)),
]


class AcceptanceError(RuntimeError):
    pass


def read_identity():
    result = {"kernel_release": platform.release(), "dmi": {}}
    for key in IDENTITY:
        try:
            result["dmi"][key] = (Path("/sys/class/dmi/id") / key).read_text(encoding="ascii").strip()[:256]
        except (OSError, UnicodeError):
            result["dmi"][key] = None
    return result


class Evidence:
    """New, normal-user-owned evidence only; never overwrite an earlier run."""
    def __init__(self, directory):
        if os.geteuid() == 0:
            raise ValueError("Evidence must be written by a normal user")
        path = Path(directory).absolute()
        path.mkdir(mode=0o700)
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self.lock = threading.Lock()
        try:
            fd = os.open("events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
            self.stream = os.fdopen(fd, "w", encoding="ascii", buffering=1)
        except BaseException:
            os.close(self.fd)
            raise

    def event(self, event):
        with self.lock:
            self.stream.write(json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n")

    def finish(self, report):
        with self.lock:
            self.stream.flush()
            os.fsync(self.stream.fileno())
            fd = os.open(".result.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                json.dump(report, stream, allow_nan=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(".result.tmp", "result.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)

    def close(self):
        self.stream.close()
        os.close(self.fd)


class AcceptanceRunner:
    def __init__(self, *, execute_approved=False, validation_mode=False, client=None,
                 identity_reader=None, clock=time.monotonic, wait=None, emit=None):
        self.execute = execute_approved
        self.validation_mode = validation_mode
        # All injected transports are explicitly offline evidence. There is no fake CLI.
        self.offline = client is not None
        self.evidence_kind = "offline-simulation" if self.offline else "real-controller"
        self.client = client if client is not None else SocketClient(read_only=not execute_approved)
        self.identity_reader = read_identity if identity_reader is None else identity_reader
        self.clock = clock
        self.sink = emit
        self.halt = threading.Event()
        self.wait = wait if wait is not None else self.halt.wait
        self.hb_stop = threading.Event()
        self.hb_ready = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.token = None
        self.acquire_attempted = False
        self.failure = None
        self.stages = []
        self.last = None

    def _emit(self, kind, stage, **data):
        if self.sink is not None:
            self.sink({"utc": datetime.now(timezone.utc).isoformat(), "monotonic": self.clock(),
                       "evidence_kind": self.evidence_kind,
                       "kind": kind, "stage": stage, **data})

    @staticmethod
    def _snapshot(status):
        # Never serialize arbitrary response text/events: these may contain secrets.
        backend = status["backend"]
        return {"schema": status["schema"],
                "backend": {key: backend[key] for key in
                            ("connected", "control_available", "control_enabled", "hardware_verified")},
                "validation_mode_observed": backend.get("reason") == VALIDATION_REASON,
                **{channel: {key: status[channel][key] for key in
                             ("temperature_c", "rpm", "mode", "pwm", "percent", "requested_mode",
                              "requested_percent", "confirmation", "rpm_response")}
                   for channel in ("cpu", "gpu")},
                "lease": {key: status["lease"][key] for key in
                          ("active", "owned_by_client", "owner_pid", "remaining_seconds")},
                "sample_age_seconds": status["sample_age_seconds"],
                "fault_present": status.get("error") is not None or status["recovery"].get("error") is not None
                or status["recovery"]["state"] == "fault"}

    def _checkpoint(self):
        with self.lock:
            failure = self.failure
        if failure:
            raise AcceptanceError(failure)

    def _rpc(self, op, stage, *, lane=None, targets=None, deadline=None, cleanup=False):
        if not cleanup:
            self._checkpoint()
        client = lane if lane is not None else self.client
        remaining = float("inf") if deadline is None else deadline - self.clock()
        if remaining <= 0:
            raise AcceptanceError("Non-Auto command/observation budget expired")
        client.timeout = min(RPC_SECONDS, remaining)
        client.mutation_timeout = min(MUTATION_TIMEOUT, remaining)
        client.acquire_timeout = ACQUIRE_TIMEOUT
        params = {} if op in ("status", "acquire") else {"token": self.token}
        if targets is not None:
            params.update(targets)
        began = self.clock()
        try:
            if op == "acquire":
                self.acquire_attempted = True
            response = client.request(op, **params)
            received = self.clock()
            if not self.offline and client.peer_uid != 0:
                raise AcceptanceError("Controller peer is not root")
            if type(response) is not dict or type(response.get("ok")) is not bool:
                raise AcceptanceError("Malformed controller reply; outcome unconfirmed")
            validate_status(response.get("status"))
            status = response["status"]
            if op == "acquire" and response["ok"]:
                token = response.get("token")
                if type(token) is not str or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
                    raise AcceptanceError("Acquisition token unavailable; outcome unconfirmed")
                self.token = token
            sample = self._snapshot(status)
            self.last = sample
            if not (lane is not None and self.hb_stop.is_set()):
                self._emit("rpc", stage, op=op, ok=response["ok"], elapsed_seconds=received - began,
                           request_monotonic=began, response_monotonic=received,
                           requested=targets, observed=sample)
            if not response["ok"]:
                raise AcceptanceError("Controller rejected operation; state is not confirmed")
            if deadline is not None and self.clock() >= deadline:
                raise AcceptanceError("Non-Auto command/observation budget expired")
            return status
        except AcceptanceError:
            raise
        except Exception:
            # Do not copy exception/response strings, argv, or polkit data into evidence.
            raise AcceptanceError("Transport, schema or evidence failure; outcome unconfirmed") from None

    def _safe(self, status, *, cool=False, lease=False):
        backend = status["backend"]
        if not all(backend[key] for key in ("connected", "control_available", "control_enabled")):
            raise AcceptanceError("Controller is disconnected, unsupported or read-only")
        if status.get("error") is not None or status["recovery"].get("error") is not None or status["recovery"]["state"] == "fault":
            raise AcceptanceError("Controller/recovery reports a fault")
        if not 0 <= status["sample_age_seconds"] <= MAX_SAMPLE_AGE:
            raise AcceptanceError("Telemetry missing or stale")
        for channel, limit in (("cpu", 75 if cool else 85), ("gpu", 70 if cool else 80)):
            data = status[channel]
            temperature, rpm, pwm = data["temperature_c"], data["rpm"], data["pwm"]
            if (type(temperature) not in (int, float) or not math.isfinite(temperature)
                    or not 0 < temperature < limit):
                raise AcceptanceError("Temperature missing/unsafe; stop the workload")
            if type(rpm) is not int or rpm < 0 or type(pwm) is not int or not 0 <= pwm <= 255:
                raise AcceptanceError("Actual fan telemetry missing/invalid")
            if data["mode"] not in ("auto", "max", "manual"):
                raise AcceptanceError("Firmware mode unavailable")
        if lease:
            owner = status["lease"]
            if (not owner["active"] or not owner["owned_by_client"] or owner["owner_pid"] != os.getpid()
                    or not 0 < owner["remaining_seconds"] <= 5.01):
                raise AcceptanceError("Control lease is no longer owned/live")

    @staticmethod
    def _confirmed(status, targets):
        if not all(status["backend"][key] for key in ("connected", "control_available", "control_enabled")):
            raise AcceptanceError("Controller unavailable for firmware confirmation")
        if not 0 <= status["sample_age_seconds"] <= MAX_SAMPLE_AGE:
            raise AcceptanceError("Firmware confirmation is stale")
        if status.get("error") is not None or status["recovery"].get("error") is not None or status["recovery"]["state"] == "fault":
            raise AcceptanceError("Firmware/recovery fault; Auto is not fully confirmed")
        for channel, target in targets.items():
            data = status[channel]
            if (data["mode"] != target["mode"] or data["requested_mode"] != target["mode"]
                    or data["confirmation"] != "firmware-confirmed" or type(data["pwm"]) is not int
                    or not 0 <= data["pwm"] <= 255):
                raise AcceptanceError("Firmware mode/setpoint did not match both requested channels")
            if target["mode"] == "manual" and (data["pwm"] != expected_pwm(target["percent"])
                                                or data["percent"] != target["percent"]
                                                or data["requested_percent"] != target["percent"]):
                raise AcceptanceError("Manual firmware percentage did not match")

    def _heartbeat(self, lane):
        due = self.clock()
        while not self.hb_stop.wait(max(0.0, due - self.clock())):
            try:
                status = self._rpc("heartbeat", "lease_heartbeat", lane=lane)
                if self.hb_stop.is_set():
                    return
                self._safe(status, lease=True)
            except Exception:
                if not self.hb_stop.is_set():
                    with self.lock:
                        self.failure = "Heartbeat/telemetry failure; further test writes stopped"
                    self.halt.set()
                self.hb_ready.set()
                return
            self.hb_ready.set()
            due = max(due + 1.0, self.clock())

    def _start_heartbeat(self):
        lane = self.client.clone()
        self.thread = threading.Thread(target=self._heartbeat, args=(lane,), daemon=True)
        self.thread.start()
        if not self.hb_ready.wait(RPC_SECONDS + 0.5):
            raise AcceptanceError("First lease heartbeat did not complete")
        self._checkpoint()

    def _stop_heartbeat(self):
        self.hb_stop.set()
        if self.thread is not None:
            self.thread.join(timeout=RPC_SECONDS + 0.5)
            if self.thread.is_alive():
                raise AcceptanceError("Lease heartbeat thread did not quiesce")

    def _auto(self, stage):
        status = self._rpc("auto", stage)
        self._safe(status, lease=True)
        self._confirmed(status, AUTO)
        return status

    def _phase(self, name, targets, observed_channels):
        baseline = self._rpc("status", name + "_baseline")
        self._safe(baseline, cool=name == "both_max", lease=True)
        self._confirmed(baseline, AUTO)
        started = self.clock()
        deadline = started + PHASE_SECONDS
        before = {channel: baseline[channel]["rpm"] for channel in ("cpu", "gpu")}
        observed = {channel: False for channel in observed_channels}
        self._emit("phase_start", name, requested=targets, baseline_rpm=before,
                   baseline_monotonic=started, nonauto_budget_seconds=PHASE_SECONDS)
        status = self._rpc("max" if name == "both_max" else "set", name, deadline=deadline,
                           targets=None if name == "both_max" else targets)
        while True:
            self._checkpoint()
            self._safe(status, lease=True)
            self._confirmed(status, targets)
            for channel in observed:
                observed[channel] |= abs(status[channel]["rpm"] - before[channel]) >= 100
            if all(observed.values()):
                break
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise AcceptanceError("No independent actual RPM response within the eight-second budget")
            self.wait(min(POLL_SECONDS, remaining))
            status = self._rpc("status", name + "_observe", deadline=deadline)
        summary = {"stage": name, "requested": targets, "baseline_rpm": before,
                   "baseline_monotonic": started, "observed_rpm": {c: status[c]["rpm"] for c in before},
                   "rpm_change_observed": observed, "elapsed_seconds": self.clock() - started}
        self.stages.append(summary)
        self._emit("phase_observed", name, result=summary)

    def _cleanup(self):
        result = {"auto_attempted": False, "both_auto_confirmed": False,
                  "release_attempted": False, "release_confirmed": False,
                  "ownership_unknown": self.acquire_attempted and self.token is None, "errors": []}
        if self.token is not None:
            result["auto_attempted"] = True
            try:
                status = self._rpc("auto", "finally_auto", cleanup=True)
                self._confirmed(status, AUTO)
                result["both_auto_confirmed"] = True
            except BaseException:
                result["errors"].append("Final Auto request/readback failed or unconfirmed")
            try:
                self._stop_heartbeat()
            except BaseException:
                result["errors"].append("Heartbeat shutdown unconfirmed")
            result["release_attempted"] = True
            try:
                status = self._rpc("release", "finally_release", cleanup=True)
                self._confirmed(status, AUTO)
                if (status["lease"]["active"] or status["lease"]["owned_by_client"]
                        or status["lease"]["owner_pid"] is not None or status["lease"]["remaining_seconds"] != 0):
                    raise AcceptanceError("Release did not clear ownership")
                result["release_confirmed"] = True
            except BaseException:
                result["errors"].append("Release/both-Auto/inactive-lease confirmation failed")
            self.token = None
        else:
            self.hb_stop.set()
            if self.acquire_attempted:
                result["errors"].append("Acquisition outcome unknown; no token available for reset/release")
        return result

    def run(self, *, plan_only=False):
        if plan_only and not self.offline:
            self.evidence_kind = "plan-no-execution"
        raw = self.identity_reader()
        identity = {"kernel_release": raw.get("kernel_release"),
                    "dmi": {key: raw.get("dmi", {}).get(key) for key in IDENTITY}}
        report = {"schema": 1, "source_version": __version__, "identity": identity,
                  "started_utc": datetime.now(timezone.utc).isoformat(), "started_monotonic": self.clock(),
                  "evidence_kind": self.evidence_kind,
                  "execution_requested": self.execute, "outcome": "plan" if plan_only else "read-only",
                  "full_acceptance": False, "hardware_verified_record_written": False,
                  "requires_operator_review": True, "pending_real_tests": list(PENDING_TESTS),
                  "limits": ["RPM changes are observations, not calibration or causality proof",
                             "Eight seconds bounds new non-Auto command/observation work, not wedged firmware recovery",
                             "Kernel release/DMI do not attest same-release module bytes"],
                  "plan": ["verified Auto baseline", "both Max, observe each actual RPM", "verified Auto",
                           "CPU Manual 80 / GPU Auto, observe CPU", "verified Auto",
                           "GPU Manual 80 / CPU Auto, observe GPU", "verified Auto + release"],
                  "stages": self.stages, "errors": []}
        self._emit("start", "preflight", identity=identity, execution_requested=self.execute)
        if plan_only:
            report.update(finished_utc=datetime.now(timezone.utc).isoformat(), finished_monotonic=self.clock())
            return report
        try:
            status = self._rpc("status", "preflight")
            report["initial_status"] = self._snapshot(status)
            if not self.execute:
                return report
            if not self.offline and (self.sink is None or os.geteuid() == 0):
                raise AcceptanceError("Execution requires a normal user and operator evidence sink")
            if identity["kernel_release"] != TEST_KERNEL or identity["dmi"] != IDENTITY:
                raise AcceptanceError("Test-kernel/DMI gate failed; finish the read-only boot gate first")
            self._safe(status, cool=True)
            if not status["backend"]["hardware_verified"] and (
                    not self.validation_mode or status["backend"].get("reason") != VALIDATION_REASON):
                raise AcceptanceError("Unverified hardware requires explicit validation mode in runner and backend")
            if status["lease"]["active"]:
                raise AcceptanceError("An existing control lease must be released by its owner")
            status = self._rpc("acquire", "acquire")
            self._safe(status, cool=True, lease=True)
            self._start_heartbeat()
            self._auto("verified_auto_baseline")
            for index, (name, targets, observed) in enumerate(PHASES):
                self._phase(name, targets, observed)
                if index < len(PHASES) - 1:
                    self._auto(name + "_auto")
            self._checkpoint()
            report["outcome"] = "steady-state-sequence-passed"
        except (KeyboardInterrupt, SystemExit):
            report["errors"].append("Interrupted; hardware outcome requires review")
            report["outcome"] = "failed"
        except Exception as exc:
            report["errors"].append(str(exc) if isinstance(exc, AcceptanceError) else
                                    "Unexpected runner/evidence failure; outcome unconfirmed")
            report["outcome"] = "failed"
        finally:
            if self.execute:
                report["cleanup"] = self._cleanup()
                if report["cleanup"]["errors"]:
                    report["outcome"] = "failed"
            if self.failure:
                report["errors"].append(self.failure)
                report["outcome"] = "failed"
            report["last_status"] = self.last
            report.update(finished_utc=datetime.now(timezone.utc).isoformat(), finished_monotonic=self.clock())
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="Print the plan without contacting the controller")
    modes.add_argument("--execute-approved", action="store_true", help="Execute only after the separate test-boot/read-only gate")
    parser.add_argument("--validation-mode", action="store_true", help="Acknowledge unverified hardware; backend must also be in validation mode")
    parser.add_argument("--output-dir", type=Path, help="New operator-owned directory for events.jsonl and result.json; parent must exist")
    args = parser.parse_args(argv)
    if args.execute_approved and (os.geteuid() == 0 or args.output_dir is None):
        parser.error("Execution requires a normal user and an explicit --output-dir")
    evidence = None
    previous_term = signal.getsignal(signal.SIGTERM)

    def interrupted(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if args.output_dir is not None:
            evidence = Evidence(args.output_dir)
        runner = AcceptanceRunner(execute_approved=args.execute_approved, validation_mode=args.validation_mode,
                                  emit=evidence.event if evidence is not None else None)
        report = runner.run(plan_only=args.plan)
        if evidence is not None:
            evidence.finish(report)
    except Exception:
        report = {"outcome": "failed", "error": "Evidence directory or runner unavailable; no result may be treated as acceptance"}
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if evidence is not None:
            evidence.close()
    print(json.dumps(report, allow_nan=False, indent=2))
    return 1 if report["outcome"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
