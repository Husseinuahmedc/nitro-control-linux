#!/usr/bin/env python3
"""Poll resources while supervising one explicitly supplied, unprivileged argv.

Default/--preflight is read-only: validate cwd, argv, log paths and resources.
Only --run launches anything; there is no automatic make/build command.
The resolved cwd must be under this workspace's build/, and new log files
must resolve under logs/ or build/. Existing files are never overwritten.

Defaults: stop below 12 GiB filesystem-available space (8 GiB OS reserve plus
4 GiB margin), below 2 GiB host MemAvailable, or after four hours. These are
polling policies, NOT a quota, cgroup, security sandbox or firmware Auto
recovery helper. Other writers, filesystem metadata/CoW costs and rapid
allocation can exhaust resources between samples. MemAvailable is host-wide,
not a per-job/cgroup memory limit. Arbitrary explicit commands can write
elsewhere or escape the process group; hostile path replacement is not
sandboxed. No services, system settings, boot files or fan controls are touched
by the guard itself. Uninterruptible tasks and blocking filesystem operations
cannot be given a hard realtime bound by this polling guard.

Child stdin is /dev/null. By default stdout/stderr go directly to a new
workspace file, never an undrained PIPE or an in-memory capture. Guard stdout
contains just one summary; --inherit-output explicitly permits unbounded child
output on inherited streams. JSONL records go to a separate new workspace file.
Cleanup sends TERM then KILL to only the newly created session's process group,
even on normal leader exit. The wall-time limit excludes up to --grace-seconds
(at most five) plus one second for reaping. Escaped descendants are not covered.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
GIB = 1024**3


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def parse_options(argv, workspace):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="explicitly launch argv after --")
    mode.add_argument("--preflight", action="store_true", help="read-only validation (default)")
    parser.add_argument("--cwd", default="build", help="workspace-relative or absolute build directory")
    parser.add_argument("--log", help="new JSONL file under workspace logs/ or build/")
    parser.add_argument("--output-log", help="new child stdout/stderr file (default: beside JSONL)")
    parser.add_argument("--inherit-output", action="store_true", help="inherit child output instead of logging it")
    parser.add_argument("--min-free-gib", type=positive_number, default=12.0)
    parser.add_argument("--min-mem-gib", type=positive_number, default=2.0)
    parser.add_argument("--timeout-seconds", type=positive_number, default=4 * 3600.0)
    parser.add_argument("--poll-seconds", type=positive_number, default=0.5)
    parser.add_argument("--grace-seconds", type=positive_number, default=1.0)
    argv = list(sys.argv[1:] if argv is None else argv)
    separator = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:separator])
    command = argv[separator + 1:]
    if not command or not command[0] or any("\0" in part for part in command):
        parser.error("supply an explicit, valid executable argv after --")
    if not 0.25 <= args.poll_seconds <= 1:
        parser.error("--poll-seconds must be between 0.25 and 1")
    if args.grace_seconds > 5:
        parser.error("--grace-seconds must be at most 5")
    if args.inherit_output and args.output_log:
        parser.error("--inherit-output and --output-log are mutually exclusive")

    try:
        workspace = Path(workspace).resolve(strict=True)
        args.cwd = (workspace / args.cwd).resolve(strict=True)
        if not args.cwd.is_relative_to(workspace / "build") or not args.cwd.is_dir():
            raise ValueError("resolved cwd must be a directory inside workspace/build")
        # Relative PATH entries, including the empty entry, belong to the child cwd.
        search_path = os.pathsep.join(str(args.cwd / item) for item in os.get_exec_path())
        executable = (args.cwd / command[0] if "/" in command[0]
                      else shutil.which(command[0], path=search_path))
        if not executable:
            raise ValueError("executable not found")
        executable = Path(executable)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("command is not an executable file")
        args.command = [str(executable), *command[1:]]

        def new_log_path(value):
            requested = workspace / value
            if requested.is_symlink():
                raise ValueError("log files must not be symlinks")
            resolved = requested.resolve()
            if not any(resolved.is_relative_to(workspace / name) for name in ("logs", "build")):
                raise ValueError("resolved log path must be inside workspace/logs or workspace/build")
            if not resolved.parent.is_dir() or resolved.exists():
                raise ValueError("log parent must exist and log file must not exist")
            return resolved

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        args.log = new_log_path(args.log or f"logs/resource-guard-{stamp}-{os.getpid()}.jsonl")
        args.output_log = (None if args.inherit_output else new_log_path(
            args.output_log or args.log.with_suffix(".output.log")))
        if args.output_log == args.log:
            raise ValueError("JSONL and child output must use different new files")
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return args, workspace


def read_resources(workspace):
    info = os.statvfs(workspace)
    if info.f_bavail < 0 or info.f_frsize <= 0:
        raise ValueError("invalid statvfs availability")
    with Path("/proc/meminfo").open(encoding="ascii") as stream:
        for line in stream:
            fields = line.split()
            if fields and fields[0] == "MemAvailable:":
                if len(fields) != 3 or fields[2] != "kB":
                    raise ValueError("invalid MemAvailable record")
                return info.f_bavail * info.f_frsize, int(fields[1]) * 1024
    raise ValueError("MemAvailable missing from /proc/meminfo")


def open_exclusive(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600), "w", encoding="utf-8")


def stop_group(child, grace):
    if child.pid <= 1 or child.pid == os.getpgrp():
        raise RuntimeError("refusing to signal a group not dedicated to the child")
    # WNOWAIT reserves the leader PID through both signals, even if it has exited.
    # Never poll()/reap it first: a reused numeric PGID could belong to another job.
    os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    try:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            time.sleep(grace)
    finally:
        try:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            child.wait(timeout=1)


def main(argv=None, *, _workspace=ROOT, _resources=None):
    # Injection is internal to offline tests, never a CLI/env source of fake readings.
    if os.geteuid() == 0:
        print("resource-guard refuses EUID 0", file=sys.stderr)
        return 2
    args, workspace = parse_options(argv, _workspace)
    resources = read_resources if _resources is None else _resources
    started = time.monotonic()
    deadline = started + args.timeout_seconds
    child = log = output = None
    requested_signal = 0
    handlers = {}
    reason = error = None
    phase = "monitor"
    summary = {
        "event": "summary", "mode": "run" if args.run else "preflight",
        "cwd": str(args.cwd), "log": str(args.log) if args.run else None,
        "output": "inherit" if args.inherit_output else str(args.output_log),
        "minimum_free_bytes": None, "minimum_mem_available_bytes": None,
        "child_pid": None, "child_pgid": None, "child_exit_code": None,
    }

    def record(data):
        if log is not None:
            log.write(json.dumps({"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                  **data}, allow_nan=False) + "\n")
            log.flush()

    def sample():
        free, memory = resources(workspace)
        if any(type(value) is not int or value < 0 for value in (free, memory)):
            raise ValueError("resource readings must be nonnegative integer byte counts")
        for key, value in (("minimum_free_bytes", free), ("minimum_mem_available_bytes", memory)):
            summary[key] = value if summary[key] is None else min(value, summary[key])
        record({"event": "sample", "free_bytes": free, "mem_available_bytes": memory})
        if free / GIB < args.min_free_gib:
            return "low_disk"
        if memory / GIB < args.min_mem_gib:
            return "low_memory"
        return None

    def request_stop(signum, frame):
        nonlocal requested_signal
        if not requested_signal:
            requested_signal = signum

    try:
        try:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                handlers[signum] = signal.signal(signum, request_stop)
            if args.run and signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
                raise RuntimeError("default SIGCHLD handling is required to own child reaping")
            if args.run:
                log = open_exclusive(args.log)
            record({"event": "start", "workspace": str(workspace), "cwd": str(args.cwd),
                    "argv": args.command, "output": summary["output"],
                    "min_free_gib": args.min_free_gib, "min_mem_gib": args.min_mem_gib,
                    "timeout_seconds": args.timeout_seconds, "poll_seconds": args.poll_seconds,
                    "grace_seconds": args.grace_seconds})
            reason = sample()
            if requested_signal:
                reason = "signal"
            elif reason is None and time.monotonic() >= deadline:
                reason = "timeout"
            if reason is None and not args.run:
                reason = "preflight_ok"
            if reason is None:
                if args.output_log is not None:
                    output = open_exclusive(args.output_log)
                if requested_signal:
                    reason = "signal"
                elif time.monotonic() >= deadline:
                    reason = "timeout"
                else:
                    phase = "spawn"
                    child = subprocess.Popen(
                        args.command, cwd=args.cwd, shell=False, start_new_session=True,
                        stdin=subprocess.DEVNULL, stdout=output,
                        stderr=subprocess.STDOUT if output is not None else None,
                    )
                    phase = "monitor"
                    summary["child_pid"] = summary["child_pgid"] = child.pid
                    record({"event": "spawn", "child_pid": child.pid, "child_pgid": child.pid})
                    while reason is None:
                        if requested_signal:
                            reason = "signal"
                            break
                        if time.monotonic() >= deadline:
                            reason = "timeout"
                            break
                        reason = sample()
                        if requested_signal:
                            reason = "signal"
                        elif reason is None and time.monotonic() >= deadline:
                            reason = "timeout"
                        if reason is not None:
                            break
                        if os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                            reason = "child_exit"
                            break
                        time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
        except Exception as exc:
            reason = "spawn_error" if phase == "spawn" else "monitor_error"
            error = f"{type(exc).__name__}: {exc}"[:512]
        finally:
            # Cleanup comes before any further logging, including after a log failure.
            if child is not None:
                try:
                    stop_group(child, args.grace_seconds)
                except Exception as exc:
                    reason, error = "cleanup_error", f"{type(exc).__name__}: {exc}"[:512]
                summary["child_exit_code"] = child.returncode
            if output is not None:
                try:
                    output.close()
                except Exception as exc:
                    reason, error = "monitor_error", f"{type(exc).__name__}: {exc}"[:512]

        if requested_signal and reason in ("child_exit", "preflight_ok"):
            reason = "signal"
        if reason == "child_exit":
            code = child.returncode if child.returncode >= 0 else 128 - child.returncode
        else:
            code = {"preflight_ok": 0, "low_disk": 3, "low_memory": 3, "timeout": 124,
                    "signal": 128 + requested_signal, "spawn_error": 126}.get(reason, 125)
        summary.update(exit_reason=reason, exit_code=code, received_signal=requested_signal or None,
                       duration_seconds=round(time.monotonic() - started, 6),
                       timestamp_utc=datetime.now(timezone.utc).isoformat())
        if error is not None:
            summary["error"] = error
        try:
            try:
                record(summary)
            finally:
                if log is not None:
                    closing, log = log, None
                    closing.close()
        except Exception as exc:
            summary.update(exit_reason="monitor_error", exit_code=125,
                           error=f"{type(exc).__name__}: {exc}"[:512])
        print(json.dumps(summary, allow_nan=False))
        return summary["exit_code"]
    finally:
        try:
            if log is not None:
                log.close()
        finally:
            for signum, previous in handlers.items():
                signal.signal(signum, previous)


if __name__ == "__main__":
    sys.exit(main())
