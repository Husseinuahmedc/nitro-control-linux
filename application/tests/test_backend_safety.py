"""Regression tests for authority boundaries, pinned paths and slow I/O."""

import os
from pathlib import Path
import stat
import threading
import time
import unittest
from unittest.mock import patch

from nitro_control.auth import Peer
from nitro_control.controller import Controller, LeaseLost
from nitro_control.hardware import Hardware
from nitro_control.recovery import Runtime, read_state

from test_backend import Clock, FakeHardware, FakeTelemetry, WorkspaceTest


class RuntimeSafetyTests(WorkspaceTest):
    def test_root_readonly_rejects_user_ancestor_before_creation_or_chmod(self):
        target = self.root / "root-readonly"
        with (patch("nitro_control.recovery.os.geteuid", return_value=0),
              patch("nitro_control.recovery.os.mkdir") as mkdir,
              patch("nitro_control.recovery.os.fchmod") as fchmod,
              patch("nitro_control.recovery.os.chmod") as chmod):
            with self.assertRaisesRegex(ValueError, "ancestry"):
                Runtime(target, control=False)
        mkdir.assert_not_called()
        fchmod.assert_not_called()
        chmod.assert_not_called()
        self.assertFalse(target.exists())

    def test_root_readonly_rejects_writable_ancestor_even_with_root_owned_leaf(self):
        parent = self.root / "writable"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        target = parent / "runtime"
        target.mkdir(mode=0o755)
        real_fstat = os.fstat

        def root_stat(fd):
            data = list(real_fstat(fd))
            data[4] = 0
            return os.stat_result(data)

        # Metadata is mocked, not privileges. Actual I/O stays at the test UID.
        with (patch("nitro_control.recovery.os.geteuid", return_value=0),
              patch("nitro_control.recovery.os.fstat", side_effect=root_stat),
              patch("nitro_control.recovery.os.fchmod") as fchmod,
              patch("nitro_control.recovery.os.chmod") as chmod):
            with self.assertRaisesRegex(ValueError, "ancestry"):
                Runtime(target, control=False)
        fchmod.assert_not_called()
        chmod.assert_not_called()

    def test_substitution_after_mkdir_never_chmods_symlink_victim(self):
        target = self.root / "runtime"
        moved = self.root / "moved"
        victim = self.root / "victim"
        victim.write_text("unchanged")
        victim.chmod(0o600)
        real_fstat, real_mkdir = os.fstat, os.mkdir

        def root_stat(fd):
            data = list(real_fstat(fd))
            data[4] = 0
            return os.stat_result(data)

        def substitute(path, mode=0o777, *, dir_fd=None):
            real_mkdir(path, mode, dir_fd=dir_fd)
            if Path(path).name == "runtime":
                target.rename(moved)
                target.symlink_to(victim)

        with (patch("nitro_control.recovery.os.geteuid", return_value=0),
              patch("nitro_control.recovery.os.fstat", side_effect=root_stat),
              patch("nitro_control.recovery.os.mkdir", side_effect=substitute)):
            with self.assertRaises((OSError, ValueError)):
                Runtime(target, control=False)
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o600)
        self.assertEqual(victim.read_text(), "unchanged")

    def test_new_directory_fchmod_uses_pinned_inode_after_rename(self):
        target, moved, victim = (self.root / name for name in ("runtime", "moved", "victim"))
        victim.write_text("unchanged")
        victim.chmod(0o600)
        real_fstat, real_fchmod = os.fstat, os.fchmod

        def substitute(fd, mode):
            target.rename(moved)
            target.symlink_to(victim)
            real_fchmod(fd, mode)

        with patch("nitro_control.recovery.os.fchmod", side_effect=substitute):
            runtime = Runtime(target)
        self.addCleanup(runtime.close)
        self.assertEqual(real_fstat(runtime.dir_fd).st_ino, moved.stat().st_ino)
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(moved.stat().st_mode), 0o755)

    def test_lock_state_socket_and_cleanup_use_pinned_directory_after_substitution(self):
        target, moved, other = (self.root / name for name in ("runtime", "moved", "other"))
        other.mkdir()
        sentinel = other / "recovery.json"
        sentinel.write_text("victim data")
        sentinel.chmod(0o600)
        runtime = Runtime(target)
        self.addCleanup(runtime.close)
        target.rename(moved)
        target.symlink_to(other, target_is_directory=True)
        runtime.acquire()
        runtime.write_state({"schema": 1, "state": "auto", "worker": None})
        self.assertTrue((moved / "owner.lock").exists())
        self.assertFalse((other / "owner.lock").exists())
        self.assertEqual(read_state(runtime)["state"], "auto")
        self.assertEqual(sentinel.read_text(), "victim data")
        server = runtime.socket("control.sock", 0o666)
        server.close()
        self.assertTrue((moved / "control.sock").is_socket())
        self.assertFalse((other / "control.sock").exists())
        runtime.remove_socket("control.sock")
        self.assertFalse((moved / "control.sock").exists())
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o600)

    def test_socket_permission_update_never_follows_substituted_symlink(self):
        runtime = Runtime(self.root / "runtime")
        self.addCleanup(runtime.close)
        victim = self.root / "victim"
        victim.write_text("unchanged")
        victim.chmod(0o600)
        real_chmod = os.chmod
        called = []

        def substitute(path, mode, **kwargs):
            called.append(kwargs)
            directory_fd = kwargs.get("dir_fd")
            os.unlink(path, dir_fd=directory_fd)
            os.symlink(victim, path, dir_fd=directory_fd)
            return real_chmod(path, mode, **kwargs)

        with patch("nitro_control.recovery.os.chmod", side_effect=substitute):
            with self.assertRaises((OSError, ValueError)):
                runtime.socket("control.sock", 0o666)
        self.assertFalse(called[0]["follow_symlinks"])
        self.assertEqual(called[0]["dir_fd"], runtime.dir_fd)
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o600)

    def test_closed_runtime_never_falls_back_to_cwd_socket_operations(self):
        runtime = Runtime(self.root / "runtime")
        runtime.close()
        with (patch("nitro_control.recovery.os.stat") as inspect,
              patch("nitro_control.recovery.os.chmod") as chmod):
            with self.assertRaisesRegex(RuntimeError, "closed"):
                runtime.socket("control.sock", 0o666)
        inspect.assert_not_called()
        chmod.assert_not_called()


class LeaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.hardware = FakeHardware()
        self.telemetry = FakeTelemetry(self.hardware, clock=self.clock)
        self.live = True
        self.peer = Peer(os.getuid(), 10001, 234)
        self.controller = Controller(self.hardware, self.telemetry, clock=self.clock,
                                     alive=lambda _peer: self.live)
        self.controller.tick()
        result = self.controller.handle({"op": "acquire"}, self.peer, authorized=True)
        self.assertTrue(result["ok"], result)
        self.token = result["token"]
        self.hardware.operations.clear()

    def command(self, mode):
        request = {"op": "max", "token": self.token} if mode == "max" else {
            "op": "set", "token": self.token,
            "cpu": {"mode": "manual", "percent": 70},
            "gpu": {"mode": "manual", "percent": 75}}
        return self.controller.handle(request, self.peer)

    def lose(self, loss):
        if loss == "expiry":
            self.clock.advance(5.1)
        else:
            self.live = False

    def assert_cancelled(self, result):
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["status"]["lease"]["active"])
        self.assertIsNone(self.controller.lease)
        self.assertTrue(self.controller.fatal)
        for channel in ("cpu", "gpu"):
            self.assertIn(("mode", channel, "auto"), self.hardware.operations)
            self.assertIn(("read", channel, None), self.hardware.operations)
            self.assertEqual(self.hardware.channels[channel]["mode"], "auto")

    def test_slow_baseline_or_dead_owner_blocks_first_manual_and_max_write(self):
        for mode in ("manual", "max"):
            for loss in ("expiry", "death"):
                with self.subTest(mode=mode, loss=loss):
                    self.setUp()
                    first = True

                    def read(_channel, data):
                        nonlocal first
                        if first:
                            first = False
                            self.lose(loss)
                        return data

                    self.hardware.read_transform = read
                    self.assert_cancelled(self.command(mode))
                    self.assertFalse(any(op == "pwm" or (op == "mode" and value != "auto")
                                         for op, _channel, value in self.hardware.operations))

    def test_loss_during_pwm_blocks_manual_mode_and_next_channel(self):
        for loss in ("expiry", "death"):
            with self.subTest(loss=loss):
                self.setUp()
                original = self.hardware.write_pwm

                def write(channel, pwm, **kwargs):
                    original(channel, pwm, **kwargs)
                    self.lose(loss)

                self.hardware.write_pwm = write
                self.assert_cancelled(self.command("manual"))
                writes = [entry for entry in self.hardware.operations if entry[0] != "read"]
                self.assertEqual(writes, [("pwm", "cpu", 179), ("mode", "cpu", "auto"),
                                          ("mode", "gpu", "auto")])

    def test_loss_during_second_channel_baseline_blocks_its_nonauto_writes(self):
        for mode in ("manual", "max"):
            for loss in ("expiry", "death"):
                with self.subTest(mode=mode, loss=loss):
                    self.setUp()
                    first_gpu_read = True

                    def read(channel, data):
                        nonlocal first_gpu_read
                        if channel == "gpu" and first_gpu_read:
                            first_gpu_read = False
                            self.lose(loss)
                        return data

                    self.hardware.read_transform = read
                    self.assert_cancelled(self.command(mode))
                    self.assertFalse(any(channel == "gpu" and (op == "pwm" or
                                         (op == "mode" and value != "auto"))
                                         for op, channel, value in self.hardware.operations))

    def test_loss_during_issued_mode_write_stops_before_next_channel(self):
        for loss in ("expiry", "death"):
            with self.subTest(loss=loss):
                self.setUp()
                original = self.hardware.write_mode

                def write(channel, mode, **kwargs):
                    original(channel, mode, **kwargs)
                    if channel == "cpu" and mode == "max":
                        self.lose(loss)

                self.hardware.write_mode = write
                self.assert_cancelled(self.command("max"))
                self.assertNotIn(("mode", "gpu", "max"), self.hardware.operations)

    def test_owner_dies_during_final_readback_never_returns_ok(self):
        final = False
        original = self.hardware.write_mode

        def write(channel, mode, **kwargs):
            nonlocal final
            original(channel, mode, **kwargs)
            if channel == "gpu" and mode == "max":
                final = True

        def read(channel, data):
            if final and channel == "gpu":
                self.live = False
            return data

        self.hardware.write_mode = write
        self.hardware.read_transform = read
        self.assert_cancelled(self.command("max"))

    def test_slow_identity_read_before_success_rechecks_deadline_after_read(self):
        advanced = False

        def alive(_peer):
            nonlocal advanced
            if not advanced and any(event["type"] == "command" for event in self.controller.events):
                advanced = True
                self.clock.advance(5.1)
            return True

        self.controller.alive = alive
        self.assert_cancelled(self.command("max"))
        self.assertIn("expired", self.controller.error)

    def test_heartbeat_cannot_resurrect_expired_lease_or_do_recovery_writes(self):
        self.clock.advance(5.1)
        result = self.controller.submit({"op": "heartbeat", "token": self.token}, self.peer).result()
        self.assertFalse(result["ok"])
        self.assertFalse(result["status"]["lease"]["active"])
        self.assertFalse(self.hardware.operations)
        self.controller.tick()
        self.assertIn(("mode", "cpu", "auto"), self.hardware.operations)
        self.assertIn(("mode", "gpu", "auto"), self.hardware.operations)

    def test_refresh_does_not_hold_state_lock_or_starve_submitted_heartbeat(self):
        entered, release = threading.Event(), threading.Event()

        def slow_temperature():
            entered.set()
            release.wait(3)
            return 77

        self.clock.advance(1)
        self.telemetry._gpu_temperature = slow_temperature
        loop = threading.Thread(target=self.controller.tick)
        loop.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.controller.lock.acquire(timeout=0.1))
            self.controller.lock.release()
            self.assertEqual(self.controller.status()["gpu"]["temperature_c"], 55)
            self.assertEqual(self.telemetry.sampled_at, 100)
            self.clock.advance(1.1)
            started = time.monotonic()
            future = self.controller.submit({"op": "heartbeat", "token": self.token}, self.peer)
            self.assertTrue(future.done())
            self.assertTrue(future.result()["ok"])
            self.assertLess(time.monotonic() - started, 0.2)
            status = self.controller.status()
            self.assertIsNone(status["gpu"]["temperature_c"])
            self.assertIsNone(status["cpu"]["rpm"])
            self.assertEqual(self.telemetry.sampled_at, 100)
        finally:
            release.set()
            loop.join(timeout=2)
        self.assertFalse(loop.is_alive())
        self.assertEqual(self.controller.status()["gpu"]["temperature_c"], 77)
        self.assertEqual(self.telemetry.sampled_at, 101)


class HardwareBoundaryTests(WorkspaceTest):
    def test_last_guard_runs_after_validation_and_open_before_actual_write(self):
        root, node = self.sysfs()
        hardware = Hardware(sys_root=root)
        clock = Clock()

        def slow_validation():
            # This injected gate applies ONLY to disposable workspace files.
            hardware.control_enabled = hardware.recovery_enabled = True
            clock.advance(5.1)

        def guard():
            if clock() >= deadline:
                raise LeaseLost("expired before actual sysfs write")

        for name, write, value in (("pwm1", hardware.write_pwm, 200),
                                   ("pwm1_enable", hardware.write_mode, "max")):
            with self.subTest(name=name):
                deadline = clock() + 5
                before = (node / name).read_text()
                with patch.object(hardware, "_evaluate", side_effect=slow_validation):
                    with self.assertRaises(LeaseLost):
                        write("cpu", value, before_write=guard)
                self.assertEqual((node / name).read_text(), before)
        self.assertFalse(hardware.hardware_verified)


if __name__ == "__main__":
    unittest.main()
