#!/usr/bin/env python3
"""Offline reboot-policy and damaged-output tests; no kernel jobs are run."""

import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import os


sys.dont_write_bytecode = True
import kernel_reboot_recovery as recovery

spec = importlib.util.spec_from_file_location("pipeline_recovery_tests", Path(__file__).with_name("kernel-pipeline.py"))
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def minimal_elf():
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    return ident + struct.pack("<HHIQQQIHHHHHH", 1, 62, 1, 0, 0, 64, 0, 64, 0, 0, 64, 1, 0) + bytes(64)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stage = {"started_at_utc": stamp(1000.3)}
        self.state = {"updated_at_utc": stamp(1000.8)}
        self.boot = {"boot_id": "new", "btime_epoch": 1370}
        self.boots = [{"index": -1, "boot_id": "old", "first_entry": 100 * 1_000_000,
                       "last_entry": 1024 * 1_000_000},
                      {"index": 0, "boot_id": "new", "first_entry": 1429 * 1_000_000,
                       "last_entry": 20_000 * 1_000_000}]

    def test_budget_includes_downtime_and_rounds_conservatively(self):
        value = recovery.reboot_budget(self.state, self.stage, self.boot, self.boots)
        self.assertEqual(value["elapsed_upper_bound_seconds"], 371)
        self.assertEqual(value["remaining_timeout_seconds"], 3229)
        self.assertLessEqual(value["remaining_timeout_seconds"], 3600 - (1370 - 1000.3))
        self.assertTrue(value["legacy_boot_association"])

    def test_original_start_is_not_reset_on_repeated_recovery(self):
        self.stage.update(original_started_at_utc=stamp(1000.3), started_at_utc=stamp(1800),
                          boot={"boot_id": "old"})
        self.state["updated_at_utc"] = stamp(1800.5)
        self.boots[0]["last_entry"] = 1810 * 1_000_000
        self.boot["btime_epoch"] = 2100
        self.boots[1]["first_entry"] = 2150 * 1_000_000
        result = recovery.reboot_budget(self.state, self.stage, self.boot, self.boots)
        self.assertEqual(result["remaining_timeout_seconds"], 2499)
        self.assertFalse(result["legacy_boot_association"])

    def test_no_new_hour_after_exhaustion_or_bad_evidence(self):
        for change in ("exhausted", "same-boot", "uncovered-start", "wrong-boot", "unknown-timezone", "recorded-boot"):
            with self.subTest(change=change):
                state, stage, boot, boots = copy.deepcopy((self.state, self.stage, self.boot, self.boots))
                if change == "exhausted":
                    boot["btime_epoch"] = 5000
                    boots[1]["first_entry"] = 5001 * 1_000_000
                elif change == "same-boot":
                    boots[0]["boot_id"] = "new"
                elif change == "uncovered-start":
                    boots[0]["last_entry"] = 999 * 1_000_000
                elif change == "wrong-boot":
                    boot["boot_id"] = "other"
                elif change == "unknown-timezone":
                    stage["started_at_utc"] = "1970-01-01T00:16:40"
                elif change == "recorded-boot":
                    stage["boot"] = {"boot_id": "wrong"}
                with self.assertRaises(RuntimeError):
                    recovery.reboot_budget(state, stage, boot, boots)

    def test_real_retained_usb_budget(self):
        state = {"updated_at_utc": "2026-10-18T22:26:32.817509+00:00"}
        stage = {"started_at_utc": "2026-10-18T22:26:32.305153+00:00"}
        boot = {"boot_id": "d611e48b21fa4647a2887186691ff570", "btime_epoch": 1792362762}
        boots = [{"index": -1, "boot_id": "458805de90724f609eb57343b849b9fa",
                  "first_entry": 1792339533670227, "last_entry": 1792362416739129},
                 {"index": 0, "boot_id": boot["boot_id"], "first_entry": 1792362821001752,
                  "last_entry": 1792379700755012}]
        self.assertEqual(recovery.reboot_budget(state, stage, boot, boots)["remaining_timeout_seconds"], 3229)

    def test_empty_logs_remain_unknown_not_a_fabricated_success(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
            logs = {}
            for key in ("resources", "summary", "output"):
                path = Path(temporary) / key
                path.touch()
                logs[key] = str(path)
            stage = {"logs": logs}
            original = copy.deepcopy(stage)
            recovery.check_interrupted_attempt(stage)
            self.assertEqual(stage, original)
            self.assertTrue(all(Path(value).stat().st_size == 0 for value in logs.values()))

    def test_terminal_or_resource_evidence_cannot_be_retried(self):
        cases = [{"event": "summary", "exit_reason": reason, "exit_code": code}
                 for reason, code in (("low_disk", 3), ("low_memory", 3), ("timeout", 124),
                                      ("child_exit", 2), ("child_exit", 0))]
        cases += [{"event": "sample", "free_bytes": 11 * 1024**3, "mem_available_bytes": 8 * 1024**3},
                  {"event": "sample", "free_bytes": 20 * 1024**3, "mem_available_bytes": 1024**3},
                  {"event": "start", "min_free_gib": 8, "min_mem_gib": 2}]
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
            logs = {key: str(Path(temporary) / key) for key in ("resources", "summary", "output")}
            for case in cases:
                with self.subTest(case=case):
                    for value in logs.values():
                        Path(value).write_text("")
                    Path(logs["resources"]).write_text(json.dumps(case) + "\n")
                    with self.assertRaises(RuntimeError):
                        recovery.check_interrupted_attempt({"logs": logs})
            Path(logs["resources"]).write_text('{"partial":')
            with self.assertRaises(ValueError):
                recovery.check_interrupted_attempt({"logs": logs})

    def test_changed_inode_or_timestamp_is_not_device_only(self):
        original = {"st_dev": 53, "st_ino": 10, "st_size": 100, "st_mtime_ns": 20, "st_ctime_ns": 30}
        changed = {**original, "st_dev": 99}
        self.assertFalse(any(changed[key] != value for key, value in original.items() if key != "st_dev"))
        for key in ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
            with self.subTest(key=key):
                changed = {**original, key: original[key] + 1}
                self.assertTrue(any(changed[field] != value for field, value in original.items() if field != "st_dev"))

    def test_structural_elf_and_archive_checks(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
            path = Path(temporary) / "test.o"
            path.write_bytes(minimal_elf())
            self.assertIsNone(recovery.elf_problem(path))
            for contents in (b"", bytes(128), minimal_elf()[:63], minimal_elf()[:-1]):
                path.write_bytes(contents)
                self.assertIsNotNone(recovery.elf_problem(path))
            for contents in (b"!<arch>\n", b"!<thin>\n"):
                path.write_bytes(contents)
                self.assertIsNone(recovery.archive_problem(path))
            for contents in (b"", bytes(64), b"!<arch>\n" + bytes(59)):
                path.write_bytes(contents)
                self.assertIsNotNone(recovery.archive_problem(path))

    def test_quarantine_preserves_invalid_outputs_and_good_object_metadata(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
            root = Path(temporary)
            tree = root / "tree"
            tree.mkdir()
            good = tree / "good.o"
            good.write_bytes(minimal_elf())
            before = good.stat()
            bad = tree / "bad.o"
            bad.touch()
            truncated = tree / "bad.a"
            truncated.write_bytes(b"!<arch>\ntruncated")
            external = root / "do-not-touch.o"
            external.write_bytes(b"personal data")
            (tree / "external.o").symlink_to(external)
            state = {"log_namespace": "reboot-test"}
            p = SimpleNamespace(TREE=tree, now=pipeline.now, sha256=pipeline.sha256,
                                atomic_json=pipeline.atomic_json, workdir=lambda state: root / "work")
            report_path = recovery.scan_generated(p, state, {})
            report = json.loads(Path(report_path).read_text())
            self.assertEqual(report["status"], "completed")
            self.assertEqual(len(report["invalid"]), 2)
            self.assertFalse(bad.exists())
            self.assertFalse(truncated.exists())
            for item in report["invalid"]:
                self.assertEqual(item["status"], "preserved_in_quarantine")
                self.assertEqual(pipeline.sha256(item["quarantine_path"]), item["sha256"])
            self.assertEqual(external.read_bytes(), b"personal data")
            self.assertEqual(good.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(good.stat().st_ino, before.st_ino)

    def test_pinned_checkpoint_cannot_be_quarantined(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
            root = Path(temporary)
            tree = root / "tree"
            tree.mkdir()
            checkpoint = tree / "vmlinux.o"
            checkpoint.touch()
            p = SimpleNamespace(TREE=tree, now=pipeline.now, sha256=pipeline.sha256,
                                atomic_json=pipeline.atomic_json, workdir=lambda state: root / "work")
            with self.assertRaisesRegex(RuntimeError, "pinned checkpoint"):
                recovery.scan_generated(p, {"log_namespace": "test"}, {})
            self.assertTrue(checkpoint.exists())

    def test_existing_process_is_a_blocker_not_a_signal_target(self):
        p = SimpleNamespace()
        with patch.object(recovery, "process_identity", return_value={"pid": 123}), \
                self.assertRaisesRegex(RuntimeError, "ownership is uncertain"):
            recovery.assert_quiescent(p, {"pipeline_pid": 123})

    def test_protected_unrelated_daemon_is_not_an_uninspected_build_candidate(self):
        p = SimpleNamespace(TREE=Path("/workspace/tree"), SCRIPT=Path("/workspace/scripts/kernel-pipeline.py"),
                            workdir=lambda state: Path("/workspace/run"))
        for executable, blocked in ((b"/usr/lib/systemd/systemd\0--user\0", False),
                                    (b"/usr/bin/gcc\0-c\0file.c\0", True)):
            with self.subTest(executable=executable):
                entry = MagicMock()
                entry.name = "1160"
                entry.stat.return_value.st_uid = os.getuid()
                command = Mock()
                command.read_bytes.return_value = executable
                cwd = Mock()
                cwd.resolve.side_effect = PermissionError("protected cwd")
                entry.__truediv__.side_effect = lambda name: command if name == "cmdline" else cwd
                with patch.object(Path, "iterdir", return_value=iter([entry])), \
                        patch.object(recovery, "process_identity", return_value=None):
                    if blocked:
                        with self.assertRaises(PermissionError):
                            recovery.assert_quiescent(p, {"pipeline_pid": 100, "guard_pid": 101, "child_pid": 102})
                    else:
                        result = recovery.assert_quiescent(p, {"pipeline_pid": 100, "guard_pid": 101, "child_pid": 102})
                        self.assertEqual(result["build_candidate_working_directories_checked"], 0)
                        cwd.resolve.assert_not_called()

    def test_explicit_recovery_snapshot_history_and_failure_preservation(self):
        for case in ("success", "terminal", "checkpoint-time", "unknown-script"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
                root = Path(temporary)
                scripts = root / "scripts"
                scripts.mkdir()
                script = scripts / "kernel-pipeline.py"
                script.write_text("new recovery-capable pipeline\n")
                helper = scripts / "kernel_reboot_recovery.py"
                helper.write_text("new recovery helper\n")
                guard = scripts / "resource-guard.py"
                guard.write_text("unchanged guard\n")
                tree = root / "build/tree"
                (tree / "drivers/platform/x86").mkdir(parents=True)
                driver = tree / "drivers/platform/x86/acer-wmi.c"
                driver.write_text("prepared driver\n")
                (tree / "localversion.10-pkgrel").write_text("-1\n")
                (tree / "localversion.20-pkgname").write_text("-nitro-test\n")
                (root / "packaging").mkdir()
                patch_file = root / "packaging/0001-acer-wmi-anv15-51.patch"
                patch_file.write_text("approved patch\n")
                logs = root / "logs"
                logs.mkdir()
                console = logs / "old-console.log"
                console.write_text("START scsi\n")
                work = root / "build/run"
                (work / "results").mkdir(parents=True)
                state = {"status": "running", "stage": "usb", "run_id": "old-run",
                         "limits_per_stage": pipeline.LIMITS, "environment": pipeline.environment(),
                         "kernel_release": pipeline.KREL, "config_sha256": pipeline.CONFIG_SHA,
                         "checkpoint_sha256": pipeline.OBJECT_SHA, "recipe_sha256": pipeline.RECIPE_SHA,
                         "pipeline_pid": 100, "guard_pid": 101, "child_pid": 102,
                         "checkpoint_stat": {"st_dev": 53, "st_ino": 10, "st_size": 100,
                                             "st_mtime_ns": 20, "st_ctime_ns": 30},
                         "script_pins": {"scripts/kernel-pipeline.py": recovery.LEGACY_SCRIPT_SHA,
                                         "scripts/resource-guard.py": pipeline.sha256(guard)},
                         "logs": {"pipeline": str(console)}, "updated_at_utc": self.state["updated_at_utc"],
                         "stages": [], "artifact_paths": {}, "last_result": {"stage": "scsi", "returncode": 0}}
                for index, (name, kind, targets) in enumerate(pipeline.STAGES):
                    stage = {"name": name, "kind": kind, "targets": targets,
                             "status": "succeeded" if index < 11 else "running" if index == 11 else "pending"}
                    if index <= 11:
                        stage["logs"] = {}
                        for key in ("resources", "summary", "output"):
                            path = logs / (name + "." + key)
                            path.touch()
                            stage["logs"][key] = str(path)
                        stage["started_at_utc"] = self.stage["started_at_utc"] if index == 11 else stamp(900)
                    if index < 11:
                        stage["guard_result"] = {"exit_reason": "child_exit", "exit_code": 0, "child_exit_code": 0,
                                                 "duration_seconds": 1, "minimum_free_bytes": 20 * 1024**3,
                                                 "minimum_mem_available_bytes": 4 * 1024**3}
                        stage["result"] = {"artifact_paths": {}}
                        (work / "results" / (name + ".json")).write_text(json.dumps(stage["result"]))
                    state["stages"].append(stage)
                current_stat = {**state["checkpoint_stat"], "st_dev": 99}
                if case == "terminal":
                    Path(state["stages"][11]["logs"]["summary"]).write_text(
                        '{"event":"summary","exit_reason":"timeout","exit_code":124}\n')
                if case == "checkpoint-time":
                    current_stat["st_mtime_ns"] += 1
                if case == "unknown-script":
                    state["script_pins"]["scripts/kernel-pipeline.py"] = "unrecognized"
                plan = root / "plan"
                plan.mkdir()
                state_path = plan / "kernel-pipeline-state.json"
                pipeline.atomic_json(state_path, state)
                original_bytes = state_path.read_bytes()
                original = copy.deepcopy(state)
                verify = unittest.mock.Mock(return_value={"config_sha256": pipeline.CONFIG_SHA})
                p = SimpleNamespace(ROOT=root, TREE=tree, SCRIPT=script, STATE=state_path,
                                    STAGES=pipeline.STAGES, LIMITS=pipeline.LIMITS, KREL=pipeline.KREL,
                                    CONFIG_SHA=pipeline.CONFIG_SHA, OBJECT_SHA=pipeline.OBJECT_SHA,
                                    RECIPE_SHA=pipeline.RECIPE_SHA, environment=pipeline.environment,
                                    object_stat=lambda: current_stat, verify_pins=verify,
                                    now=pipeline.now, sha256=pipeline.sha256, atomic_json=pipeline.atomic_json,
                                    workdir=lambda state: work, save=lambda state: pipeline.atomic_json(state_path, state))
                old_tail = [{"_BOOT_ID": "old", "__REALTIME_TIMESTAMP": "1024000000", "__MONOTONIC_TIMESTAMP": "24000000"}]
                new_early = [{"_BOOT_ID": "new", "__REALTIME_TIMESTAMP": "1429000000", "__MONOTONIC_TIMESTAMP": "59000000"}]
                evidence = [(self.boots, ["list-boots"]), (old_tail, ["old-tail"]),
                            (new_early, ["new-early"]), ([], ["diagnostics"])]
                with patch.object(recovery, "assert_quiescent", return_value={"active_kernel_jobs_found": False}), \
                        patch.object(recovery, "journal", side_effect=evidence), \
                        patch.object(recovery, "boot_identity", return_value=self.boot), \
                        patch.object(recovery, "process_identity", return_value={"pid": os.getpid(), "starttime_ticks": 10}), \
                        patch.object(recovery, "__file__", str(helper)), \
                        patch.object(recovery, "DRIVER_SHA", pipeline.sha256(driver)), \
                        patch.object(recovery, "PATCH_SHA", pipeline.sha256(patch_file)):
                    if case == "success":
                        recovered = recovery.recover_reboot(p, state, logs / "new-console.log")
                        self.assertEqual(recovered["stages"][:11], original["stages"][:11])
                        usb = recovered["stages"][11]
                        self.assertEqual(usb["status"], "pending")
                        self.assertEqual(usb["reboot_remaining_timeout_seconds"], 3229)
                        self.assertEqual(usb["attempts"][0]["started_at_utc"], original["stages"][11]["started_at_utc"])
                        self.assertNotIn("guard_result", usb["attempts"][0])
                        self.assertEqual(recovered["last_result"], original["last_result"])
                        self.assertEqual(recovered["checkpoint_stat"]["st_dev"], 99)
                        self.assertEqual(recovered["script_pins"]["scripts/resource-guard.py"], original["script_pins"]["scripts/resource-guard.py"])
                        self.assertEqual(len(recovered["recovery_history"]), 1)
                        report = json.loads(Path(recovered["recovery_history"][0]["record"]).read_text())
                        snapshot = Path(report["snapshot_directory"]) / "original/plan/kernel-pipeline-state.json"
                        self.assertEqual(snapshot.read_bytes(), original_bytes)
                        verify.assert_called_once()
                        self.assertTrue(verify.call_args.kwargs["full_object"])
                        for value in original["stages"][11]["logs"].values():
                            self.assertEqual(Path(value).stat().st_size, 0)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "reboot recovery blocked"):
                            recovery.recover_reboot(p, state, logs / "new-console.log")
                        self.assertEqual(state_path.read_bytes(), original_bytes)
                        report = json.loads(next((plan / "kernel-pipeline-recoveries").glob("*/recovery.json")).read_text())
                        self.assertEqual(report["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
