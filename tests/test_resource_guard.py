"""Offline resource-guard tests: tiny Python processes, no real resource pressure."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("resource_guard", ROOT / "scripts/resource-guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
HEALTHY = (64 * guard.GIB, 8 * guard.GIB)
SLEEPER = "import time; time.sleep(5)"


class ResourceGuardTests(unittest.TestCase):
    def setUp(self):
        build = ROOT / "build"
        build.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=build, prefix="test-resource-guard-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        (self.workspace / "build").mkdir()
        (self.workspace / "logs").mkdir()
        self.serial = 0

    def arguments(self, extra=(), command=None, run=True):
        self.serial += 1
        args = ["--cwd", "build", "--log", f"logs/test-{self.serial}.jsonl",
                "--poll-seconds", "0.25", "--grace-seconds", "0.05"]
        if run:
            args.append("--run")
        return [*args, *extra, "--", *(command or [sys.executable, "-c", "pass"])]

    def invoke(self, extra=(), command=None, resources=None, run=True):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = guard.main(self.arguments(extra, command, run), _workspace=self.workspace,
                              _resources=resources or (lambda _: HEALTHY))
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        report = json.loads(stdout.getvalue())
        self.assertEqual(code, report["exit_code"])
        if report["child_pid"] is not None:
            self.assertNotEqual(report["child_pgid"], os.getpgrp())
            self.assertEqual(report["child_pid"], report["child_pgid"])
            with self.assertRaises(ChildProcessError):
                os.waitpid(report["child_pid"], os.WNOHANG)
        return report

    def invalid(self, args):
        with redirect_stderr(io.StringIO()), mock.patch.object(guard.subprocess, "Popen") as spawn:
            with self.assertRaises(SystemExit) as raised:
                guard.main(args, _workspace=self.workspace, _resources=lambda _: HEALTHY)
        self.assertEqual(raised.exception.code, 2)
        spawn.assert_not_called()

    def assert_not_live(self, pid):
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                return
            if state == "Z":  # Orphan zombies await the host's reaper, not more signals.
                return
            time.sleep(0.01)
        self.fail(f"dummy descendant {pid} is still live")

    def test_default_preflight_is_read_only_and_uses_default_policy(self):
        with mock.patch.object(guard.subprocess, "Popen") as spawn:
            report = self.invoke(run=False)
        spawn.assert_not_called()
        self.assertEqual(report["exit_reason"], "preflight_ok")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["minimum_free_bytes"], HEALTHY[0])
        self.assertEqual(report["minimum_mem_available_bytes"], HEALTHY[1])
        self.assertEqual(list((self.workspace / "logs").iterdir()), [])
        args, _ = guard.parse_options(["--", sys.executable, "-c", "pass"], self.workspace)
        self.assertEqual((args.min_free_gib, args.min_mem_gib, args.timeout_seconds),
                         (12, 2, 14400))
        self.assertTrue(0.25 <= args.poll_seconds <= 1)
        self.assertFalse(args.run)
        self.assertEqual(self.invoke(extra=["--preflight"], run=False)["exit_code"], 0)

    def test_healthy_exit_and_timestamped_logs(self):
        report = self.invoke(command=[sys.executable, "-c",
                                      "import sys; print('dummy'); print('stderr', file=sys.stderr)"])
        self.assertEqual((report["exit_reason"], report["exit_code"], report["child_exit_code"]),
                         ("child_exit", 0, 0))
        events = [json.loads(line) for line in Path(report["log"]).read_text().splitlines()]
        self.assertEqual(events[0]["event"], "start")
        self.assertIn("spawn", [event["event"] for event in events])
        self.assertIn("sample", [event["event"] for event in events])
        self.assertEqual(events[-1], report)
        for event in events:
            self.assertIsNotNone(datetime.fromisoformat(event["timestamp_utc"]).tzinfo)
        self.assertCountEqual(Path(report["output"]).read_text().splitlines(), ["stderr", "dummy"])
        self.assertEqual(Path(report["log"]).stat().st_mode & 0o777, 0o600)
        self.assertGreater(report["duration_seconds"], 0)

    def test_nonzero_and_signal_child_exit_codes(self):
        for script, expected, child_code in (
            ("raise SystemExit(7)", 7, 7),
            ("import os, signal; os.kill(os.getpid(), signal.SIGTERM)", 143, -15),
        ):
            with self.subTest(script=script):
                report = self.invoke(command=[sys.executable, "-c", script])
                self.assertEqual(report["exit_code"], expected)
                self.assertEqual(report["child_exit_code"], child_code)

    def test_low_resources_before_launch_never_spawn(self):
        for values, reason in (((11 * guard.GIB, HEALTHY[1]), "low_disk"),
                               ((HEALTHY[0], guard.GIB), "low_memory")):
            with self.subTest(reason=reason), mock.patch.object(guard.subprocess, "Popen") as spawn:
                report = self.invoke(resources=lambda _: values)
                spawn.assert_not_called()
                self.assertEqual(report["exit_reason"], reason)
                self.assertEqual(report["exit_code"], 3)
                self.assertIsNone(report["child_pid"])
                self.assertFalse(Path(report["output"]).exists())

    def test_invalid_or_failed_initial_monitor_never_spawns(self):
        for value in (OSError("monitor unavailable"), (-1, HEALTHY[1]),
                      (float("nan"), HEALTHY[1]), (HEALTHY[0], None)):
            with self.subTest(value=value), mock.patch.object(guard.subprocess, "Popen") as spawn:
                report = self.invoke(resources=mock.Mock(side_effect=[value]))
                spawn.assert_not_called()
                self.assertEqual(report["exit_reason"], "monitor_error")
                self.assertEqual(report["exit_code"], 125)

    def test_simulated_resource_drops_and_monitor_exception_clean_up(self):
        for value, reason in (((11 * guard.GIB, HEALTHY[1]), "low_disk"),
                              ((HEALTHY[0], guard.GIB), "low_memory"),
                              (OSError("dummy monitoring failure"), "monitor_error")):
            with self.subTest(reason=reason):
                report = self.invoke(command=[sys.executable, "-c", SLEEPER],
                                     resources=mock.Mock(side_effect=[HEALTHY, value]))
                self.assertEqual(report["exit_reason"], reason)
                self.assertIsNotNone(report["child_pid"])
                self.assertLess(report["child_exit_code"], 0)
                if reason == "low_disk":
                    self.assertEqual(report["minimum_free_bytes"], value[0])
                if reason == "low_memory":
                    self.assertEqual(report["minimum_mem_available_bytes"], value[1])

    def test_timeout_does_not_signal_other_groups(self):
        unrelated = subprocess.Popen([sys.executable, "-c", SLEEPER], cwd=self.workspace / "build",
                                     start_new_session=True, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def clean_unrelated():
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=1)

        self.addCleanup(clean_unrelated)
        with mock.patch.object(guard.os, "killpg", wraps=os.killpg) as killpg:
            report = self.invoke(extra=["--timeout-seconds", "0.35"],
                                 command=[sys.executable, "-c", SLEEPER])
        self.assertEqual((report["exit_reason"], report["exit_code"]), ("timeout", 124))
        self.assertIsNone(unrelated.poll())
        self.assertEqual(killpg.call_args_list, [mock.call(report["child_pgid"], signal.SIGTERM),
                                                mock.call(report["child_pgid"], signal.SIGKILL)])
        self.assertLess(report["duration_seconds"], 2)

    def descendant_command(self, leader_exits):
        ready = self.workspace / "build" / "descendant.pid"
        child_script = ("import os, signal, time; from pathlib import Path; "
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(5)")
        script = ("import signal, subprocess, sys, time\n"
                  "from pathlib import Path\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  f"subprocess.Popen([sys.executable, '-c', {child_script!r}])\n"
                  f"ready = Path({str(ready)!r})\n"
                  "while not ready.exists(): time.sleep(0.005)\n" +
                  ("raise SystemExit(0)\n" if leader_exits else "time.sleep(5)\n"))
        return [sys.executable, "-c", script], ready

    def test_remaining_descendant_killed_after_leader_exits(self):
        command, ready = self.descendant_command(leader_exits=True)
        report = self.invoke(command=command)
        self.assertEqual((report["exit_reason"], report["child_exit_code"]), ("child_exit", 0))
        self.assertTrue(ready.exists())
        self.assert_not_live(int(ready.read_text()))

    def test_timeout_kills_sigterm_ignoring_leader_and_descendant(self):
        command, ready = self.descendant_command(leader_exits=False)
        report = self.invoke(extra=["--timeout-seconds", "0.6"], command=command)
        self.assertEqual((report["exit_reason"], report["child_exit_code"]), ("timeout", -9))
        self.assertTrue(ready.exists())
        self.assert_not_live(int(ready.read_text()))

    def test_user_signals_after_launch_restore_handlers_and_clean_up(self):
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous = signal.getsignal(signum)
            calls = 0

            def resources(_):
                nonlocal calls
                calls += 1
                if calls == 2:
                    os.kill(os.getpid(), signum)
                return HEALTHY

            with self.subTest(signum=signum):
                report = self.invoke(command=[sys.executable, "-c", SLEEPER], resources=resources)
                self.assertEqual(report["exit_reason"], "signal")
                self.assertEqual(report["exit_code"], 128 + signum)
                self.assertEqual(report["received_signal"], signum)
                self.assertLess(report["child_exit_code"], 0)
                self.assertEqual(signal.getsignal(signum), previous)

    def test_signal_before_launch_does_not_spawn(self):
        def resources(_):
            os.kill(os.getpid(), signal.SIGTERM)
            return HEALTHY

        with mock.patch.object(guard.subprocess, "Popen") as spawn:
            report = self.invoke(resources=resources)
        spawn.assert_not_called()
        self.assertEqual((report["exit_reason"], report["exit_code"]), ("signal", 143))

    def test_signal_during_popen_return_does_not_lose_child(self):
        real_spawn = subprocess.Popen

        def interrupted_spawn(*args, **kwargs):
            child = real_spawn(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGTERM)
            return child

        with mock.patch.object(guard.subprocess, "Popen", side_effect=interrupted_spawn):
            report = self.invoke(command=[sys.executable, "-c", SLEEPER])
        self.assertEqual((report["exit_reason"], report["exit_code"]), ("signal", 143))
        self.assertIsNotNone(report["child_pid"])
        self.assertLess(report["child_exit_code"], 0)

    def test_spawn_failure_is_reported(self):
        with mock.patch.object(guard.subprocess, "Popen", side_effect=OSError("dummy spawn failure")):
            report = self.invoke()
        self.assertEqual((report["exit_reason"], report["exit_code"]), ("spawn_error", 126))
        self.assertIsNone(report["child_pid"])

    def test_logging_failure_after_spawn_still_cleans_up(self):
        real_open = guard.open_exclusive

        def open_log(path):
            stream = real_open(path)
            if path.suffix == ".jsonl":
                original_write = stream.write
                original_close = stream.close
                failed = False

                def write(text):
                    nonlocal failed
                    if failed or json.loads(text)["event"] == "spawn":
                        failed = True
                        raise OSError("dummy log failure")
                    return original_write(text)

                def close():
                    original_close()
                    raise OSError("dummy log close failure")

                stream.write = write
                stream.close = close
            return stream

        with mock.patch.object(guard, "open_exclusive", side_effect=open_log):
            report = self.invoke(command=[sys.executable, "-c", SLEEPER])
        self.assertEqual((report["exit_reason"], report["exit_code"]), ("monitor_error", 125))
        self.assertLess(report["child_exit_code"], 0)

    def test_inherit_output_uses_no_pipe_or_output_file(self):
        with mock.patch.object(guard.subprocess, "Popen", wraps=subprocess.Popen) as spawn:
            report = self.invoke(extra=["--inherit-output"])
        self.assertEqual(report["output"], "inherit")
        self.assertEqual(report["exit_code"], 0)
        self.assertIsNone(spawn.call_args.kwargs["stdout"])
        self.assertIsNone(spawn.call_args.kwargs["stderr"])
        self.assertFalse(spawn.call_args.kwargs["shell"])
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(list((self.workspace / "logs").glob("*.output.log")), [])

    def test_invalid_paths_and_existing_logs_are_rejected(self):
        (self.workspace / "build" / "escape").symlink_to(self.workspace / "logs", target_is_directory=True)
        (self.workspace / "logs" / "escape").symlink_to(ROOT, target_is_directory=True)
        existing = self.workspace / "logs" / "existing.jsonl"
        existing.write_text("preserve me\n")
        (self.workspace / "logs" / "link.jsonl").symlink_to(existing)
        (self.workspace / "logs" / "dangling.jsonl").symlink_to(ROOT / "missing-resource-guard-log")
        for extra in (["--cwd", "."], ["--cwd", "build/escape"], ["--cwd", "build/missing"],
                      ["--log", "outside.jsonl"], ["--log", "logs/escape/rejected.jsonl"],
                      ["--log", "logs/existing.jsonl"], ["--log", "logs/link.jsonl"],
                      ["--log", "logs/dangling.jsonl"], ["--log", "logs/missing/rejected.jsonl"],
                      ["--output-log", "../outside.log"], ["--output-log", "logs/existing.jsonl"],
                      ["--log", "logs/same", "--output-log", "logs/same"],
                      ["--inherit-output", "--output-log", "logs/output.log"]):
            with self.subTest(extra=extra):
                self.invalid(self.arguments(extra=extra))
        self.assertEqual(existing.read_text(), "preserve me\n")

    def test_internal_symlink_cwd_and_build_logs_are_allowed(self):
        (self.workspace / "build" / "nested").mkdir()
        (self.workspace / "build" / "alias").symlink_to(self.workspace / "build" / "nested")
        report = self.invoke(extra=["--cwd", "build/alias", "--log", "build/check.jsonl"])
        self.assertEqual(report["cwd"], str(self.workspace / "build" / "nested"))
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(Path(report["log"]).is_relative_to(self.workspace / "build"))

    def test_executable_symlink_and_relative_path_search_preserve_invocation(self):
        alias = self.workspace / "build" / "python-alias"
        alias.symlink_to(sys.executable)
        for command in ("./python-alias", "python-alias"):
            with self.subTest(command=command), mock.patch.dict(os.environ, {"PATH": "."}):
                args, _ = guard.parse_options(["--", command, "-c", "pass"], self.workspace)
            self.assertEqual(args.command, [str(alias), "-c", "pass"])

    def test_exclusive_open_protects_existing_file_and_symlink(self):
        target = self.workspace / "logs" / "target"
        target.write_text("preserve")
        link = self.workspace / "logs" / "link"
        link.symlink_to(target)
        for path in (target, link):
            with self.subTest(path=path), self.assertRaises(FileExistsError):
                guard.open_exclusive(path)
        self.assertEqual(target.read_text(), "preserve")

    def test_invalid_numbers_and_commands_are_rejected(self):
        for flag in ("--min-free-gib", "--min-mem-gib", "--timeout-seconds",
                     "--poll-seconds", "--grace-seconds"):
            for value in ("0", "-1", "nan", "inf", "1e309"):
                with self.subTest(flag=flag, value=value):
                    self.invalid(self.arguments(extra=[flag, value]))
        for extra in (["--poll-seconds", "0.24"], ["--poll-seconds", "1.01"],
                      ["--grace-seconds", "5.01"], ["--resources", "fake.json"]):
            self.invalid(self.arguments(extra=extra))
        for abbreviation in ("--r", "--ru"):
            self.invalid(self.arguments(extra=[abbreviation], run=False))
        for args in (["--run"], ["--run", sys.executable], ["--", ""],
                     ["--", "missing-resource-guard-executable"], ["--", "./missing"],
                     ["--", sys.executable, "bad\0argument"]):
            self.invalid(args)

    def test_euid_zero_is_rejected_before_resources_or_spawn(self):
        with mock.patch.object(guard.os, "geteuid", return_value=0), \
                mock.patch.object(guard.subprocess, "Popen") as spawn, \
                redirect_stderr(io.StringIO()) as stderr:
            resources = mock.Mock(return_value=HEALTHY)
            code = guard.main(self.arguments(), _workspace=self.workspace, _resources=resources)
        self.assertEqual(code, 2)
        self.assertIn("EUID 0", stderr.getvalue())
        resources.assert_not_called()
        spawn.assert_not_called()

    def test_production_readers_use_workspace_statvfs_and_host_memavailable(self):
        with mock.patch.object(guard.os, "statvfs", return_value=SimpleNamespace(
                f_bavail=123, f_frsize=4096)) as statvfs, \
                mock.patch.object(Path, "open", mock.mock_open(
                    read_data="MemTotal: 9999 kB\nMemAvailable: 2345 kB\n")):
            self.assertEqual(guard.read_resources(self.workspace), (123 * 4096, 2345 * 1024))
        statvfs.assert_called_once_with(self.workspace)
        for contents in ("MemTotal: 9999 kB\n", "MemAvailable: unknown kB\n",
                         "MemAvailable: 2345 MB\n"):
            with self.subTest(contents=contents), \
                    mock.patch.object(guard.os, "statvfs", return_value=SimpleNamespace(
                        f_bavail=123, f_frsize=4096)), \
                    mock.patch.object(Path, "open", mock.mock_open(read_data=contents)), \
                    self.assertRaises(ValueError):
                guard.read_resources(self.workspace)


if __name__ == "__main__":
    unittest.main()
