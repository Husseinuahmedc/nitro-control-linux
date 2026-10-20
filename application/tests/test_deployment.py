"""Offline deployment tests. Host-writing subprocesses and host locks are mocked."""

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nitro_control import deployment as d


def manifest_fixture(workspace):
    artifacts = {role: {"path": path, "sha256": "a" * 64, "bytes": 100}
                 for role, path in d.FIXED_INPUTS.items()}
    artifacts["archive_validation"] = {"path": "build/kernel-pipeline/run/archive-validation.json",
                                       "sha256": "b" * 64, "bytes": 100}
    return {"schema_version": 1, "created_at_utc": "2026-10-19T00:00:00+00:00",
            "workspace": str(workspace), "target": dict(d.TARGET), "artifacts": artifacts,
            "authorization": "operator-sha256-local-unsigned-archives",
            "baseline": {"protected": {path: {} for path in d.PROTECTED}, "packages": {"nitro-control": "0.1.0-9"},
                         "running_kernel": d.STOCK, "secure_boot": False, "target_absent": True}}


TABLE = ("loading packages...\nresolving dependencies...\nlooking for conflicting packages...\n\n"
         "Package (2)                 Old Version  New Version    Net Change\n\n"
         "linux-nitro-test                         7.2.9.arch1-1   161.63 MiB\n"
         "linux-nitro-test-headers                 7.2.9.arch1-1   321.36 MiB\n\n"
         "Total Installed Size:  482.99 MiB\n\n:: Proceed with installation? [Y/n] ")
LIVE = "quiet cryptdevice=PARTUUID=example:root root=/dev/mapper/root rootflags=subvol=@ rw rootfstype=btrfs"
CMDLINE = LIVE + " acer_wmi.predator_v4=0\n"
MENU = ("default_entry: 2\n/+Omarchy\n  //linux\n  protocol: efi\n"
        "  path: boot():/EFI/Linux/omarchy_linux.efi\n  cmdline: " + LIVE + "\n\n"
        "  //Snapshots\n  comment: existing snapshots\n/EFI fallback\nprotocol: efi\n")


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nitro-deployment-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # An accidentally unmocked command is a test failure, not a host operation.
        self.run = self.enterContext(patch.object(d.subprocess, "run", side_effect=AssertionError("unmocked host command")))
        self.popen = self.enterContext(patch.object(d.subprocess, "Popen", side_effect=AssertionError("unmocked host process")))

    def save_manifest(self, value=None):
        value = value or manifest_fixture(self.root)
        data = (json.dumps(value) + "\n").encode()
        path = self.root / "manifest.json"
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()


class ManifestTests(OfflineTests):
    def test_manifest_hash_is_checked_before_json_or_any_writes(self):
        path = self.root / "manifest.json"
        path.write_bytes(b"not even json")
        with patch.object(d, "mkdir_root") as create, self.assertRaisesRegex(d.DeploymentError, "SHA-256 mismatch"):
            d.load_manifest(path, "0" * 64, apply=True)
        create.assert_not_called()
        self.run.assert_not_called()

    def test_apply_requires_explicit_pin(self):
        path, _ = self.save_manifest()
        for pin in (None, "", "a", "A" * 64):
            with self.subTest(pin=pin), self.assertRaises(d.DeploymentError):
                d.load_manifest(path, pin, apply=True)

    def test_manifest_accepts_only_exact_target_and_source_roles(self):
        path, digest = self.save_manifest()
        plan, actual, _ = d.load_manifest(path, digest, apply=True)
        self.assertEqual(actual, digest)
        self.assertEqual(plan["target"], d.TARGET)
        for mutate in (
            lambda p: p["artifacts"].update({"extra_package": p["artifacts"]["kernel_package"]}),
            lambda p: p["artifacts"]["uki"].update(path="../../boot/evil.efi"),
            lambda p: p["artifacts"]["uki"].update(path="/boot/evil.efi"),
            lambda p: p["target"].update(kernel_release=d.STOCK),
            lambda p: p["target"].update(kernel_config_sha256="0" * 64),
            lambda p: p["target"].update(module_signing_key="00:BAD:FPR"),
            lambda p: p["target"].update(signing_certificate_sha256="0" * 64),
            lambda p: p["baseline"].update(secure_boot=True),
            lambda p: p["baseline"]["packages"].update({d.PACKAGES[0]: d.VERSION}),
            lambda p: p["baseline"]["protected"].update({"/etc/shadow": {}}),
            lambda p: p.update(destinations={"uki": "/boot/other.efi"}),
        ):
            plan = manifest_fixture(self.root)
            mutate(plan)
            path, digest = self.save_manifest(plan)
            with self.subTest(plan=plan), self.assertRaises(d.DeploymentError):
                d.load_manifest(path, digest, apply=True)

    def test_duplicate_json_keys_fail(self):
        with self.assertRaisesRegex(d.DeploymentError, "duplicate"):
            d.decode_json('{"target": 1, "target": 2}')

    def test_secure_boot_true_unknown_and_missing_abort(self):
        path = self.root / "SecureBoot"
        for value in (b"\x07\0\0\0\x01", b"\x07\0\0\0", b"\0" * 6):
            path.write_bytes(value)
            with patch.object(d, "SECUREBOOT", path), self.assertRaises(d.DeploymentError):
                d.secure_boot_disabled()
        path.write_bytes(b"\x07\0\0\0\0")
        with patch.object(d, "SECUREBOOT", path):
            d.secure_boot_disabled()

    def test_no_source_symlink_or_parent_symlink(self):
        original = self.root / "source"
        original.write_bytes(b"input")
        link = self.root / "link"
        link.symlink_to(original)
        directory = self.root / "alias"
        directory.symlink_to(self.root, target_is_directory=True)
        for path in (link, directory / "source"):
            with self.subTest(path=path), self.assertRaises(OSError):
                with d.input_file(path):
                    self.fail("symlink was followed")

    def test_root_foreign_file_group_write_and_symlink_fail(self):
        safe = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
        for bad in (SimpleNamespace(st_uid=1000, st_mode=stat.S_IFREG | 0o600),
                    SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o664),
                    SimpleNamespace(st_uid=0, st_mode=stat.S_IFLNK | 0o777)):
            def info(path):
                return bad if path == Path("/var/lib/nitro-control/foreign") else safe
            with patch.object(Path, "lstat", autospec=True, side_effect=info), self.assertRaises(d.DeploymentError):
                d.root_path("/var/lib/nitro-control/foreign")

    def test_root_existing_target_never_force_overwritten(self):
        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
        with patch.object(Path, "lstat", return_value=info), self.assertRaisesRegex(d.DeploymentError, "already exists"):
            d.root_path("/var/lib/nitro-control/deployment/existing", absent=True)

    def test_cache_copies_and_rehashes_before_use(self):
        source = self.root / "work"
        source.mkdir()
        (source / "input").write_bytes(b"pinned bytes")
        cache = self.root / "cache"
        cache.mkdir()
        manifest = {"workspace": str(source), "artifacts": {"uki": d.descriptor(source, source / "input")}}
        paths = d.cache_inputs(manifest, b"operator manifest", cache)
        self.assertEqual(paths["uki"].read_bytes(), b"pinned bytes")
        (source / "input").write_bytes(b"other bytes!")
        self.assertEqual(paths["uki"].read_bytes(), b"pinned bytes")
        second = self.root / "second"
        second.mkdir()
        with self.assertRaisesRegex(d.DeploymentError, "hash/size mismatch"):
            d.cache_inputs(manifest, b"manifest", second)
        self.assertTrue((second / "inputs").is_dir())  # failure evidence is retained

    def test_complete_cmdline_and_stock_entry_required(self):
        d.validate_cmdline(CMDLINE, LIVE, MENU)
        for bad in (LIVE + "\n", CMDLINE + "extra\n", CMDLINE.replace("rootflags=subvol=@ ", ""),
                    CMDLINE.replace("predator_v4=0", "predator_v4=1"), CMDLINE.replace("quiet", "quiet debug"),
                    CMDLINE.replace("\n", " acer_wmi.predator_v4=0\n")):
            with self.subTest(cmdline=bad), self.assertRaises(d.DeploymentError):
                d.validate_cmdline(bad, LIVE, MENU)
        with self.assertRaises(d.DeploymentError):
            d.validate_cmdline(CMDLINE, LIVE, MENU.replace("rootflags=subvol=@", "rootflags=subvol=other"))

    def test_boot_entry_preserves_stock_default_and_os_order(self):
        entry = ("  //linux-nitro-test\n  comment: nitro test\n  protocol: efi\n"
                 "  path: boot():/EFI/Linux/omarchy_linux-nitro-test.efi#hash\n  cmdline: " + CMDLINE + "\n")
        after = MENU.replace("  //Snapshots", entry + "  //Snapshots")
        d.check_boot_menu(MENU, after, CMDLINE)
        for bad in (after.replace("default_entry: 2", "default_entry: 3"),
                    after.replace("omarchy_linux.efi", "changed-stock.efi"),
                    after.replace(CMDLINE, LIVE + "\n"), after + entry):
            with self.subTest(menu=bad), self.assertRaises(d.DeploymentError):
                d.check_boot_menu(MENU, bad, CMDLINE)

    def test_pipeline_must_have_completed_all_guarded_stages(self):
        state = {"status": "completed", "kernel_release": d.KREL, "stages": [{"status": "succeeded",
                 "guard_result": {"exit_reason": "child_exit", "exit_code": 0, "child_exit_code": 0}}]}
        d.completed(state)
        for bad in ({**state, "status": "running"}, {**state, "kernel_release": d.STOCK},
                    {**state, "stages": [{"status": "succeeded"}]}, {**state, "stages": []}):
            with self.assertRaises(d.DeploymentError):
                d.completed(bad)


class PackageTests(OfflineTests):
    @contextmanager
    def package(self, extra=(), metadata="", package=d.PACKAGES[0]):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            data = ("pkgname = " + package + "\npkgbase = linux-nitro-test\npkgver = " + d.VERSION
                    + "\narch = x86_64\n" + metadata).encode()
            member = tarfile.TarInfo(".PKGINFO")
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
            for name, kind, data in extra:
                member = tarfile.TarInfo(name)
                member.mode = 0o755 if kind == "directory" else 0o644
                if kind == "symlink":
                    member.type, member.linkname = tarfile.SYMTYPE, data
                elif kind == "directory":
                    member.type = tarfile.DIRTYPE
                else:
                    member.size = len(data)
                archive.addfile(member, io.BytesIO(data) if kind == "file" else None)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer, mode="r|") as archive:
            yield archive

    def test_kernel_archive_target_only_and_no_host_writes(self):
        base = "usr/lib/modules/" + d.KREL
        with patch.object(d, "package_tar", lambda path: self.package([(base + "/pkgbase", "file", b"linux-nitro-test\n")])), \
                patch.object(d, "root_path"), \
                patch.object(Path, "exists", return_value=False), \
                patch.object(Path, "is_symlink", return_value=False):
            files = d.audit_package(Path("unused"), d.PACKAGES[0])
        self.assertIn(base + "/pkgbase", files)
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_archive_extra_package_install_global_payload_and_links_rejected(self):
        base = "usr/lib/modules/" + d.KREL
        for member in ((".INSTALL", "file", b"exit 0"), ("../../etc/evil", "file", b"evil"),
                       ("/etc/evil", "file", b"evil"), ("etc/modprobe.d/evil.conf", "file", b"evil"),
                       ("boot/test.efi", "file", b"evil"), ("usr/lib/modules/" + d.STOCK + "/evil", "file", b"evil"),
                       (base + "/escape", "symlink", "../../../../etc")):
            with patch.object(d, "package_tar", lambda path: self.package([member])), \
                    patch.object(d, "root_path"), self.subTest(member=member), self.assertRaises(d.DeploymentError):
                d.audit_package(Path("unused"), d.PACKAGES[0])
        with self.assertRaisesRegex(d.DeploymentError, "extra package"):
            d.audit_package(Path("unused"), "linux")
        for metadata in ("replaces = linux\n", "conflict = linux\n", "provides = linux\n", "install = evil\n"):
            with patch.object(d, "package_tar", lambda path: self.package(metadata=metadata)), self.assertRaises(d.DeploymentError):
                d.audit_package(Path("unused"), d.PACKAGES[0])

    def test_archive_rejects_write_through_internal_symlink(self):
        base = "usr/lib/modules/" + d.KREL
        members = [(base + "/alias", "symlink", "build"), (base + "/alias/evil", "file", b"evil")]
        with patch.object(d, "package_tar", lambda path: self.package(members)), patch.object(d, "root_path"), \
                self.assertRaisesRegex(d.DeploymentError, "symlink parent"):
            d.audit_package(Path("unused"), d.PACKAGES[0])

    def test_dkms_foreign_kernel_and_symlink_archive_rejected(self):
        for name, kind in (("dkms_main_tree/" + d.STOCK, tarfile.DIRTYPE), ("NVIDIA-COPYING", tarfile.SYMTYPE),
                           ("../escape", tarfile.REGTYPE)):
            path = self.root / "dkms.tar.gz"
            with tarfile.open(path, "w:gz") as archive:
                member = tarfile.TarInfo(name)
                member.type, member.linkname = kind, "/etc/evil"
                archive.addfile(member)
            with self.assertRaises(d.DeploymentError):
                d.audit_dkms_archive(path, {})

    def test_only_expected_alpm_hooks_trigger_and_masks_take_last_precedence(self):
        paths = []
        for name, text in (
            ("10-limine-snapper-lock.hook", "[Trigger]\nType = Package\nOperation = Install\nTarget = *\n"),
            ("35-systemd-update.hook", "[Trigger]\nType = Path\nOperation = Install\nOperation = Upgrade\nOperation = Remove\nTarget = usr/\n"),
            ("60-depmod.hook", "[Trigger]\nType = Path\nOperation = Install\nTarget = usr/lib/modules/*/\nTarget = !usr/lib/modules/*/?*\n"),
            ("70-dkms-upgrade.hook", "[Trigger]\nType = Package\nOperation = Upgrade\nTarget = *\n"),
            ("90-mkinitcpio-install.hook", "[Trigger]\nType = Package\nOperation = Install\nTarget = *\n"),
        ):
            path = self.root / name
            path.write_text(text)
            paths.append(path)
        payload = {"usr/": "5", "usr/lib/": "5", "usr/lib/modules/" + d.KREL + "/": "5", "usr/lib/modules/" + d.KREL + "/build/": "5"}
        with patch.object(Path, "glob", return_value=iter(paths)):
            d.applicable_hooks(payload)
        foreign = self.root / "99-unexpected.hook"
        foreign.write_text("[Trigger]\nType = Package\nOperation = Install\nTarget = linux-nitro-test\n")
        with patch.object(Path, "glob", return_value=iter([*paths, foreign])), self.assertRaisesRegex(d.DeploymentError, "unexpected applicable"):
            d.applicable_hooks(payload)


class EvidenceTests(OfflineTests):
    def evidence(self):
        plan = manifest_fixture(self.root)
        stages = [{"status": "succeeded", "guard_result": {"exit_reason": "child_exit", "exit_code": 0, "child_exit_code": 0}}]
        original = lambda role: str(self.root / plan["artifacts"][role]["path"])
        packages = {name: {"path": original(role), "sha256": "a" * 64, "selected_file_sha256": selected}
                    for name, role, selected in (
                        (d.PACKAGES[0], "kernel_package", {"usr/lib/modules/" + d.KREL + "/vmlinuz": "c" * 64}),
                        (d.PACKAGES[1], "headers_package", {"usr/lib/modules/" + d.KREL + "/build/.config": d.CONFIG_SHA256}))}
        kernel = {"status": "completed", "kernel_release": d.KREL, "stages": stages, "run_id": "run",
                  "config_sha256": d.CONFIG_SHA256, "recipe_sha256": "e" * 64, "script_pins": {}, "input_pins": {},
                  "archive_pins": {str(Path(data["path"]).relative_to(self.root)): data["sha256"] for data in packages.values()},
                  "artifact_paths": {"archive-validation": original("archive_validation")}}
        nstate = {"status": "completed", "kernel_release": d.KREL, "stages": stages, "nvidia_driver": d.DRIVER,
                  "kernel": kernel, "artifacts": {"manifest": original("nvidia_manifest")},
                  "script_sha256": "d" * 64, "system_pins": {"/usr/bin/dkms": d.DKMS_SHA256}}
        plan["baseline"]["protected"]["/usr/bin/dkms"] = {".": {"kind": "file", "sha256": d.DKMS_SHA256}}
        plan["artifacts"]["pin:scripts/prepare-nvidia-uki.py"] = {
            "path": "scripts/prepare-nvidia-uki.py", "sha256": "d" * 64, "bytes": 100}
        magic = d.KREL + " SMP preempt mod_unload"
        report = {"kernel_release": d.KREL, "config_sha256": d.CONFIG_SHA256, "packages": packages, "acer_wmi_vermagic": magic}
        nv = {"kernel_release": d.KREL, "nvidia_driver": d.DRIVER,
              "validated_inputs": {"packages": packages, "header_config_sha256": d.CONFIG_SHA256,
                                   "validation_sha256": "b" * 64, "signing_certificate_sha256": d.SIGNING_CERT_SHA256,
                                   "host_baseline": {}},
              "nvidia": {"normal_modpost": True, "modules": {name: {
                  "sig_key": d.SIGNING_KEY, "vermagic": magic, "version": d.DRIVER, "signer": "test"} for name in d.MODULES}},
              "host_baseline_unchanged": True, "system_installation": False,
              "module_load_performed": False, "boot_or_runtime_validated": False,
              "uki": {"path": original("uki"), "sha256": "a" * 64, "bytes": 100},
              "dkms_archive": {"path": original("dkms_archive"), "sha256": "a" * 64,
                               "native_private_import_verified": True, "target_kernel_only": d.KREL},
              "initramfs_nvidia_modules": {name: "usr/lib/modules/" + d.KREL + "/updates/dkms/" + name + ".ko"
                                           for name in d.MODULES if name != "nvidia-peermem"},
              "cmdline_sha256": "a" * 64, "efi_sections": {".linux": {"sha256": "c" * 64}}}
        app = {"package_version": "0.1.0-9", "package_release": 9, "package": original("app_package"), "package_sha256": "a" * 64}
        documents = dict(zip(("kernel_state", "nvidia_state", "archive_validation", "nvidia_manifest", "app_report"),
                             (kernel, nstate, report, nv, app)))
        paths = {role: self.root / (role + ".json") for role in documents}
        paths["import_status"] = self.root / "import-status.txt"
        paths["import_status"].write_text(f"nvidia/{d.DRIVER}, {d.KREL}, x86_64: built\n")
        for role, document in documents.items():
            paths[role].write_text(json.dumps(document))
        return plan, paths, documents

    def test_actual_manifest_schema_links_kernel_nvidia_and_app_evidence(self):
        plan, paths, _ = self.evidence()
        report, nv = d.validate_evidence(plan, paths)
        self.assertEqual(set(report["packages"]), set(d.PACKAGES))
        self.assertEqual(nv["kernel_release"], d.KREL)

    def test_evidence_failures_cannot_be_hidden_by_pinning_outer_json(self):
        for role, mutate in (
            ("nvidia_manifest", lambda v: v["nvidia"]["modules"]["nvidia"].update(sig_key="BAD:FPR")),
            ("nvidia_manifest", lambda v: v["dkms_archive"].update(native_private_import_verified=False)),
            ("nvidia_manifest", lambda v: v["validated_inputs"]["host_baseline"].update({"/etc/shadow": "a" * 64})),
            ("nvidia_manifest", lambda v: v.update(system_installation=True)),
            ("kernel_state", lambda v: v.update(status="running")),
            ("archive_validation", lambda v: v["packages"].update({"linux": {}})),
            ("app_report", lambda v: v.update(package_release=1)),
        ):
            plan, paths, documents = self.evidence()
            mutate(documents[role])
            paths[role].write_text(json.dumps(documents[role]))
            with self.subTest(role=role), self.assertRaises(d.DeploymentError):
                d.validate_evidence(plan, paths)

    def uki(self, *, release=d.KREL):
        values = {".linux": b"validated kernel bytes", ".uname": release.encode() + b"\0",
                  ".cmdline": CMDLINE.strip().encode() + b"\0", ".initrd": b"validated initramfs bytes",
                  ".osrel": ("NAME=Test\nVERSION_ID=" + d.KREL + "\n").encode()}
        dos, pe = bytearray(64), bytearray(24)
        dos[:2], pe[:4] = b"MZ", b"PE\0\0"
        struct.pack_into("<I", dos, 60, 64)
        struct.pack_into("<HH", pe, 4, 0x8664, len(values))
        offset, rows, payload, reports = 88 + len(values) * 40, bytearray(), bytearray(), {}
        for name, data in values.items():
            rows.extend(struct.pack("<8sIIII16x", name.encode(), len(data), 0, len(data), offset))
            reports[name] = {"size": len(data), "offset": offset, "raw_size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            offset += len(data)
            payload.extend(data)
        path = self.root / "staged.efi"
        path.write_bytes(dos + pe + rows + payload)
        return path, {"efi_sections": reports}

    def test_pe_lengths_hashes_release_and_complete_cmdline(self):
        path, nv = self.uki()
        d.audit_uki(path, nv, CMDLINE)
        with self.assertRaisesRegex(d.DeploymentError, "root command line"):
            d.audit_uki(path, nv, LIVE)
        nv["efi_sections"][".linux"]["size"] += 1
        with self.assertRaisesRegex(d.DeploymentError, "length"):
            d.audit_uki(path, nv, CMDLINE)
        path, nv = self.uki(release=d.STOCK)
        with self.assertRaisesRegex(d.DeploymentError, "release"):
            d.audit_uki(path, nv, CMDLINE)

    def test_protected_snapshot_detects_unrelated_concurrent_change(self):
        root = self.root / "protected"
        root.mkdir()
        (root / "stock").write_bytes(b"stock")
        with patch.object(d, "PROTECTED", (str(root),)):
            before = d.protected_snapshot()
            d.assert_protected(before)
            (root / "foreign").write_bytes(b"concurrent")
            with self.assertRaisesRegex(d.DeploymentError, "protected host files changed"):
                d.assert_protected(before)
        self.assertEqual((root / "foreign").read_bytes(), b"concurrent")


class TransactionTests(OfflineTests):
    def test_strict_native_table_accepts_exact_new_packages(self):
        self.assertEqual(d.strict_package_table(TABLE), list(d.PACKAGES))

    def test_strict_native_table_rejects_all_unexpected_actions(self):
        for table in (
            TABLE.replace("Package (2)", "Package (3)"),
            TABLE.replace("Old Version", "Repository"),
            TABLE.replace("linux-nitro-test-headers", "nvidia-open-dkms"),
            TABLE.replace("161.63 MiB", "7.2.8.arch1-1 161.63 MiB"),
            TABLE.replace(d.VERSION, "wrong-version", 1),
            TABLE.replace("Total Installed Size:", "Total Removed Size:"),
            TABLE.replace("Total Installed Size:", "Total Net Upgrade Size:"),
            TABLE.replace("[Y/n]", "[y/N]"),
            TABLE.replace(":: Proceed", "linux 7.2.9.arch1-1 100.00 MiB\n:: Proceed"),
            ":: Replace linux with linux-nitro-test? [Y/n] \n" + TABLE,
            ":: Import PGP key? [Y/n] \n" + TABLE,
            TABLE.replace("loading packages...", "\x1b[31mloading packages..."),
            TABLE.replace("Total Installed Size:  482.99 MiB\n", ""),
        ):
            with self.subTest(table=table), self.assertRaises(d.DeploymentError):
                d.strict_package_table(table)

    def fake_pacman(self, output):
        self.read_fd, write_fd = os.pipe()
        os.write(write_fd, output)
        os.close(write_fd)
        stdout = os.fdopen(self.read_fd, "rb", buffering=0)
        stdin = Mock(closed=False)
        process = Mock(stdout=stdout, stdin=stdin, returncode=0)
        process.poll.return_value = 0
        process.wait.return_value = 0
        self.popen.side_effect = None
        self.popen.return_value = process
        return process

    def test_actual_prompt_not_a_separate_dry_run_gates_yes(self):
        process = self.fake_pacman(TABLE.encode() + b"checking integrity...\n")
        # Feed prompt and later output as separate actual reads.
        with patch.object(d.os, "read", side_effect=[TABLE.encode(), b"checking integrity...\n", b""]):
            gate = Mock()
            d.pacman_transaction(["/usr/bin/pacman", "-U", "cached-input"], self.root / "pacman.log", gate)
        gate.assert_called_once_with()
        process.stdin.write.assert_called_once_with(b"y\n")
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(self.popen.call_args.kwargs["env"]["LC_ALL"], "C")

    def test_bad_actual_table_never_answers_yes(self):
        process = self.fake_pacman(TABLE.replace("Package (2)", "Package (3)").encode())
        gate = Mock()
        with self.assertRaises(d.DeploymentError):
            d.pacman_transaction(["/usr/bin/pacman"], self.root / "bad.log", gate)
        gate.assert_not_called()
        process.stdin.write.assert_not_called()

    def test_gate_recheck_failure_never_answers_yes(self):
        process = self.fake_pacman(TABLE.encode())
        with self.assertRaisesRegex(d.DeploymentError, "baseline changed"):
            d.pacman_transaction(["/usr/bin/pacman"], self.root / "changed.log",
                                 Mock(side_effect=d.DeploymentError("baseline changed")))
        process.stdin.write.assert_not_called()

    def test_boot_lock_fails_closed_and_unlocks_only_when_held(self):
        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_ino=7)
        with patch.object(d, "root_path"), patch.object(d.os, "open", return_value=123), \
                patch.object(d.os, "close") as close, patch.object(d.os, "fstat", return_value=info), \
                patch.object(d.os, "stat", return_value=info), patch.object(d.fcntl, "flock") as flock:
            with d.boot_writer_lock():
                self.assertEqual(flock.call_args.args, (123, d.fcntl.LOCK_EX | d.fcntl.LOCK_NB))
            self.assertEqual(flock.call_args.args, (123, d.fcntl.LOCK_UN))
            close.assert_called_once_with(123)
        with patch.object(d, "root_path"), patch.object(d.os, "open", return_value=123), \
                patch.object(d.os, "close"), patch.object(d.os, "fstat", return_value=info), \
                patch.object(d.fcntl, "flock", side_effect=BlockingIOError) as flock:
            called = False
            with self.assertRaisesRegex(d.DeploymentError, "lock timeout"):
                with d.boot_writer_lock(timeout=0):
                    called = True
            self.assertFalse(called)
            self.assertEqual(flock.call_count, 1)
            self.run.assert_not_called()

    def test_boot_lock_inode_replacement_aborts(self):
        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_ino=7)
        with patch.object(d, "root_path"), patch.object(d.os, "open", return_value=123), \
                patch.object(d.os, "close"), patch.object(d.os, "fstat", return_value=info), \
                patch.object(d.os, "stat", return_value=SimpleNamespace(st_ino=8)), patch.object(d.fcntl, "flock"), \
                self.assertRaisesRegex(d.DeploymentError, "replaced"):
            with d.boot_writer_lock():
                self.fail("replaced lock accepted")

    def test_dkms_sandbox_has_no_compiler_or_stock_module_write_mount(self):
        command = d.dkms_sandbox(self.root, ["/usr/bin/dkms", "ldtarball", "--archive", "cached.tar.gz"])
        self.assertIn("--unshare-net", command)
        self.assertIn("--cap-drop", command)
        self.assertIn("/usr/bin/false", command)
        self.assertNotIn("--force", command)
        self.assertNotIn("--all", command)
        writable = [command[index + 1] for index, word in enumerate(command) if word == "--bind"]
        self.assertEqual(writable, [str(self.root)])
        self.assertNotIn("/usr/lib/modules/" + d.STOCK, writable)

    def test_apply_uid_and_installed_location_gate_before_writes(self):
        with patch.object(d.os, "geteuid", return_value=1000), patch.object(d, "preflight") as preflight, \
                patch.object(d, "mkdir_root") as mkdir, self.assertRaisesRegex(d.DeploymentError, "UID 0"):
            d.apply_deployment({}, "a" * 64, b"{}")
        mkdir.assert_not_called()
        preflight.assert_not_called()
        with patch.object(d.os, "geteuid", return_value=0), patch.object(d, "preflight") as preflight, \
                self.assertRaisesRegex(d.DeploymentError, "installed reviewed app"):
            d.apply_deployment({}, "a" * 64, b"{}")
        preflight.assert_not_called()

    def test_default_cli_is_read_only(self):
        path, digest = self.save_manifest()
        with patch.object(d, "preflight", return_value={"status": "preflight-passed", "writes_performed": False}) as preflight, \
                patch.object(d, "apply_deployment") as apply, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(d.main(["--manifest", str(path)]), 0)
        apply.assert_not_called()
        preflight.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())["manifest_sha256"], digest)

    def test_full_mocked_apply_never_holds_boot_lock_during_pacman_or_nvidia(self):
        boot, base = self.root / "boot", self.root / "state"
        boot.mkdir()
        (boot / "limine.conf").write_text(MENU)
        (boot / "stock.efi").write_bytes(b"stock bytes")
        cached = self.root / "cached"
        cached.mkdir()
        paths = {}
        for role, data in (("uki", b"new UKI"), ("cmdline", CMDLINE.encode()),
                           ("kernel_package", b"kernel"), ("headers_package", b"headers")):
            paths[role] = cached / role
            paths[role].write_bytes(data)
        plan = manifest_fixture(self.root)
        plan["artifacts"]["uki"] = {"sha256": d.sha(paths["uki"])}
        plan["baseline"]["protected"]["/boot"] = {"limine.conf": {"sha256": d.sha(boot / "limine.conf")}}
        expected = {**plan["baseline"]["packages"], **dict.fromkeys(d.PACKAGES, d.VERSION)}
        held, events, installed = False, [], False
        @contextmanager
        def writer_lock():
            nonlocal held
            held = True
            events.append("boot-lock")
            yield 123
            held = False
        def pacman(command, log, gate):
            nonlocal installed
            self.assertFalse(held)
            self.assertEqual(command[0], "/usr/bin/pacman")
            self.assertNotIn("--noconfirm", command)
            events.append("pacman")
            gate()
            installed = True
        def nvidia(cache, cached_paths, nv, record):
            self.assertFalse(held)
            events.append("nvidia")
        def native(command):
            self.assertTrue(held)
            self.assertEqual(command[0], "/usr/bin/limine-entry-tool")
            self.assertEqual(command[-2:], ["--no-hooks", "--no-mutex"])
            events.append("add-uki")
            (boot / "new.efi").write_bytes(paths["uki"].read_bytes())
            entry = ("  //linux-nitro-test\n  protocol: efi\n  path: boot():/EFI/Linux/new.efi\n  cmdline: " + CMDLINE + "\n")
            (boot / "limine.conf").write_text(MENU.replace("  //Snapshots", entry + "  //Snapshots"))
            return ""
        real_stat = os.stat
        real_text = Path.read_text
        def read_text(path, *args, **kwargs):
            return LIVE if path == Path("/proc/cmdline") else real_text(path, *args, **kwargs)
        def pretend_esp(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if Path(path) != boot:
                return info
            values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
            values["st_dev"] += 1
            return SimpleNamespace(**values)
        installed_module = self.root / "app/deployment.py"
        fragment = self.root / "etc/limine-entry-tool.d/90-nitro-control-test.conf"
        with patch.multiple(d, BASE=base, BOOT=boot, STOCK_UKI=boot / "stock.efi", NEW_UKI=boot / "new.efi",
                            STOCK_UKI_SHA256=d.sha(boot / "stock.efi"), FRAGMENT=fragment,
                            APPROOT=installed_module.parent, __file__=str(installed_module)), \
                patch.object(d.os, "geteuid", return_value=0), patch.object(d.os, "umask"), patch.object(d, "root_path"), \
                patch.object(d, "preflight"), patch.object(d, "cache_inputs", return_value=paths), \
                patch.object(d, "validate_evidence", return_value=({}, {})), patch.object(d, "verify_inputs"), \
                patch.object(d, "assert_protected"), patch.object(d, "secure_boot_disabled"), patch.object(d, "check_limine_config"), \
                patch.object(d, "package_versions", side_effect=lambda: expected if installed else plan["baseline"]["packages"]), \
                patch.object(d, "pacman_transaction", side_effect=pacman), patch.object(d, "install_nvidia", side_effect=nvidia), \
                patch.object(d, "boot_writer_lock", writer_lock), patch.object(d, "run", side_effect=native), \
                patch.object(Path, "read_text", autospec=True, side_effect=read_text), \
                patch.object(d.os, "stat", side_effect=pretend_esp), patch.object(d.shutil, "disk_usage", return_value=SimpleNamespace(free=100*d.GIB)), \
                patch.object(d.fcntl, "flock"):
            summary = d.apply_deployment(plan, "a" * 64, b"manifest")
        self.assertEqual(events, ["pacman", "nvidia", "boot-lock", "add-uki"])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual((boot / "stock.efi").read_bytes(), b"stock bytes")
        self.assertIn("predator_v4=N", summary["boot_instruction"])
        self.assertEqual(json.loads((Path(summary["cache"]) / "summary.json").read_text()), summary)
        self.assertTrue(list((base / "boot-backups").glob("*/boot/stock.efi")))

    def test_failure_keeps_root_cache_and_reports_precise_step_without_rollback(self):
        base, installed = self.root / "state", self.root / "app/deployment.py"
        foreign = self.root / "unrelated"
        foreign.write_bytes(b"leave this alone")
        with patch.multiple(d, BASE=base, APPROOT=installed.parent, __file__=str(installed)), \
                patch.object(d.os, "geteuid", return_value=0), patch.object(d.os, "umask"), patch.object(d, "root_path"), \
                patch.object(d.fcntl, "flock"), patch.object(d, "preflight"), patch.object(d, "snapshot", return_value={".": {"kind": "absent"}}), \
                patch.object(d, "package_versions", return_value={}), \
                patch.object(d, "cache_inputs", side_effect=d.DeploymentError("artifact changed during copy")), \
                patch.object(d, "pacman_transaction") as pacman, patch.object(d, "boot_writer_lock") as boot_lock, \
                self.assertRaisesRegex(d.DeploymentError, "failed step cache-inputs; root-owned summary:"):
            d.apply_deployment(manifest_fixture(self.root), "a" * 64, b"manifest")
        pacman.assert_not_called()
        boot_lock.assert_not_called()
        summaries = list((base / "deployment").glob("*/summary.json"))
        self.assertEqual(len(summaries), 1)
        summary = json.loads(summaries[0].read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["step"], "cache-inputs")
        self.assertFalse(summary["automatic_rollback"])
        self.assertEqual(foreign.read_bytes(), b"leave this alone")
        self.assertEqual(stat.S_IMODE(summaries[0].parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(summaries[0].stat().st_mode), 0o600)


class NvidiaTests(OfflineTests):
    def test_private_native_import_merges_only_target_and_never_builds(self):
        cache, source, nvtree, modroot = (self.root / name for name in ("cache", "source", "nvidia", "modules"))
        for path in (cache, source, nvtree, modroot):
            path.mkdir()
        version = nvtree / d.DRIVER
        version.mkdir()
        (version / "source").symlink_to(source)
        stock = version / d.STOCK
        stock.mkdir()
        (stock / "sentinel").write_bytes(b"stock DKMS state")
        archive = self.root / "prebuilt.tar.gz"
        archive.write_bytes(b"already-audited archive")
        binaries = {name: ("signed module " + name).encode() for name in d.MODULES}
        nv = {"nvidia": {"modules": {name: {"dkms_sha256": hashlib.sha256(data).hexdigest(),
                                            "vermagic": d.KREL + " SMP preempt mod_unload"}
                                      for name, data in binaries.items()}}}
        target = version / d.KREL
        link = nvtree / ("kernel-" + d.KREL + "-x86_64")
        commands, installed, records = [], False, []
        def native(command):
            nonlocal installed
            command = list(map(str, command))
            commands.append(command)
            if "ldtarball" in command:
                private = cache / "dkms-import/nvidia" / d.DRIVER
                directory = private / d.KREL / "x86_64/module"
                directory.mkdir(parents=True)
                (private / "source").symlink_to(source)
                for name, data in binaries.items():
                    (directory / (name + ".ko.zst")).write_bytes(data)
            elif "status" in command:
                return f"nvidia/{d.DRIVER}, {d.KREL}, x86_64: " + ("installed" if installed else "built") + "\n"
            elif "install" in command:
                self.assertTrue((target / "x86_64/module").is_dir())
                destination = modroot / "updates/dkms"
                destination.mkdir(parents=True)
                for name, data in binaries.items():
                    (destination / (name + ".ko.zst")).write_bytes(data)
                link.symlink_to(d.DRIVER + "/" + d.KREL + "/x86_64")
                installed = True
            elif command[0] == "/usr/bin/modinfo":
                return {"sig_key": d.SIGNING_KEY, "version": d.DRIVER,
                        "vermagic": d.KREL + " SMP preempt mod_unload"}[command[2]] + "\n"
            return ""
        real_sha = d.sha
        with patch.multiple(d, SOURCE=source, NVTREE=nvtree, NVTARGET=target, NVLINK=link, MODROOT=modroot), \
                patch.object(d, "root_path"), patch.object(d, "run", side_effect=native), \
                patch.object(d, "sha", side_effect=lambda path: d.DKMS_SHA256 if str(path) == "/usr/bin/dkms" else real_sha(path)):
            d.install_nvidia(cache, {"dkms_archive": archive}, nv, lambda step, change: records.append((step, change)))
        self.assertTrue(installed)
        self.assertEqual((stock / "sentinel").read_bytes(), b"stock DKMS state")
        self.assertEqual(os.readlink(version / "source"), str(source))
        imports = [command for command in commands if "ldtarball" in command]
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0][imports[0].index("--dkmstree") + 1], str(cache / "dkms-import"))
        installs = [command for command in commands if "install" in command]
        self.assertEqual(len(installs), 1)
        self.assertIn("--no-depmod", installs[0])
        self.assertEqual(installs[0][installs[0].index("-k") + 1], d.KREL)
        self.assertFalse(any("--force" in command or "--all" in command or "build" in command for command in commands))
        self.assertIn(["/usr/bin/depmod", d.KREL], commands)
        self.assertEqual([step for step, _ in records], ["nvidia-merge", "nvidia-installed"])

    def test_changed_dkms_is_a_blocker_not_a_root_build_fallback(self):
        with patch.object(d, "sha", return_value="0" * 64), patch.object(d, "run") as native, \
                self.assertRaisesRegex(d.DeploymentError, "cannot guarantee binary-only"):
            d.install_nvidia(self.root, {}, {}, Mock())
        native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
