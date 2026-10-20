"""Long-path regressions and a software-only deep package check driver."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nitro_control.auth import Peer, process_identity, socket_peer
from nitro_control.recovery import Runtime, connect_socket, receive_message, send_message

from test_backend import APP, WorkspaceTest


class LongSocketPathTests(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.directory = (self.root / ("package-source-" + "x" * 64) / "build/application-package"
                          / "makepkg/nitro-control/src/nitro-control-0.1.0/application/runtime")
        self.directory.parent.mkdir(parents=True)
        self.runtime = Runtime(self.directory)
        self.runtime.acquire()
        self.addCleanup(self.runtime.close)
        self.assertGreater(len(os.fsencode(self.directory / "control.sock")), 107)

    def test_public_and_private_connections_use_real_long_paths_and_kernel_peers(self):
        for name, mode in (("control.sock", 0o666), ("supervisor.sock", 0o600)):
            with self.subTest(socket=name), self.runtime.socket(name, mode) as server:
                server.settimeout(1)
                for directory in (self.directory, self.runtime):
                    with connect_socket(directory, name, timeout=1) as client:
                        self.assertEqual(socket_peer(client), process_identity(os.getpid()))
                        accepted, _ = server.accept()
                        with accepted:
                            send_message(client, {"op": "status"})
                            self.assertEqual(receive_message(accepted), {"op": "status"})
                            send_message(accepted, {"ok": True})
                            self.assertEqual(receive_message(client), {"ok": True})
            self.runtime.remove_socket(name)

    def test_long_connection_preserves_peer_mode_symlink_and_root_ancestry_checks(self):
        with self.runtime.socket("control.sock", 0o666) as server:
            server.settimeout(1)
            identity = process_identity(os.getpid())
            foreign = Peer(identity.uid + 1, identity.pid, identity.start_ticks)
            with patch("nitro_control.recovery.socket_peer", return_value=foreign):
                with self.assertRaisesRegex(ValueError, "peer"):
                    connect_socket(self.directory, timeout=1)
            accepted, _ = server.accept()
            with accepted:
                self.assertEqual(accepted.recv(1), b"")

            path = self.directory / "control.sock"
            path.chmod(0o660)
            with self.assertRaisesRegex(ValueError, "Untrusted"):
                connect_socket(self.directory, timeout=1)
            path.unlink()
            victim = self.directory / "victim"
            victim.write_text("unchanged")
            victim.chmod(0o600)
            path.symlink_to(victim)
            with self.assertRaisesRegex(ValueError, "Untrusted"):
                connect_socket(self.directory, timeout=1)
            self.assertEqual(victim.read_text(), "unchanged")
            self.assertEqual(victim.stat().st_mode & 0o777, 0o600)

            with (patch("nitro_control.recovery.os.geteuid", return_value=0),
                  patch("nitro_control.recovery.socket.socket") as create):
                with self.assertRaisesRegex(ValueError, "ancestry"):
                    connect_socket(self.directory, timeout=1)
            create.assert_not_called()

    def test_closed_runtime_and_arbitrary_socket_names_are_rejected(self):
        with self.assertRaises(ValueError):
            connect_socket(self.runtime, "../control.sock")
        self.runtime.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            connect_socket(self.runtime)


def deep_check():
    """Copy workspace app sources/assets, not system/kernel files, and check all tests."""
    project = APP.parent
    with tempfile.TemporaryDirectory(prefix=".backend-deep-check-", dir=APP) as temporary:
        root = (Path(temporary) / ("package-source-" + "x" * 48) / "build/application-package"
                / "makepkg/nitro-control/src/nitro-control-0.1.0")
        application = root / "application"
        ignore = shutil.ignore_patterns(".backend-*", "__pycache__", "*.pyc")
        # Preserve any source symlink itself; never follow it to copy external files.
        shutil.copytree(APP, application, ignore=ignore, symlinks=True)
        for relative in ("bin", "data", "packaging/application"):
            shutil.copytree(project / relative, root / relative, symlinks=True)
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.update(QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        print(f"Deep workspace check: {application}", flush=True)
        return subprocess.run([sys.executable, "-W", "error::ResourceWarning", "-m", "unittest",
                               "discover", "-s", "tests", "-v"],
                              cwd=application, env=environment, check=False).returncode


if __name__ == "__main__":
    if sys.argv[1:] == ["--deep-check"]:
        raise SystemExit(deep_check())
    unittest.main()
