"""Administrator-only, hash-authorized deployment. No GUI/backend IPC surface.

Original project code, GPL-3.0-or-later. Preflight never creates files or locks.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import time


KREL = "7.2.9-arch1-1-nitro-test"
STOCK = "7.2.9-arch1-1"
DRIVER = "615.78.08"
VERSION = "7.2.9.arch1-1"
CONFIG_SHA256 = "37e9b95c827d45cfb16b26bfdd236dd3b73f1064b6cb10c2955f59bd527e39ad"
STOCK_UKI_SHA256 = "495b147276e33d253fdc71460853a97ecacb0e83740668c1cd1b558e2fac5925"
DKMS_SHA256 = "165cfb35ac92e6dc7ad47a49da352d4f7ba49f2a10d7a73979797313149fd61f"
NVIDIA_CONF_SHA256 = "cc553802e1bc47e9464cf1c458cfb71bed1c47f5a8c981f3b7f4f7e6d94890de"
SIGNING_CERT_SHA256 = "aef06000e5edf2cf2d91b69d440248ecf137818d7c5438f00e8bdd4943cc076b"
SIGNING_KEY = "09:52:C9:19:B3:CC:45:AA:F5:F8:5D:32:2E:06:6A:04:E4:69:3B:83"
PACKAGES = ("linux-nitro-test", "linux-nitro-test-headers")
MODULES = ("nvidia", "nvidia-modeset", "nvidia-uvm", "nvidia-drm", "nvidia-peermem")
TARGET = {"kernel_release": KREL, "kernel_config_sha256": CONFIG_SHA256,
          "nvidia_driver": DRIVER, "package_version": VERSION,
          "application_version": "0.1.0-9", "signing_certificate_sha256": SIGNING_CERT_SHA256,
          "module_signing_key": SIGNING_KEY}
BASE = Path("/var/lib/nitro-control")
BOOT = Path("/boot")
STOCK_UKI = BOOT / "EFI/Linux/omarchy_linux.efi"
NEW_UKI = BOOT / "EFI/Linux/omarchy_linux-nitro-test.efi"
FRAGMENT = Path("/etc/limine-entry-tool.d/90-nitro-control-test.conf")
MODROOT = Path("/usr/lib/modules") / KREL
SOURCE = Path("/usr/src") / ("nvidia-" + DRIVER)
NVTREE = Path("/var/lib/dkms/nvidia")
NVTARGET = NVTREE / DRIVER / KREL
NVLINK = NVTREE / ("kernel-" + KREL + "-x86_64")
BOOT_LOCK = Path("/run/lock/boot-partition.lock")
SECUREBOOT = Path("/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c")
APPROOT = Path("/usr/lib/nitro-control/nitro_control")
MASKS = ("10-linux-modules-pre.hook", "10-linux-modules-post.hook",
         "70-dkms-install.hook", "90-mkinitcpio-install.hook")
TOOLS = ("pacman", "pacman-conf", "dkms", "depmod", "modinfo", "zstd", "bwrap",
         "limine-entry-tool", "mkinitcpio")
PROTECTED = (
    "/boot", "/etc/pacman.conf", "/etc/pacman.d/hooks", "/etc/pacman.d/mirrorlist",
    "/etc/mkinitcpio.conf", "/etc/mkinitcpio.conf.d", "/etc/mkinitcpio.d",
    "/etc/initcpio", "/etc/kernel", "/etc/modprobe.d", "/etc/modules-load.d",
    "/etc/dkms", "/etc/limine-entry-tool.conf", "/etc/limine-entry-tool.d",
    "/etc/default/limine", "/etc/boot", "/etc/nitro-control", "/etc/systemd/system",
    "/usr/share/libalpm/hooks", "/usr/share/libalpm/scripts", "/usr/lib/initcpio",
    "/usr/lib/limine", "/usr/share/dkms", "/usr/lib/modules/" + STOCK,
    "/var/lib/dkms/nvidia", "/usr/src/nvidia-" + DRIVER + "/dkms.conf",
    "/usr/src/nvidia-" + DRIVER + "/Makefile", "/usr/src/nvidia-" + DRIVER + "/COPYING",
    "/usr/lib/nitro-control", "/usr/bin/nitro-control", "/usr/bin/nitro-control-deploy",
    "/var/lib/nitro-control/hardware-approved",
    *("/usr/bin/" + name for name in TOOLS),
)
FIXED_INPUTS = {
    "kernel_state": "plan/kernel-pipeline-state.json",
    "nvidia_state": "plan/nvidia-uki-state.json",
    "nvidia_manifest": "build/nvidia-uki/artifacts/manifest.json",
    "import_status": "build/nvidia-uki/audit/dkms-import-status.txt",
    "app_report": "plan/application-package.json",
    "cmdline": "plan/test-kernel.cmdline",
    "kernel_package": "packages/linux-nitro-test-" + VERSION + "-x86_64.pkg.tar.zst",
    "headers_package": "packages/linux-nitro-test-headers-" + VERSION + "-x86_64.pkg.tar.zst",
    "app_package": "packages/nitro-control-0.1.0-9-any.pkg.tar.zst",
    "uki": "build/nvidia-uki/boot/omarchy_linux-nitro-test.efi",
    "dkms_archive": "build/nvidia-uki/artifacts/nvidia-" + DRIVER + "-" + KREL + "-x86_64.dkms.tar.gz",
}
ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C", "TZ": "UTC",
       "TERM": "dumb", "HOME": "/root", "PYTHONDONTWRITEBYTECODE": "1"}
GIB = 1024**3


class DeploymentError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def decode_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda value: require(False, "non-finite JSON number"))
    except (ValueError, UnicodeError) as error:
        raise DeploymentError("invalid JSON") from error


def relative(value):
    require(isinstance(value, str) and value and "\x00" not in value, "invalid input path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts and str(path) == value,
            "non-canonical input path or path traversal")
    return path


@contextmanager
def input_file(path):
    """Open every component without following links, including user-owned parents."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "input must be an absolute canonical path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    stream = None
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        stream = os.fdopen(leaf, "rb")
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "input is not a regular file")
        yield stream
    finally:
        if stream is not None:
            stream.close()
        os.close(fd)


def descriptor(workspace, path):
    path = Path(path)
    rel = str(path.relative_to(workspace))
    relative(rel)
    with input_file(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": rel, "sha256": digest, "bytes": size}


def load_manifest(path, expected=None, *, apply=False):
    require(not apply or (isinstance(expected, str) and re.fullmatch("[0-9a-f]{64}", expected)),
            "--apply requires --manifest-sha256 with the operator-pinned SHA-256")
    with input_file(Path(path).absolute()) as stream:
        data = stream.read(16 * 1024**2 + 1)
    require(len(data) <= 16 * 1024**2, "manifest too large")
    digest = hashlib.sha256(data).hexdigest()
    require(expected is None or digest == expected, "manifest SHA-256 mismatch; no privileged writes")
    manifest = decode_json(data)
    require(isinstance(manifest, dict), "manifest must be a JSON object")
    require(set(manifest) == {"schema_version", "created_at_utc", "workspace", "target",
                              "artifacts", "baseline", "authorization"}, "unknown/missing manifest fields")
    require(manifest["schema_version"] == 1 and manifest["target"] == TARGET,
            "wrong release, configuration or signing fingerprint")
    require(manifest["authorization"] == "operator-sha256-local-unsigned-archives",
            "unsigned local archives require explicit hash authorization")
    workspace = Path(manifest["workspace"])
    require(workspace.is_absolute() and str(workspace) == manifest["workspace"]
            and ".." not in workspace.parts and workspace != Path("/"), "invalid workspace")
    artifacts = manifest["artifacts"]
    require(isinstance(artifacts, dict) and set(FIXED_INPUTS) <= set(artifacts)
            and "archive_validation" in artifacts, "missing deployment inputs")
    for role, item in artifacts.items():
        require(set(item) == {"path", "sha256", "bytes"}, "invalid artifact descriptor")
        path = str(relative(item["path"]))
        require(re.fullmatch("[0-9a-f]{64}", item["sha256"]) is not None
                and type(item["bytes"]) is int and 0 < item["bytes"] <= 4 * GIB, "invalid artifact hash/size")
        if role in FIXED_INPUTS:
            require(path == FIXED_INPUTS[role], "unexpected artifact path: " + role)
        elif role == "archive_validation":
            require(re.fullmatch(r"build/kernel-pipeline/[A-Za-z0-9_.-]+/archive-validation\.json", path),
                    "invalid archive validation path")
        else:
            require(role == "pin:" + path and path.startswith(("scripts/", "packaging/", "build/makepkg/"))
                    and not any(word in path for word in ("signing_key", ".pem", ".key")),
                    "unexpected input role or secret material")
    baseline = manifest["baseline"]
    require(set(baseline) == {"protected", "packages", "running_kernel", "secure_boot", "target_absent"},
            "invalid baseline")
    require(baseline["running_kernel"] == STOCK and baseline["secure_boot"] is False
            and baseline["target_absent"] is True, "unexpected baseline or Secure Boot enabled")
    require(isinstance(baseline["packages"], dict) and baseline["packages"].get("nitro-control") == "0.1.0-9"
            and not (set(PACKAGES) & baseline["packages"].keys()), "application pkgrel9/absent targets required")
    require(isinstance(baseline["protected"], dict) and set(baseline["protected"]) == set(PROTECTED),
            "protected roots cannot be supplied by the client")
    return manifest, digest, data


def run(command):
    result = subprocess.run(list(map(str, command)), stdin=subprocess.DEVNULL,
                            capture_output=True, env=ENV, cwd="/", timeout=600)
    require(result.returncode == 0, "command failed: " + str(command[0]) + " (exit " + str(result.returncode) + ")")
    return result.stdout.decode("utf-8", errors="strict")


def package_versions():
    result = {}
    for line in run(["/usr/bin/pacman", "-Q"]).splitlines():
        fields = line.split()
        require(len(fields) == 2 and fields[0] not in result, "invalid installed package inventory")
        result[fields[0]] = fields[1]
    return result


def snapshot(path, excluded=()):
    path = Path(path)
    result = {}
    def visit(item):
        if path == Path("/usr/lib/nitro-control") and "__pycache__" in item.parts:
            return
        if any(item == skip or skip in item.parents for skip in excluded):
            return
        key = str(item.relative_to(path))
        try:
            info = item.lstat()
        except FileNotFoundError:
            result[key] = {"kind": "absent"}
            return
        row = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
        if stat.S_ISLNK(info.st_mode):
            row.update(kind="symlink", target=os.readlink(item))
        elif stat.S_ISREG(info.st_mode):
            row.update(kind="file", bytes=info.st_size, sha256=sha(item))
        elif stat.S_ISDIR(info.st_mode):
            row.update(kind="directory")
        else:
            raise DeploymentError("special file in protected tree: " + str(item))
        result[key] = row
        if row["kind"] == "directory":
            for child in sorted(item.iterdir()):
                visit(child)
    visit(path)
    return result


def protected_snapshot(*, deployed=False):
    excluded = (NEW_UKI, BOOT / "limine.conf", BOOT / "limine.conf.old", FRAGMENT,
                NVTARGET, NVLINK, NVTREE / "original_module" / KREL)
    return {path: snapshot(path, excluded if deployed else ()) for path in PROTECTED}


def assert_protected(baseline, *, deployed=False):
    current = protected_snapshot(deployed=deployed)
    expected = baseline
    if deployed:
        expected = json.loads(json.dumps(baseline))
        roots = {"/boot": ("limine.conf", "limine.conf.old", "EFI/Linux/" + NEW_UKI.name),
                 str(FRAGMENT.parent): (FRAGMENT.name,),
                 str(NVTREE): (DRIVER + "/" + KREL, NVLINK.name, "original_module/" + KREL)}
        for root, prefixes in roots.items():
            for key in list(expected[root]):
                if any(key == prefix or key.startswith(prefix + "/") for prefix in prefixes):
                    del expected[root][key]
        fragment_root = str(FRAGMENT.parent)
        if expected[fragment_root].get(".") == {"kind": "absent"}:
            created = current[fragment_root].get(".")
            require(created in ({"kind": "absent"}, {"kind": "directory", "mode": 0o755, "uid": 0, "gid": 0}),
                    "owned drop-in directory has unexpected ownership/permissions")
            del expected[fragment_root]["."]
            current[fragment_root].pop(".", None)
    require(current == expected, "protected host files changed; preserve evidence and reconcile manually")


def secure_boot_disabled():
    data = SECUREBOOT.read_bytes()
    require(len(data) == 5 and data[4] == 0, "Secure Boot must be verifiably disabled; it will not be changed")


def root_path(path, *, absent=False):
    """Reject foreign-owned/writable parents, symlinks, and occupied owned destinations."""
    path = Path(path)
    require(path.is_absolute(), "root destination must be absolute")
    for current in (*reversed(path.parents), path):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        require(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode)
                and not info.st_mode & 0o022, "foreign or unsafe root path: " + str(current))
        if current != path:
            require(stat.S_ISDIR(info.st_mode), "non-directory root parent")
        elif absent:
            raise DeploymentError("owned target already exists: " + str(path))


def validate_cmdline(prepared, live, limine):
    require(isinstance(prepared, str) and prepared.endswith("\n") and prepared.count("\n") == 1
            and all(32 <= ord(c) < 127 for c in prepared[:-1]) and "#" not in prepared,
            "command line must be one complete printable line")
    tokens, current = shlex.split(prepared), shlex.split(live)
    opt = "acer_wmi.predator_v4="
    require([word for word in tokens if word.startswith(opt)] == [opt + "0"], "effective fan opt-out required")
    strip = lambda words: [word for word in words if not word.startswith(opt)]
    require(strip(tokens) == strip(current), "complete current encrypted-root command line changed")
    require(all(sum(word.startswith(prefix) for word in tokens) == 1
                for prefix in ("cryptdevice=", "root=", "rootflags=", "rootfstype=")),
            "complete encrypted-root parameters required")
    block = menu_block(limine, "linux")
    lines = re.findall(r"(?m)^\s*cmdline:\s*(.+)$", block)
    require(len(lines) == 1 and strip(shlex.split(lines[0])) == strip(current),
            "stock entry does not corroborate the live root command line")


def menu_block(text, name):
    matches = list(re.finditer(r"(?m)^\s*//" + re.escape(name) + r"\s*$", text))
    require(len(matches) == 1, "missing/ambiguous Limine entry: " + name)
    start = matches[0].start()
    end = re.search(r"(?m)^\s*/", text[matches[0].end():])
    return text[start:matches[0].end() + end.start() if end else len(text)]


def check_boot_menu(before, after=None, cmdline=None):
    require(re.findall(r"(?m)^default_entry:\s*(\d+)\s*$", before) == ["2"], "stock default_entry must remain 2")
    require(re.findall(r"(?m)^\s*//([^/\n]+)$", before)[0].strip() == "linux", "stock kernel must be first")
    require("//linux-nitro-test" not in before, "test boot entry already exists")
    if after is not None:
        block = menu_block(after, "linux-nitro-test")
        require(re.findall(r"(?m)^\s*cmdline:\s*(.+)$", block) == [cmdline.strip()],
                "test entry's effective command line is not exact")
        require(re.findall(r"(?m)^\s*path:\s*(.+)$", block)[0].split("#")[0]
                == "boot():/EFI/Linux/" + NEW_UKI.name, "wrong test UKI destination")
        normalize = lambda text: [line.strip() for line in text.splitlines() if line.strip()]
        require(normalize(after.replace(block, "", 1)) == normalize(before),
                "stock entry, OS order, defaults or unrelated boot configuration changed")


def check_limine_config():
    values = {}
    files = [Path("/etc/limine-entry-tool.conf")]
    files.extend(sorted(FRAGMENT.parent.glob("*.conf")))
    files.append(Path("/etc/default/limine"))
    for path in files:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            require(not line.startswith("KERNEL_CMDLINE[linux-nitro-test]"), "pre-existing target command-line configuration")
            match = re.fullmatch(r"([A-Z_]+)=(.*)", line)
            if match:
                words = shlex.split(match[2])
                values[match[1]] = " ".join(words)
    require(all(values.get(key, default) == expected for key, default, expected in (
        ("TARGET_OS_NAME", "", "Omarchy"), ("ESP_PATH", "", "/boot"),
        ("CUSTOM_UKI_NAME", "", "omarchy"), ("ENABLE_SORT", "no", "no"),
        ("ENABLE_ENROLL_LIMINE_CONFIG", "no", "no"),
        ("BOOT_ORDER", "", "*, *fallback, Snapshots"))), "unsupported Limine configuration; no automatic edits")


@contextmanager
def package_tar(path):
    process = subprocess.Popen(["/usr/bin/zstd", "-dc", "-T2", str(path)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV)
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            yield archive
        process.stdout.close()
        require(process.wait(timeout=30) == 0, "package decompression failed")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


def pkginfo(data):
    result = {}
    for line in data.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(" = ")
        require(sep and key and (value or key == "url"), "malformed .PKGINFO")
        result.setdefault(key, []).append(value)
    return result


def audit_package(path, package, expected_info=None, *, installed=False):
    require(package in (*PACKAGES, "nitro-control"), "extra package target")
    seen, links, files, selected, info, count = set(), set(), {}, {}, None, 0
    base = "usr/lib/modules/" + KREL
    with package_tar(path) as archive:
        for member in archive:
            name = str(PurePosixPath(member.name))
            require(not PurePosixPath(member.name).is_absolute() and ".." not in PurePosixPath(member.name).parts
                    and name not in seen, "archive path traversal/duplicate")
            seen.add(name)
            count += 1
            require(member.isfile() or member.isdir() or member.issym(), "archive special file/hardlink forbidden")
            require((member.issym() or not member.mode & 0o7022) and member.uid == 0 and member.gid == 0,
                    "unsafe package ownership/mode")
            if name == ".":
                require(member.isdir(), "invalid archive root")
                continue
            if name in (".PKGINFO", ".BUILDINFO", ".MTREE"):
                require(member.isfile() and member.size < 4 * 1024**2, "invalid metadata")
                if name == ".PKGINFO":
                    info = pkginfo(archive.extractfile(member).read())
                continue
            require(not name.startswith("."), "scriptlet or unexpected metadata forbidden")
            if member.issym():
                links.add(name)
            if package in PACKAGES:
                parents = ("usr", "usr/lib", "usr/lib/modules", "usr/src")
                alias = name == "usr/src/linux-nitro-test"
                require((name in parents and member.isdir()) or name == base or name.startswith(base + "/")
                        or (alias and member.issym() and package == PACKAGES[1]), "global package file/collision forbidden: " + name)
                if member.issym():
                    link = PurePosixPath(member.linkname)
                    resolved = os.path.normpath(str(PurePosixPath(name).parent / link))
                    require(not link.is_absolute() and (resolved == base or resolved.startswith(base + "/")),
                            "archive symlink escapes target release")
                    if alias:
                        require(resolved == base + "/build", "headers alias must resolve to the exact target build tree")
                if package == PACKAGES[0]:
                    require(name != base + "/build" and not name.startswith(base + "/build/"), "kernel package contains build payload")
                else:
                    vdso = name.startswith(base + "/vdso/")
                    require(member.isdir() or alias or vdso and member.isfile()
                            and PurePosixPath(name).name in ("vdso32.so", "vdso64.so", "vdsox32.so")
                            or name.startswith(base + "/build/"), "headers package contains non-header payload")
                if name in (base + "/pkgbase", base + "/vmlinuz", base + "/build/.config",
                            base + "/build/include/config/kernel.release", base + "/build/version", base + "/build/vmlinux.h"):
                    require(member.isfile(), "selected file is not regular")
                    selected[name] = hashlib.file_digest(archive.extractfile(member), "sha256").hexdigest()
                destination = Path("/") / name
                if destination.exists() or destination.is_symlink():
                    require(member.isdir() and destination.is_dir() and not destination.is_symlink(),
                            "existing host file collision: " + name)
                root_path(destination)
            else:
                require(name.startswith(("usr/lib/nitro-control/", "usr/bin/nitro-control",
                                         "usr/lib/systemd/system/nitro-control", "usr/share/")) or member.isdir(),
                        "unexpected application payload")
                if installed and not member.isdir():
                    destination = Path("/") / name
                    if member.issym():
                        require(destination.is_symlink() and os.readlink(destination) == member.linkname
                                and destination.lstat().st_uid == 0, "installed application link differs")
                        root_path(destination.parent)
                    else:
                        root_path(destination)
                        require(destination.is_file() and sha(destination) == hashlib.file_digest(
                            archive.extractfile(member), "sha256").hexdigest(), "installed pkgrel9 bytes differ: " + name)
            files[name + ("/" if member.isdir() else "")] = member.type.decode("ascii")
    require(info is not None, "missing .PKGINFO")
    require(not any(name.startswith(link + "/") for name in seen for link in links),
            "archive writes through a symlink parent")
    for key, value in (("pkgname", package), ("pkgbase", "linux-nitro-test" if package in PACKAGES else package),
                       ("pkgver", VERSION if package in PACKAGES else "0.1.0-9"),
                       ("arch", "x86_64" if package in PACKAGES else "any")):
        require(info.get(key) == [value], "wrong package metadata: " + key)
    require(not any(key in info for key in ("install", "replaces", "conflict", "conflicts", "backup")), "unsafe package metadata")
    require(not any(re.split(r"[<>=]", value)[0] in ("linux", "linux-headers") for value in info.get("provides", [])),
            "stock alias forbidden")
    if expected_info is not None:
        require(info == expected_info["pkginfo"] and selected == expected_info["selected_file_sha256"]
                and count == expected_info["members"], "package inventory/metadata differs from validated archive")
    return files


def audit_dkms_archive(path, manifest):
    prefix = "dkms_main_tree/" + KREL + "/x86_64"
    expected_files = {"dkms_binaries_only/PACKAGE_NAME", "dkms_binaries_only/PACKAGE_VERSION",
                      "dkms_binaries_only/dkms.conf", "NVIDIA-COPYING", prefix + "/log/make.log",
                      *(prefix + "/module/" + name + ".ko.zst" for name in MODULES)}
    expected_dirs = {".", "dkms_main_tree", "dkms_main_tree/" + KREL, prefix,
                     prefix + "/log", prefix + "/module", "dkms_binaries_only"}
    seen = set()
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = str(PurePosixPath(member.name))
            require(name not in seen and not PurePosixPath(member.name).is_absolute()
                    and ".." not in PurePosixPath(member.name).parts, "unsafe DKMS archive path")
            seen.add(name)
            require((member.isdir() and name in expected_dirs) or (member.isfile() and name in expected_files),
                    "DKMS archive must contain only licensed target binaries/config/log")
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            if name.endswith("/PACKAGE_NAME") or name.endswith("/PACKAGE_VERSION"):
                require(stream.read(128).strip() == (b"nvidia" if name.endswith("/PACKAGE_NAME") else DRIVER.encode()),
                        "wrong DKMS package identity")
            elif name.endswith("/dkms.conf"):
                require(hashlib.file_digest(stream, "sha256").hexdigest() == NVIDIA_CONF_SHA256,
                        "DKMS executable configuration differs from existing original")
            elif "/module/" in name:
                module = PurePosixPath(name).name.removesuffix(".ko.zst")
                require(hashlib.file_digest(stream, "sha256").hexdigest()
                        == manifest["nvidia"]["modules"][module]["dkms_sha256"], "DKMS binary hash mismatch")
            elif name.endswith("/make.log"):
                require(hashlib.file_digest(stream, "sha256").hexdigest() == manifest["nvidia"]["build_log_sha256"],
                        "DKMS build log mismatch")
    require(seen == expected_files | expected_dirs and len(seen) == manifest["dkms_archive"]["members"],
            "incomplete DKMS archive/license")


def audit_uki(path, manifest, cmdline):
    sections = {}
    size = Path(path).stat().st_size
    with Path(path).open("rb") as stream:
        dos = stream.read(64)
        require(len(dos) == 64 and dos[:2] == b"MZ", "UKI missing PE header")
        offset = struct.unpack_from("<I", dos, 60)[0]
        require(offset + 24 <= size, "UKI PE offset out of bounds")
        stream.seek(offset)
        pe = stream.read(24)
        require(pe[:4] == b"PE\0\0" and struct.unpack_from("<H", pe, 4)[0] == 0x8664, "wrong UKI architecture")
        count, optional = struct.unpack_from("<H", pe, 6)[0], struct.unpack_from("<H", pe, 20)[0]
        require(1 <= count <= 96 and offset + 24 + optional + count * 40 <= size, "bad PE section table")
        stream.seek(offset + 24 + optional)
        for _ in range(count):
            row = stream.read(40)
            name = row[:8].rstrip(b"\0").decode("ascii")
            length, _, raw_size, raw_offset = struct.unpack_from("<IIII", row, 8)
            require(name not in sections and raw_offset + raw_size <= size, "duplicate/out-of-bounds EFI section")
            sections[name] = (length, raw_offset, raw_size)
        for name, report in manifest["efi_sections"].items():
            require(name in (".linux", ".uname", ".cmdline", ".initrd", ".osrel") and name in sections,
                    "missing/unexpected UKI section")
            length, offset, raw = sections[name]
            require(0 < length <= raw and (length, offset, raw) == (report["size"], report["offset"], report["raw_size"]),
                    "UKI section length mismatch")
            stream.seek(offset)
            digest, remaining, small = hashlib.sha256(), length, bytearray()
            while remaining:
                chunk = stream.read(min(1024**2, remaining))
                require(chunk, "truncated UKI")
                remaining -= len(chunk)
                digest.update(chunk)
                if name in (".uname", ".cmdline", ".osrel"):
                    require(length < 65536, "oversized UKI text section")
                    small.extend(chunk)
            require(digest.hexdigest() == report["sha256"], "UKI section hash mismatch")
            if name == ".uname":
                require(small.rstrip(b"\0") == KREL.encode(), "wrong UKI release")
            if name == ".cmdline":
                require(small.decode().strip("\0 \r\n\t") == cmdline.strip(), "wrong embedded root command line")
            if name == ".osrel":
                require("VERSION_ID=" + KREL in small.decode().strip("\0").splitlines(), "wrong UKI os-release")
    require(set(manifest["efi_sections"]) == {".linux", ".uname", ".cmdline", ".initrd", ".osrel"}, "incomplete UKI audit")


def completed(state):
    require(state.get("status") == "completed" and state.get("kernel_release") == KREL
            and state.get("stages") and all(item.get("status") == "succeeded" for item in state["stages"]),
            "kernel/NVIDIA pipeline must be COMPLETED with every stage succeeded")
    for stage in state["stages"]:
        guard = stage.get("guard_result", {})
        require(guard.get("exit_reason") == "child_exit" and guard.get("exit_code") == 0
                and guard.get("child_exit_code") == 0, "pipeline lacks successful guarded stage result")


def validate_evidence(manifest, paths):
    load = lambda role: decode_json(paths[role].read_bytes())
    kernel, nstate, report, nv, app = (load(role) for role in (
        "kernel_state", "nvidia_state", "archive_validation", "nvidia_manifest", "app_report"))
    completed(kernel)
    completed(nstate)
    require(kernel["config_sha256"] == CONFIG_SHA256 and report["config_sha256"] == CONFIG_SHA256
            and report["kernel_release"] == KREL and nstate["nvidia_driver"] == DRIVER, "wrong pipeline target/config")
    require(set(report["packages"]) == set(PACKAGES), "extra package target")
    workspace = Path(manifest["workspace"])
    original = lambda role: str(workspace / manifest["artifacts"][role]["path"])
    require(kernel["artifact_paths"]["archive-validation"] == original("archive_validation"), "validation report not declared by pipeline")
    require(nstate["artifacts"]["manifest"] == original("nvidia_manifest")
            and nstate["kernel"]["run_id"] == kernel["run_id"], "NVIDIA/kernel provenance mismatch")
    for key in ("config_sha256", "recipe_sha256", "script_pins", "input_pins", "archive_pins"):
        require(nstate["kernel"].get(key) == kernel.get(key), "NVIDIA/kernel pin mismatch: " + key)
    require(kernel["archive_pins"] == {str(Path(item["path"]).relative_to(workspace)): item["sha256"]
                                       for item in report["packages"].values()}, "completed kernel archive pins differ")
    require(report["packages"][PACKAGES[1]]["selected_file_sha256"]["usr/lib/modules/" + KREL + "/build/.config"]
            == CONFIG_SHA256, "headers are not the stable 37e9 configuration")
    expected_pins = {**kernel["script_pins"], **kernel.get("input_pins", {}),
                     "scripts/prepare-nvidia-uki.py": nstate["script_sha256"]}
    require({key[4:]: value["sha256"] for key, value in manifest["artifacts"].items() if key.startswith("pin:")}
            == expected_pins, "pipeline input/script pins differ")
    require(nv["kernel_release"] == KREL and nv["nvidia_driver"] == DRIVER
            and nv["validated_inputs"]["packages"] == report["packages"]
            and nv["validated_inputs"]["header_config_sha256"] == CONFIG_SHA256
            and nv["validated_inputs"]["validation_sha256"] == manifest["artifacts"]["archive_validation"]["sha256"],
            "NVIDIA compatibility manifest differs from validated kernel archives")
    require(nv["validated_inputs"]["signing_certificate_sha256"] == SIGNING_CERT_SHA256
            and set(nv["nvidia"]["modules"]) == set(MODULES), "wrong signing fingerprint/module set")
    require(nv["nvidia"]["normal_modpost"] is True and nv["host_baseline_unchanged"] is True
            and nv["system_installation"] is False and nv["module_load_performed"] is False
            and nv["boot_or_runtime_validated"] is False, "invalid compatibility-only audit")
    protected_hashes = {str(Path(root) / relative_path): row["sha256"]
                        for root, inventory in manifest["baseline"]["protected"].items()
                        for relative_path, row in inventory.items() if row.get("kind") == "file"}
    for path, expected in {**nstate["system_pins"], **nv["validated_inputs"]["host_baseline"]}.items():
        actual = (manifest["artifacts"]["cmdline"]["sha256"] if path == original("cmdline") else protected_hashes.get(path))
        require(actual == expected, "compatibility audit's protected host input changed: " + path)
    require(nv["dkms_archive"]["native_private_import_verified"] is True
            and nv["dkms_archive"]["target_kernel_only"] == KREL
            and paths["import_status"].read_text().strip() == f"nvidia/{DRIVER}, {KREL}, x86_64: built",
            "native target binary import evidence missing")
    require(nv["initramfs_nvidia_modules"] == {name: "usr/lib/modules/" + KREL + "/updates/dkms/" + name + ".ko"
                                              for name in MODULES if name != "nvidia-peermem"},
            "UKI audit lacks exact NVIDIA initramfs module inventory")
    for data in nv["nvidia"]["modules"].values():
        require(data["sig_key"] == SIGNING_KEY and data["vermagic"] == report["acer_wmi_vermagic"]
                and data["vermagic"].split()[0] == KREL and data["version"] == DRIVER and data["signer"],
                "NVIDIA signing fingerprint/vermagic/version mismatch")
    for role, record in (("uki", nv["uki"]), ("dkms_archive", nv["dkms_archive"]),
                         ("kernel_package", report["packages"][PACKAGES[0]]),
                         ("headers_package", report["packages"][PACKAGES[1]])):
        require(record["path"] == original(role) and record["sha256"] == manifest["artifacts"][role]["sha256"],
                "artifact not from actual completed manifest: " + role)
    require(app.get("package_version") == "0.1.0-9" and app.get("package_release") == 9
            and app["package"] == original("app_package")
            and app["package_sha256"] == manifest["artifacts"]["app_package"]["sha256"], "app report is not pkgrel9")
    require(nv["cmdline_sha256"] == manifest["artifacts"]["cmdline"]["sha256"]
            and nv["uki"]["bytes"] == manifest["artifacts"]["uki"]["bytes"]
            and nv["efi_sections"][".linux"]["sha256"] == report["packages"][PACKAGES[0]]["selected_file_sha256"][
                "usr/lib/modules/" + KREL + "/vmlinuz"], "UKI inputs mismatch")
    return report, nv


def verify_inputs(manifest, paths):
    for role, path in paths.items():
        item = manifest["artifacts"][role]
        with input_file(path) as stream:
            require(os.fstat(stream.fileno()).st_size == item["bytes"]
                    and hashlib.file_digest(stream, "sha256").hexdigest() == item["sha256"], "input hash/size mismatch: " + role)


def applicable_hooks(files):
    hooks = {}
    for directory in (Path("/usr/share/libalpm/hooks"), Path("/etc/pacman.d/hooks")):
        hooks.update({path.name: path for path in directory.glob("*.hook")})
    triggered = set()
    for name, path in hooks.items():
        if name in MASKS or (path.is_symlink() and os.readlink(path) == "/dev/null"):
            continue
        section, triggers, current = None, [], None
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                section = line
                if section == "[Trigger]":
                    current = {}
                    triggers.append(current)
            elif section == "[Trigger]":
                key, sep, value = line.partition("=")
                require(sep, "unsupported ALPM hook syntax")
                current.setdefault(key.strip(), []).append(value.strip())
        for trigger in triggers:
            if "Install" not in trigger.get("Operation", []):
                continue
            candidates = PACKAGES if trigger.get("Type") == ["Package"] else files
            require(trigger.get("Type") in (["Package"], ["Path"]), "unsupported ALPM trigger type")
            patterns = trigger.get("Target", [])
            if any(any(fnmatch.fnmatchcase(item, pattern) for pattern in patterns if not pattern.startswith("!"))
                   and not any(fnmatch.fnmatchcase(item, pattern[1:]) for pattern in patterns if pattern.startswith("!"))
                   for item in candidates):
                triggered.add(name)
    require(triggered == {"10-limine-snapper-lock.hook", "35-systemd-update.hook", "60-depmod.hook"},
            "unexpected applicable ALPM hooks: " + ", ".join(sorted(triggered)))


def preflight(manifest, paths=None):
    paths = paths or {role: Path(manifest["workspace"]) / item["path"] for role, item in manifest["artifacts"].items()}
    verify_inputs(manifest, paths)
    report, nv = validate_evidence(manifest, paths)
    secure_boot_disabled()
    require(run(["/usr/bin/pacman-conf", "RootDir", "DBPath", "HookDir"]).splitlines()
            == ["RootDir = /", "DBPath = /var/lib/pacman/", "HookDir = /etc/pacman.d/hooks/"],
            "unsupported pacman root/database/hook directories")
    require(os.uname().release == STOCK and KREL != os.uname().release, "unexpected running kernel")
    require(package_versions() == manifest["baseline"]["packages"], "installed package baseline changed")
    require(not (set(PACKAGES) & package_versions().keys()), "custom target already installed; no force/retry")
    for path in (MODROOT, Path("/usr/src/linux-nitro-test"), NVTARGET, NVLINK, NEW_UKI, FRAGMENT,
                 NVTREE / "original_module" / KREL):
        root_path(path, absent=True)
    assert_protected(manifest["baseline"]["protected"])
    require(sha(STOCK_UKI) == STOCK_UKI_SHA256 and sha("/usr/bin/dkms") == DKMS_SHA256
            and sha(SOURCE / "dkms.conf") == NVIDIA_CONF_SHA256, "audited stock UKI/DKMS/source changed")
    source_link = NVTREE / DRIVER / "source"
    require(source_link.is_symlink() and source_link.resolve(strict=True) == SOURCE, "existing NVIDIA source registration required")
    require(run(["/usr/bin/dkms", "status", "nvidia/" + DRIVER, "-k", STOCK, "-a", "x86_64"]).strip()
            == f"nvidia/{DRIVER}, {STOCK}, x86_64: installed", "stock NVIDIA registration changed")
    root_path(source_link.parent)
    require(not Path("/var/lib/nitro-control/hardware-approved").exists()
            and not Path("/etc/nitro-control/hardware-approved").exists(), "hardware approval marker must remain absent")
    for unit in ("nitro-control.service", "nitro-control-sleep.service"):
        active = subprocess.run(["/usr/bin/systemctl", "is-active", unit], capture_output=True, env=ENV, timeout=10)
        enabled = subprocess.run(["/usr/bin/systemctl", "is-enabled", unit], capture_output=True, env=ENV, timeout=10)
        require(active.stdout.strip() == b"inactive" and enabled.stdout.strip() == b"disabled", "application services must remain disabled/inactive")
    cmdline = paths["cmdline"].read_text()
    validate_cmdline(cmdline, Path("/proc/cmdline").read_text(), (BOOT / "limine.conf").read_text())
    check_boot_menu((BOOT / "limine.conf").read_text())
    check_limine_config()
    require(os.path.ismount(BOOT) and os.stat(BOOT).st_dev != os.stat(BASE.parent).st_dev, "ESP must be a separate /boot mount")
    require(shutil.disk_usage(BOOT).free > manifest["artifacts"]["uki"]["bytes"] + 128 * 1024**2, "ESP lacks UKI plus 128 MiB reserve")
    boot_bytes = sum(row.get("bytes", 0) for row in manifest["baseline"]["protected"]["/boot"].values())
    inputs_bytes = sum(item["bytes"] for item in manifest["artifacts"].values())
    installed_bytes = sum(int(item["pkginfo"]["size"][0]) for item in report["packages"].values())
    require(shutil.disk_usage(BASE.parent).free >= 12 * GIB + 2 * inputs_bytes + 2 * boot_bytes + installed_bytes,
            "root needs 12 GiB reserve plus conservative input/backup/install headroom")
    files = {}
    for role, package in zip(("kernel_package", "headers_package"), PACKAGES):
        payload = audit_package(paths[role], package, report["packages"][package])
        require(not any(name in files and kind != "5" for name, kind in payload.items()), "cross-package file collision")
        files.update(payload)
    require(not any(name.startswith(link.rstrip("/") + "/") for name in files
                    for link, kind in files.items() if kind == "2"), "cross-package symlink parent collision")
    audit_package(paths["app_package"], "nitro-control", installed=True)
    require(str(APPROOT.relative_to("/")) + "/deployment.py" in audit_app_names(paths["app_package"]),
            "pkgrel9 must contain the deployment module")
    applicable_hooks(files)
    audit_dkms_archive(paths["dkms_archive"], nv)
    audit_uki(paths["uki"], nv, cmdline)
    return {"status": "preflight-passed", "kernel_release": KREL, "nvidia_driver": DRIVER,
            "package_targets": list(PACKAGES), "writes_performed": False,
            "boot_action": "manual one-time menu selection only; default unchanged"}


def audit_app_names(path):
    with package_tar(path) as archive:
        return {str(PurePosixPath(member.name)) for member in archive}


def create_plan(workspace):
    require(os.geteuid() != 0, "manifest generation must run as an ordinary user")
    workspace = Path(workspace).absolute()
    require(workspace.resolve(strict=True) == workspace, "workspace must not contain symlinks")
    load = lambda relative_path: decode_json((workspace / relative_path).read_bytes())
    kernel, nvidia = load(FIXED_INPUTS["kernel_state"]), load(FIXED_INPUTS["nvidia_state"])
    completed(kernel)
    completed(nvidia)
    artifacts = {role: descriptor(workspace, workspace / path) for role, path in FIXED_INPUTS.items()}
    artifacts["archive_validation"] = descriptor(workspace, Path(kernel["artifact_paths"]["archive-validation"]))
    for path in {**kernel["script_pins"], **kernel.get("input_pins", {}),
                 "scripts/prepare-nvidia-uki.py": nvidia["script_sha256"]}:
        artifacts["pin:" + path] = descriptor(workspace, workspace / path)
    secure_boot_disabled()
    packages = package_versions()
    require(packages.get("nitro-control") == "0.1.0-9", "install reviewed app 0.1.0-9 before generating the deployment manifest")
    plan = {"schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "workspace": str(workspace), "target": TARGET, "artifacts": artifacts,
            "authorization": "operator-sha256-local-unsigned-archives",
            "baseline": {"protected": protected_snapshot(), "packages": packages,
                         "running_kernel": os.uname().release, "secure_boot": False, "target_absent": True}}
    preflight(plan)
    return plan


def strict_package_table(text):
    require("\x1b" not in text and "\r" not in text and "\x00" not in text, "non-C/color transaction output")
    prompt = ":: Proceed with installation? [Y/n] "
    require(text.endswith(prompt) and text.count(prompt) == 1, "not the actual installation prompt")
    header = re.search(r"(?m)^Package \(2\)\s+(?:Old Version\s+)?New Version\s+Net Change\s*$", text)
    require(header and len(re.findall(r"(?m)^Package[s]? \(", text)) == 1, "exact verbose two-package table required")
    require(all(line in ("loading packages...", "resolving dependencies...", "looking for conflicting packages...")
                for line in text[:header.start()].splitlines() if line), "unexpected transaction action or earlier prompt")
    rows, totals = [], 0
    for line in text[header.end():-len(prompt)].splitlines():
        if not line.strip():
            continue
        if line.startswith("Total Installed Size:"):
            require(re.fullmatch(r"Total Installed Size:\s+[0-9]+(?:\.[0-9]+)? MiB", line), "bad total size")
            totals += 1
            continue
        fields = line.split()
        require(len(fields) == 4 and fields[0] in PACKAGES and fields[1] == VERSION
                and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", fields[2]) and fields[3] == "MiB",
                "transaction includes upgrades, dependencies, removals or unexpected packages")
        rows.append(fields[0])
    require(len(rows) == 2 and set(rows) == set(PACKAGES) and totals == 1,
            "transaction targets are not exactly the two NEW kernel packages")
    return rows


def pacman_transaction(command, log, gate):
    """Answer only the real transaction's final prompt, never a separate dry-run."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env={**ENV, "COLUMNS": "240"}, cwd="/", bufsize=0)
    accepted, output, since_prompt, deadline = False, bytearray(), bytearray(), time.monotonic() + 600
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        with Path(log).open("xb") as record:
            while selector.get_map():
                require(time.monotonic() < deadline, "pacman transaction timed out; inspect root-owned log")
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    record.write(chunk)
                    record.flush()
                    output.extend(chunk)
                    since_prompt.extend(chunk)
                    require(len(output) < 8 * 1024**2, "unexpected pacman output size")
                    text = since_prompt.decode("utf-8")
                    if "[Y/n]" in text or "[y/N]" in text or re.search(r"\?[^\n]*\[[^\n]+/[^\n]+\]", text):
                        require(not accepted, "unexpected second pacman prompt")
                        # Wait for the trailing space, not just a partial prompt read.
                        if not text.endswith(":: Proceed with installation? [Y/n] "):
                            require(text.endswith("[Y/n]"), "unexpected pacman question; transaction cancelled")
                            continue
                        strict_package_table(output.decode("utf-8"))
                        gate()
                        process.stdin.write(b"y\n")
                        process.stdin.flush()
                        accepted = True
                        since_prompt.clear()
            require(process.wait(timeout=30) == 0 and accepted, "pacman transaction failed or lacked validated prompt")
    finally:
        selector.close()
        if process.poll() is None:
            process.stdin.close()
            process.terminate()
            process.wait(timeout=30)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


@contextmanager
def boot_writer_lock(timeout=10):
    root_path(BOOT_LOCK)
    fd = os.open(BOOT_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    held = False
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1,
                "unsafe boot lock file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                require(time.monotonic() < deadline, "boot writer lock timeout; no unlocked fallback")
                time.sleep(0.05)
        require(os.stat(BOOT_LOCK, follow_symlinks=False).st_ino == info.st_ino, "boot lock was replaced")
        yield fd
    finally:
        if held:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def mkdir_root(path, mode=0o700):
    root_path(path)
    path = Path(path)
    if not path.exists():
        mkdir_root(path.parent)
        path.mkdir(mode=mode)
        path.chmod(mode)
    require(path.is_dir(), "root cache parent is not a directory")


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="ascii") as stream:
        os.chmod(temporary, 0o600)
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


def cache_inputs(manifest, data, cache):
    inputs = cache / "inputs"
    inputs.mkdir(mode=0o700)
    (inputs / "deployment-manifest.json").write_bytes(data)
    require(sha(inputs / "deployment-manifest.json") == hashlib.sha256(data).hexdigest(), "cached manifest changed")
    paths = {}
    for index, (role, item) in enumerate(sorted(manifest["artifacts"].items())):
        destination = inputs / (str(index).zfill(3) + "-" + PurePosixPath(item["path"]).name)
        with input_file(Path(manifest["workspace"]) / item["path"]) as source, destination.open("xb") as target:
            require(os.fstat(source.fileno()).st_size == item["bytes"], "source changed before caching")
            copied = 0
            while chunk := source.read(1024**2):
                copied += len(chunk)
                require(copied <= item["bytes"], "source grew during caching")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        paths[role] = destination
    verify_inputs(manifest, paths)
    write_json(cache / "input-index.json", {role: str(path) for role, path in paths.items()})
    return paths


def dkms_sandbox(cache, command, *, install=False):
    config = cache / "dkms-config"
    args = ["/usr/bin/bwrap", "--unshare-net", "--die-with-parent", "--cap-drop", "ALL",
            "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", str(cache), str(cache), "--ro-bind", str(config), "/etc/dkms"]
    if install:
        args += ["--bind", str(NVTREE), str(NVTREE), "--bind", str(MODROOT), str(MODROOT)]
        for child in sorted(NVTREE.iterdir()):
            if child.is_dir() and not child.is_symlink() and child.name != DRIVER:
                args += ["--ro-bind", str(child), str(child)]
        for child in sorted((NVTREE / DRIVER).iterdir()):
            if child.is_dir() and not child.is_symlink() and child.name != KREL:
                args += ["--ro-bind", str(child), str(child)]
    # The native install path is already built. Even an unexpected fallback cannot compile.
    for tool in ("make", "gcc", "cc", "clang", "ld", "as"):
        executable = Path("/usr/bin") / tool
        if executable.exists():
            args += ["--ro-bind", "/usr/bin/false", str(executable.resolve())]
    return [*args, "--chdir", str(cache), "--", *map(str, command)]


def install_nvidia(cache, paths, nv, record):
    require(sha("/usr/bin/dkms") == DKMS_SHA256, "DKMS implementation changed; cannot guarantee binary-only import")
    config = cache / "dkms-config"
    config.mkdir(mode=0o700)
    (config / "framework.conf").write_text(
        "tmp_location=" + shlex.quote(str(cache / "tmp")) + "\n"
        "try_sign_modules=false\nmodprobe_on_install=\npost_transaction=\nbuild_environment=\n")
    (config / f"nvidia-{DRIVER}-{KREL}-x86_64.conf").write_text(
        'MAKE[0]="/usr/bin/false"\nAUTOINSTALL="no"\nNO_WEAK_MODULES="yes"\n'
        'PRE_INSTALL=""\nPOST_INSTALL=""\nBUILD_DEPENDS_REBUILD=""\n')
    (cache / "tmp").mkdir(mode=0o700)
    private = cache / "dkms-import"
    private.mkdir(mode=0o700)
    args = ["--dkmstree", str(private), "--sourcetree", "/usr/src", "--installtree", "/usr/lib/modules",
            "-k", KREL, "-a", "x86_64"]
    run(dkms_sandbox(cache, ["/usr/bin/dkms", "ldtarball", "--archive", paths["dkms_archive"], *args]))
    version = private / "nvidia" / DRIVER
    # load_tarball refuses an already-added version. Its final add_module creates
    # this link only in our fresh private tree, never in the existing host tree.
    require((version / "source").is_symlink() and (version / "source").resolve(strict=True) == SOURCE,
            "native private import did not register the existing source read-only")
    require(run(dkms_sandbox(cache, ["/usr/bin/dkms", "status", "nvidia/" + DRIVER, *args])).strip()
            == f"nvidia/{DRIVER}, {KREL}, x86_64: built", "private import is not binary-built")
    imported = version / KREL
    for name in MODULES:
        module = imported / "x86_64/module" / (name + ".ko.zst")
        require(sha(module) == nv["nvidia"]["modules"][name]["dkms_sha256"], "private import altered a binary")
        for field, expected in (("sig_key", SIGNING_KEY), ("vermagic", nv["nvidia"]["modules"][name]["vermagic"]),
                                ("version", DRIVER)):
            require(run(["/usr/bin/modinfo", "-F", field, module]).strip() == expected, "imported binary metadata mismatch")
    root_path(NVTARGET, absent=True)
    root_path(NVLINK, absent=True)
    require(not any(match for name in MODULES for match in MODROOT.rglob(name + ".ko*")),
            "target already contains NVIDIA modules")
    record("nvidia-merge", {"target": str(NVTARGET), "before": "absent", "archive_sha256": sha(paths["dkms_archive"])})
    shutil.copytree(imported, NVTARGET, symlinks=False)
    require((NVTARGET / "x86_64/module").is_dir(), "no binary-built module marker; root build forbidden")
    command = ["/usr/bin/dkms", "install", "nvidia/" + DRIVER, "-k", KREL, "-a", "x86_64", "--no-depmod"]
    require(run(dkms_sandbox(cache, ["/usr/bin/dkms", "status", "nvidia/" + DRIVER, "-k", KREL, "-a", "x86_64"], install=True)).strip()
            == f"nvidia/{DRIVER}, {KREL}, x86_64: built", "merged target is not built; refusing root compile")
    run(dkms_sandbox(cache, command, install=True))
    umask = os.umask(0o022)
    try:
        run(["/usr/bin/depmod", KREL])
    finally:
        os.umask(umask)
    for index in ("modules.dep", "modules.dep.bin", "modules.alias", "modules.alias.bin",
                  "modules.builtin.alias.bin", "modules.builtin.bin", "modules.devname",
                  "modules.softdep", "modules.symbols", "modules.symbols.bin", "modules.weakdep"):
        path = MODROOT / index
        require(not path.exists() or path.stat().st_mode & 0o777 == 0o644,
                "depmod index permissions unexpected: " + index)
    require(run(dkms_sandbox(cache, ["/usr/bin/dkms", "status", "nvidia/" + DRIVER, "-k", KREL, "-a", "x86_64"], install=True)).strip()
            == f"nvidia/{DRIVER}, {KREL}, x86_64: installed", "target NVIDIA install incomplete")
    for name in MODULES:
        module = MODROOT / "updates/dkms" / (name + ".ko.zst")
        require(module.is_file() and not module.is_symlink() and sha(module) == nv["nvidia"]["modules"][name]["dkms_sha256"],
                "installed NVIDIA binary differs from signed prebuilt input")
    record("nvidia-installed", {"target": str(NVTARGET), "after": snapshot(NVTARGET),
                                "active_link": {"path": str(NVLINK), "target": os.readlink(NVLINK)}})


def apply_deployment(manifest, digest, data):
    require(os.geteuid() == 0, "--apply requires UID 0 via the installed administrator launcher")
    require(Path(__file__).absolute() == APPROOT / "deployment.py", "root apply must use the installed reviewed app, not workspace Python")
    root_path(APPROOT / "deployment.py")
    preflight(manifest)
    os.umask(0o077)
    mkdir_root(BASE)
    root_path(BASE / "deployment.lock")
    lock = os.open(BASE / "deployment.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    cache, summary = None, None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeploymentError("another Nitro deployment is running") from error
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        parent = BASE / "deployment"
        mkdir_root(parent)
        cache = parent / stamp
        cache.mkdir(mode=0o700)
        summary = {"schema_version": 1, "status": "running", "step": "cache-inputs", "manifest_sha256": digest,
                   "kernel_release": KREL, "nvidia_driver": DRIVER, "cache": str(cache), "changes": [],
                   "automatic_rollback": False, "reboot_performed": False, "hardware_approved": False,
                   "services_enabled": False, "fan_writes_performed": False}
        def record(step, change=None):
            summary["step"] = step
            if change is not None:
                summary["changes"].append({"step": step, **change})
            write_json(cache / "summary.json", summary)
        record("cache-inputs")
        paths = cache_inputs(manifest, data, cache)
        record("cached-preflight")
        preflight(manifest, paths)
        _, nv = validate_evidence(manifest, paths)
        hooks = cache / "alpm-hook-masks"
        hooks.mkdir(mode=0o700)
        for name in MASKS:
            (hooks / name).symlink_to("/dev/null")
        config = cache / "pacman.conf"
        config.write_text("[options]\nArchitecture = x86_64\nVerbosePkgLists\nCheckSpace\n"
                          "SigLevel = Required DatabaseOptional\nLocalFileSigLevel = Optional\n")
        command = ["/usr/bin/pacman", "--config", str(config), "--color", "never", "--hookdir", "/etc/pacman.d/hooks",
                   "--hookdir", str(hooks), "--noscriptlet", "-U", str(paths["kernel_package"]), str(paths["headers_package"])]
        record("pacman-transaction", {"packages": list(PACKAGES), "version": VERSION, "before": "absent",
                                      "hook_masks": list(MASKS), "boot_lock_held": False})
        def gate():
            secure_boot_disabled()
            require(package_versions() == manifest["baseline"]["packages"], "package baseline changed inside transaction")
            assert_protected(manifest["baseline"]["protected"])
            verify_inputs(manifest, paths)
        pacman_transaction(command, cache / "pacman.log", gate)
        expected_packages = {**manifest["baseline"]["packages"], **dict.fromkeys(PACKAGES, VERSION)}
        require(package_versions() == expected_packages, "pacman changed unexpected packages")
        record("packages-installed", {"packages": dict.fromkeys(PACKAGES, VERSION), "release_tree": str(MODROOT)})
        assert_protected(manifest["baseline"]["protected"])
        record("nvidia-import-install")
        install_nvidia(cache, paths, nv, record)
        assert_protected(manifest["baseline"]["protected"], deployed=True)
        record("boot-lock")
        with boot_writer_lock():
            record("boot-backup")
            secure_boot_disabled()
            # Pacman and DKMS are finished before acquiring this shared writer lock.
            require(sha(BOOT / "limine.conf") == manifest["baseline"]["protected"]["/boot"]["limine.conf"]["sha256"],
                    "boot configuration changed before writer lock")
            require(sha(STOCK_UKI) == STOCK_UKI_SHA256, "stock UKI changed")
            root_path(NEW_UKI, absent=True)
            root_path(FRAGMENT, absent=True)
            check_limine_config()
            before = (BOOT / "limine.conf").read_text()
            check_boot_menu(before)
            cmdline = paths["cmdline"].read_text()
            validate_cmdline(cmdline, Path("/proc/cmdline").read_text(), before)
            require(shutil.disk_usage(BOOT).free > paths["uki"].stat().st_size + 128 * 1024**2, "ESP reserve changed")
            backup_parent = BASE / "boot-backups"
            boot_bytes = sum(row.get("bytes", 0) for row in snapshot(BOOT).values())
            require(shutil.disk_usage(BASE).free >= 12 * GIB + 2 * boot_bytes, "root backup reserve changed")
            mkdir_root(backup_parent)
            backup = backup_parent / stamp
            backup.mkdir(mode=0o700)
            require(os.stat(backup).st_dev != os.stat(BOOT).st_dev, "backup must be off ESP")
            inventory = snapshot(BOOT)
            shutil.copytree(BOOT, backup / "boot", symlinks=True, copy_function=shutil.copy2)
            require(snapshot(BOOT) == inventory, "boot files changed during full backup")
            saved = snapshot(backup / "boot")
            require(saved == inventory, "off-ESP backup verification failed")
            write_json(backup / "inventory.json", inventory)
            record("boot-backed-up", {"backup": str(backup), "inventory_sha256": sha(backup / "inventory.json"),
                                       "stock_uki_sha256": STOCK_UKI_SHA256})
            created_parent = not FRAGMENT.parent.exists()
            if created_parent:
                mkdir_root(FRAGMENT.parent, 0o755)
            content = ("# Nitro Control test entry only; stock/default command lines are untouched.\n"
                       "KERNEL_CMDLINE[linux-nitro-test]=" + cmdline)
            record("entry-config-create", {"path": str(FRAGMENT), "before": {"kind": "absent"},
                                            "expected_after_sha256": hashlib.sha256(content.encode()).hexdigest(),
                                            "created_parent": created_parent})
            with FRAGMENT.open("x", encoding="ascii") as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            record("entry-config-created", {"path": str(FRAGMENT), "after_sha256": sha(FRAGMENT)})
            verify_inputs(manifest, paths)
            record("add-test-uki", {"path": str(NEW_UKI), "before": "absent", "expected_after_sha256": sha(paths["uki"]),
                                    "entry": "Omarchy/linux-nitro-test", "external_boot_lock_held": True,
                                    "limine_before_sha256": sha(BOOT / "limine.conf")})
            run(["/usr/bin/limine-entry-tool", "--add-uki", "linux-nitro-test", paths["uki"],
                 "--comment", "Kernel version: " + KREL + "; NVIDIA " + DRIVER + "; nitro test",
                 "--no-hooks", "--no-mutex"])
            require(sha(NEW_UKI) == manifest["artifacts"]["uki"]["sha256"], "installed staged UKI hash differs")
            check_boot_menu(before, (BOOT / "limine.conf").read_text(), cmdline)
            assert_protected(manifest["baseline"]["protected"], deployed=True)
            require(sha(STOCK_UKI) == STOCK_UKI_SHA256 and package_versions() == expected_packages, "protected final baseline changed")
            record("test-entry-installed", {"path": str(NEW_UKI), "after_sha256": sha(NEW_UKI),
                                            "limine_after_sha256": sha(BOOT / "limine.conf"),
                                            "limine_old_after": snapshot(BOOT / "limine.conf.old"),
                                            "default_entry": 2})
        summary.update(status="completed", step="awaiting-manual-test-boot", boot_instruction=(
            "For ONE boot, manually choose Omarchy -> linux-nitro-test in Limine. Leave the default unchanged. "
            "After boot require uname -r=" + KREL + " and /sys/module/acer_wmi/parameters/predator_v4=N "
            "before the separate post-boot acceptance gate; this helper does not reboot or enable control."))
        write_json(cache / "summary.json", summary)
        return summary
    except BaseException as error:
        if summary is not None:
            summary.update(status="failed", error=type(error).__name__ + ": " + str(error),
                           recovery="Artifacts retained. Compare live state with recorded before/after hashes; no automatic rollback/retry.")
            try:
                summary["observed_owned_state"] = {str(path): snapshot(path) for path in (MODROOT, NVTARGET, NVLINK, NEW_UKI, FRAGMENT)}
                summary["observed_packages"] = package_versions()
            except Exception:
                summary["observation_incomplete"] = True
            write_json(cache / "summary.json", summary)
            raise DeploymentError("failed step " + summary["step"] + "; root-owned summary: "
                                  + str(cache / "summary.json") + "; " + str(error)) from error
        raise
    finally:
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--preflight", action="store_true", help="read-only (the default)")
    modes.add_argument("--apply", action="store_true", help="UID 0, installed pkgrel9 and pinned manifest required")
    args = parser.parse_args(argv)
    try:
        manifest, digest, data = load_manifest(args.manifest, args.manifest_sha256, apply=args.apply)
        if args.apply:
            result = apply_deployment(manifest, digest, data)
        else:
            result = {**preflight(manifest), "manifest_sha256": digest}
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except (DeploymentError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "blocked", "error": str(error), "automatic_rollback": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
