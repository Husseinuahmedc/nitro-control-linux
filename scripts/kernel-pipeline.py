#!/usr/bin/env python3
"""Detached, fail-closed continuation of the existing unprivileged kernel build."""

import argparse
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time

import kernel_reboot_recovery as recovery


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/kernel-pipeline.py"
TREE = ROOT / "build/makepkg/linux-nitro-test/src/linux-7.2.9"
STATE = ROOT / "plan/kernel-pipeline-state.json"
GUARD = ROOT / "scripts/resource-guard.py"
RECIPE = ROOT / "packaging/PKGBUILD"
KREL = "7.2.9-arch1-1-nitro-test"
PKGVER = "7.2.9.arch1-1"
CONFIG_SHA = "37e9b95c827d45cfb16b26bfdd236dd3b73f1064b6cb10c2955f59bd527e39ad"
OBJECT_SHA = "f85aeaf862734af84361205e4c355b1108e29ce9b7f0b495f610b2c63fe8a8ea"
RECIPE_SHA = "012c2b324245999fb2bf95e94d3e0bd980faf9fd1b3b8b746f14cf43e86997f0"
LIMITS = {"make_jobs": 2, "min_free_gib": 12, "min_mem_gib": 2,
          "timeout_seconds": 3600, "poll_seconds": 0.5}
VALIDATION_REPAIR_SCRIPT_SHA = "2e6327b7945726d74c4a93a7c2e1b970989975770d47a314d22204c2d7474e3f"
VALIDATION_REPAIR_ARCHIVE_SHAS = {
    "linux-nitro-test": "d7144aa697274310bc2e97ee4d68540413cc05a1c9a00338da1baef42ab7f1b3",
    "linux-nitro-test-headers": "e5af250648c0ba56b979623e91d132b82e162368f80bca0a565cb3bc481c7065",
}

# Fixed before launching: these are subsystem boundaries, never timeout retries.
STAGES = [
    ("vmlinux-btf-link", "make", ["vmlinux"]),
    ("gpu-amdgpu", "make", ["drivers/gpu/drm/amd/amdgpu/"]),
    ("gpu-i915", "make", ["drivers/gpu/drm/i915/"]),
    ("gpu-xe", "make", ["drivers/gpu/drm/xe/"]),
    ("gpu-nouveau", "make", ["drivers/gpu/drm/nouveau/"]),
    ("gpu-remaining", "make", ["drivers/gpu/"]),
    ("net-ethernet", "make", ["drivers/net/ethernet/"]),
    ("net-wireless", "make", ["drivers/net/wireless/"]),
    ("net-drivers-remaining", "make", ["drivers/net/"]),
    ("media", "make", ["drivers/media/"]),
    ("scsi", "make", ["drivers/scsi/"]),
    ("usb", "make", ["drivers/usb/"]),
    ("infiniband", "make", ["drivers/infiniband/"]),
    ("staging", "make", ["drivers/staging/"]),
    ("iio", "make", ["drivers/iio/"]),
    ("sound", "make", ["sound/"]),
    ("drivers-remaining", "make", ["drivers/"]),
    ("filesystems", "make", ["fs/"]),
    ("network-stack", "make", ["net/"]),
    ("remaining-core", "make", ["arch/x86/", "block/", "crypto/", "io_uring/",
                                 "kernel/", "lib/", "mm/", "rust/", "security/", "virt/"]),
    ("modules-final", "make", ["modules"]),
    ("kernel-image", "make", ["bzImage"]),
    ("bpftool-vmlinux-header", "make", ["-C", "tools/bpf/bpftool", "vmlinux.h",
                                         "feature-clang-bpf-co-re=1"]),
    ("package-kernel", "package", ["linux-nitro-test"]),
    ("package-headers", "package", ["linux-nitro-test-headers"]),
    ("validate-archives", "validate", []),
    ("compatibility-uki-inputs", "stage-inputs", []),
]


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_json(path, data):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("x", encoding="ascii") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    recovery.sync_directory(path.parent)


def save(state):
    state["updated_at_utc"] = now()
    atomic_json(STATE, state)


def environment():
    return {
        "PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LC_ALL": "C", "TZ": "UTC",
        "TMPDIR": str(ROOT / "build/tmp"), "XDG_CACHE_HOME": str(ROOT / "build/cache"),
        "GNUPGHOME": str(ROOT / "build/gnupg"), "PYTHONDONTWRITEBYTECODE": "1",
        "MAKEFLAGS": "-j2", "SOURCE_DATE_EPOCH": "1791027808",
        "KBUILD_BUILD_HOST": "archlinux", "KBUILD_BUILD_USER": "linux-nitro-test",
        "KBUILD_BUILD_TIMESTAMP": subprocess.check_output(
            ["/usr/bin/date", "-Ru", "-d", "@1791027808"],
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"}, text=True).strip(),
        "ZSTD_NBTHREADS": "2",
    }


def object_stat():
    item = (TREE / "vmlinux.o").stat()
    return {key: getattr(item, key) for key in
            ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}


def verify_pins(state, *, full_object=False):
    for path in (TREE / ".config", ROOT / "packaging/config.x86_64"):
        if sha256(path) != CONFIG_SHA:
            raise RuntimeError(f"stock configuration pin mismatch: {path}")
    if sha256(RECIPE) != RECIPE_SHA:
        raise RuntimeError("authoritative PKGBUILD changed; ask main before proceeding")
    for path, expected in state["script_pins"].items():
        if sha256(ROOT / path) != expected:
            raise RuntimeError(f"pipeline/guard/helper changed since declaration: {path}")
    for path, expected in state.get("input_pins", {}).items():
        if sha256(ROOT / path) != expected:
            raise RuntimeError(f"prepared input changed: {path}")
    for path, expected in state.get("archive_pins", {}).items():
        if sha256(ROOT / path) != expected:
            raise RuntimeError(f"completed package changed: {path}")
    for relative in ("version", "include/config/kernel.release"):
        if (TREE / relative).read_text().strip() != KREL:
            raise RuntimeError(f"release mismatch in {relative}")
    if (TREE / "include/generated/utsrelease.h").read_text().strip() != f'#define UTS_RELEASE "{KREL}"':
        raise RuntimeError("UTS_RELEASE mismatch")
    if object_stat() != state["checkpoint_stat"]:
        raise RuntimeError("vmlinux.o checkpoint changed; refusing an unapproved rebuild")
    if full_object and sha256(TREE / "vmlinux.o") != OBJECT_SHA:
        raise RuntimeError("vmlinux.o checksum mismatch")
    return {"config_sha256": CONFIG_SHA, "kernel_release": KREL,
            "checkpoint_stat_unchanged": True, "checked_at_utc": now()}


def workdir(state):
    return ROOT / "build/kernel-pipeline" / state["run_id"]


def archive_path(package):
    return ROOT / "packages" / f"{package}-{PKGVER}-x86_64.pkg.tar.zst"


def run(command, *, cwd=TREE, env=None):
    print("+ " + shlex.join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), cwd=cwd, env=env or environment(),
                   stdin=subprocess.DEVNULL, check=True)


def package_stage(state, package):
    destination = archive_path(package)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"refusing to overwrite existing package: {destination}")
    directory = workdir(state) / package
    directory.mkdir()
    (directory / "src").symlink_to(TREE.parent, target_is_directory=True)
    (directory / "PKGBUILD").symlink_to(ROOT / "scripts/kernel-pipeline.PKGBUILD")
    env = environment()
    env.update(NITRO_PIPELINE_ROOT=str(ROOT), NITRO_PIPELINE_PACKAGE=package,
               BUILDDIR=str(directory), PKGDEST=str(ROOT / "packages"),
               SRCDEST=str(ROOT / "downloads"), SRCPKGDEST=str(ROOT / "packages"),
               LOGDEST=str(ROOT / "logs"))
    run(["/usr/bin/makepkg", "--repackage", "--noextract", "--noprepare", "--nocheck",
         "--nosign", "--nocolor", "--config", ROOT / "scripts/kernel-pipeline.makepkg.conf"],
        cwd=directory, env=env)
    if not destination.is_file():
        raise RuntimeError(f"missing archive after makepkg: {destination}")
    return {package: str(destination), package + "-pkgdir": str(directory / "pkg" / package)}


def safe_member(member, package):
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts or member.isdev() or member.isfifo():
        raise RuntimeError(f"unsafe archive member: {member.name}")
    name = str(path)
    if name in (".", ".PKGINFO", ".BUILDINFO", ".MTREE"):
        if not (member.isdir() if name == "." else member.isfile()):
            raise RuntimeError(f"invalid archive metadata type: {name}")
        return name
    base = f"usr/lib/modules/{KREL}"
    allowed = name in ("usr", "usr/lib", "usr/lib/modules", base) or name.startswith(base + "/")
    if package.endswith("-headers"):
        allowed |= name in ("usr/src", "usr/src/linux-nitro-test")
    if not allowed or path.name.upper() == ".INSTALL":
        raise RuntimeError(f"unexpected package payload (boot/etc/hooks/stock aliases forbidden): {name}")
    if member.issym() or member.islnk():
        target = PurePosixPath(member.linkname)
        if target.is_absolute():
            raise RuntimeError(f"absolute package link: {name} -> {target}")
        resolved = os.path.normpath(str(path.parent / target) if member.issym() else str(target))
        if not (resolved == base or resolved.startswith(base + "/")):
            raise RuntimeError(f"package link escapes the unique kernel tree: {name} -> {target}")
    return name


def validate_archives(state):
    report = {"kernel_release": KREL, "config_sha256": CONFIG_SHA, "packages": {}}
    prefix = f"usr/lib/modules/{KREL}"
    for package in ("linux-nitro-test", "linux-nitro-test-headers"):
        archive = archive_path(package)
        names, module_names, metadata, selected = set(), set(), {}, {}
        selected_paths = {f"{prefix}/pkgbase", f"{prefix}/vmlinuz", f"{prefix}/build/version",
                          f"{prefix}/build/.config", f"{prefix}/build/include/config/kernel.release",
                          f"{prefix}/build/vmlinux.h"}
        with subprocess.Popen(["/usr/bin/zstd", "-dc", str(archive)],
                              stdout=subprocess.PIPE, env=environment()) as decompressor:
            try:
                with tarfile.open(fileobj=decompressor.stdout, mode="r|") as stream:
                    for member in stream:
                        name = safe_member(member, package)
                        if name in names:
                            raise RuntimeError(f"duplicate archive member: {name}")
                        names.add(name)
                        if name.endswith(".ko.zst"):
                            module_names.add(name)
                        if name in (".PKGINFO", ".BUILDINFO"):
                            if not member.isfile() or member.size > 1024 * 1024:
                                raise RuntimeError(f"invalid package metadata: {name}")
                            metadata[name] = stream.extractfile(member).read().decode("utf-8")
                        if name in selected_paths:
                            if not member.isfile():
                                raise RuntimeError(f"expected regular file: {name}")
                            selected[name] = hashlib.file_digest(stream.extractfile(member), "sha256").hexdigest()
                        if name == "usr/src/linux-nitro-test":
                            if not member.issym() or os.path.normpath("usr/src/" + member.linkname) != prefix + "/build":
                                raise RuntimeError("headers source alias does not point to the unique build directory")
                decompressor.stdout.close()
                if decompressor.wait() != 0:
                    raise RuntimeError("archive decompression failed")
            finally:
                if decompressor.poll() is None:
                    decompressor.terminate()
                    decompressor.wait()
        fields = {}
        for line in metadata.get(".PKGINFO", "").splitlines():
            key, separator, value = line.partition(" = ")
            if separator:
                fields.setdefault(key, []).append(value)
        for key, value in (("pkgname", package), ("pkgbase", "linux-nitro-test"),
                           ("pkgver", PKGVER), ("arch", "x86_64")):
            if fields.get(key) != [value]:
                raise RuntimeError(f"archive internal {key} mismatch: {archive}")
        for key in ("provides", "replaces", "conflict"):
            if any(re.split(r"[<>=]", value)[0] in ("linux", "linux-headers")
                   for value in fields.get(key, [])):
                raise RuntimeError(f"stock alias in archive metadata: {key}")
        if package.endswith("-headers"):
            required = {prefix + "/build/Module.symvers", prefix + "/build/vmlinux",
                        prefix + "/build/vmlinux.h", "usr/src/linux-nitro-test"}
            expected_hashes = {prefix + "/build/.config": CONFIG_SHA,
                               prefix + "/build/vmlinux.h": sha256(TREE / "tools/bpf/bpftool/vmlinux.h"),
                               **{prefix + "/build/" + relative: sha256(TREE / relative)
                                  for relative in ("version", "include/config/kernel.release")}}
            missing = sorted((required | expected_hashes.keys()) - names)
            mismatched = {name: {"expected": expected, "actual": selected.get(name)}
                          for name, expected in expected_hashes.items()
                          if name in names and selected.get(name) != expected}
            if missing or mismatched:
                raise RuntimeError("headers archive validation failed: " + json.dumps(
                    {"missing": missing, "hash_mismatches": mismatched}, sort_keys=True))
            if not any(name.startswith(prefix + "/build/rust/") and name.endswith(".rmeta") for name in names):
                raise RuntimeError("Rust metadata absent from headers archive")
        else:
            expected_modules = {prefix + "/kernel/" + str(PurePosixPath(line).with_suffix(".ko.zst"))
                                for line in (TREE / "modules.order").read_text().splitlines() if line}
            if not expected_modules or module_names != expected_modules:
                raise RuntimeError("archive modules do not exactly match modules.order")
            if selected.get(prefix + "/vmlinuz") != sha256(TREE / "arch/x86/boot/bzImage"):
                raise RuntimeError("archive kernel image does not match the completed image")
            if selected.get(prefix + "/pkgbase") != hashlib.sha256(b"linux-nitro-test\n").hexdigest():
                raise RuntimeError("kernel archive pkgbase mismatch")
            if any(name.startswith(prefix + "/build") for name in names):
                raise RuntimeError("kernel binary archive unexpectedly contains a build link")
        report["packages"][package] = {"path": str(archive), "sha256": sha256(archive),
                                        "members": len(names), "modules": len(module_names),
                                        "selected_file_sha256": selected,
                                        "pkginfo": fields, "buildinfo": metadata.get(".BUILDINFO")}
    sections = subprocess.check_output(["/usr/bin/readelf", "-SW", TREE / "vmlinux"], text=True)
    if not all(re.search(r"\s" + re.escape(section) + r"\s", sections)
               for section in (".BTF", ".debug_info")):
        raise RuntimeError("completed vmlinux lost BTF or DWARF")
    module = (workdir(state) / "linux-nitro-test/pkg/linux-nitro-test" / prefix /
              "kernel/drivers/platform/x86/acer-wmi.ko.zst")
    vermagic = subprocess.check_output(["/usr/bin/modinfo", "-F", "vermagic", module], text=True).strip()
    if vermagic.split()[0] != KREL:
        raise RuntimeError("packaged acer-wmi module vermagic mismatch")
    report.update(acer_wmi_vermagic=vermagic, validated_at_utc=now())
    path = workdir(state) / "archive-validation.json"
    atomic_json(path, report)
    return {"archive-validation": str(path)}


def stage_inputs(state):
    validation = json.loads((workdir(state) / "archive-validation.json").read_text())
    for data in validation["packages"].values():
        if sha256(data["path"]) != data["sha256"]:
            raise RuntimeError("validated package changed before input staging")
    destination = ROOT / "build/boot" / (KREL + "-" + state["run_id"])
    destination.mkdir()
    inputs = {"vmlinuz": TREE / "arch/x86/boot/bzImage", "config": TREE / ".config",
              "System.map": TREE / "System.map", "cmdline": ROOT / "plan/test-kernel.cmdline"}
    manifest = {"kernel_release": KREL, "inputs": {}, "packages": validation["packages"],
                "uki_status": "inputs-only; initramfs/NVIDIA compatibility and UKI assembly deferred to main",
                "system_installation": False, "bootable_or_runtime_validated": False,
                "headers_build": str(workdir(state) / "linux-nitro-test-headers/pkg/linux-nitro-test-headers" /
                                     f"usr/lib/modules/{KREL}/build")}
    for name, source in inputs.items():
        shutil.copyfile(source, destination / name)
        manifest["inputs"][name] = {"path": str(destination / name), "sha256": sha256(destination / name)}
    cmdline = (destination / "cmdline").read_text().split()
    if "acer_wmi.predator_v4=0" not in cmdline:
        raise RuntimeError("local UKI inputs must retain the explicit fan-feature opt-out")
    path = destination / "manifest.json"
    atomic_json(path, manifest)
    return {"local-uki-inputs": str(destination), "compatibility-manifest": str(path)}


def execute_stage(name):
    state = json.loads(STATE.read_text())
    if state["status"] != "running" or state["stage"] != name:
        raise RuntimeError("stage must be selected by the active pipeline")
    if "boot" in state:
        deadline = time.monotonic() + 10
        identity = recovery.process_identity(os.getpid())
        while True:
            state = json.loads(STATE.read_text())
            stage = next(item for item in state["stages"] if item["name"] == name)
            if stage.get("child_identity") == identity and state["status"] == "running":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("durable child identity handshake was not completed")
            time.sleep(0.1)
    before = verify_pins(state, full_object=name == STAGES[0][0])
    _, kind, targets = next(stage for stage in STAGES if stage[0] == name)
    artifacts = {}
    if "boot" in state and stage.get("recovery_scan_required"):
        artifacts["reboot-object-inspection"] = recovery.scan_generated(sys.modules[__name__], state, stage)
    if kind == "make":
        run(["/usr/bin/make", "-j2", *targets])
        if name == "vmlinux-btf-link":
            artifacts = {file: str(TREE / file) for file in ("vmlinux", "vmlinux.unstripped", "System.map")}
        elif name == "kernel-image":
            artifacts = {"bzImage": str(TREE / "arch/x86/boot/bzImage")}
        elif name == "modules-final":
            artifacts = {file: str(TREE / file) for file in ("Module.symvers", "modules.order")}
        elif name == "bpftool-vmlinux-header":
            artifacts = {"vmlinux.h": str(TREE / "tools/bpf/bpftool/vmlinux.h")}
    elif kind == "package":
        artifacts = package_stage(state, targets[0])
    elif kind == "validate":
        artifacts = validate_archives(state)
    elif kind == "stage-inputs":
        artifacts = stage_inputs(state)
    for path in artifacts.values():
        if not Path(path).exists():
            raise RuntimeError(f"missing stage artifact: {path}")
    release = subprocess.check_output(["/usr/bin/make", "-j2", "-s", "kernelrelease"],
                                      cwd=TREE, env=environment(), text=True).strip()
    if release != KREL:
        raise RuntimeError(f"make kernelrelease mismatch: {release}")
    after = verify_pins(state)
    atomic_json(workdir(state) / "results" / (name + ".json"),
                {"before": before, "after": after, "artifact_paths": artifacts,
                 "completed_at_utc": now()})


def guard_summary(path):
    if not Path(path).is_file():
        return None
    for line in reversed(Path(path).read_text().splitlines()):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("event") == "summary":
            return item
    return None


def accept_result(state, stage, summary, code):
    stage["guard_result"] = summary
    result = workdir(state) / "results" / (stage["name"] + ".json")
    pin_error = None
    try:
        stage["checks_after"] = verify_pins(state)
    except (OSError, RuntimeError) as error:
        pin_error = str(error)
        stage["checks_after"] = {"error": pin_error, "checked_at_utc": now()}
    success = (code == 0 and summary is not None and summary.get("exit_reason") == "child_exit"
               and summary.get("child_exit_code") == 0 and summary.get("exit_code") == 0
               and result.is_file() and pin_error is None)
    stage.update(status="succeeded" if success else "failed", finished_at_utc=now())
    state["guard_pid"] = state["child_pid"] = None
    state["last_result"] = {"stage": stage["name"], "returncode": code, "guard": summary}
    if success:
        stage["result"] = json.loads(result.read_text())
        state["artifact_paths"].update(stage["result"]["artifact_paths"])
        state["status"] = "ready"
    else:
        reason = summary.get("exit_reason", "missing_guard_summary") if summary else "missing_guard_summary"
        if pin_error:
            reason += "; " + pin_error
        state.update(status="stopped", finish_reason=f"{stage['name']}: {reason}; exit={code}; no automatic retry")
    save(state)
    return success


def new_state(console):
    helpers = [SCRIPT, GUARD, ROOT / "scripts/kernel-pipeline.PKGBUILD",
               ROOT / "scripts/kernel-pipeline.makepkg.conf", ROOT / "scripts/kernel_reboot_recovery.py"]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    boot = recovery.boot_identity()
    return {"schema_version": 2, "run_id": run_id, "status": "ready", "stage": None,
            "boot": boot, "pipeline_identity": recovery.process_identity(os.getpid(), boot),
            "pipeline_pid": os.getpid(), "guard_pid": None, "child_pid": None,
            "started_at_utc": now(), "finish_reason": None, "last_result": None,
            "logs": {"pipeline": str(console)}, "limits_per_stage": LIMITS,
            "environment": environment(), "kernel_release": KREL, "config_sha256": CONFIG_SHA,
            "checkpoint_sha256": OBJECT_SHA, "checkpoint_stat": object_stat(),
            "recipe_sha256": RECIPE_SHA, "artifact_paths": {},
            "script_pins": {str(path.relative_to(ROOT)): sha256(path) for path in helpers},
            "stages": [{"name": name, "kind": kind, "targets": targets, "status": "pending"}
                       for name, kind, targets in STAGES]}


def repair_validation(state, console):
    if (state.get("status"), state.get("stage")) != ("stopped", "validate-archives") or state.get("limits_per_stage") != LIMITS:
        raise RuntimeError("validation repair requires the stopped validator and original resource limits")
    if [(item["name"], item["kind"], item["targets"]) for item in state["stages"]] != STAGES:
        raise RuntimeError("validation repair cannot change the declared plan")
    index = next(index for index, item in enumerate(state["stages"]) if item["name"] == "validate-archives")
    stage = state["stages"][index]
    if stage["status"] != "failed" or stage["kind"] != "validate" or stage.get("attempts"):
        raise RuntimeError("only the original failed read-only validation attempt is eligible")
    if any(item["status"] != "succeeded" for item in state["stages"][:index]) or any(
            item["status"] != "pending" or item["kind"] != "stage-inputs" for item in state["stages"][index + 1:]):
        raise RuntimeError("validation repair must not rerun any build or packaging stage")
    boot = recovery.boot_identity()
    if state["boot"]["boot_id"] != boot["boot_id"]:
        raise RuntimeError("validation repair is not reboot recovery")
    for pid in (state["pipeline_pid"], stage["guard_pid"], stage["child_pid"]):
        if type(pid) is not int or pid <= 1 or recovery.process_identity(pid) is not None:
            raise RuntimeError(f"previous validation owner is still present or uncertain: {pid}")
    summary = stage.get("guard_result", {})
    if not (summary.get("exit_reason") == "child_exit" and summary.get("exit_code") == 1
            and summary.get("child_exit_code") == 1 and summary.get("received_signal") is None
            and summary.get("minimum_free_bytes", -1) >= 12 * 1024**3
            and summary.get("minimum_mem_available_bytes", -1) >= 2 * 1024**3):
        raise RuntimeError("resource stops, timeouts, signals and unknown failures are not repairable")
    if state.get("last_result") != {"stage": stage["name"], "returncode": 1, "guard": summary}:
        raise RuntimeError("failed validation result is inconsistent with state")
    for key in ("resources", "summary"):
        if guard_summary(stage["logs"][key]) != summary:
            raise RuntimeError("validation repair needs matching durable guard summaries")
        for line in Path(stage["logs"][key]).read_text().splitlines():
            event = json.loads(line)
            if event.get("event") == "summary" and event != summary:
                raise RuntimeError("another terminal result exists; refusing validation repair")
            if event.get("event") == "sample" and (event["free_bytes"] < 12 * 1024**3
                                                     or event["mem_available_bytes"] < 2 * 1024**3):
                raise RuntimeError("resource event exists; refusing validation repair")
    diagnostic = "kernel-pipeline: incomplete headers archive or changed configuration"
    lines = Path(stage["logs"]["output"]).read_text().splitlines()
    if lines.count(diagnostic) != 1 or any(line not in (
            diagnostic, "Warning : decompression does not support multi-threading") for line in lines):
        raise RuntimeError("not the recognized flattened-header checker bug")
    duration = summary.get("duration_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration < 3600:
        raise RuntimeError("validation's consumed budget is uncertain or exhausted")
    charged = math.ceil(duration)
    remaining = 3600 - charged
    if remaining <= 0:
        raise RuntimeError("no validation budget remains")
    script_key = str(SCRIPT.relative_to(ROOT))
    if state["script_pins"].get(script_key) != VALIDATION_REPAIR_SCRIPT_SHA:
        raise RuntimeError("unknown original validator fingerprint; no self-repin authorized")
    candidate = copy.deepcopy(state)
    candidate["script_pins"][script_key] = sha256(SCRIPT)
    candidate["archive_pins"] = {str(archive_path(package).relative_to(ROOT)): expected
                                 for package, expected in VALIDATION_REPAIR_ARCHIVE_SHAS.items()}
    verified = verify_pins(candidate, full_object=True)
    for completed in state["stages"][:index]:
        result = completed.get("guard_result", {})
        if not (result.get("exit_reason") == "child_exit" and result.get("exit_code") == 0
                and result.get("child_exit_code") == 0):
            raise RuntimeError("an earlier stage did not complete successfully")
        if json.loads((workdir(state) / "results" / (completed["name"] + ".json")).read_text()) != completed["result"]:
            raise RuntimeError(f"completed result changed: {completed['name']}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    directory = ROOT / "plan/kernel-pipeline-validation-repairs" / stamp
    directory.mkdir(parents=True)
    recovery.sync_directory(directory.parent)
    evidence = {}
    for source in {STATE, Path(state["logs"]["pipeline"]), *(Path(path) for path in stage["logs"].values())}:
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
            raise RuntimeError("unsafe validation repair evidence path")
        destination = directory / "original" / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        recovery.sync_file(destination)
        recovery.sync_directory(destination.parent)
        evidence[str(source.relative_to(ROOT))] = {"snapshot": str(destination), "sha256": sha256(destination)}
        if sha256(source) != sha256(destination):
            raise RuntimeError("validation repair evidence changed while snapshotting")
    packages = {relative: {"sha256": expected, "bytes": (ROOT / relative).stat().st_size,
                          "mtime_ns": (ROOT / relative).stat().st_mtime_ns,
                          "inode": (ROOT / relative).stat().st_ino}
                for relative, expected in candidate["archive_pins"].items()}
    record = {"event": "explicit_validation_checker_repair", "recorded_at_utc": now(),
              "reason": "official headers function installs generated vmlinux.h at build root",
              "old_script_sha256": VALIDATION_REPAIR_SCRIPT_SHA, "new_script_sha256": candidate["script_pins"][script_key],
              "previous_guard_duration_seconds": duration, "charged_seconds": charged,
              "remaining_timeout_seconds": remaining, "budget_seconds": 3600,
              "packages": packages, "verified_pins": verified, "original_evidence": evidence}
    record_path = directory / "repair.json"
    atomic_json(record_path, record)
    failed_attempt = copy.deepcopy(stage)
    repaired_stage = candidate["stages"][index]
    for key in tuple(repaired_stage):
        if key not in ("name", "kind", "targets"):
            repaired_stage.pop(key)
    repaired_stage.update(status="pending", attempts=[failed_attempt],
                          original_started_at_utc=stage["started_at_utc"],
                          validation_remaining_timeout_seconds=remaining, validation_repair_record=str(record_path))
    candidate.update(status="ready", finish_reason=None, pipeline_pid=os.getpid(), guard_pid=None, child_pid=None,
                     pipeline_identity=recovery.process_identity(os.getpid(), boot),
                     log_namespace=state["run_id"] + "-validation-repair-" + stamp,
                     validation_repair_history=[*state.get("validation_repair_history", []),
                                                {**record, "record": str(record_path)}],
                     logs={**state["logs"], "previous_pipeline": state["logs"]["pipeline"], "pipeline": str(console)})
    verify_pins(candidate)
    save(candidate)
    print(f"{now()} VALIDATION REPAIR {charged}s charged, {remaining}s remaining; evidence {record_path}", flush=True)
    return candidate


def supervise(console, resume, recover_reboot=False, validation_repair=False):
    with (ROOT / "plan/kernel-pipeline.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if STATE.exists():
            if validation_repair:
                state = repair_validation(json.loads(STATE.read_text()), console)
            elif recover_reboot:
                state = recovery.recover_reboot(sys.modules[__name__], json.loads(STATE.read_text()), console)
            elif not resume:
                raise RuntimeError("state already exists; do not launch a duplicate build")
            else:
                state = json.loads(STATE.read_text())
                if state.get("boot", {}).get("boot_id") != recovery.boot_identity()["boot_id"]:
                    raise RuntimeError("different/unknown boot: use explicit --recover-reboot, not --resume")
                if state["status"] in ("stopped", "completed") or any(
                        stage["status"] == "failed" for stage in state["stages"]):
                    raise RuntimeError("stopped/failed/completed pipeline cannot be retried with --resume")
                verify_pins(state)
                for stage in state["stages"]:
                    if stage["status"] == "running":
                        summary = guard_summary(stage["logs"]["resources"])
                        if not accept_result(state, stage, summary, summary.get("exit_code", 125) if summary else 125):
                            return 1
                state["pipeline_pid"] = os.getpid()
                state["pipeline_identity"] = recovery.process_identity(os.getpid(), state["boot"])
                state["logs"]["previous_pipeline"] = state["logs"]["pipeline"]
                state["logs"]["pipeline"] = str(console)
        else:
            if resume or recover_reboot or validation_repair:
                raise RuntimeError("there is no pipeline to resume")
            state = new_state(console)
            verify_pins(state)
            (workdir(state) / "results").mkdir(parents=True)
        save(state)
        requested_signal = 0
        guard = None

        def stop(signum, frame):
            nonlocal requested_signal
            requested_signal = requested_signal or signum
            if guard is not None and guard.poll() is None:
                guard.send_signal(signal.SIGTERM)

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, stop)
        try:
            for stage in state["stages"]:
                if stage["status"] == "succeeded":
                    continue
                if requested_signal:
                    state.update(status="ready", finish_reason="signal between stages; explicit --resume required")
                    save(state)
                    return 128 + requested_signal
                stage["checks_before"] = verify_pins(state)
                name = stage["name"]
                base = ROOT / "logs" / f"kernel-pipeline-{state.get('log_namespace', state['run_id'])}-{name}"
                logs = {"resources": str(base) + ".jsonl", "output": str(base) + ".output.log",
                        "summary": str(base) + ".summary.log"}
                timeout = stage.get("validation_remaining_timeout_seconds", stage.get("reboot_remaining_timeout_seconds", 3600))
                if type(timeout) is not int or not 0 < timeout <= 3600:
                    raise RuntimeError("invalid stage timeout; cannot bypass the original stage budget")
                command = ["/usr/bin/python", str(GUARD), "--run", "--cwd", str(TREE),
                           "--min-free-gib", "12", "--min-mem-gib", "2", "--timeout-seconds",
                           str(timeout),
                           "--poll-seconds", "0.5", "--log", logs["resources"], "--output-log", logs["output"],
                           "--", "/usr/bin/python", str(SCRIPT), "--stage", name]
                stage.update(status="running", started_at_utc=now(), logs=logs, guard_argv=command)
                stage.update(boot=recovery.boot_identity(), monotonic_started_seconds=time.monotonic(),
                             boottime_started_seconds=time.clock_gettime(time.CLOCK_BOOTTIME))
                state.update(status="running", stage=name, logs={**state["logs"], **logs}, finish_reason=None)
                save(state)
                print(f"{now()} START {name}", flush=True)
                with Path(logs["summary"]).open("x") as output:
                    # The guard retains the lock if the supervisor is killed abruptly.
                    guard = subprocess.Popen(command, cwd=TREE, env=environment(), stdin=subprocess.DEVNULL,
                                             stdout=output, stderr=subprocess.STDOUT, pass_fds=(lock.fileno(),))
                    state["guard_pid"] = guard.pid
                    stage["guard_pid"] = guard.pid
                    stage["guard_identity"] = recovery.process_identity(guard.pid, stage["boot"])
                    save(state)
                    last_sync = time.monotonic()
                    while guard.poll() is None:
                        if state["child_pid"] is None and Path(logs["resources"]).exists():
                            with Path(logs["resources"]).open() as events:
                                for line in events:
                                    try:
                                        event = json.loads(line)
                                    except ValueError:
                                        continue
                                    if event.get("event") == "spawn":
                                        state["child_pid"] = stage["child_pid"] = event["child_pid"]
                                        stage["child_identity"] = recovery.process_identity(event["child_pid"], stage["boot"])
                                        for path in logs.values():
                                            if Path(path).is_file():
                                                recovery.sync_file(path)
                                        recovery.sync_directory(ROOT / "logs")
                                        save(state)
                                        break
                        if time.monotonic() - last_sync >= 10:
                            for path in logs.values():
                                if Path(path).is_file():
                                    recovery.sync_file(path)
                            last_sync = time.monotonic()
                        time.sleep(0.5)
                    code = guard.returncode
                guard = None
                for path in logs.values():
                    if Path(path).is_file():
                        recovery.sync_file(path)
                summary = guard_summary(logs["resources"])
                if not accept_result(state, stage, summary, code):
                    print(f"{now()} STOP {state['finish_reason']}", flush=True)
                    return code or 1
                print(f"{now()} SUCCEEDED {name}", flush=True)
            state.update(status="completed", stage=None, finish_reason="all declared safe stages completed",
                         finished_at_utc=now())
            save(state)
            print(f"{now()} COMPLETED; no installation, NVIDIA activation or UKI publication", flush=True)
            return 0
        except BaseException as exc:
            if guard is not None and guard.poll() is None:
                guard.send_signal(signal.SIGTERM)
                guard.wait()
            state.update(status="stopped", guard_pid=None, child_pid=None,
                         finish_reason=f"supervisor error: {type(exc).__name__}: {exc}; no automatic retry",
                         last_result={"stage": state["stage"], "error": str(exc)})
            save(state)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--start", action="store_true", help="detach a new pipeline")
    mode.add_argument("--resume", action="store_true", help="detach continuation after successful stages only")
    mode.add_argument("--recover-reboot", action="store_true", help="explicitly recover a proven reboot within the original stage budget")
    mode.add_argument("--repair-validation", action="store_true", help="explicitly repair the recognized read-only header-checker failure")
    mode.add_argument("--status", action="store_true", help="print current state (default)")
    mode.add_argument("--plan", action="store_true", help="print fixed stages without starting work")
    mode.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--stage", choices=[stage[0] for stage in STAGES], help=argparse.SUPPRESS)
    parser.add_argument("--continue-completed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--reboot-recovery-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validation-repair-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--console-log", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("ordinary user only; no sudo")
    if args.plan:
        print(json.dumps({"limits_per_stage": LIMITS, "stages": STAGES}, indent=2))
    elif args.stage:
        execute_stage(args.stage)
    elif args.run:
        if args.console_log is None or args.console_log.parent.resolve() != ROOT / "logs":
            parser.error("worker requires a workspace console log")
        return supervise(args.console_log, args.continue_completed, args.reboot_recovery_worker, args.validation_repair_worker)
    elif args.start or args.resume or args.recover_reboot or args.repair_validation:
        if args.start and STATE.exists():
            parser.error("state already exists; no duplicate build and no automatic retries")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        console = ROOT / "logs" / f"kernel-pipeline-{stamp}.log"
        with console.open("x") as output:
            command = ["/usr/bin/python", str(SCRIPT), "--run", "--console-log", str(console)]
            if args.resume:
                command.append("--continue-completed")
            if args.recover_reboot:
                command.append("--reboot-recovery-worker")
            if args.repair_validation:
                command.append("--validation-repair-worker")
            child = subprocess.Popen(command, cwd=ROOT, env=environment(), stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        print(json.dumps({"pipeline_pid": child.pid, "state": str(STATE), "pipeline_log": str(console)}))
    else:
        print(STATE.read_text() if STATE.exists() else '{"status": "not_started"}')
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"kernel-pipeline: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
