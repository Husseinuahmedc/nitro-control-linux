#!/usr/bin/env python3
"""Small real archive fixtures and explicit validator-repair policy tests."""

from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("archive_pipeline", Path(__file__).with_name("kernel-pipeline.py"))
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)
PREFIX = f"usr/lib/modules/{pipeline.KREL}"


def write_archive(path, members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, contents in members.items():
            item = tarfile.TarInfo(name)
            if isinstance(contents, tuple):
                item.type = tarfile.SYMTYPE
                item.linkname = contents[0]
                archive.addfile(item)
            else:
                item.size = len(contents)
                archive.addfile(item, io.BytesIO(contents))
    subprocess.run(["/usr/bin/zstd", "-q", "-T2", "-f", "-o", str(path)], input=buffer.getvalue(), check=True)


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory(dir="/tmp/opencode") as temporary:
        root = Path(temporary)
        tree = root / "tree"
        files = {".config": b"CONFIG_DEBUG_INFO_DWARF5=y\nCONFIG_DEBUG_INFO_BTF=y\nCONFIG_RUST=y\n",
                 "version": (pipeline.KREL + "\n").encode(),
                 "include/config/kernel.release": (pipeline.KREL + "\n").encode(),
                 "tools/bpf/bpftool/vmlinux.h": b"struct fixture_vmlinux { int member; };\n",
                 "arch/x86/boot/bzImage": b"fixture image\n",
                 "modules.order": b"drivers/platform/x86/acer-wmi.o\n"}
        for relative, contents in files.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        (root / "packages").mkdir()
        (root / "build/kernel-pipeline/fixture").mkdir(parents=True)

        def pkginfo(package):
            return (f"pkgname = {package}\npkgbase = linux-nitro-test\npkgver = {pipeline.PKGVER}\narch = x86_64\n").encode()

        kernel = {".PKGINFO": pkginfo("linux-nitro-test"), ".BUILDINFO": b"format = 2\n",
                  PREFIX + "/pkgbase": b"linux-nitro-test\n", PREFIX + "/vmlinuz": files["arch/x86/boot/bzImage"],
                  PREFIX + "/kernel/drivers/platform/x86/acer-wmi.ko.zst": b"fixture module"}
        headers = {".PKGINFO": pkginfo("linux-nitro-test-headers"), ".BUILDINFO": b"format = 2\n",
                   PREFIX + "/build/.config": files[".config"],
                   PREFIX + "/build/version": files["version"],
                   PREFIX + "/build/include/config/kernel.release": files["include/config/kernel.release"],
                   PREFIX + "/build/vmlinux.h": files["tools/bpf/bpftool/vmlinux.h"],
                   PREFIX + "/build/Module.symvers": b"fixture symbols\n",
                   PREFIX + "/build/vmlinux": b"fixture ELF\n",
                   PREFIX + "/build/rust/libkernel.rmeta": b"fixture Rust metadata\n",
                   "usr/src/linux-nitro-test": (f"../lib/modules/{pipeline.KREL}/build",)}
        kernel_archive = root / "packages" / f"linux-nitro-test-{pipeline.PKGVER}-x86_64.pkg.tar.zst"
        header_archive = root / "packages" / f"linux-nitro-test-headers-{pipeline.PKGVER}-x86_64.pkg.tar.zst"
        write_archive(kernel_archive, kernel)
        write_archive(header_archive, headers)

        def inspect(command, **kwargs):
            if command[0] == "/usr/bin/readelf":
                return " [1] .BTF PROGBITS\n [2] .debug_info PROGBITS\n"
            if command[0] == "/usr/bin/modinfo":
                return pipeline.KREL + " SMP preempt mod_unload\n"
            raise AssertionError(f"unexpected external inspection: {command}")

        with patch.multiple(pipeline, ROOT=root, TREE=tree,
                            CONFIG_SHA=hashlib.sha256(files[".config"]).hexdigest()), \
                patch.object(pipeline, "environment", return_value={"PATH": "/usr/bin:/bin"}), \
                patch.object(pipeline.subprocess, "check_output", side_effect=inspect):
            yield root, tree, headers, header_archive


class ArchiveValidationTests(unittest.TestCase):
    def test_official_flattened_header_is_accepted_and_hash_recorded(self):
        with fixture() as (root, tree, headers, archive):
            result = pipeline.validate_archives({"run_id": "fixture"})
            report = json.loads(Path(result["archive-validation"]).read_text())
            actual = report["packages"]["linux-nitro-test-headers"]["selected_file_sha256"]
            self.assertEqual(actual[PREFIX + "/build/vmlinux.h"], pipeline.sha256(tree / "tools/bpf/bpftool/vmlinux.h"))
            self.assertEqual(actual[PREFIX + "/build/.config"], pipeline.CONFIG_SHA)
            self.assertNotIn(PREFIX + "/build/tools/bpf/bpftool/vmlinux.h", headers)

    def test_missing_or_changed_flattened_header_has_specific_error(self):
        for operation in ("missing", "changed", "nested-only"):
            with self.subTest(operation=operation), fixture() as (root, tree, headers, archive):
                path = PREFIX + "/build/vmlinux.h"
                if operation == "changed":
                    headers[path] = b"different header\n"
                else:
                    original = headers.pop(path)
                    if operation == "nested-only":
                        headers[PREFIX + "/build/tools/bpf/bpftool/vmlinux.h"] = original
                write_archive(archive, headers)
                with self.assertRaises(RuntimeError) as caught:
                    pipeline.validate_archives({"run_id": "fixture"})
                message = str(caught.exception)
                self.assertIn(path, message)
                self.assertIn("hash_mismatches" if operation == "changed" else "missing", message)
                if operation == "changed":
                    self.assertIn("expected", message)
                    self.assertIn("actual", message)

    def test_other_headers_config_release_rust_and_paths_remain_required(self):
        for operation in ("symvers", "vmlinux", "config", "release", "rust", "unsafe"):
            with self.subTest(operation=operation), fixture() as (root, tree, headers, archive):
                if operation == "symvers":
                    headers.pop(PREFIX + "/build/Module.symvers")
                elif operation == "vmlinux":
                    headers.pop(PREFIX + "/build/vmlinux")
                elif operation == "config":
                    headers[PREFIX + "/build/.config"] += b"changed\n"
                elif operation == "release":
                    headers[PREFIX + "/build/version"] = b"stock-release\n"
                elif operation == "rust":
                    headers.pop(PREFIX + "/build/rust/libkernel.rmeta")
                elif operation == "unsafe":
                    headers["etc/modprobe.d/forbidden.conf"] = b"forbidden\n"
                write_archive(archive, headers)
                with self.assertRaises(RuntimeError):
                    pipeline.validate_archives({"run_id": "fixture"})

    def test_btf_and_vermagic_are_still_checked(self):
        for operation in ("btf", "vermagic"):
            with self.subTest(operation=operation), fixture():
                def inspect(command, **kwargs):
                    if command[0] == "/usr/bin/readelf":
                        return " .debug_info " if operation == "btf" else " .BTF  .debug_info "
                    return "stock-kernel SMP\n"
                with patch.object(pipeline.subprocess, "check_output", side_effect=inspect), self.assertRaises(RuntimeError):
                    pipeline.validate_archives({"run_id": "fixture"})

    def test_explicit_repair_preserves_failed_state_and_packages(self):
        for operation in ("success", "timeout", "low_disk", "low_memory", "changed-package", "unknown-script", "exhausted"):
            with self.subTest(operation=operation), fixture() as (root, tree, headers, archive):
                scripts = root / "scripts"
                scripts.mkdir()
                script = scripts / "kernel-pipeline.py"
                script.write_text("corrected fixture validator\n")
                logs = root / "logs"
                logs.mkdir()
                console = logs / "pipeline.log"
                console.write_text("STOP validate-archives\n")
                stages = [{"name": name, "kind": kind, "targets": targets,
                           "status": "succeeded" if index < 25 else "failed" if index == 25 else "pending"}
                          for index, (name, kind, targets) in enumerate(pipeline.STAGES)]
                results = root / "build/kernel-pipeline/fixture/results"
                results.mkdir()
                for completed in stages[:25]:
                    completed.update(guard_result={"exit_reason": "child_exit", "exit_code": 0, "child_exit_code": 0},
                                     result={"artifact_paths": {}})
                    (results / (completed["name"] + ".json")).write_text(json.dumps(completed["result"]))
                summary = {"event": "summary", "exit_reason": "child_exit", "exit_code": 1, "child_exit_code": 1,
                           "received_signal": None, "duration_seconds": 2.501668,
                           "minimum_free_bytes": 20 * 1024**3, "minimum_mem_available_bytes": 4 * 1024**3}
                if operation in ("timeout", "low_disk", "low_memory"):
                    summary["exit_reason"] = operation
                if operation == "exhausted":
                    summary["duration_seconds"] = 3600
                paths = {key: str(logs / key) for key in ("resources", "summary", "output")}
                for key in ("resources", "summary"):
                    Path(paths[key]).write_text(json.dumps(summary) + "\n")
                Path(paths["output"]).write_text("kernel-pipeline: incomplete headers archive or changed configuration\n")
                stages[25].update(logs=paths, guard_result=summary, guard_pid=101, child_pid=102,
                                  started_at_utc="2026-10-19T17:52:08.032011+00:00")
                archive_pins = {package: pipeline.sha256(pipeline.archive_path(package))
                                for package in ("linux-nitro-test", "linux-nitro-test-headers")}
                if operation == "changed-package":
                    archive_pins["linux-nitro-test-headers"] = "wrong-hash"
                state = {"status": "stopped", "stage": "validate-archives", "limits_per_stage": pipeline.LIMITS,
                         "stages": stages, "pipeline_pid": 100, "boot": {"boot_id": "current"}, "run_id": "fixture",
                         "last_result": {"stage": "validate-archives", "returncode": 1, "guard": summary},
                         "script_pins": {"scripts/kernel-pipeline.py": pipeline.VALIDATION_REPAIR_SCRIPT_SHA},
                         "logs": {"pipeline": str(console)}, "recovery_history": [{"original": "retained"}]}
                if operation == "unknown-script":
                    state["script_pins"]["scripts/kernel-pipeline.py"] = "unknown"
                plan = root / "plan"
                plan.mkdir()
                state_path = plan / "kernel-pipeline-state.json"
                pipeline.atomic_json(state_path, state)
                before = state_path.read_bytes()
                old_stages = copy.deepcopy(stages[:25])
                package_before = {package: (pipeline.sha256(pipeline.archive_path(package)), pipeline.archive_path(package).stat())
                                  for package in archive_pins}

                def verify(candidate, **kwargs):
                    for path, expected in candidate.get("archive_pins", {}).items():
                        if pipeline.sha256(root / path) != expected:
                            raise RuntimeError("completed package changed")
                    return {"checkpoint_stat_unchanged": True}

                with patch.multiple(pipeline, SCRIPT=script, STATE=state_path, VALIDATION_REPAIR_ARCHIVE_SHAS=archive_pins), \
                        patch.object(pipeline, "verify_pins", side_effect=verify), \
                        patch.object(pipeline.recovery, "boot_identity", return_value={"boot_id": "current"}), \
                        patch.object(pipeline.recovery, "process_identity", return_value=None):
                    if operation == "success":
                        candidate = pipeline.repair_validation(state, logs / "new-pipeline.log")
                        self.assertEqual(candidate["stages"][:25], old_stages)
                        self.assertEqual(candidate["stages"][25]["validation_remaining_timeout_seconds"], 3597)
                        self.assertEqual(candidate["stages"][25]["attempts"][0]["status"], "failed")
                        self.assertEqual(candidate["last_result"], state["last_result"])
                        self.assertEqual(candidate["recovery_history"], state["recovery_history"])
                        audit = candidate["validation_repair_history"][0]
                        snapshot = Path(audit["record"]).parent / "original/plan/kernel-pipeline-state.json"
                        self.assertEqual(snapshot.read_bytes(), before)
                    else:
                        with self.assertRaises(RuntimeError):
                            pipeline.repair_validation(state, logs / "new-pipeline.log")
                        self.assertEqual(state_path.read_bytes(), before)
                for package, (digest, old_stat) in package_before.items():
                    self.assertEqual(pipeline.sha256(pipeline.archive_path(package)), digest)
                    self.assertEqual(pipeline.archive_path(package).stat().st_mtime_ns, old_stat.st_mtime_ns)


if __name__ == "__main__":
    unittest.main()
