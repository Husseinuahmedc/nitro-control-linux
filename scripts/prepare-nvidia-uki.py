#!/usr/bin/env python3
"""One-shot, ordinary-user NVIDIA compatibility build and private UKI staging."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import time


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepare-nvidia-uki.py"
WORK = ROOT / "build/nvidia-uki"
STATE = ROOT / "plan/nvidia-uki-state.json"
KSTATE = ROOT / "plan/kernel-pipeline-state.json"
TREE = ROOT / "build/makepkg/linux-nitro-test/src/linux-7.2.9"
GUARD = ROOT / "scripts/resource-guard.py"
KREL = "7.2.9-arch1-1-nitro-test"
DRIVER = "615.78.08"
SOURCE = Path("/usr/src") / ("nvidia-" + DRIVER)
MODROOT = WORK / "module-root"
MODULES = MODROOT / "usr/lib/modules" / KREL
HEADERS = MODULES / "build"
DKMS = WORK / "dkms"
IMPORT = WORK / "dkms-import"
SOURCES = WORK / "sources"
CONFIG = WORK / "etc-dkms"
ARTIFACTS = WORK / "artifacts"
UKI = WORK / "boot/omarchy_linux-nitro-test.efi"
INITRD = WORK / "boot/initramfs-linux-nitro-test.img"
CMDLINE = WORK / "inputs/test-kernel.cmdline"
TARBALL = ARTIFACTS / f"nvidia-{DRIVER}-{KREL}-x86_64.dkms.tar.gz"
STAGES = ("prepare-private-inputs", "nvidia-build", "validate-nvidia",
          "dkms-binary-export", "stage-uki", "audit-uki")
LIMITS = {"make_jobs": 2, "min_free_gib": 12, "min_mem_gib": 2,
          "timeout_seconds": 3600, "poll_seconds": 0.5}
CHECKPOINT_SHA = "f85aeaf862734af84361205e4c355b1108e29ce9b7f0b495f610b2c63fe8a8ea"
SECUREBOOT = Path("/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c")


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="ascii"))


def owned_path(path):
    path = Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(WORK):
        raise RuntimeError(f"not a private output: {path}")
    return path


def write_json(path, data):
    path = Path(path)
    if path != STATE:
        owned_path(path)
    elif path.is_symlink() or path.parent.resolve() != ROOT / "plan":
        raise RuntimeError("unsafe state path")
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("x", encoding="ascii") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_text(path, text):
    with owned_path(path).open("x", encoding="ascii") as stream:
        stream.write(text)


def save(state):
    state["updated_at_utc"] = now()
    write_json(STATE, state)


def environment():
    return {"PATH": "/usr/bin:/bin", "HOME": str(WORK / "home"), "LC_ALL": "C",
            "TZ": "UTC", "TMPDIR": str(WORK / "tmp"),
            "XDG_CACHE_HOME": str(WORK / "cache"), "XDG_CONFIG_HOME": str(WORK / "home/.config"),
            "PYTHONDONTWRITEBYTECODE": "1", "MAKEFLAGS": "-j2", "ZSTD_NBTHREADS": "2",
            "SOURCE_DATE_EPOCH": "1791027808"}


def sandbox(command):
    # /etc/dkms is overlaid ONLY in this unprivileged mount namespace.
    return ["/usr/bin/bwrap", "--unshare-user", "--unshare-net", "--die-with-parent",
            "--cap-drop", "ALL", "--ro-bind", "/", "/", "--dev", "/dev",
            "--bind", str(WORK), str(WORK), "--ro-bind", str(CONFIG), "/etc/dkms",
            "--chdir", str(WORK), "--", *map(str, command)]


def run(command, *, output=None):
    print("+ " + shlex.join(map(str, command)), flush=True)
    if output is None:
        subprocess.run(list(map(str, command)), cwd=WORK, stdin=subprocess.DEVNULL, check=True)
        return None
    with owned_path(output).open("x", encoding="utf-8") as stream:
        subprocess.run(list(map(str, command)), cwd=WORK, stdin=subprocess.DEVNULL,
                       stdout=stream, stderr=subprocess.STDOUT, check=True)
    return Path(output).read_text(encoding="utf-8")


def proc_state(pid, starttime):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    if int(fields[19]) != starttime:
        return None
    return fields[0]


def validate_cmdline(prepared, live, limine):
    tokens, current = shlex.split(prepared), shlex.split(live)
    opt = "acer_wmi.predator_v4="
    if [item for item in tokens if item.startswith(opt)] != [opt + "0"]:
        raise RuntimeError("test command line must contain exactly one fan-feature opt-out")
    without_opt = lambda items: [item for item in items if not item.startswith(opt)]
    if without_opt(tokens) != without_opt(current):
        raise RuntimeError("prepared command line differs from the complete current working command line")
    if not all(any(item.startswith(prefix) for item in tokens)
               for prefix in ("cryptdevice=", "root=", "rootflags=", "rootfstype=")):
        raise RuntimeError("complete cryptroot arguments are required; no UUID guessing")
    entry = re.search(r"(?m)^  //linux\s*$([\s\S]*?)(?=^\s*//|\Z)", limine)
    lines = re.findall(r"(?m)^\s*cmdline:\s*(.+)$", entry[1]) if entry else []
    if len(lines) != 1 or without_opt(shlex.split(lines[0])) != without_opt(current):
        raise RuntimeError("current stock Limine entry does not corroborate the working command line")
    return tokens


def system_pins():
    files = [Path("/usr/bin/dkms"), Path("/usr/bin/mkinitcpio"),
             Path("/usr/lib/initcpio/functions"), Path("/etc/mkinitcpio.conf"),
             ROOT / "plan/test-kernel.cmdline", Path("/boot/limine.conf"),
             SOURCE / "dkms.conf", SOURCE / "Makefile", SOURCE / "COPYING"]
    directories = [Path("/etc/mkinitcpio.conf.d"), Path("/etc/initcpio/install"),
                   Path("/etc/initcpio/hooks"), Path("/usr/lib/initcpio/install"),
                   Path("/usr/lib/initcpio/hooks")]
    for directory in directories:
        if directory.exists():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    return {str(path): sha(path) for path in sorted(set(files))}


def verify_system(state):
    if system_pins() != state["system_pins"]:
        raise RuntimeError("audited source/configuration/hook inputs changed; no automatic refresh")
    secureboot = SECUREBOOT.read_bytes()
    if len(secureboot) != 5 or secureboot[4] != 0:
        raise RuntimeError("Secure Boot is not verifiably disabled; never change it here")
    validate_cmdline((ROOT / "plan/test-kernel.cmdline").read_text(),
                     Path("/proc/cmdline").read_text(), Path("/boot/limine.conf").read_text())


def verify_kernel(state, *, completed=False):
    kernel = load(KSTATE)
    expected = state["kernel"]
    for key in ("run_id", "pipeline_pid", "pipeline_identity", "kernel_release",
                "config_sha256", "recipe_sha256", "script_pins", "input_pins",
                *(key for key in ("archive_pins", "checkpoint_sha256", "checkpoint_stat") if key in expected)):
        if kernel.get(key) != expected.get(key):
            raise RuntimeError(f"kernel pipeline identity/input declaration changed: {key}")
    if kernel["kernel_release"] != KREL:
        raise RuntimeError("not the explicitly authorized recovered kernel pipeline")
    for item in kernel["stages"]:
        if item["status"] == "failed":
            raise RuntimeError(f"kernel pipeline stage failed: {item['name']}")
    if kernel["status"] not in ("ready", "running", "completed"):
        raise RuntimeError(f"kernel pipeline {kernel['status']}: {kernel.get('finish_reason')}")
    for relative, digest in {**kernel["script_pins"], **kernel.get("input_pins", {})}.items():
        path = (ROOT / relative).resolve(strict=True)
        if not path.is_relative_to(ROOT) or sha(path) != digest:
            raise RuntimeError(f"kernel pinned file changed: {relative}")
    if completed:
        if kernel["status"] != "completed" or any(item["status"] != "succeeded" for item in kernel["stages"]):
            raise RuntimeError("kernel pipeline must have completed every declared stage")
        validation = next(item for item in kernel["stages"] if item["name"] == "validate-archives")
        summary = validation.get("guard_result", {})
        if (summary.get("exit_reason") != "child_exit" or summary.get("exit_code") != 0
                or summary.get("child_exit_code") != 0):
            raise RuntimeError("validate-archives lacks a positive guard result")
        result_path = ROOT / "build/kernel-pipeline" / kernel["run_id"] / "results/validate-archives.json"
        if load(result_path) != validation["result"]:
            raise RuntimeError("durable validate-archives result changed")
    return kernel


def no_kernel_compiler():
    for directory in Path("/proc").iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            name = (directory / "comm").read_text().strip()
            if name not in ("make", "cc1", "cc1plus", "gcc", "clang", "rustc", "ld", "ld.bfd", "ld.lld"):
                continue
            cwd = (directory / "cwd").resolve(strict=True)
            if cwd.is_relative_to(TREE) or cwd.is_relative_to(ROOT / "build/kernel-pipeline"):
                raise RuntimeError(f"kernel compiler still active: PID {directory.name}, {name}")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue


def dkms_args(action, *, tree=DKMS):
    return ["/usr/bin/dkms", action, f"nvidia/{DRIVER}", "-k", KREL, "-a", "x86_64",
            "--dkmstree", str(tree), "--sourcetree", str(SOURCES),
            "--installtree", str(MODROOT / "usr/lib/modules"),
            "--kernelsourcedir", str(HEADERS), "-j", "2"]


def archive_member(member):
    path = PurePosixPath(member.name)
    name = str(path)
    base = f"usr/lib/modules/{KREL}"
    if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
        raise RuntimeError(f"unsafe archive member: {member.name}")
    if name in (".", ".PKGINFO", ".BUILDINFO", ".MTREE"):
        return False
    if not (name in ("usr", "usr/lib", "usr/lib/modules", "usr/src", "usr/src/linux-nitro-test", base)
            or name.startswith(base + "/")):
        raise RuntimeError(f"unexpected archive payload: {name}")
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        resolved = os.path.normpath(str(path.parent / target) if member.issym() else str(target))
        if target.is_absolute() or not (resolved == base or resolved.startswith(base + "/")):
            raise RuntimeError(f"archive link escapes the unique release: {name}")
    return True


def prepare_inputs(state):
    kernel = verify_kernel(state, completed=True)
    validation_path = Path(kernel["artifact_paths"]["archive-validation"]).resolve(strict=True)
    if not validation_path.is_relative_to(ROOT / "build/kernel-pipeline" / kernel["run_id"]):
        raise RuntimeError("validation report is outside the declared pipeline")
    validation = load(validation_path)
    if validation["kernel_release"] != KREL or validation["config_sha256"] != kernel["config_sha256"]:
        raise RuntimeError("archive validation release/config mismatch")
    packages = validation["packages"]
    if set(packages) != {"linux-nitro-test", "linux-nitro-test-headers"}:
        raise RuntimeError("exactly the two validated kernel archives are required")
    MODROOT.mkdir()
    for package, data in packages.items():
        archive = Path(data["path"])
        expected = ROOT / "packages" / f"{package}-7.2.9.arch1-1-x86_64.pkg.tar.zst"
        if (archive != expected or archive.is_symlink() or sha(archive) != data["sha256"]
                or data["members"] <= 0):
            raise RuntimeError(f"validated archive changed: {package}")
        if package == "linux-nitro-test" and data["modules"] <= 0:
            raise RuntimeError("kernel archive contains no validated modules")
        with subprocess.Popen(["/usr/bin/zstd", "-dc", "-T2", str(archive)],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE) as decompressor:
            try:
                with tarfile.open(fileobj=decompressor.stdout, mode="r|") as stream:
                    for member in stream:
                        if archive_member(member):
                            stream.extract(member, MODROOT, filter="data")
                decompressor.stdout.close()
                if decompressor.wait() != 0:
                    raise RuntimeError("validated archive decompression failed")
            finally:
                if decompressor.poll() is None:
                    decompressor.terminate()
                    decompressor.wait()
        if sha(archive) != data["sha256"]:
            raise RuntimeError("archive changed during extraction")
    (MODROOT / "lib").symlink_to("usr/lib", target_is_directory=True)
    for relative in ("Makefile", "Module.symvers", "System.map", "vmlinux", "vmlinux.h",
                     "scripts/mod/modpost", "scripts/sign-file"):
        if not (HEADERS / relative).is_file():
            raise RuntimeError(f"incomplete packaged headers: {relative}")
    if (sha(HEADERS / ".config") != kernel["config_sha256"]
            or (HEADERS / "include/config/kernel.release").read_text().strip() != KREL
            or (HEADERS / "include/generated/utsrelease.h").read_text().strip() != f'#define UTS_RELEASE "{KREL}"'):
        raise RuntimeError("private packaged headers do not match the validated release/configuration")
    header_sha = sha(HEADERS / "vmlinux.h")
    selected = packages["linux-nitro-test-headers"]["selected_file_sha256"]
    if (header_sha != selected[f"usr/lib/modules/{KREL}/build/vmlinux.h"]
            or header_sha != sha(TREE / "tools/bpf/bpftool/vmlinux.h")):
        raise RuntimeError("official flattened packaged vmlinux.h differs from the validated source header")
    shutil.copyfile(ROOT / "plan/test-kernel.cmdline", CMDLINE)
    for path in SOURCE.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(SOURCE):
            raise RuntimeError(f"installed NVIDIA source link escapes source root: {path}")
    destination = SOURCES / SOURCE.name
    run(["/usr/bin/cp", "-a", "--reflink=auto", SOURCE, destination])
    source_manifest = {str(path.relative_to(destination)): sha(path)
                       for path in destination.rglob("*") if path.is_file()}
    write_json(WORK / "inputs/nvidia-source-files.json", source_manifest)
    shutil.copyfile(destination / "COPYING", ARTIFACTS / "NVIDIA-COPYING")
    key, certificate = TREE / "certs/signing_key.pem", TREE / "certs/signing_key.x509"
    if not key.is_file() or not certificate.is_file() or key.is_symlink() or certificate.is_symlink():
        raise RuntimeError("existing workspace test-kernel signing material is required; never generate/enroll keys")
    # Only sign-file will open the private key. Never inspect, hash or print its contents.
    framework = {
        "dkms_tree": str(DKMS), "source_tree": str(SOURCES),
        "install_tree": str(MODROOT / "usr/lib/modules"), "tmp_location": str(WORK / "tmp"),
        "parallel_jobs": "2", "compress_zstd_opts": "-q --rm -T2",
        "sign_file": str(HEADERS / "scripts/sign-file"), "mok_signing_key": str(key),
        "mok_certificate": str(certificate), "try_sign_modules": "true",
        "modprobe_on_install": "", "post_transaction": "", "build_environment": "",
    }
    write_text(CONFIG / "framework.conf", "".join(f"{key}={shlex.quote(value)}\n" for key, value in framework.items()))
    override = ("AUTOINSTALL=\"no\"\n"
                "MAKE[0]=\"'make' -j2 SYSSRC=${kernel_source_dir} SYSOUT=${kernel_source_dir} "
                "KERNEL_UNAME=${kernelver} modules\"\n")
    write_text(CONFIG / f"nvidia-{DRIVER}-{KREL}-x86_64.conf", override)
    baseline = {str(path): sha(path) for path in (Path("/boot/limine.conf"),
                Path("/boot/EFI/Linux/omarchy_linux.efi"))}
    stock_modules = Path("/usr/lib/modules") / os.uname().release / "updates/dkms"
    for path in sorted(stock_modules.glob("nvidia*.ko*")):
        baseline[str(path)] = sha(path)
    if not any("/updates/dkms/" in path for path in baseline):
        raise RuntimeError("cannot record the existing stock NVIDIA modules")
    result = {"kernel_release": KREL, "driver": DRIVER, "packages": packages,
              "validation_report": str(validation_path), "validation_sha256": sha(validation_path),
              "header_config_sha256": sha(HEADERS / ".config"),
              "header_symvers_sha256": sha(HEADERS / "Module.symvers"),
              "header_vmlinux_h_sha256": header_sha,
              "cmdline_sha256": sha(CMDLINE), "source_manifest": str(WORK / "inputs/nvidia-source-files.json"),
              "host_baseline": baseline, "signing_certificate_sha256": sha(certificate),
              "dkms_mode": "native non-root, private trees and namespace-only framework configuration"}
    write_json(WORK / "inputs/manifest.json", result)
    return result


def modinfo(path, field):
    return subprocess.check_output(["/usr/bin/modinfo", "-F", field, str(path)], text=True).strip()


def validate_nvidia():
    directory = DKMS / "nvidia" / DRIVER / KREL / "x86_64"
    log = directory / "log/make.log"
    text = log.read_text(errors="replace")
    if not re.search(r"\bMODPOST\b", text) or re.search(r"(?:ERROR|WARNING): modpost|undefined!", text):
        raise RuntimeError("normal modpost did not succeed cleanly; do not force or weaken symbol checking")
    expected_magic = modinfo(MODULES / "kernel/drivers/platform/x86/acer-wmi.ko.zst", "vermagic")
    expected_key = modinfo(MODULES / "kernel/drivers/platform/x86/acer-wmi.ko.zst", "sig_key")
    if expected_magic.split()[0] != KREL or not expected_key:
        raise RuntimeError("validated kernel module has unexpected vermagic/signature")
    destination = MODULES / "updates/dkms"
    destination.mkdir(parents=True)
    reports = {}
    for name in ("nvidia", "nvidia-modeset", "nvidia-uvm", "nvidia-drm", "nvidia-peermem"):
        path = directory / "module" / (name + ".ko.zst")
        if not path.is_file():
            raise RuntimeError(f"missing NVIDIA module: {name}")
        fields = {field: modinfo(path, field) for field in ("vermagic", "version", "signer", "sig_key", "depends", "license")}
        if fields["vermagic"] != expected_magic or not fields["signer"] or fields["sig_key"] != expected_key:
            raise RuntimeError(f"NVIDIA vermagic or private test-kernel signature mismatch: {name}")
        if name == "nvidia" and fields["version"] != DRIVER:
            raise RuntimeError("built NVIDIA driver version mismatch")
        output = destination / (name + ".ko")
        with output.open("xb") as stream:
            subprocess.run(["/usr/bin/zstd", "-dc", "-T2", str(path)], stdout=stream,
                           stdin=subprocess.DEVNULL, check=True)
        reports[name] = {**fields, "path": str(output), "sha256": sha(output),
                         "dkms_binary": str(path), "dkms_sha256": sha(path)}
    depmod = run(["/usr/bin/depmod", "-b", MODROOT, "-e", "-E", HEADERS / "Module.symvers", KREL],
                 output=WORK / "audit/depmod.log")
    if re.search(r"unknown symbol|disagrees about version|needs unknown|(?:ERROR|WARNING):", depmod, re.I):
        raise RuntimeError("private depmod symbol/version audit reported a problem")
    for name, data in reports.items():
        resolved = subprocess.check_output(["/usr/bin/modinfo", "-b", str(MODROOT), "-k", KREL,
                                            "-F", "filename", name], text=True).strip()
        if Path(resolved).resolve() != Path(data["path"]).resolve():
            raise RuntimeError(f"private module lookup selected the wrong NVIDIA module: {name}")
    report = {"modules": reports, "normal_modpost": True, "exported_symbol_version_check": "depmod -e -E Module.symvers",
              "build_log": str(log), "build_log_sha256": sha(log), "runtime_tested": False}
    write_json(WORK / "audit/nvidia.json", report)
    return report


def export_dkms():
    run([*dkms_args("mktarball"), "--binaries-only", "--archive", TARBALL])
    # Retain the native DKMS layout and include the upstream license in the deliverable.
    licensed = ARTIFACTS / (TARBALL.name + ".licensed")
    with tarfile.open(TARBALL, "r:gz") as source, tarfile.open(licensed, "w:gz") as target:
        for member in source:
            target.addfile(member, source.extractfile(member) if member.isfile() else None)
        target.add(ARTIFACTS / "NVIDIA-COPYING", arcname="NVIDIA-COPYING", recursive=False)
    os.replace(licensed, TARBALL)
    with tarfile.open(TARBALL, "r:gz") as archive:
        members = archive.getmembers()
        names = [str(PurePosixPath(member.name)) for member in members]
        prefix = f"dkms_main_tree/{KREL}/x86_64"
        for member, name in zip(members, names):
            if (PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                    or not (member.isfile() or member.isdir())
                    or not (name in (".", "NVIDIA-COPYING", "dkms_main_tree", f"dkms_main_tree/{KREL}", prefix,
                                     "dkms_binaries_only") or name.startswith(prefix + "/")
                            or name.startswith("dkms_binaries_only/"))):
                raise RuntimeError(f"DKMS archive is not target-only: {name}")
        if len(names) != len(set(names)) or "NVIDIA-COPYING" not in names:
            raise RuntimeError("duplicate members or missing NVIDIA license in DKMS archive")
        for name in load(WORK / "audit/nvidia.json")["modules"]:
            if f"{prefix}/module/{name}.ko.zst" not in names:
                raise RuntimeError("DKMS archive missing a validated module")
    command = dkms_args("ldtarball", tree=IMPORT)
    # ldtarball takes the archive instead of a preselected module/version.
    command.remove(f"nvidia/{DRIVER}")
    run([*command, "--archive", TARBALL])
    for name, data in load(WORK / "audit/nvidia.json")["modules"].items():
        imported = IMPORT / "nvidia" / DRIVER / KREL / "x86_64/module" / (name + ".ko.zst")
        if sha(imported) != data["dkms_sha256"]:
            raise RuntimeError("native private DKMS import changed a module")
    status = run(dkms_args("status", tree=IMPORT), output=WORK / "audit/dkms-import-status.txt")
    if f"nvidia/{DRIVER}, {KREL}, x86_64: built" not in status:
        raise RuntimeError("native private import was not recognized as built")
    return {"path": str(TARBALL), "sha256": sha(TARBALL), "members": len(names),
            "native_private_import_verified": True, "target_kernel_only": KREL,
            "upstream_license": str(ARTIFACTS / "NVIDIA-COPYING")}


def pe_sections(path):
    with Path(path).open("rb") as stream:
        header = stream.read(64)
        if header[:2] != b"MZ" or len(header) != 64:
            raise RuntimeError("UKI has no DOS/PE header")
        offset = struct.unpack_from("<I", header, 60)[0]
        stream.seek(offset)
        header = stream.read(24)
        if header[:4] != b"PE\0\0" or struct.unpack_from("<H", header, 4)[0] != 0x8664:
            raise RuntimeError("UKI is not an x86_64 PE executable")
        count, optional = struct.unpack_from("<H", header, 6)[0], struct.unpack_from("<H", header, 20)[0]
        if not 1 <= count <= 96:
            raise RuntimeError("invalid PE section count")
        stream.seek(offset + 24 + optional)
        result = {}
        for _ in range(count):
            row = stream.read(40)
            if len(row) != 40:
                raise RuntimeError("truncated PE section table")
            name = row[:8].rstrip(b"\0").decode("ascii")
            size, _, raw_size, raw_offset = struct.unpack_from("<IIII", row, 8)
            payload = name in (".linux", ".uname", ".cmdline", ".initrd", ".osrel")
            if (name in result or (payload and size > raw_size)
                    or raw_offset + raw_size > Path(path).stat().st_size):
                raise RuntimeError("invalid/duplicate PE section bounds")
            result[name] = {"size": size, "offset": raw_offset, "raw_size": raw_size}
        return result


def audit_uki(state):
    sections = pe_sections(UKI)
    reports = {}
    with UKI.open("rb") as source:
        for name in (".linux", ".uname", ".cmdline", ".initrd", ".osrel"):
            data = sections[name]
            output = WORK / "audit" / ("efi" + name)
            source.seek(data["offset"])
            remaining = data["size"]
            with output.open("xb") as target:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError("truncated EFI section")
                    target.write(chunk)
                    remaining -= len(chunk)
            reports[name] = {**data, "sha256": sha(output), "path": str(output)}
    if reports[".linux"]["sha256"] != sha(MODULES / "vmlinuz"):
        raise RuntimeError("EFI .linux does not match the validated packaged kernel")
    if (WORK / "audit/efi.uname").read_bytes().rstrip(b"\0") != KREL.encode("ascii"):
        raise RuntimeError("EFI .uname release mismatch")
    embedded_cmdline = (WORK / "audit/efi.cmdline").read_text().strip("\0 \t\r\n")
    if embedded_cmdline != CMDLINE.read_text().strip():
        raise RuntimeError("EFI command line differs from the protected complete input")
    if f"VERSION_ID={KREL}" not in (WORK / "audit/efi.osrel").read_text().splitlines():
        raise RuntimeError("EFI .osrel version mismatch")
    with (WORK / "audit/efi.linux").open("rb") as stream:
        header = stream.read(0x210)
        if header[0x202:0x206] != b"HdrS":
            raise RuntimeError("EFI .linux is not an x86 Linux boot image")
        stream.seek(0x200 + struct.unpack_from("<H", header, 0x20e)[0])
        version = stream.read(256).split(b"\0", 1)[0].decode("ascii")
        if version.split()[0] != KREL:
            raise RuntimeError("EFI .linux embedded version mismatch")
    if reports[".initrd"]["sha256"] != sha(INITRD):
        raise RuntimeError("EFI initramfs does not match the staged initramfs")
    listing = run(["/usr/bin/lsinitcpio", "--list", WORK / "audit/efi.initrd"],
                  output=WORK / "audit/initramfs.list")
    paths = {line.strip().lstrip("./") for line in listing.splitlines()}
    included = {}
    for name in ("nvidia", "nvidia-modeset", "nvidia-uvm", "nvidia-drm"):
        matches = [path for path in paths if re.fullmatch(
            rf"(?:usr/)?lib/modules/{re.escape(KREL)}/updates/dkms/{re.escape(name)}\.ko(?:\.(?:zst|xz|gz))?", path)]
        if len(matches) != 1:
            raise RuntimeError(f"EFI initramfs missing/ambiguous NVIDIA module: {name}")
        included[name] = matches[0]
    for required in ("usr/bin/cryptsetup", "hooks/encrypt"):
        if required not in paths:
            raise RuntimeError(f"EFI cryptroot initramfs input absent: {required}")
    verify_system(state)
    inputs = load(WORK / "inputs/manifest.json")
    for path, expected in inputs["host_baseline"].items():
        if sha(path) != expected:
            raise RuntimeError(f"host baseline changed during staging: {path}")
    # This is a new-entry fragment only. Nothing consumes or installs it automatically.
    entry = ("# Workspace proposal only. Main owns ESP backup, activation and one-time boot.\n"
             "  //linux-nitro-test\n"
             f"  comment: Kernel version: {KREL}; NVIDIA {DRIVER}; build audit only\n"
             "  protocol: efi\n"
             "  path: boot():/EFI/Linux/omarchy_linux-nitro-test.efi\n"
             f"  cmdline: {CMDLINE.read_text().strip()}\n")
    write_text(WORK / "boot/limine-nitro-test.conf.fragment", entry)
    manifest = {"kernel_release": KREL, "nvidia_driver": DRIVER, "created_at_utc": now(),
                "uki": {"path": str(UKI), "sha256": sha(UKI), "bytes": UKI.stat().st_size},
                "efi_sections": reports, "linux_embedded_version": version,
                "cmdline_sha256": sha(CMDLINE), "initramfs_nvidia_modules": included,
                "nvidia": load(WORK / "audit/nvidia.json"),
                "dkms_archive": load(WORK / "results/dkms-binary-export.json"),
                "validated_inputs": inputs, "host_baseline_unchanged": True,
                "secure_boot": "observed disabled; unchanged", "system_installation": False,
                "module_load_performed": False, "boot_or_runtime_validated": False,
                "activation_owner": "main; separate privileged installation/backups/activation",
                "entry_proposal": str(WORK / "boot/limine-nitro-test.conf.fragment")}
    write_json(ARTIFACTS / "manifest.json", manifest)
    return {"manifest": str(ARTIFACTS / "manifest.json"), "uki": str(UKI), "uki_sha256": sha(UKI),
            "dkms_archive": str(TARBALL), "compatibility": "build/symbol/UKI-content audit only; not runtime"}


def execute_stage(name):
    state = load(STATE)
    if (state["status"] != "running" or state["stage"] != name
            or os.environ.get("NVIDIA_UKI_RUN_ID") != state["run_id"]):
        raise RuntimeError("stage must be selected by this active guarded pipeline")
    verify_kernel(state, completed=True)
    verify_system(state)
    if name == "prepare-private-inputs":
        result = prepare_inputs(state)
    elif name == "nvidia-build":
        run(dkms_args("build"))
        result = {"dkms_tree": str(DKMS), "kernel_release": KREL, "driver": DRIVER}
    elif name == "validate-nvidia":
        result = validate_nvidia()
    elif name == "dkms-binary-export":
        result = export_dkms()
    elif name == "stage-uki":
        if UKI.exists() or UKI.is_symlink() or INITRD.exists() or INITRD.is_symlink():
            raise RuntimeError("refusing to overwrite any staged boot output")
        run(["/usr/bin/fakeroot", "--", "/usr/bin/mkinitcpio", "--moduleroot", MODROOT,
             "--kernel", KREL, "--kernelimage", MODULES / "vmlinuz", "--cmdline", CMDLINE,
             "--uki", UKI, "--generate", INITRD, "--builddir", WORK / "mkinitcpio",
             "--nopost", "--no-ukify", "--nocolor"])
        result = {"uki": str(UKI), "initramfs": str(INITRD)}
    else:
        result = audit_uki(state)
    verify_kernel(state, completed=True)
    verify_system(state)
    write_json(WORK / "results" / (name + ".json"), result)


def guard_command(state, name):
    base = ROOT / "logs" / f"nvidia-uki-{state['run_id']}-{name}"
    logs = {"resources": str(base) + ".jsonl", "output": str(base) + ".output.log",
            "summary": str(base) + ".summary.log"}
    command = ["/usr/bin/python", str(GUARD), "--run", "--cwd", str(WORK),
               "--min-free-gib", "12", "--min-mem-gib", "2", "--timeout-seconds", "3600",
               "--poll-seconds", "0.5", "--log", logs["resources"], "--output-log", logs["output"],
               "--", *sandbox(["/usr/bin/python", "-B", SCRIPT, "--stage", name])]
    return command, logs


def supervise():
    state = load(STATE)
    lock = int(os.environ["NVIDIA_UKI_LOCK_FD"])
    os.fstat(lock)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state.update(pid=os.getpid(), status="waiting", stage="wait-kernel", started_at_utc=now())
    save(state)
    guard = None
    kernel_lock = None
    requested_signal = 0

    def stop(signum, frame):
        nonlocal requested_signal
        requested_signal = requested_signal or signum
        if guard is not None and guard.poll() is None:
            guard.send_signal(signal.SIGTERM)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    try:
        previous = None
        while True:
            if requested_signal:
                raise RuntimeError(f"received signal {requested_signal}; no automatic retry")
            kernel = verify_kernel(state)
            identity = kernel["pipeline_identity"]
            process = proc_state(kernel["pipeline_pid"], identity["starttime_ticks"])
            if process in ("T", "t"):
                raise RuntimeError("kernel pipeline process is stopped")
            observation = {"status": kernel["status"], "stage": kernel["stage"], "process_state": process}
            if observation != previous:
                state["waiting_for"] = observation
                save(state)
                print(f"{now()} WAIT {json.dumps(observation)}", flush=True)
                previous = observation
            if kernel["status"] == "completed":
                verify_kernel(state, completed=True)
                if process not in (None, "Z", "X"):
                    time.sleep(2)
                    continue
                kernel_lock = (ROOT / "plan/kernel-pipeline.lock").open("r")
                fcntl.flock(kernel_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            if process in (None, "Z", "X") or kernel.get("finish_reason"):
                raise RuntimeError(f"kernel pipeline exited/stopped before completion: {kernel.get('finish_reason')}")
            time.sleep(5)
        verify_system(state)
        for stage in state["stages"]:
            if requested_signal:
                raise RuntimeError(f"received signal {requested_signal}")
            verify_kernel(state, completed=True)
            no_kernel_compiler()
            if sha(SCRIPT) != state["script_sha256"]:
                raise RuntimeError("NVIDIA/UKI helper changed after declaration")
            name = stage["name"]
            command, logs = guard_command(state, name)
            stage.update(status="running", started_at_utc=now(), logs=logs)
            state.update(status="running", stage=name, guard_pid=None)
            save(state)
            print(f"{now()} START {name}", flush=True)
            env = environment()
            env["NVIDIA_UKI_RUN_ID"] = state["run_id"]
            with Path(logs["summary"]).open("x") as output:
                guard = subprocess.Popen(command, cwd=WORK, env=env, stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=subprocess.STDOUT,
                                         pass_fds=(lock, kernel_lock.fileno()))
                state["guard_pid"] = guard.pid
                save(state)
                while guard.poll() is None:
                    if requested_signal:
                        guard.send_signal(signal.SIGTERM)
                    verify_kernel(state, completed=True)
                    no_kernel_compiler()
                    time.sleep(0.5)
                code = guard.returncode
            guard = None
            summary = None
            if Path(logs["resources"]).is_file():
                for line in Path(logs["resources"]).read_text().splitlines():
                    event = json.loads(line)
                    if event.get("event") == "summary":
                        summary = event
            stage.update(guard_result=summary, finished_at_utc=now())
            state["guard_pid"] = None
            result_path = WORK / "results" / (name + ".json")
            if (code != 0 or not summary or summary.get("exit_reason") != "child_exit"
                    or summary.get("exit_code") != 0 or summary.get("child_exit_code") != 0
                    or not result_path.is_file() or requested_signal):
                stage["status"] = "failed"
                raise RuntimeError(f"{name}: exit={code}, reason={summary.get('exit_reason') if summary else 'missing_summary'}")
            stage["status"] = "succeeded"
            stage["result"] = str(result_path)
            save(state)
            print(f"{now()} SUCCEEDED {name}", flush=True)
        state.update(status="completed", stage=None, finished_at_utc=now(), waiting_for=None,
                     artifacts=load(WORK / "results/audit-uki.json"),
                     finish_reason="private compatibility build and actual UKI content audit completed; no activation")
        save(state)
        print(f"{now()} COMPLETED {json.dumps(state['artifacts'])}", flush=True)
        return 0
    except BaseException as error:
        if guard is not None and guard.poll() is None:
            guard.send_signal(signal.SIGTERM)
            guard.wait()
        for stage in state["stages"]:
            if stage["status"] == "running":
                stage["status"] = "failed"
        state.update(status="blocked", guard_pid=None, finished_at_utc=now(),
                     finish_reason=f"{type(error).__name__}: {error}; no retries or limit changes")
        save(state)
        print(f"{now()} BLOCKED {state['finish_reason']}", flush=True)
        return 1
    finally:
        if kernel_lock is not None:
            kernel_lock.close()


def resume_eligible(state):
    if ((state.get("status"), state.get("stage")) != ("blocked", "wait-kernel")
            or state.get("finish_reason") != "RuntimeError: kernel pipeline stage failed: validate-archives; no retries or limit changes"
            or state.get("guard_pid") is not None or state.get("limits_per_stage") != LIMITS
            or state.get("stages") != [{"name": name, "status": "pending"} for name in STAGES]
            or state.get("resume_history")):
        raise RuntimeError("kernel-fix resume requires the original upstream validation blocker and no NVIDIA stage ever started")


def resume_after_kernel_fix():
    state = load(STATE)
    resume_eligible(state)
    if Path(f"/proc/{state['pid']}").exists():
        raise RuntimeError("previous NVIDIA waiter PID is still present or uncertain")
    lock = os.open(owned_path(WORK / "pipeline.lock"), os.O_RDWR | os.O_NOFOLLOW)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        for name in ("artifacts", "audit", "boot", "cache", "dkms", "dkms-import", "etc-dkms",
                     "home", "inputs", "mkinitcpio", "results", "sources", "tmp"):
            directory = owned_path(WORK / name)
            if any(directory.iterdir()):
                raise RuntimeError(f"private stage output exists; cannot reset/retry: {name}")
        if list((ROOT / "logs").glob(f"nvidia-uki-{state['run_id']}-*")):
            raise RuntimeError("NVIDIA stage/guard log exists; cannot retry a resource event or prior attempt")
        verify_system(state)
        kernel = load(KSTATE)
        old = state["kernel"]
        for key in ("run_id", "kernel_release", "config_sha256", "recipe_sha256", "input_pins"):
            if kernel.get(key) != old.get(key):
                raise RuntimeError(f"validation repair changed an immutable kernel input: {key}")
        history = kernel.get("validation_repair_history", [])
        if len(history) != 1:
            raise RuntimeError("exactly one traceable validation-only repair is required")
        repair = history[0]
        record_path = Path(repair["record"]).resolve(strict=True)
        if not record_path.is_relative_to(ROOT / "plan/kernel-pipeline-validation-repairs"):
            raise RuntimeError("invalid validation-repair record path")
        if load(record_path) != {key: value for key, value in repair.items() if key != "record"}:
            raise RuntimeError("validation repair record differs from completed state")
        key = "scripts/kernel-pipeline.py"
        expected_scripts = {**old["script_pins"], key: repair["new_script_sha256"]}
        if (repair["event"] != "explicit_validation_checker_repair"
                or repair["old_script_sha256"] != old["script_pins"][key]
                or expected_scripts != kernel["script_pins"]
                or sha(ROOT / key) != repair["new_script_sha256"]):
            raise RuntimeError("not the recorded old-to-new validator-only script pin repair")
        for evidence in repair["original_evidence"].values():
            path = Path(evidence["snapshot"]).resolve(strict=True)
            if not path.is_relative_to(record_path.parent / "original") or sha(path) != evidence["sha256"]:
                raise RuntimeError("original validation failure evidence changed")
        original = load(repair["original_evidence"]["plan/kernel-pipeline-state.json"]["snapshot"])
        for field, value in old.items():
            if original.get(field) != value:
                raise RuntimeError(f"repair's original state does not match the blocked waiter's kernel: {field}")
        failed = next(item for item in original["stages"] if item["name"] == "validate-archives")
        summary = failed["guard_result"]
        if ((original["status"], original["stage"], failed["status"]) != ("stopped", "validate-archives", "failed")
                or summary.get("exit_reason") != "child_exit" or summary.get("exit_code") != 1
                or summary.get("child_exit_code") != 1 or summary.get("received_signal") is not None
                or summary.get("minimum_free_bytes", 0) < 12 * 1024**3
                or summary.get("minimum_mem_available_bytes", 0) < 2 * 1024**3
                or not 0 < summary.get("duration_seconds", 3600) < 3600):
            raise RuntimeError("original validator failure was not a within-limits tool error")
        events_path = repair["original_evidence"][str(Path(failed["logs"]["resources"]).relative_to(ROOT))]["snapshot"]
        for line in Path(events_path).read_text().splitlines():
            event = json.loads(line)
            if ((event.get("event") == "summary" and event != summary)
                    or (event.get("event") == "sample" and (event["free_bytes"] < 12 * 1024**3
                        or event["mem_available_bytes"] < 2 * 1024**3))):
                raise RuntimeError("original validation had a resource event or inconsistent terminal result")
        prefix = original["stages"].index(failed)
        if kernel["stages"][:prefix] != original["stages"][:prefix]:
            raise RuntimeError("a completed kernel compile/package stage changed during validation repair")
        for path in (TREE / ".config", ROOT / "packaging/config.x86_64"):
            if sha(path) != old["config_sha256"]:
                raise RuntimeError("original stock kernel configuration changed")
        if sha(ROOT / "packaging/PKGBUILD") != old["recipe_sha256"]:
            raise RuntimeError("kernel packaging recipe changed")
        if (kernel["checkpoint_sha256"] != CHECKPOINT_SHA or original["checkpoint_sha256"] != CHECKPOINT_SHA
                or kernel["checkpoint_stat"] != original["checkpoint_stat"]
                or sha(TREE / "vmlinux.o") != CHECKPOINT_SHA):
            raise RuntimeError("f85 vmlinux.o checkpoint changed; no kernel rebuilding is permitted")
        actual_stat = (TREE / "vmlinux.o").stat()
        if any(getattr(actual_stat, key) != value for key, value in kernel["checkpoint_stat"].items()):
            raise RuntimeError("kernel checkpoint file identity changed")
        new_pins = {key: kernel[key] for key in (*old, "archive_pins", "checkpoint_sha256", "checkpoint_stat")}
        candidate = {**state, "kernel": new_pins}
        verify_kernel(candidate, completed=True)
        identity = kernel["pipeline_identity"]
        if proc_state(kernel["pipeline_pid"], identity["starttime_ticks"]) not in (None, "Z", "X"):
            raise RuntimeError("repaired kernel pipeline has not exited")
        with (ROOT / "plan/kernel-pipeline.lock").open("r") as kernel_lock:
            fcntl.flock(kernel_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            no_kernel_compiler()
            validation = load(kernel["artifact_paths"]["archive-validation"])
            manifest_path = Path(kernel["artifact_paths"]["compatibility-manifest"])
            manifest = load(manifest_path)
            if (validation["kernel_release"] != KREL or validation["config_sha256"] != old["config_sha256"]
                    or manifest["kernel_release"] != KREL or manifest["packages"] != validation["packages"]
                    or manifest["system_installation"] is not False or manifest["bootable_or_runtime_validated"] is not False
                    or not manifest["uki_status"].startswith("inputs-only;")):
                raise RuntimeError("completed archive/input manifest is inconsistent or claims boot validation")
            pins = {str(Path(data["path"]).relative_to(ROOT)): data["sha256"]
                    for data in validation["packages"].values()}
            if pins != kernel["archive_pins"] or pins != {path: data["sha256"] for path, data in repair["packages"].items()}:
                raise RuntimeError("archive hashes differ between repair, validation and completed state")
            for path, expected in pins.items():
                if not (ROOT / path).resolve(strict=True).is_relative_to(ROOT / "packages") or sha(ROOT / path) != expected:
                    raise RuntimeError("validated package bytes changed since the validation repair")
            for data in manifest["inputs"].values():
                path = Path(data["path"]).resolve(strict=True)
                if not path.is_relative_to(ROOT / "build/boot") or sha(path) != data["sha256"]:
                    raise RuntimeError("completed compatibility-only input changed")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        directory = WORK / "history" / ("kernel-fix-" + stamp)
        directory.mkdir(parents=True, mode=0o700)
        previous_state = directory / "blocked-state.json"
        shutil.copy2(STATE, previous_state)
        previous_log = directory / "blocked-pipeline.log"
        shutil.copy2(state["log"], previous_log)
        record = {"event": "resume_after_upstream_validation_only_fix", "at_utc": now(),
                  "previous_state": str(previous_state), "previous_state_sha256": sha(previous_state),
                  "previous_log": str(previous_log), "previous_log_sha256": sha(previous_log),
                  "old_helper_sha256": state["script_sha256"], "new_helper_sha256": sha(SCRIPT),
                  "old_kernel": old, "new_kernel": new_pins, "repair_record": str(record_path),
                  "repair_sha256": sha(record_path), "compatibility_manifest": str(manifest_path),
                  "compatibility_manifest_sha256": sha(manifest_path), "packages_rehashed": pins,
                  "checkpoint_rehashed": CHECKPOINT_SHA, "no_nvidia_stage_ever_started": True}
        write_json(directory / "resume.json", record)
        state.update(kernel=new_pins, script_sha256=sha(SCRIPT), resume_history=[record],
                     run_id=state["run_id"] + "-kernel-fix-" + stamp, status="launching", pid=None,
                     guard_pid=None, finish_reason=None, finished_at_utc=None, waiting_for=None)
        console = ROOT / "logs" / f"nvidia-uki-{state['run_id']}.log"
        state["log"] = str(console)
        save(state)
        env = environment()
        env["NVIDIA_UKI_LOCK_FD"] = str(lock)
        with console.open("x") as output:
            child = subprocess.Popen(["/usr/bin/python", "-B", str(SCRIPT), "--run"], cwd=WORK,
                                     env=env, stdin=subprocess.DEVNULL, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock,))
        print(json.dumps({"pid": child.pid, "state": str(STATE), "log": str(console),
                          "resume_record": str(directory / "resume.json"), "stages": STAGES}))
    finally:
        os.close(lock)
    return 0


def start():
    if STATE.exists() or STATE.is_symlink() or WORK.exists() or WORK.is_symlink():
        raise RuntimeError("state/work tree already exists; no duplicate run or automatic retry")
    kernel = load(KSTATE)
    identity = kernel["pipeline_identity"]
    if (kernel["pipeline_pid"] != 183648 or kernel["kernel_release"] != KREL
            or identity["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
            or proc_state(183648, identity["starttime_ticks"]) in (None, "T", "t", "Z", "X")):
        raise RuntimeError("authorized recovered kernel pipeline is not live with the expected identity")
    for name in ("build", "logs", "plan", "scripts", "docs"):
        if (ROOT / name).is_symlink() or (ROOT / name).resolve() != ROOT / name:
            raise RuntimeError(f"unsafe workspace directory: {name}")
    state = {"schema_version": 1, "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"),
             "status": "launching", "stage": "wait-kernel", "pid": None, "guard_pid": None,
             "kernel_release": KREL, "nvidia_driver": DRIVER, "limits_per_stage": LIMITS,
             "finish_reason": None, "script_sha256": sha(SCRIPT), "system_pins": system_pins(),
             "kernel": {key: kernel.get(key) for key in ("run_id", "pipeline_pid", "pipeline_identity", "kernel_release",
                        "config_sha256", "recipe_sha256", "script_pins", "input_pins")},
             "stages": [{"name": name, "status": "pending"} for name in STAGES]}
    verify_kernel(state)
    verify_system(state)
    WORK.mkdir(mode=0o700)
    for name in ("home", "tmp", "cache", "etc-dkms", "sources", "dkms", "dkms-import", "artifacts",
                 "inputs", "results", "audit", "boot", "mkinitcpio"):
        (WORK / name).mkdir(mode=0o700)
    lock = os.open(WORK / "pipeline.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    console = ROOT / "logs" / f"nvidia-uki-{state['run_id']}.log"
    state["log"] = str(console)
    save(state)
    try:
        env = environment()
        env["NVIDIA_UKI_LOCK_FD"] = str(lock)
        with console.open("x") as output:
            child = subprocess.Popen(["/usr/bin/python", "-B", str(SCRIPT), "--run"], cwd=WORK,
                                     env=env, stdin=subprocess.DEVNULL, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock,))
    finally:
        os.close(lock)
    print(json.dumps({"pid": child.pid, "state": str(STATE), "log": str(console), "work": str(WORK),
                      "stages": ["wait-kernel-completed-and-validated", *STAGES]}))
    return 0


def self_test():
    live = "quiet cryptdevice=PARTUUID=example:root root=/dev/mapper/root rootflags=subvol=@ rw rootfstype=btrfs"
    limine = "  //linux\n  protocol: efi\n  cmdline: " + live + "\n\n  //Snapshots\n"
    assert validate_cmdline(live + " acer_wmi.predator_v4=0", live, limine)
    for bad in (live, live + " acer_wmi.predator_v4=0 acer_wmi.predator_v4=1",
                live.replace("subvol=@", "subvol=wrong") + " acer_wmi.predator_v4=0"):
        try:
            validate_cmdline(bad, live, limine)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unsafe command line accepted")
    for name in ("../etc/passwd", "/boot/test.efi", "usr/lib/modules/stock/kernel/bad.ko"):
        try:
            archive_member(tarfile.TarInfo(name))
        except RuntimeError:
            pass
        else:
            raise AssertionError("unsafe archive path accepted")
    link = tarfile.TarInfo(f"usr/lib/modules/{KREL}/build/escape")
    link.type, link.linkname = tarfile.SYMTYPE, "/usr/src"
    try:
        archive_member(link)
    except RuntimeError:
        pass
    else:
        raise AssertionError("escaping archive link accepted")
    for name in STAGES:
        command, _ = guard_command({"run_id": "self-test"}, name)
        assert command.index(str(GUARD)) < command.index("/usr/bin/bwrap")
        assert "--timeout-seconds" in command and command[command.index("--timeout-seconds") + 1] == "3600"
        assert command[command.index("--cwd") + 1] == str(WORK)
        assert "/usr/bin/fakeroot" not in command
    args = dkms_args("build")
    assert all(option in args for option in ("--dkmstree", "--sourcetree", "--installtree", "--kernelsourcedir"))
    assert "--force" not in args and "--all" not in args
    eligible = {"status": "blocked", "stage": "wait-kernel", "guard_pid": None, "limits_per_stage": LIMITS,
                "finish_reason": "RuntimeError: kernel pipeline stage failed: validate-archives; no retries or limit changes",
                "stages": [{"name": name, "status": "pending"} for name in STAGES]}
    resume_eligible(eligible)
    for change in ({"status": "running"}, {"stage": "nvidia-build"}, {"guard_pid": 123},
                   {"finish_reason": "low_memory"}, {"resume_history": [{}]},
                   {"limits_per_stage": {**LIMITS, "timeout_seconds": 7200}},
                   {"stages": [{"name": name, "status": "failed"} for name in STAGES]}):
        try:
            resume_eligible({**eligible, **change})
        except RuntimeError:
            pass
        else:
            raise AssertionError("unsafe kernel-fix resume accepted")
    print("self-test: command-line, archive, DKMS, guard and no-stage/no-resource-bypass resume gates passed")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--resume-after-kernel-fix", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--stage", choices=STAGES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() == 0 or os.getuid() == 0:
        parser.error("ordinary user only; never run the helper or resource guard in fakeroot/root")
    if SCRIPT.resolve() != Path(__file__).resolve() or ROOT.resolve() != ROOT:
        parser.error("helper must remain in the explicitly authorized workspace")
    os.umask(0o077)
    if args.self_test:
        return self_test()
    if args.start:
        return start()
    if args.resume_after_kernel_fix:
        return resume_after_kernel_fix()
    if args.run:
        return supervise()
    if args.stage:
        execute_stage(args.stage)
        return 0
    if STATE.exists():
        state = load(STATE)
        print(json.dumps({key: state.get(key) for key in ("pid", "status", "stage", "guard_pid", "waiting_for",
                         "updated_at_utc", "finish_reason", "log", "artifacts")}, indent=2))
    else:
        print('{"status": "not_started"}')
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"prepare-nvidia-uki: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
