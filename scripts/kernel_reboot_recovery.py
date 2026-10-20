"""Explicit reboot recovery evidence and structural checks, never a build retry."""

import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time


LEGACY_SCRIPT_SHA = "370dca3ccfe5dd0d60462598f0c9714c93446fc4577a6100af992d9c7cef8592"
DRIVER_SHA = "3f6d2a6d4b56c41baffdc19ca42d5c9fe2663ed85d1e1a98ec01266d01052918"
PATCH_SHA = "94c6dfb8e8208b7ee083229bd0c4f5211d6389ffaa5ac6d140689774de3a6572"


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_file(path):
    with Path(path).open("rb") as stream:
        os.fsync(stream.fileno())


def boot_identity():
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
    btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                 if line.startswith("btime "))
    uptime = time.clock_gettime(time.CLOCK_BOOTTIME)
    wall = time.time()
    if len(boot_id) != 32 or not 0 <= wall - uptime - btime < 2:
        raise RuntimeError("uncertain kernel boot time / realtime clock relationship")
    return {"boot_id": boot_id, "btime_epoch": btime,
            "btime_utc": datetime.fromtimestamp(btime, timezone.utc).isoformat(),
            "observed_at_utc": datetime.fromtimestamp(wall, timezone.utc).isoformat(),
            "monotonic_seconds": time.monotonic(), "boottime_seconds": uptime,
            "clock_ticks_per_second": os.sysconf("SC_CLK_TCK")}


def process_identity(pid, boot=None):
    try:
        value = Path(f"/proc/{int(pid)}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = value.rsplit(")", 1)[1].split()
    return {"pid": int(pid), "boot_id": (boot or boot_identity())["boot_id"],
            "starttime_ticks": int(fields[19]), "ppid": int(fields[1]),
            "pgrp": int(fields[2]), "session": int(fields[3])}


def journal(arguments):
    command = ["/usr/bin/journalctl", "--no-pager", "--output=json", *arguments]
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TZ": "UTC"}, timeout=20)
    if completed.returncode not in (0, 1) or completed.stderr.strip():
        raise RuntimeError(f"journal evidence unavailable: {completed.stderr.strip()}")
    text = completed.stdout.strip()
    if not text:
        return [], command
    if text.startswith("["):
        return json.loads(text), command
    return [json.loads(line) for line in text.splitlines()], command


def utc_seconds(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise RuntimeError("recovery needs an unambiguous UTC stage start")
    return stamp.timestamp()


def reboot_budget(state, stage, boot, boots):
    if len(boots) != 2 or [item["index"] for item in boots] != [-1, 0]:
        raise RuntimeError("need evidence of the immediately previous and current boots")
    previous, current = boots
    if current["boot_id"] != boot["boot_id"] or previous["boot_id"] == current["boot_id"]:
        raise RuntimeError("journal/kernel boot ID mismatch; no proven reboot")
    start = utc_seconds(stage.get("original_started_at_utc", stage["started_at_utc"]))
    active_start = utc_seconds(stage["started_at_utc"])
    updated = utc_seconds(state["updated_at_utc"])
    if not (previous["first_entry"] / 1e6 <= start <= active_start <= updated
            <= previous["last_entry"] / 1e6 < boot["btime_epoch"]
            <= current["first_entry"] / 1e6):
        raise RuntimeError("stage timestamps do not establish the requested reboot boundary")
    recorded_boot = stage.get("boot", {}).get("boot_id")
    if recorded_boot is not None and recorded_boot != previous["boot_id"]:
        raise RuntimeError("recorded attempt boot does not match previous journal boot")
    # /proc/stat btime is integer seconds; add a full second before rounding up.
    elapsed = math.ceil(boot["btime_epoch"] + 1 - start)
    remaining = 3600 - elapsed
    if not 0 < elapsed < 3600 or remaining <= 0:
        raise RuntimeError(f"reboot-inclusive stage budget exhausted/uncertain: {elapsed}s consumed")
    return {"original_started_at_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "elapsed_upper_bound_seconds": elapsed, "remaining_timeout_seconds": remaining,
            "budget_seconds": 3600, "btime_rounding_allowance_seconds": 1,
            "method": "ceil(current kernel btime + 1 - original active UTC start); includes downtime",
            "previous_boot": previous, "current_boot": current,
            "legacy_boot_association": recorded_boot is None}


def assert_quiescent(p, state):
    observations = {}
    for key in ("pipeline_pid", "guard_pid", "child_pid"):
        pid = state.get(key)
        if not isinstance(pid, int) or pid <= 1 or process_identity(pid) is not None:
            raise RuntimeError(f"old {key} is present or its ownership is uncertain: {pid}")
        observations[key] = {"pid": pid, "present": False}
    tools = {"make", "gmake", "makepkg", "cc", "c++", "gcc", "g++", "cc1", "cc1plus", "clang", "clang++", "rustc",
             "bindgen", "pahole", "ld", "ld.bfd", "ld.lld", "ld.gold", "lld", "collect2", "ar", "as", "modpost", "objtool"}
    runners = {"python", "python3", "bash", "sh", "perl", "ruby"}
    scanned = 0
    cwd_checked = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if not argv[0]:
                continue
            command = [part.decode("utf-8", "replace") for part in argv if part]
            scanned += 1
            pipeline_job = any(part.endswith("/kernel-pipeline.py") for part in command) and not any(
                option in command for option in ("--status", "--plan", "--recover-reboot"))
            references_tree = any(str(p.TREE) in part or str(p.workdir(state)) in part for part in command)
            if pipeline_job or references_tree:
                raise RuntimeError(f"active kernel job still exists: PID {entry.name}")
            if Path(command[0]).name not in tools | runners:
                continue
            # Non-dumpable unrelated daemons need no cwd inspection; build candidates do.
            cwd = (entry / "cwd").resolve(strict=True)
            cwd_checked += 1
        except (FileNotFoundError, ProcessLookupError):
            continue
        if cwd.is_relative_to(p.TREE) or cwd.is_relative_to(p.workdir(state)):
            raise RuntimeError(f"active kernel job still exists: PID {entry.name}")
    return {"old_processes": observations, "same_user_processes_checked": scanned,
            "build_candidate_working_directories_checked": cwd_checked,
            "owner_lock": "exclusive nonblocking lock held by recovery supervisor",
            "active_kernel_jobs_found": False}


def check_interrupted_attempt(stage):
    for attempt in [stage, *stage.get("attempts", [])]:
        if attempt.get("guard_result") is not None:
            raise RuntimeError("interrupted attempt has a terminal guard result; recovery is not a retry")
        for key in ("resources", "summary"):
            path = Path(attempt["logs"][key])
            for line in path.read_text().splitlines():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise RuntimeError("uncertain non-object guard log record")
                if record.get("event") == "summary" or record.get("exit_reason") is not None:
                    raise RuntimeError("terminal guard summary exists; reboot recovery refuses to retry it")
                if record.get("event") == "start" and any(
                        record.get(field) != value for field, value in
                        (("min_free_gib", 12), ("min_mem_gib", 2))):
                    raise RuntimeError("interrupted guard thresholds changed")
                if record.get("event") == "sample" and (
                        record.get("free_bytes", -1) < 12 * 1024**3
                        or record.get("mem_available_bytes", -1) < 2 * 1024**3):
                    raise RuntimeError("interrupted guard recorded a resource-limit violation")
        output = Path(attempt["logs"]["output"]).read_text(errors="replace")
        if any(text in output.lower() for text in
               ("no space left", "out of memory", "fatal error:", "*** [", "traceback (")):
            raise RuntimeError("interrupted build output contains failure evidence")


def recover_reboot(p, state, console):
    if state.get("status") != "running" or state.get("limits_per_stage") != p.LIMITS:
        raise RuntimeError("reboot recovery requires the original running state and exact limits")
    if any(state.get(key) != expected for key, expected in
           (("kernel_release", p.KREL), ("config_sha256", p.CONFIG_SHA),
            ("checkpoint_sha256", p.OBJECT_SHA), ("recipe_sha256", p.RECIPE_SHA))) or state.get("environment") != p.environment():
        raise RuntimeError("recorded build identity or immutable input metadata changed")
    if [(item["name"], item["kind"], item["targets"]) for item in state["stages"]] != p.STAGES:
        raise RuntimeError("declared stage plan changed")
    running = [item for item in state["stages"] if item["status"] == "running"]
    if len(running) != 1 or running[0]["name"] != state["stage"] or running[0]["kind"] != "make":
        raise RuntimeError("need exactly one interrupted compile stage")
    stage = running[0]
    index = state["stages"].index(stage)
    if any(item["status"] != "succeeded" for item in state["stages"][:index]) or any(
            item["status"] != "pending" for item in state["stages"][index + 1:]):
        raise RuntimeError("failed/ambiguous stage history cannot be reboot-recovered")
    quiescence = assert_quiescent(p, state)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    directory = p.ROOT / "plan/kernel-pipeline-recoveries" / stamp
    directory.mkdir(parents=True)
    sync_directory(directory.parent)
    snapshot = {"requested_at_utc": p.now(), "status": "checking", "snapshot_directory": str(directory),
                "quiescence": quiescence, "files": {}}
    report_path = directory / "recovery.json"
    try:
        paths = {p.STATE}
        for item in state["stages"][:index + 1]:
            paths.update(Path(value) for value in item["logs"].values())
            result = p.workdir(state) / "results" / (item["name"] + ".json")
            if result.exists():
                paths.add(result)
        paths.add(Path(state["logs"]["pipeline"]))
        for source in sorted(paths):
            if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(p.ROOT):
                raise RuntimeError(f"cannot safely snapshot original evidence: {source}")
            relative = source.relative_to(p.ROOT)
            destination = directory / "original" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            sync_file(destination)
            sync_directory(destination.parent)
            snapshot["files"][str(relative)] = {"snapshot": str(destination),
                                               "bytes": source.stat().st_size, "sha256": p.sha256(destination)}
            if p.sha256(source) != p.sha256(destination):
                raise RuntimeError("original evidence changed during snapshot")
        p.atomic_json(report_path, snapshot)
        check_interrupted_attempt(stage)
        boot = boot_identity()
        boots, boots_command = journal(["--list-boots", "-n", "2"])
        budget = reboot_budget(state, stage, boot, boots)
        snapshot.update(boot=boot, budget=budget, journal_boots_command=boots_command)
        fields = "_BOOT_ID,__REALTIME_TIMESTAMP,__MONOTONIC_TIMESTAMP"
        tail, tail_command = journal(["-b", boots[0]["boot_id"], "--since",
                                      "@" + str(utc_seconds(stage["started_at_utc"]) - 1),
                                      "--output-fields=" + fields])
        offsets = [int(item["__REALTIME_TIMESTAMP"]) - int(item["__MONOTONIC_TIMESTAMP"]) for item in tail]
        if not offsets or max(offsets) - min(offsets) > 1_000_000 or any(
                item["_BOOT_ID"] != boots[0]["boot_id"] for item in tail):
            raise RuntimeError("old-boot clock evidence is missing or stepped near the interrupted attempt")
        early, early_command = journal(["-b", boot["boot_id"], "--until",
                                        "@" + str(boots[1]["first_entry"] / 1e6 + 1), "-n", "1",
                                        "--output-fields=" + fields])
        if len(early) != 1 or not 0 <= (
                int(early[0]["__REALTIME_TIMESTAMP"]) - int(early[0]["__MONOTONIC_TIMESTAMP"])) / 1e6 - boot["btime_epoch"] < 2:
            raise RuntimeError("new-boot journal clock does not corroborate kernel btime")
        oom, oom_command = journal(["-b", boots[0]["boot_id"], "-k", "--grep",
                                    "(?i)out of memory|oom-kill|oom_reaper|Killed process|page allocation failure|I/O error|BTRFS.*error",
                                    "--output-fields=" + fields + ",MESSAGE"])
        snapshot["journal_evidence"] = {"previous_tail": tail, "previous_tail_command": tail_command,
                                         "current_early": early, "current_early_command": early_command,
                                         "resource_diagnostics": oom, "resource_diagnostics_command": oom_command,
                                         "caveat": "no recorded match is not proof of the cause of reboot or absence of lost diagnostics"}
        if oom:
            raise RuntimeError("previous boot has resource/I/O diagnostics; main must review")
        candidate = copy.deepcopy(state)
        current_stat = p.object_stat()
        if any(current_stat[key] != value for key, value in state["checkpoint_stat"].items() if key != "st_dev"):
            raise RuntimeError("checkpoint inode/size/timestamps changed; device-only re-pin is insufficient")
        candidate["checkpoint_stat"] = current_stat
        script_key = str(p.SCRIPT.relative_to(p.ROOT))
        old_script_sha = state["script_pins"][script_key]
        current_script_sha = p.sha256(p.SCRIPT)
        if old_script_sha != current_script_sha and old_script_sha != LEGACY_SCRIPT_SHA:
            raise RuntimeError("unrecognized prior pipeline pin; cannot authorize a self-recovery re-pin")
        candidate["script_pins"][script_key] = current_script_sha
        helper = Path(__file__).resolve()
        helper_key = str(helper.relative_to(p.ROOT))
        if helper_key in state["script_pins"] and p.sha256(helper) != state["script_pins"][helper_key]:
            raise RuntimeError("previously pinned recovery helper changed")
        candidate["script_pins"][helper_key] = p.sha256(helper)
        inputs = {str((p.TREE / "drivers/platform/x86/acer-wmi.c").relative_to(p.ROOT)): DRIVER_SHA,
                  "packaging/0001-acer-wmi-anv15-51.patch": PATCH_SHA}
        if any(relative in state.get("input_pins", {}) and state["input_pins"][relative] != expected
               for relative, expected in inputs.items()):
            raise RuntimeError("recorded prepared-input pins disagree with the approved inputs")
        candidate["input_pins"] = {**state.get("input_pins", {}), **inputs}
        for relative, expected in candidate["input_pins"].items():
            if p.sha256(p.ROOT / relative) != expected:
                raise RuntimeError(f"prepared input pin mismatch: {relative}")
        if (p.TREE / "localversion.10-pkgrel").read_text() != "-1\n" or (
                p.TREE / "localversion.20-pkgname").read_text() != "-nitro-test\n":
            raise RuntimeError("prepared localversion inputs changed")
        verified = p.verify_pins(candidate, full_object=True)
        for completed in state["stages"][:index]:
            summary = completed.get("guard_result", {})
            if not (summary.get("exit_reason") == "child_exit" and summary.get("exit_code") == 0
                    and summary.get("child_exit_code") == 0 and 0 <= summary.get("duration_seconds", -1) <= 3600
                    and summary.get("minimum_free_bytes", -1) >= 12 * 1024**3
                    and summary.get("minimum_mem_available_bytes", -1) >= 2 * 1024**3):
                raise RuntimeError(f"completed stage lacks a successful within-limits result: {completed['name']}")
            for key in ("resources", "summary"):
                for line in Path(completed["logs"][key]).read_text().splitlines():
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and record.get("event") == "summary" and not (
                            record.get("exit_reason") == "child_exit" and record.get("exit_code") == 0
                            and record.get("child_exit_code") == 0):
                        raise RuntimeError("resource/tool-failed summary exists despite completed state")
            result_path = p.workdir(state) / "results" / (completed["name"] + ".json")
            if json.loads(result_path.read_text()) != completed["result"]:
                raise RuntimeError(f"completed stage result was lost or changed: {completed['name']}")
        attempt = copy.deepcopy({key: value for key, value in stage.items() if key != "attempts"})
        attempt.update(outcome="interrupted_by_confirmed_reboot", guard_summary_status="missing; not reconstructed",
                       recovery_snapshot=str(directory), previous_boot_id=boots[0]["boot_id"])
        recovered_stage = candidate["stages"][index]
        for key in ("started_at_utc", "logs", "guard_argv", "guard_pid", "child_pid", "guard_identity", "child_identity",
                    "boot", "monotonic_started_seconds", "boottime_started_seconds", "checks_before"):
            recovered_stage.pop(key, None)
        recovered_stage.update(status="pending", attempts=[*stage.get("attempts", []), attempt],
                               original_started_at_utc=budget["original_started_at_utc"],
                               reboot_remaining_timeout_seconds=budget["remaining_timeout_seconds"],
                               recovery_scan_required=True, recovery_snapshot=str(directory))
        snapshot.update(status="approved", verified_pins=verified, input_pins=candidate["input_pins"],
                        old_checkpoint_stat=state["checkpoint_stat"], new_checkpoint_stat=current_stat,
                        old_script_pins=state["script_pins"], new_script_pins=candidate["script_pins"],
                        previous_state_sha256=p.sha256(p.STATE), completed_stages_preserved=index,
                        checked_at_utc=p.now())
        p.atomic_json(report_path, snapshot)
        snapshot["quiescence_before_commit"] = assert_quiescent(p, state)
        p.atomic_json(report_path, snapshot)
        candidate.update(schema_version=2, status="ready", finish_reason=None, boot=boot,
                         pipeline_pid=os.getpid(), guard_pid=None, child_pid=None,
                         pipeline_identity=process_identity(os.getpid(), boot),
                         log_namespace=state["run_id"] + "-reboot-" + stamp,
                         recovery_history=[*state.get("recovery_history", []),
                                           {"event": "explicit_reboot_recovery", "recorded_at_utc": p.now(),
                                            "record": str(report_path), "budget": budget,
                                            "old_script_pins": state["script_pins"],
                                            "new_script_pins": candidate["script_pins"],
                                            "old_checkpoint_stat": state["checkpoint_stat"],
                                            "new_checkpoint_stat": current_stat}],
                         logs={**state["logs"], "previous_pipeline": state["logs"]["pipeline"],
                               "pipeline": str(console)})
        p.save(candidate)
        print(f"{p.now()} REBOOT RECOVERY {stage['name']}: {budget['elapsed_upper_bound_seconds']}s charged, "
              f"{budget['remaining_timeout_seconds']}s remaining; evidence {report_path}", flush=True)
        return candidate
    except Exception as error:
        snapshot.update(status="blocked", blocker=f"{type(error).__name__}: {error}", checked_at_utc=p.now())
        p.atomic_json(report_path, snapshot)
        raise RuntimeError(f"reboot recovery blocked: {error}; original state retained; evidence {report_path}") from error


def elf_problem(path):
    size = path.stat().st_size
    if size == 0:
        return "zero-length object"
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) < 16 or header[:4] != b"\x7fELF":
            return "missing/truncated ELF identification"
        elf_class, encoding = header[4:6]
        if elf_class not in (1, 2) or encoding not in (1, 2):
            return "invalid ELF class/encoding"
        endian = "<" if encoding == 1 else ">"
        is64 = elf_class == 2
        header_size, section_size, program_size = (64, 64, 56) if is64 else (52, 40, 32)
        if len(header) < header_size:
            return "truncated ELF header"
        fields = struct.unpack(endian + ("HHIQQQIHHHHHH" if is64 else "HHIIIIIHHHHHH"), header[16:header_size])
        kind, _, version, _, phoff, shoff, _, ehsize, phsize, phnum, shsize, shnum, shstr = fields
        if kind != 1 or version != 1 or ehsize != header_size or shsize != section_size:
            return "invalid relocatable ELF header"
        if not shoff or shoff + section_size > size:
            return "missing/truncated ELF section table"
        stream.seek(shoff)
        first = struct.unpack(endian + ("IIQQQQIIQQ" if is64 else "IIIIIIIIII"), stream.read(section_size))
        shnum = shnum or first[5]
        phnum = first[7] if phnum == 0xFFFF else phnum
        shstr = first[6] if shstr == 0xFFFF else shstr
        if not shnum or shoff + shnum * section_size > size or shstr >= shnum:
            return "truncated/out-of-range ELF section table"
        if phnum and (phsize != program_size or phoff + phnum * phsize > size):
            return "truncated ELF program table"
        stream.seek(shoff)
        for index in range(shnum):
            section = struct.unpack(endian + ("IIQQQQIIQQ" if is64 else "IIIIIIIIII"), stream.read(section_size))
            if index and section[1] != 8 and section[4] + section[5] > size:
                return "section data extends past end of ELF"
    return None


def archive_problem(path):
    size = path.stat().st_size
    with path.open("rb") as stream:
        magic = stream.read(8)
        if magic not in (b"!<arch>\n", b"!<thin>\n"):
            return "missing/truncated archive header"
        while stream.tell() < size:
            header = stream.read(60)
            if len(header) != 60 or header[58:] != b"`\n":
                return "truncated/invalid archive member header"
            try:
                member_size = int(header[48:58])
            except ValueError:
                return "invalid archive member size"
            if member_size < 0:
                return "negative archive member size"
            embedded = magic == b"!<arch>\n" or header[:16].strip() in (b"/", b"//", b"/SYM64/")
            if embedded:
                if stream.tell() + member_size > size:
                    return "archive member extends past end of file"
                stream.seek(member_size + member_size % 2, os.SEEK_CUR)
        if stream.tell() != size:
            return "truncated archive alignment"
    return None


def scan_generated(p, state, stage):
    directory = p.workdir(state) / "quarantine" / state["log_namespace"]
    directory.mkdir(parents=True)
    sync_directory(directory.parent)
    report_path = directory / "inspection.json"
    report = {"started_at_utc": p.now(), "tree": str(p.TREE), "status": "scanning",
              "scope": "regular .o/.a files only; no symlinks followed",
              "caveat": "structural ELF/archive bounds, not a content checksum proof for unpinned objects",
              "objects_checked": 0, "archives_checked": 0, "invalid": []}
    try:
        files = []
        for parent, directories, names in os.walk(p.TREE, followlinks=False):
            directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
            files.extend(Path(parent) / name for name in names if name.endswith((".o", ".a"))
                         and not (Path(parent) / name).is_symlink())
        for path in sorted(files):
            if not path.resolve().is_relative_to(p.TREE) or not path.is_file():
                raise RuntimeError(f"uncertain generated output: {path}")
            kind = "objects_checked" if path.suffix == ".o" else "archives_checked"
            report[kind] += 1
            problem = elf_problem(path) if path.suffix == ".o" else archive_problem(path)
            if problem:
                report["invalid"].append({"relative_path": str(path.relative_to(p.TREE)), "reason": problem,
                                          "bytes": path.stat().st_size, "sha256": p.sha256(path), "status": "found"})
        if any(item["relative_path"] == "vmlinux.o" for item in report["invalid"]):
            raise RuntimeError("pinned checkpoint is structurally invalid; never quarantine/rebuild it")
        report["status"] = "quarantining" if report["invalid"] else "clean"
        p.atomic_json(report_path, report)
        for item in report["invalid"]:
            source = p.TREE / item["relative_path"]
            destination = directory / "outputs" / item["relative_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            item.update(quarantine_path=str(destination), status="move_planned")
            p.atomic_json(report_path, report)
            if destination.exists() or source.is_symlink() or p.sha256(source) != item["sha256"]:
                raise RuntimeError("generated output changed during quarantine")
            source.rename(destination)
            sync_file(destination)
            sync_directory(source.parent)
            sync_directory(destination.parent)
            item["status"] = "preserved_in_quarantine"
            p.atomic_json(report_path, report)
        report.update(status="completed", finished_at_utc=p.now())
        p.atomic_json(report_path, report)
        print(f"{p.now()} STRUCTURAL CHECK {report['objects_checked']} objects, {report['archives_checked']} archives; "
              f"{len(report['invalid'])} invalid outputs preserved; report {report_path}", flush=True)
        return str(report_path)
    except Exception as error:
        report.update(status="blocked", blocker=str(error), finished_at_utc=p.now())
        p.atomic_json(report_path, report)
        raise
