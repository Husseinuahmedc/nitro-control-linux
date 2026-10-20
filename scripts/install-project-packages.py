#!/usr/bin/env python3
"""Install only the reviewed project archives through a scoped pacman prompt."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def table_matches(text, expected, previous=None):
    previous = previous or {}
    matches = list(re.finditer(r"(?m)^Package \((\d+)\)[^\n]*\n", text))
    if len(matches) != 1 or int(matches[0][1]) != len(expected):
        return False
    upgrade = "Old Version" in matches[0].group()
    if upgrade != bool(previous):
        return False
    end = text.find("Total Installed Size:", matches[0].end())
    if end < 0:
        return False
    rows = [line.split() for line in text[matches[0].end():end].splitlines() if line.strip()]
    if upgrade:
        return (len(rows) == len(expected) and all(len(row) == 5 for row in rows)
                and {row[0]: row[1] for row in rows} == previous
                and {row[0]: row[2] for row in rows} == expected)
    return (len(rows) == len(expected) and all(len(row) == 4 for row in rows)
            and {row[0]: row[1] for row in rows} == expected)


def inventory():
    return dict(line.split(" ", 1) for line in subprocess.check_output(["pacman", "-Q"], text=True).splitlines())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", action="store_true", required=True)
    parser.add_argument("--install-approved", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run as the desktop user; only native pacman is elevated")
    manifest = json.loads((ROOT / "plan/application-package.json").read_text())
    package = Path(manifest["package"])
    if manifest.get("package_version") != "0.1.0-9" or package != ROOT / "packages/nitro-control-0.1.0-9-any.pkg.tar.zst":
        raise ValueError("Unexpected application archive path")
    if hashlib.sha256(package.read_bytes()).hexdigest() != manifest["package_sha256"]:
        raise ValueError("Application archive checksum changed")
    listing = subprocess.check_output(["bsdtar", "-tf", str(package)], text=True).splitlines()
    if ".INSTALL" in listing or any(name.startswith(("boot/", "etc/", "usr/lib/modules/")) for name in listing):
        raise ValueError("Unexpected application scriptlet or boot/config/kernel payload")
    expected = {"nitro-control": "0.1.0-9"}
    preview = subprocess.check_output(["pacman", "--color", "never", "-U", "--print",
                                       "--print-format", "%n\t%v", str(package)], text=True)
    if preview.splitlines() != ["nitro-control\t0.1.0-9"]:
        raise ValueError(f"Unexpected dependency transaction: {preview}")
    before = inventory()
    previous = {name: before[name] for name in expected if name in before}
    if previous and previous != {"nitro-control": "0.1.0-8"}:
        raise ValueError("Unexpected installed application version; stop for review")
    print(f"Verified local, unsigned project archive SHA-256: {manifest['package_sha256']}", flush=True)
    if not args.install_approved:
        print("Preflight only; no installation performed")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    log = ROOT / "logs" / f"application-install-{stamp}.log"
    session = {"expected": expected, "confirmed": False, "package_sha256": manifest["package_sha256"]}
    process = subprocess.Popen(["pkexec", "--disable-internal-agent", "/usr/bin/pacman", "--color", "never",
                                "-U", "--needed", str(package)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = ""
    with log.open("x") as output:
        while selector.get_map():
            for key, _ in selector.select(1):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                text = data.decode("utf-8", "replace")
                output.write(text)
                output.flush()
                print(text, end="", flush=True)
                pending += text
                if re.search(r"\[[Yy]/[Nn]\]", pending):
                    accept = (not session["confirmed"] and ":: Proceed with installation? [Y/n]" in pending
                              and table_matches(pending, expected, previous))
                    process.stdin.write(b"y\n" if accept else b"n\n")
                    process.stdin.flush()
                    session["confirmed"] = session["confirmed"] or accept
                    pending = ""
    selector.close()
    session["exit_code"] = process.wait()
    process.stdin.close()
    after = inventory()
    session["added"] = {name: after[name] for name in after.keys() - before.keys()}
    session["removed"] = sorted(before.keys() - after.keys())
    session["changed"] = {name: [before[name], after[name]] for name in before.keys() & after.keys()
                          if before[name] != after[name]}
    new = {name: version for name, version in expected.items() if name not in previous}
    changes = {name: [version, expected[name]] for name, version in previous.items()}
    good = (session["confirmed"] and session["exit_code"] == 0 and session["added"] == new
            and not session["removed"] and session["changed"] == changes)
    session["status"] = "INSTALLED" if good else "STOP: inspect transaction"
    session["log"] = str(log)
    (ROOT / "plan/application-installation.json").write_text(json.dumps(session, indent=2) + "\n")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
