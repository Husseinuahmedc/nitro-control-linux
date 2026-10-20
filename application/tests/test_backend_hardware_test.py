"""Offline acceptance-runner tests. No socket, sysfs write or real authorization."""

import copy
import io
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from nitro_control.client import ClientError
from nitro_control.hardware import IDENTITY, pwm_to_percent
from nitro_control.hardware_test import (AcceptanceRunner, Evidence, TEST_KERNEL, VALIDATION_REASON, main)
from nitro_control.telemetry import empty_channel

from test_backend import Clock, WorkspaceTest


def identity():
    return {"kernel_release": TEST_KERNEL, "dmi": dict(IDENTITY)}


class State:
    def __init__(self, clock=None):
        self.clock = clock
        self.lock = threading.Lock()
        self.calls = []
        self.active = False
        self.token = "e" * 64
        self.channels = {c: {**empty_channel(), "temperature_c": t, "rpm": 2000, "mode": "auto",
                             "pwm": 153, "percent": 60, "requested_mode": "auto",
                             "confirmation": "firmware-confirmed"}
                         for c, t in (("cpu", 60), ("gpu", 55))}
        self.available = True
        self.reason = VALIDATION_REASON
        self.respond = True
        self.hook = None
        self.slow_max = 0

    def snapshot(self):
        return {"schema": 1, "backend": {"connected": True, "control_available": True,
                                          "control_enabled": self.available, "hardware_verified": False,
                                          "reason": self.reason},
                **copy.deepcopy(self.channels),
                "lease": {"active": self.active, "owned_by_client": self.active,
                          "owner_pid": os.getpid() if self.active else None,
                          "remaining_seconds": 5 if self.active else 0,
                          "token": self.token},  # Deliberately hostile extra field; never evidence.
                "recovery": {"state": "auto", "error": None}, "error": None,
                "sample_age_seconds": 0,
                "events": [{"password": "DO-NOT-LOG-PASSWORD", "token": self.token}]}


class FakeClient:
    peer_uid = 0

    def __init__(self, state):
        self.state = state

    def clone(self):
        return FakeClient(self.state)

    def request(self, op, **params):
        if op == "max" and self.state.slow_max:
            time.sleep(self.state.slow_max)
        with self.state.lock:
            if self.state.clock:
                self.state.clock.advance(0.01)
            self.state.calls.append({"op": op, "params": params, "thread": threading.get_ident(),
                                     "at": time.monotonic(),
                                     "monotonic": self.state.clock() if self.state.clock else time.monotonic(),
                                     "mutation_timeout": self.mutation_timeout})
            if op == "acquire":
                self.state.active = True
            elif op == "release":
                self.state.active = False
            if op in ("auto", "release", "acquire"):
                for data in self.state.channels.values():
                    data.update(mode="auto", requested_mode="auto", requested_percent=None,
                                confirmation="firmware-confirmed", rpm=2000)
            elif op in ("set", "max"):
                settings = ({c: {"mode": "max"} for c in ("cpu", "gpu")} if op == "max" else params)
                for channel in ("cpu", "gpu"):
                    if channel not in settings:
                        continue
                    target = settings[channel]
                    data = self.state.channels[channel]
                    data.update(mode=target["mode"], requested_mode=target["mode"],
                                requested_percent=target.get("percent"), confirmation="firmware-confirmed")
                    if target["mode"] == "manual":
                        data.update(pwm=204, percent=pwm_to_percent(204))
                    if self.state.respond:
                        # Fixture values, not an RPM-from-percent model. The Auto-side fan moves too.
                        data["rpm"] = 2600 if target["mode"] != "auto" else 2300
            response = {"ok": True, "status": self.state.snapshot()}
            if op == "acquire":
                response["token"] = self.state.token
            return self.state.hook(op, response) if self.state.hook else response


class RunnerTests(unittest.TestCase):
    def runner(self, *, execute=True, validation=True, state=None, record=None, real_clock=False):
        clock = None if real_clock else Clock()
        self.state = state if state is not None else State(clock)
        self.rows = []
        return AcceptanceRunner(client=FakeClient(self.state), execute_approved=execute,
                                validation_mode=validation, identity_reader=lambda: record or identity(),
                                clock=time.monotonic if real_clock else clock,
                                wait=None if real_clock else lambda seconds: clock.advance(seconds),
                                emit=self.rows.append)

    def test_default_status_and_plan_never_acquire_or_mutate(self):
        runner = self.runner(execute=False)
        self.assertEqual(runner.run()["outcome"], "read-only")
        self.assertEqual([c["op"] for c in self.state.calls], ["status"])
        runner = self.runner(execute=False)
        report = runner.run(plan_only=True)
        self.assertEqual(report["outcome"], "plan")
        self.assertFalse(self.state.calls)
        self.assertFalse(report["full_acceptance"])

    def test_complete_limited_sequence_and_independent_motor_observations(self):
        runner = self.runner()
        report = runner.run()
        self.assertEqual(report["outcome"], "steady-state-sequence-passed", report)
        self.assertEqual(len(report["stages"]), 3)
        self.assertEqual(report["stages"][0]["rpm_change_observed"], {"cpu": True, "gpu": True})
        self.assertEqual(report["stages"][1]["rpm_change_observed"], {"cpu": True})
        self.assertEqual(report["stages"][2]["rpm_change_observed"], {"gpu": True})
        self.assertEqual(report["stages"][1]["observed_rpm"]["gpu"], 2300)
        self.assertFalse(report["full_acceptance"])
        self.assertFalse(report["hardware_verified_record_written"])
        self.assertEqual(report["evidence_kind"], "offline-simulation")
        self.assertTrue(report["requires_operator_review"])
        self.assertIn("suspend/resume", report["pending_real_tests"])
        mutations = [c["op"] for c in self.state.calls if c["op"] not in ("status", "heartbeat")]
        self.assertEqual(mutations, ["acquire", "auto", "max", "auto", "set", "auto", "set", "auto", "release"])
        self.assertTrue(report["cleanup"]["both_auto_confirmed"])
        self.assertTrue(report["cleanup"]["release_confirmed"])
        text = json.dumps([self.rows, report], allow_nan=False)
        self.assertNotIn(self.state.token, text)
        self.assertNotIn("DO-NOT-LOG-PASSWORD", text)

    def test_kernel_dmi_capability_and_validation_gates_fail_before_acquire(self):
        for case in ("kernel", "dmi", "capability", "validation", "backend-validation"):
            with self.subTest(case=case):
                record = identity()
                if case == "kernel":
                    record["kernel_release"] = "stock-not-test"
                if case == "dmi":
                    record["dmi"]["board_name"] = None
                runner = self.runner(record=record, validation=case != "validation")
                if case == "capability":
                    self.state.available = False
                if case == "backend-validation":
                    self.state.reason = "Not validation mode"
                report = runner.run()
                self.assertEqual(report["outcome"], "failed")
                self.assertEqual([c["op"] for c in self.state.calls], ["status"])

    def test_cool_idle_guards_are_strict_and_missing_stale_data_aborts(self):
        for channel, key, value in (("cpu", "temperature_c", 75), ("gpu", "temperature_c", 70),
                                    ("cpu", "temperature_c", None), ("gpu", "rpm", None),
                                    ("gpu", "temperature_c", float("nan")), ("cpu", "pwm", None)):
            with self.subTest(channel=channel, key=key, value=value):
                runner = self.runner()
                self.state.channels[channel][key] = value
                self.assertEqual(runner.run()["outcome"], "failed")
                self.assertNotIn("acquire", [c["op"] for c in self.state.calls])

    def test_midtest_overheat_or_staleness_aborts_and_attempts_auto_release(self):
        for case in ("cpu", "gpu", "stale"):
            with self.subTest(case=case):
                runner = self.runner()

                def hook(op, response):
                    if op == "max":
                        if case == "stale":
                            response["status"]["sample_age_seconds"] = 2.1
                        else:
                            response["status"][case]["temperature_c"] = 85 if case == "cpu" else 80
                    return response

                self.state.hook = hook
                report = runner.run()
                self.assertEqual(report["outcome"], "failed")
                self.assertTrue(report["cleanup"]["auto_attempted"])
                self.assertTrue(report["cleanup"]["release_attempted"])
                self.assertFalse(any(c["op"] == "set" for c in self.state.calls))

    def test_acquisition_delay_cannot_bypass_cool_idle_gate(self):
        runner = self.runner()

        def hook(op, response):
            if op == "acquire":
                response["status"]["cpu"]["temperature_c"] = 76
            return response

        self.state.hook = hook
        report = runner.run()
        self.assertEqual(report["outcome"], "failed")
        self.assertFalse(any(c["op"] in ("max", "set") for c in self.state.calls))
        self.assertTrue(report["cleanup"]["release_attempted"])

    def test_operator_interrupt_still_attempts_both_auto_and_release(self):
        runner = self.runner()

        def hook(op, response):
            if op == "max":
                raise KeyboardInterrupt
            return response

        self.state.hook = hook
        report = runner.run()
        self.assertEqual(report["outcome"], "failed")
        self.assertTrue(report["cleanup"]["auto_attempted"])
        self.assertTrue(report["cleanup"]["release_attempted"])

    def test_no_rpm_response_or_pending_firmware_is_not_success(self):
        for case in ("no-response", "pending", "bad-manual"):
            with self.subTest(case=case):
                runner = self.runner()
                self.state.respond = case != "no-response"

                def hook(op, response):
                    if case == "pending" and op == "max":
                        response["status"]["cpu"]["confirmation"] = "pending"
                    if case == "bad-manual" and op == "set":
                        response["status"]["cpu"].update(pwm=178, percent=70)
                    return response

                self.state.hook = hook
                report = runner.run()
                self.assertEqual(report["outcome"], "failed")
                self.assertEqual(sum(c["op"] == "max" for c in self.state.calls), 1)
                self.assertTrue(report["cleanup"]["release_attempted"])

    def test_unknown_acquisition_or_mutation_never_retries_or_logs_error_secrets(self):
        for case in ("acquire", "max"):
            with self.subTest(case=case):
                runner = self.runner()

                def hook(op, response):
                    if op == case:
                        raise ClientError("DO-NOT-LOG-PASSWORD " + self.state.token)
                    return response

                self.state.hook = hook
                report = runner.run()
                self.assertEqual(report["outcome"], "failed")
                self.assertEqual(sum(c["op"] == case for c in self.state.calls), 1)
                self.assertEqual(report["cleanup"]["ownership_unknown"], case == "acquire")
                self.assertEqual(report["cleanup"]["release_attempted"], case != "acquire")
                text = json.dumps([self.rows, report])
                self.assertNotIn(self.state.token, text)
                self.assertNotIn("DO-NOT-LOG-PASSWORD", text)

    def test_partial_auto_or_active_release_never_goes_green(self):
        for case in ("auto", "release", "release-expiry"):
            with self.subTest(case=case):
                runner = self.runner()

                def hook(op, response):
                    if op == "auto" and case == "auto":
                        response["status"]["gpu"].update(mode="manual", confirmation="failed")
                    if op == "release" and case == "release":
                        response["status"]["lease"].update(active=True, owned_by_client=True, owner_pid=os.getpid())
                    if op == "release" and case == "release-expiry":
                        response["status"]["lease"]["remaining_seconds"] = 5
                    return response

                self.state.hook = hook
                report = runner.run()
                self.assertEqual(report["outcome"], "failed")
                self.assertTrue(report["cleanup"]["errors"])
                self.assertTrue(report["cleanup"]["release_attempted"])

    def test_eight_second_budget_and_late_success_are_failed_without_another_manual_command(self):
        for case in ("no-response", "late-reply"):
            with self.subTest(case=case):
                runner = self.runner()
                self.state.respond = case != "no-response"

                def hook(op, response):
                    if case == "late-reply" and op == "max":
                        self.state.clock.advance(8.1)
                    return response

                self.state.hook = hook
                report = runner.run()
                self.assertEqual(report["outcome"], "failed")
                max_call = next(c for c in self.state.calls if c["op"] == "max")
                self.assertLessEqual(max_call["mutation_timeout"], 6.5)
                if case == "no-response":
                    after = next(c for c in self.state.calls if c["op"] == "auto" and
                                 c["monotonic"] > max_call["monotonic"])
                    self.assertLessEqual(after["monotonic"] - max_call["monotonic"], 8.02)
                self.assertFalse(any(c["op"] == "set" for c in self.state.calls))
                self.assertTrue(report["cleanup"]["release_attempted"])

    def test_evidence_write_failure_after_acquire_does_not_prevent_cleanup(self):
        runner = self.runner()

        def broken(event):
            if event.get("op") == "acquire":
                raise OSError("private filesystem detail")
            self.rows.append(event)

        runner.sink = broken
        report = runner.run()
        self.assertEqual(report["outcome"], "failed")
        self.assertTrue(report["cleanup"]["auto_attempted"])
        self.assertTrue(report["cleanup"]["release_attempted"])
        self.assertNotIn("private filesystem detail", json.dumps(report))

    def test_arbitrary_server_errors_and_extra_fields_are_not_evidence(self):
        runner = self.runner()

        def hook(op, response):
            if op == "max":
                response["ok"] = False
                response["error"] = "DO-NOT-LOG-PASSWORD " + self.state.token
                response["status"]["error"] = response["error"]
                response["status"]["backend"]["reason"] = response["error"]
            return response

        self.state.hook = hook
        report = runner.run()
        self.assertEqual(report["outcome"], "failed")
        serialized = json.dumps([self.rows, report])
        self.assertNotIn(self.state.token, serialized)
        self.assertNotIn("DO-NOT-LOG-PASSWORD", serialized)

    def test_heartbeat_failure_stops_tests_but_still_attempts_cleanup(self):
        runner = self.runner()

        def hook(op, response):
            if op == "heartbeat":
                raise ClientError(self.state.token + " DO-NOT-LOG-PASSWORD")
            return response

        self.state.hook = hook
        report = runner.run()
        self.assertEqual(report["outcome"], "failed")
        self.assertFalse(any(c["op"] in ("max", "set") for c in self.state.calls))
        self.assertTrue(report["cleanup"]["auto_attempted"])
        self.assertTrue(report["cleanup"]["release_attempted"])

    def test_separate_one_hz_heartbeat_continues_during_slow_command(self):
        runner = self.runner(real_clock=True)
        self.state.slow_max = 1.25
        report = runner.run()
        self.assertEqual(report["outcome"], "steady-state-sequence-passed", report)
        beats = [c for c in self.state.calls if c["op"] == "heartbeat"]
        self.assertGreaterEqual(len(beats), 2)
        self.assertNotEqual(beats[0]["thread"], threading.get_ident())
        self.assertGreaterEqual(beats[1]["at"] - beats[0]["at"], 0.9)
        self.assertLess(beats[1]["at"] - beats[0]["at"], 1.2)


class EvidenceTests(WorkspaceTest):
    def test_jsonl_and_final_json_are_offline_explicit_private_and_no_acceptance_record(self):
        output = self.root / "evidence"
        evidence = Evidence(output)
        try:
            state = State()
            runner = AcceptanceRunner(client=FakeClient(state), execute_approved=True, validation_mode=True,
                                      identity_reader=identity, emit=evidence.event)
            report = runner.run()
            evidence.finish(report)
        finally:
            evidence.close()
        events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
        self.assertTrue(events)
        self.assertTrue(all(row["evidence_kind"] == "offline-simulation" and "utc" in row and "monotonic" in row
                            for row in events))
        result = json.loads((output / "result.json").read_text())
        self.assertEqual(result["identity"], identity())
        self.assertEqual(result["outcome"], "steady-state-sequence-passed")
        self.assertFalse(result["full_acceptance"])
        self.assertFalse((output / "hardware-verified.json").exists())
        self.assertEqual((output / "events.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual((output / "result.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            Evidence(output)

    def test_cli_execution_requires_output_and_help_plan_do_not_connect(self):
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                main(["--execute-approved"])
        with (patch("nitro_control.hardware_test.read_identity", side_effect=identity),
              patch("nitro_control.client.SocketClient.request", side_effect=AssertionError("No live request")),
              patch("sys.stdout", new_callable=io.StringIO) as output):
            self.assertEqual(main(["--plan"]), 0)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "plan")


if __name__ == "__main__":
    unittest.main()
