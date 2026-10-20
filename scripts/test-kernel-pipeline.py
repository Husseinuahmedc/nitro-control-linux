#!/usr/bin/env python3
"""Offline pipeline policy tests; no make, compiler, or hardware operations."""

import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("kernel_pipeline", Path(__file__).with_name("kernel-pipeline.py"))
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class PipelineTests(unittest.TestCase):
    def test_fixed_plan_and_limits(self):
        self.assertEqual(pipeline.LIMITS, {"make_jobs": 2, "min_free_gib": 12,
                                         "min_mem_gib": 2, "timeout_seconds": 3600, "poll_seconds": 0.5})
        names = [name for name, _, _ in pipeline.STAGES]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names[0], "vmlinux-btf-link")
        self.assertLess(names.index("modules-final"), names.index("package-kernel"))
        self.assertLess(names.index("package-headers"), names.index("validate-archives"))
        for _, kind, targets in pipeline.STAGES:
            if kind == "make":
                self.assertFalse({"clean", "mrproper", "olddefconfig", "install", "modules_install"} & set(targets))
                for target in targets:
                    if target.endswith("/"):
                        self.assertTrue(any((pipeline.TREE / target / file).is_file() for file in ("Kbuild", "Makefile")), target)

    def test_sanitized_identity(self):
        with patch.dict("os.environ", {"MAKEFLAGS": "-j32", "KBUILD_OUTPUT": "/unsafe", "PAHOLE_FLAGS": "wrong"}):
            env = pipeline.environment()
        self.assertEqual(env["MAKEFLAGS"], "-j2")
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["KBUILD_BUILD_TIMESTAMP"], "Sat, 03 Oct 2026 11:43:28 +0000")
        self.assertNotIn("KBUILD_OUTPUT", env)
        self.assertNotIn("PAHOLE_FLAGS", env)

    def test_payload_scope(self):
        base = f"usr/lib/modules/{pipeline.KREL}"
        valid = tarfile.TarInfo(base + "/kernel/drivers/test.ko.zst")
        self.assertEqual(pipeline.safe_member(valid, "linux-nitro-test"), valid.name)
        for name in ("/boot/kernel", "../escape", "etc/modprobe.d/test.conf", ".INSTALL",
                     "usr/share/libalpm/hooks/test.hook", "usr/lib/modules/7.2.9-arch1-1/vmlinuz",
                     "usr/src/linux", "usr/src/linux-nitro-test"):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                pipeline.safe_member(tarfile.TarInfo(name), "linux-nitro-test")
        alias = tarfile.TarInfo("usr/src/linux-nitro-test")
        alias.type = tarfile.SYMTYPE
        alias.linkname = f"../lib/modules/{pipeline.KREL}/build"
        pipeline.safe_member(alias, "linux-nitro-test-headers")
        for target in ("/usr/src/linux", "../lib/modules/7.2.9-arch1-1/build", "../../../../etc"):
            alias.linkname = target
            with self.subTest(target=target), self.assertRaises(RuntimeError):
                pipeline.safe_member(alias, "linux-nitro-test-headers")

    def test_no_retry_after_any_guard_stop(self):
        reasons = ("timeout", "low_disk", "low_memory", "monitor_error", "cleanup_error", "signal", "spawn_error")
        for reason in reasons:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "results").mkdir()
                (root / "results/test.json").write_text(json.dumps({"artifact_paths": {}}))
                state = {"guard_pid": 123, "child_pid": 456, "artifact_paths": {}}
                stage = {"name": "test"}
                summary = {"exit_reason": reason, "exit_code": 0, "child_exit_code": 0}
                with patch.object(pipeline, "workdir", return_value=root), patch.object(pipeline, "save"), \
                        patch.object(pipeline, "verify_pins", return_value={}):
                    self.assertFalse(pipeline.accept_result(state, stage, summary, 0))
                self.assertEqual(stage["status"], "failed")
                self.assertEqual(state["status"], "stopped")
                self.assertIn("no automatic retry", state["finish_reason"])

    def test_success_requires_result_and_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "results").mkdir()
            state = {"artifact_paths": {}}
            summary = {"exit_reason": "child_exit", "exit_code": 0, "child_exit_code": 0}
            with patch.object(pipeline, "workdir", return_value=root), patch.object(pipeline, "save"), \
                    patch.object(pipeline, "verify_pins", return_value={}):
                self.assertFalse(pipeline.accept_result(state, {"name": "test"}, summary, 0))
                (root / "results/test.json").write_text(json.dumps({"artifact_paths": {"kernel": "/workspace/kernel"}}))
                self.assertTrue(pipeline.accept_result(state, {"name": "test"}, summary, 0))
                self.assertEqual(state["artifact_paths"], {"kernel": "/workspace/kernel"})
            with patch.object(pipeline, "workdir", return_value=root), patch.object(pipeline, "save"), \
                    patch.object(pipeline, "verify_pins", side_effect=RuntimeError("config changed")):
                self.assertFalse(pipeline.accept_result(state, {"name": "test"}, summary, 0))
                self.assertIn("config changed", state["finish_reason"])

    def test_summary_ignores_partial_record(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "guard.jsonl"
            log.write_text('{"event":"sample"}\n{"event":"summary","exit_code":124}\n{"partial":')
            self.assertEqual(pipeline.guard_summary(log)["exit_code"], 124)


if __name__ == "__main__":
    unittest.main()
