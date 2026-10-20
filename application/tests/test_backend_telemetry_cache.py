"""Clocked NVIDIA-only query caching; every provider and hardware is a fake."""

import subprocess
import threading
import unittest
from unittest.mock import Mock

from nitro_control.telemetry import Telemetry
from test_backend import Clock, FakeHardware


class NvidiaCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.hardware = FakeHardware()
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="55\n"))
        self.telemetry = Telemetry(self.hardware, clock=self.clock, runner=self.runner)
        self.telemetry._cpu_temperature = Mock(return_value=60)

    def test_four_hz_fan_cpu_and_one_hz_nvidia_with_original_gpu_timestamp(self):
        for index in range(20):
            self.telemetry.refresh()
            self.assertEqual(self.telemetry.times["gpu.temperature"], 100 + index // 4)
            self.assertEqual(self.telemetry.times["cpu.temperature"], self.clock())
            self.assertEqual(self.telemetry.cached_snapshot()["gpu"]["temperature_c"], 55)
            self.clock.advance(0.25)
        self.assertEqual(self.runner.call_count, 5)
        self.assertEqual(self.telemetry._cpu_temperature.call_count, 20)
        self.assertEqual(len([op for op in self.hardware.operations if op[0] == "read"]), 40)

    def test_none_and_errors_are_cached_for_one_second_without_reusing_old_value(self):
        self.telemetry.refresh()
        self.clock.advance(1)
        self.runner.side_effect = subprocess.TimeoutExpired("fixture-only", 1)
        self.telemetry.refresh()
        self.assertIsNone(self.telemetry.cached_snapshot()["gpu"]["temperature_c"])
        for _ in range(3):
            self.clock.advance(0.25)
            self.telemetry.refresh()
        self.assertEqual(self.runner.call_count, 2)
        self.assertEqual(self.telemetry.times["gpu.temperature"], 101)
        self.runner.side_effect = None
        self.clock.advance(0.25)
        self.telemetry.refresh()
        self.assertEqual(self.runner.call_count, 3)
        self.assertEqual(self.telemetry.cached_snapshot()["gpu"]["temperature_c"], 55)
        self.clock.advance(2.1)
        self.assertIsNone(self.telemetry.cached_snapshot()["gpu"]["temperature_c"])

    def test_slow_query_holds_no_cache_lock_and_query_start_age_is_not_reset(self):
        def slow(*_args, **_kwargs):
            acquired = []

            def probe():
                locked = self.telemetry.lock.acquire(timeout=0.1)
                acquired.append(locked)
                if locked:
                    self.telemetry.lock.release()

            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(timeout=0.5)
            self.assertEqual(acquired, [True])
            self.clock.advance(1.5)
            return subprocess.CompletedProcess([], 0, stdout="56")

        self.runner.side_effect = slow
        self.telemetry.refresh()
        self.assertEqual(self.telemetry.times["gpu.temperature"], 100)
        self.assertEqual(self.telemetry.age("gpu.temperature"), 1.5)
        self.clock.advance(0.6)
        self.assertIsNone(self.telemetry.cached_snapshot()["gpu"]["temperature_c"])


if __name__ == "__main__":
    unittest.main()
