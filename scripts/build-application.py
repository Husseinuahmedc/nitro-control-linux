#!/usr/bin/env python3
"""Build a deterministic source bundle and local app package; never install."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
RELEASE = 9
EPOCH = 1791027808


def main():
    if os.geteuid() == 0:
        raise SystemExit("Build the application as the ordinary user.")
    work = ROOT / "build/application-package"
    work.mkdir(parents=True, exist_ok=True)
    for directory in (ROOT / "logs", ROOT / "plan", ROOT / "packages", ROOT / "build/tmp"):
        directory.mkdir(parents=True, exist_ok=True)
    source = work / f"nitro-control-{VERSION}.tar.gz"
    package = ROOT / "packages" / f"nitro-control-{VERSION}-{RELEASE}-any.pkg.tar.zst"
    history = work / "history"
    for previous in (source, package):
        if previous.exists():
            previous_hash = hashlib.sha256(previous.read_bytes()).hexdigest()
            history.mkdir(exist_ok=True)
            saved = history / f"{previous_hash}-{previous.name}"
            if not saved.exists():
                shutil.copy2(previous, saved)
    files = [ROOT / name for name in ("LICENSE", "NOTICE.md", "bin/nitro-control",
             "application/PROTOCOL.md", "application/BACKEND.md", "LICENSES/README.md",
             "LICENSES/SHA256SUMS", "packaging/application/PKGBUILD.in", "scripts/build-application.py")]
    for directory, pattern in (("application/nitro_control", "*.py"), ("application/tests", "*.py"),
                               ("data", "*"), ("LICENSES", "*.txt")):
        files.extend((ROOT / directory).glob(pattern))
    files.extend(ROOT / "docs" / name for name in (
        "ARCHITECTURE.md", "APPLICATION-INSTALL.md", "SUPPORTED-HARDWARE.md", "UNINSTALL.md"))
    files = sorted(set(files), key=lambda path: str(path.relative_to(ROOT)))
    with source.open("wb") as output, gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=EPOCH) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in files:
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"Refusing non-regular source: {path}")
                member = archive.gettarinfo(str(path), f"nitro-control-{VERSION}/{path.relative_to(ROOT)}")
                member.uid = member.gid = 0
                member.uname = member.gname = "root"
                member.mtime = EPOCH
                member.mode = 0o755 if path == ROOT / "bin/nitro-control" else 0o644
                member.pax_headers = {}
                with path.open("rb") as content:
                    archive.addfile(member, content)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    template = (ROOT / "packaging/application/PKGBUILD.in").read_text()
    if template.count("@SOURCE_SHA256@") != 1 or f"pkgrel={RELEASE}\n" not in template:
        raise ValueError("Expected one source checksum placeholder")
    (work / "PKGBUILD").write_text(template.replace("@SOURCE_SHA256@", digest))
    config = work / "makepkg.conf"
    config.write_text("source /etc/makepkg.conf\n"
                      "BUILDENV=(!distcc !color !ccache check !sign)\n"
                      "COMPRESSZST=(zstd -c -T2 -)\n")
    env = {**os.environ, "PATH": "/usr/bin:/bin", "SOURCE_DATE_EPOCH": str(EPOCH),
           "PYTHONDONTWRITEBYTECODE": "1", "MAKEFLAGS": "-j2",
           "BUILDDIR": str(work / "makepkg"), "PKGDEST": str(ROOT / "packages"),
           "SRCDEST": str(work), "LOGDEST": str(ROOT / "logs"),
           "TMPDIR": str(ROOT / "build/tmp")}
    subprocess.run(["/usr/bin/makepkg", "--config", str(config), "--force", "--log"], cwd=work, env=env, check=True)
    if not package.is_file():
        raise ValueError("Application package missing after makepkg")
    report = {"version": VERSION, "package_release": RELEASE, "package_version": f"{VERSION}-{RELEASE}",
              "source": str(source), "source_sha256": digest,
              "source_epoch": EPOCH, "source_files": len(files), "package": str(package),
              "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
              "installed": False}
    (ROOT / "plan/application-package.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
