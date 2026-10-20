"""Read-only distribution and visual-token checks; never starts a service."""

from configparser import ConfigParser
from pathlib import Path
import subprocess
import unittest
import xml.etree.ElementTree as ET

from nitro_control import __version__
from nitro_control.palette import colors


ROOT = Path(__file__).resolve().parents[2]


def contrast(a, b):
    def luminance(color):
        values = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055)**2.4
                  for v in (color.redF(), color.greenF(), color.blueF())]
        return sum(v * weight for v, weight in zip(values, (0.2126, 0.7152, 0.0722)))
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class DistributionTests(unittest.TestCase):
    def test_accessible_body_and_filled_actions(self):
        palette = colors()
        self.assertGreaterEqual(contrast(palette["ink"], palette["background"]), 7)
        self.assertGreaterEqual(contrast(palette["muted"], palette["surface"]), 4.5)
        self.assertGreaterEqual(contrast(palette["ink"], palette["primary_fill"]), 4.5)

    def test_source_launcher_and_package_version(self):
        self.assertEqual(__version__, "0.1.0")
        result = subprocess.run([str(ROOT / "bin/nitro-control"), "--help"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--read-only", result.stdout)
        self.assertIn("pkgver=0.1.0", (ROOT / "packaging/application/PKGBUILD.in").read_text())

    def test_service_has_independent_recovery_and_no_restart_replay(self):
        unit = ConfigParser(interpolation=None)
        unit.read(ROOT / "data/nitro-control.service")
        self.assertEqual(unit["Service"]["KillMode"], "control-group")
        self.assertEqual(unit["Service"]["Restart"], "no")
        self.assertEqual(unit["Service"]["RuntimeDirectoryPreserve"], "yes")
        self.assertIn("nitro-control-recovery --auto --enable-control", unit["Service"]["ExecStopPost"])
        self.assertIn("hardware-approved", unit["Unit"]["ConditionPathExists"])

    def test_sleep_blocks_until_the_before_hook_succeeds(self):
        unit = ConfigParser(interpolation=None)
        unit.read(ROOT / "data/nitro-control-sleep.service")
        self.assertEqual(unit["Unit"]["Before"], "sleep.target")
        self.assertEqual(unit["Install"]["RequiredBy"], "sleep.target")
        self.assertTrue(unit["Service"]["ExecStart"].endswith("--suspend"))
        self.assertTrue(unit["Service"]["ExecStop"].endswith("--resume"))
        self.assertEqual(unit["Service"]["RemainAfterExit"], "yes")

    def test_polkit_action_is_fixed_and_does_not_grant_inactive_access(self):
        policy = ET.parse(ROOT / "data/org.nitrocontrol.control.policy")
        action = policy.find("action")
        self.assertEqual(action.attrib["id"], "org.nitrocontrol.control")
        self.assertEqual(action.findtext("defaults/allow_any"), "no")
        self.assertEqual(action.findtext("defaults/allow_inactive"), "no")
        self.assertEqual(action.findtext("defaults/allow_active"), "auth_self_keep")


if __name__ == "__main__":
    unittest.main()
