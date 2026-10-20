"""All fixtures are injected; no test writes or depends on live sysfs."""

import copy
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from nitro_control.auth import Peer, authorize, process_identity, socket_peer
from nitro_control.controller import Controller, IPCServer
from nitro_control.hardware import (CHANNELS, IDENTITY, Hardware, HardwareError, expected_pwm,
                                    percent_to_pwm, pwm_to_percent, reset_auto)
from nitro_control.recovery import (MAX_MESSAGE, OwnerBusy, Runtime, auto_recover, connect_socket, decode_message,
                                    quiesce, read_state, receive_message, send_message)
from nitro_control.supervisor import Supervisor
from nitro_control.telemetry import Telemetry

APP = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeHardware:
    def __init__(self, enabled=True):
        self.sys_root = APP / ".nonexistent-test-sysfs"
        self.control_available = True
        self.control_enabled = enabled
        self.recovery_enabled = enabled
        self.hardware_verified = False
        self.reason = "Injected test hardware; NOT hardware verified"
        self.channels = {name: {"mode": "auto", "pwm": 153, "percent": 60, "rpm": 2000}
                         for name in CHANNELS}
        self.operations = []
        self.fail = {}
        self.read_transform = None

    def _operation(self, op, channel, value=None):
        self.operations.append((op, channel, value))
        error = self.fail.get((op, channel))
        if error:
            raise HardwareError(error)

    def read_channel(self, channel):
        self._operation("read", channel)
        result = copy.copy(self.channels[channel])
        return self.read_transform(channel, result) if self.read_transform else result

    def write_mode(self, channel, mode, *, before_write=None):
        if not self.control_enabled:
            raise HardwareError("Read-only fixture")
        if before_write is not None:
            before_write()
        self._operation("mode", channel, mode)
        self.channels[channel]["mode"] = mode

    def write_pwm(self, channel, pwm, *, before_write=None):
        if not self.control_enabled:
            raise HardwareError("Read-only fixture")
        if type(pwm) is not int or not 0 <= pwm <= 255:
            raise ValueError("Invalid PWM")
        if before_write is not None:
            before_write()
        self._operation("pwm", channel, pwm)
        percent = pwm * 100 // 255
        self.channels[channel].update(pwm=expected_pwm(percent), percent=percent)


class FakeTelemetry(Telemetry):
    def __init__(self, hardware, **kwargs):
        super().__init__(hardware, **kwargs)
        self.temperatures = {"cpu": 60, "gpu": 55}

    def _cpu_temperature(self):
        return self.temperatures["cpu"]

    def _gpu_temperature(self):
        return self.temperatures["gpu"]


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.hardware = FakeHardware()
        self.telemetry = FakeTelemetry(self.hardware, clock=self.clock)
        self.live = True
        self.peer = Peer(os.getuid(), 10001, 234)
        self.controller = Controller(self.hardware, self.telemetry, clock=self.clock,
                                     alive=lambda _peer: self.live)
        self.controller.tick()

    def arm(self):
        result = self.controller.handle({"op": "acquire"}, self.peer, authorized=True)
        self.assertTrue(result["ok"], result)
        self.token = result["token"]
        return result

    def command(self, op, **fields):
        return self.controller.handle({"op": op, "token": self.token, **fields}, self.peer)

    def test_default_readonly_and_status_shape(self):
        hardware = FakeHardware(enabled=False)
        controller = Controller(hardware, FakeTelemetry(hardware))
        controller.tick()
        result = controller.handle({"op": "acquire"}, self.peer, authorized=True)
        self.assertFalse(result["ok"])
        self.assertFalse(any(op[0] in ("pwm", "mode") for op in hardware.operations))
        status = result["status"]
        self.assertEqual(set(status), {"schema", "backend", "cpu", "gpu", "lease", "recovery",
                                       "error", "sample_age_seconds", "events"})
        self.assertEqual(set(status["backend"]), {"connected", "control_available", "control_enabled",
                                                  "hardware_verified", "reason"})
        self.assertEqual(set(status["cpu"]), {"temperature_c", "rpm", "mode", "pwm", "percent",
                                              "requested_mode", "requested_percent", "confirmation",
                                              "rpm_response"})

    def test_auth_required_and_token_never_in_status(self):
        result = self.controller.handle({"op": "acquire"}, self.peer)
        self.assertFalse(result["ok"])
        result = self.arm()
        self.assertEqual(len(result["token"]), 64)
        self.assertNotIn(self.token, json.dumps(result["status"]))
        self.assertTrue(result["status"]["lease"]["owned_by_client"])
        stranger = Peer(self.peer.uid, self.peer.pid + 1, self.peer.start_ticks)
        self.assertFalse(self.controller.status(stranger)["lease"]["owned_by_client"])
        self.assertFalse(self.controller.handle({"op": "max", "token": self.token}, stranger)["ok"])
        reused = Peer(self.peer.uid, self.peer.pid, self.peer.start_ticks + 1)
        self.assertFalse(self.controller.handle({"op": "max", "token": self.token}, reused)["ok"])
        self.assertFalse(self.controller.handle({"op": "acquire"}, stranger, authorized=True)["ok"])

    def test_manual_and_independent_auto_max(self):
        self.arm()
        self.hardware.operations.clear()
        result = self.command("set", cpu={"mode": "manual", "percent": 70})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"]["cpu"]["pwm"], 178)
        self.assertEqual(result["status"]["cpu"]["percent"], 70)
        self.assertEqual(result["status"]["cpu"]["confirmation"], "firmware-confirmed")
        self.assertEqual(result["status"]["cpu"]["rpm"], 2000)
        self.assertEqual(result["status"]["cpu"]["rpm_response"], "pending")
        self.assertEqual(result["status"]["gpu"]["mode"], "auto")
        self.assertFalse(any(op[1] == "gpu" and op[0] in ("mode", "pwm") for op in self.hardware.operations))
        self.assertLess(self.hardware.operations.index(("pwm", "cpu", 179)),
                        self.hardware.operations.index(("mode", "cpu", "manual")))
        result = self.command("set", gpu={"mode": "max"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"]["cpu"]["mode"], "manual")
        self.assertEqual(result["status"]["gpu"]["mode"], "max")
        result = self.command("set", cpu={"mode": "auto"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"]["gpu"]["mode"], "max")
        self.assertFalse(result["status"]["backend"]["hardware_verified"])

    def test_max_and_auto_both_mode_abi(self):
        self.arm()
        self.assertTrue(self.command("max")["ok"])
        self.assertTrue(all(data["mode"] == "max" for data in self.hardware.channels.values()))
        self.hardware.operations.clear()
        self.assertTrue(self.command("auto")["ok"])
        self.assertEqual([op for op in self.hardware.operations if op[0] == "mode"],
                         [("mode", "cpu", "auto"), ("mode", "gpu", "auto")])
        self.assertTrue(all(data["mode"] == "auto" for data in self.hardware.channels.values()))

    def test_every_integer_manual_percent_quantizes(self):
        self.arm()
        for percent in range(50, 101):
            result = self.command("set", cpu={"mode": "manual", "percent": percent})
            self.assertTrue(result["ok"], (percent, result))
            self.assertEqual(result["status"]["cpu"]["pwm"], expected_pwm(percent))
            self.assertEqual(pwm_to_percent(expected_pwm(percent)), percent)
        self.assertFalse(self.controller.status()["backend"]["hardware_verified"])
        json.dumps(self.controller.status(), allow_nan=False)

    def test_bad_inputs_have_no_writes(self):
        self.arm()
        values = [True, False, 49, 101, -1, 50.0, "70", None, float("nan"), float("inf")]
        requests = [{"op": "set", "token": self.token, "cpu": {"mode": "manual", "percent": v}}
                    for v in values]
        requests += [{"op": "set", "token": self.token, "cpu": {"mode": mode}}
                     for mode in (True, 1, "turbo", "", None)]
        requests += [{"op": "set", "token": self.token},
                     {"op": "set", "token": self.token, "cpu": {"mode": "auto", "percent": 70}},
                     {"op": "max", "token": self.token, "path": "/sys/anything"},
                     {"op": "status", "uid": 0}, {"op": "acquire", "pid": 1},
                     {"op": "set", "token": self.token, "cpu": {"mode": "manual", "pwm": 255}},
                     {"op": "max", "token": True}, {"op": "unknown"}]
        for request in requests:
            self.hardware.operations.clear()
            self.assertFalse(self.controller.handle(request, self.peer)["ok"], request)
            self.assertFalse(any(op[0] in ("mode", "pwm") for op in self.hardware.operations), request)

    def test_ttl_and_heartbeat(self):
        self.arm()
        self.command("max")
        self.clock.advance(4)
        self.assertTrue(self.command("heartbeat")["ok"])
        self.clock.advance(4)
        self.controller.tick()
        self.assertTrue(self.controller.status()["lease"]["active"])
        self.clock.advance(1)
        self.controller.tick()
        self.assertFalse(self.controller.status()["lease"]["active"])
        self.assertTrue(all(data["mode"] == "auto" for data in self.hardware.channels.values()))
        self.assertFalse(self.command("max")["ok"])

    def test_set_does_not_renew_heartbeat_lease(self):
        self.arm()
        self.clock.advance(4)
        self.command("max")
        self.clock.advance(1)
        self.controller.tick()
        self.assertIsNone(self.controller.lease)

    def test_dead_owner_release_and_stop(self):
        for action in ("death", "release", "stop"):
            with self.subTest(action=action):
                self.setUp()
                self.arm()
                self.command("max")
                if action == "death":
                    self.live = False
                    self.controller.tick()
                elif action == "release":
                    self.assertTrue(self.command("release")["ok"])
                else:
                    self.controller.shutdown()
                self.assertIsNone(self.controller.lease)
                self.assertTrue(all(data["mode"] == "auto" for data in self.hardware.channels.values()))

    def test_manual_thermal_and_missing_sensor_guards(self):
        for channel, value in (("cpu", 85), ("gpu", 80), ("cpu", None), ("gpu", None)):
            with self.subTest(channel=channel, value=value):
                self.setUp()
                self.arm()
                self.assertTrue(self.command("set", cpu={"mode": "manual", "percent": 70})["ok"])
                self.telemetry.temperatures[channel] = value
                self.controller.tick()
                self.assertIsNone(self.controller.lease)
                self.assertEqual(self.hardware.channels["cpu"]["mode"], "auto")
                self.assertIn(channel, self.controller.error)

    def test_stale_sample_blocks_manual_without_cached_success(self):
        self.arm()
        self.clock.advance(2.1)
        status = self.controller.status(self.peer)
        self.assertIsNone(status["cpu"]["rpm"])
        self.assertIsNone(status["cpu"]["temperature_c"])
        self.assertEqual(status["cpu"]["confirmation"], "failed")
        self.assertGreater(status["sample_age_seconds"], 2)
        result = self.command("set", cpu={"mode": "manual", "percent": 70})
        self.assertFalse(result["ok"])
        self.assertIsNone(self.controller.lease)

    def test_missing_rpm_manual_is_cancelled_but_never_invented(self):
        self.arm()
        self.hardware.channels["gpu"]["rpm"] = None
        self.controller.tick()
        result = self.command("set", cpu={"mode": "manual", "percent": 70})
        self.assertFalse(result["ok"])
        self.assertIsNone(result["status"]["gpu"]["rpm"])

    def test_partial_multistep_failure_resets_both(self):
        self.arm()
        self.hardware.fail[("pwm", "gpu")] = "injected EIO"
        self.hardware.operations.clear()
        result = self.command("set", cpu={"mode": "manual", "percent": 70},
                              gpu={"mode": "manual", "percent": 75})
        self.assertFalse(result["ok"])
        self.assertTrue(self.controller.fatal)
        self.assertIsNone(self.controller.lease)
        for channel in CHANNELS:
            self.assertIn(("mode", channel, "auto"), self.hardware.operations)
            self.assertEqual(self.hardware.channels[channel]["mode"], "auto")

    def test_auto_write_and_readback_attempts_both_on_failure(self):
        for failure in ("mode", "read"):
            self.setUp()
            self.arm()
            self.command("max")
            self.hardware.fail[(failure, "cpu")] = "injected EIO"
            self.hardware.operations.clear()
            result = self.command("auto")
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"]["recovery"]["state"], "fault")
            for channel in CHANNELS:
                self.assertIn(("mode", channel, "auto"), self.hardware.operations)
                self.assertIn(("read", channel, None), self.hardware.operations)
            self.assertEqual(self.hardware.channels["gpu"]["mode"], "auto")

    def test_manual_readback_mismatch_never_success(self):
        self.arm()
        self.hardware.read_transform = lambda _ch, d: ({**d, "pwm": 1} if d["mode"] == "manual" else d)
        self.assertFalse(self.command("set", cpu={"mode": "manual", "percent": 70})["ok"])
        self.assertIsNone(self.controller.lease)
        self.assertTrue(self.controller.fatal)

    def test_omitted_channel_is_freshly_verified_to_detect_firmware_coupling(self):
        self.arm()
        original = self.hardware.write_mode

        def coupled(channel, mode, **kwargs):
            original(channel, mode, **kwargs)
            if channel == "cpu":
                self.hardware.channels["gpu"]["mode"] = "max"

        self.hardware.write_mode = coupled
        result = self.command("set", cpu={"mode": "manual", "percent": 70})
        self.assertFalse(result["ok"])
        self.assertTrue(self.controller.fatal)
        self.assertIsNone(self.controller.lease)
        self.assertEqual(self.hardware.channels["gpu"]["mode"], "auto")

    def test_auto_valid_mode_but_missing_pwm_is_failure(self):
        self.arm()
        self.hardware.read_transform = lambda ch, d: {**d, "pwm": None} if ch == "cpu" else d
        self.assertFalse(self.command("auto")["ok"])
        self.assertEqual(self.controller.status()["cpu"]["confirmation"], "failed")

    def test_unexpected_firmware_change_triggers_recovery(self):
        self.arm()
        self.command("max")
        self.hardware.channels["gpu"]["mode"] = "manual"
        self.controller.tick()
        self.assertTrue(self.controller.fatal)
        self.assertIsNone(self.controller.lease)
        self.assertEqual(self.hardware.channels["gpu"]["mode"], "auto")

    def test_independent_actual_rpm_response(self):
        self.arm()
        result = self.command("max")
        self.assertEqual(result["status"]["gpu"]["rpm_response"], "pending")
        self.hardware.channels["cpu"]["rpm"] = 2700
        self.clock.advance(1)
        self.controller.tick()
        status = self.controller.status()
        self.assertEqual(status["cpu"]["rpm_response"], "observed")
        self.assertEqual(status["gpu"]["rpm_response"], "pending")
        self.clock.advance(2.1)
        self.controller.tick()
        status = self.controller.status()
        self.assertEqual(status["gpu"]["rpm_response"], "not-observed")
        self.assertFalse(status["backend"]["hardware_verified"])
        event = next(event for event in status["events"] if event["type"] == "command")
        self.assertEqual(event["pre_command_rpm"], 2000)
        self.assertEqual(event["pre_command_monotonic"], 100)

    def test_events_bounded_and_cancelled_queue_request_not_executed(self):
        self.arm()
        for _ in range(100):
            self.command("auto")
        self.assertEqual(len(self.controller.status()["events"]), 32)
        future = self.controller.submit({"op": "max", "token": self.token}, self.peer)
        future.cancel()
        self.controller.process_one()
        self.assertEqual(self.hardware.channels["cpu"]["mode"], "auto")


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".backend-test-", dir=APP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def sysfs(self):
        root = self.root / "sys"
        node = root / "devices/platform/acer-wmi/hwmon/hwmon27"
        node.mkdir(parents=True)
        driver = root / "bus/platform/drivers/acer-wmi"
        driver.mkdir(parents=True)
        (node.parents[1] / "driver").symlink_to(driver)
        (node / "name").write_text("acer\n")
        for index in (1, 2):
            for name, value in ((f"pwm{index}", "178"), (f"pwm{index}_enable", "2"),
                                (f"fan{index}_input", "2000")):
                (node / name).write_text(value)
        dmi = root / "class/dmi/id"
        dmi.mkdir(parents=True)
        for name, value in IDENTITY.items():
            (dmi / name).write_text(value)
        params = root / "module/acer_wmi/parameters"
        params.mkdir(parents=True)
        for name, value in (("predator_v4", "N"), ("ec_raw_mode", "N"), ("force_caps", "-1")):
            (params / name).write_text(value)
        cpu = root / "class/hwmon/hwmon123"
        cpu.mkdir(parents=True)
        (cpu / "name").write_text("coretemp")
        (cpu / "temp1_label").write_text("Package id 0")
        (cpu / "temp1_input").write_text("61000")
        (cpu / "temp2_label").write_text("Core 0")
        (cpu / "temp2_input").write_text("99000")
        return root, node


class HardwareTelemetryTests(WorkspaceTest):
    def test_real_adapter_discovers_platform_and_reads_quantized_files_readonly(self):
        root, node = self.sysfs()
        hardware = Hardware(sys_root=root, enable_control=True, validation_mode=True)
        self.assertEqual(hardware.hwmon, node)
        self.assertTrue(hardware.control_available)
        self.assertFalse(hardware.control_enabled)
        self.assertFalse(hardware.hardware_verified)
        with self.assertRaises(HardwareError):
            hardware.write_mode("cpu", "max")
        self.assertEqual((node / "pwm1_enable").read_text(), "2")
        for mode, number in (("auto", "2"), ("max", "0"), ("manual", "1")):
            (node / "pwm1_enable").write_text(number)
            self.assertEqual(hardware.read_channel("cpu")["mode"], mode)
        for percent in range(50, 101):
            (node / "pwm1").write_text(str(expected_pwm(percent)))
            self.assertEqual(hardware.read_channel("cpu")["percent"], percent)
        (node / "fan1_input").write_text("0")
        self.assertEqual(hardware.read_channel("cpu")["rpm"], 0)
        (node / "fan1_input").write_text("EIO")
        self.assertIsNone(hardware.read_channel("cpu")["rpm"])
        (node / "fan1_input").unlink()
        self.assertIsNone(hardware.read_channel("cpu")["rpm"])

    def test_unsupported_dmi_driver_parameters_and_spoofed_class_node(self):
        root, node = self.sysfs()
        for field in IDENTITY:
            path = root / "class/dmi/id" / field
            path.write_text("wrong")
            self.assertFalse(Hardware(sys_root=root).control_available)
            path.write_text(IDENTITY[field])
        params = root / "module/acer_wmi/parameters/predator_v4"
        params.write_text("Y")
        self.assertFalse(Hardware(sys_root=root).control_available)
        params.write_text("N")
        driver = node.parents[1] / "driver"
        driver.unlink()
        driver.symlink_to(root / "class/hwmon")
        self.assertIsNone(Hardware(sys_root=root).hwmon)

    def test_duplicate_acer_nodes_rejected(self):
        root, node = self.sysfs()
        second = node.parent / "hwmon28"
        second.mkdir()
        (second / "name").write_text("acer")
        self.assertIsNone(Hardware(sys_root=root).hwmon)

    def test_write_gate_root_marker_record_and_code(self):
        root, _node = self.sysfs()
        with patch("nitro_control.hardware.os.geteuid", return_value=1000):
            self.assertFalse(Hardware(sys_root=root, enable_control=True).control_enabled)
        with patch.object(Hardware, "_discover", return_value=None):
            self.assertFalse(Hardware(enable_control=True, validation_mode=True).control_enabled)
        with self.assertRaises(ValueError):
            percent_to_pwm(True)
        with self.assertRaises(ValueError):
            Hardware(sys_root=root).write_pwm("cpu", True)

    def test_sysfs_write_abi_and_raw_pwm_validation_without_writes(self):
        root, _node = self.sysfs()
        hardware = Hardware(sys_root=root)
        with patch.object(hardware, "_write") as write:
            hardware.write_mode("cpu", "auto")
            hardware.write_mode("gpu", "max")
            hardware.write_mode("cpu", "manual")
            self.assertEqual([call.args for call in write.call_args_list],
                             [("cpu", "_enable", 2), ("gpu", "_enable", 0), ("cpu", "_enable", 1)])
            write.reset_mock()
            for value in (True, -1, 256, 50.0, float("nan"), "255"):
                with self.assertRaises(ValueError):
                    hardware.write_pwm("cpu", value)
            write.assert_not_called()

    def test_production_authorization_gates_without_accessing_sysfs(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(Hardware, "_identity", return_value=dict(IDENTITY)))
            stack.enter_context(patch.object(Hardware, "_discover", return_value=Path("/sys/fixed-test-node")))
            stack.enter_context(patch.object(Hardware, "_safe_parameters", return_value=True))
            interfaces = stack.enter_context(patch.object(Hardware, "_interface_present", return_value=True))
            code = stack.enter_context(patch.object(Hardware, "_trusted_code", return_value=True))
            marker = stack.enter_context(patch("nitro_control.hardware.trusted_path", return_value=True))
            stack.enter_context(patch("nitro_control.hardware.trusted_json", return_value=None))
            uid = stack.enter_context(patch("nitro_control.hardware.os.geteuid", return_value=0))
            self.assertFalse(Hardware().control_enabled)
            self.assertFalse(Hardware(enable_control=True).control_enabled)
            self.assertTrue(Hardware(enable_control=True, validation_mode=True).control_enabled)
            self.assertFalse(Hardware(enable_control=True, validation_mode=True).hardware_verified)
            uid.return_value = 1000
            self.assertFalse(Hardware(enable_control=True, validation_mode=True).recovery_enabled)
            uid.return_value = 0
            marker.return_value = False
            self.assertFalse(Hardware(enable_control=True, validation_mode=True).recovery_enabled)
            marker.return_value = True
            code.return_value = False
            self.assertFalse(Hardware(enable_control=True, validation_mode=True).control_enabled)
            code.return_value = True
            interfaces.return_value = False
            partial = Hardware(enable_control=True, validation_mode=True)
            self.assertFalse(partial.control_enabled)
            self.assertTrue(partial.recovery_enabled)
            # Gate evaluation is tested above; no mocked gate is used for an actual write.

    def test_simulated_files_can_never_be_hardware_verified(self):
        root, _node = self.sysfs()
        with patch("nitro_control.hardware.trusted_json") as record:
            hardware = Hardware(sys_root=root, enable_control=True, validation_mode=True)
            self.assertFalse(hardware.hardware_verified)
            record.assert_not_called()

    def test_full_fallback_uses_coretemp_and_fixed_nvidia_query_not_acer_gpu_zero(self):
        root, node = self.sysfs()
        (node / "temp2_input").write_text("0")
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="54\n"))
        hardware = Hardware(sys_root=root)
        clock = Clock()
        telemetry = Telemetry(hardware, clock=clock, runner=runner, runtime_dir=self.root / "missing")
        status = telemetry.snapshot()
        self.assertEqual(status["cpu"]["temperature_c"], 61)
        self.assertEqual(status["gpu"]["temperature_c"], 54)
        self.assertFalse(status["backend"]["connected"])
        self.assertEqual(runner.call_args.args[0][0], "/usr/bin/nvidia-smi")
        self.assertEqual(runner.call_args.kwargs["timeout"], 1)
        self.assertNotIn("shell", runner.call_args.kwargs)
        for output in ("0", "N/A", "", "NaN", "150", "-1"):
            runner.return_value.stdout = output
            clock.advance(1)
            self.assertIsNone(telemetry.snapshot()["gpu"]["temperature_c"], output)
        runner.side_effect = subprocess.TimeoutExpired("nvidia-smi", 1)
        clock.advance(1)
        status = telemetry.snapshot()
        self.assertIsNone(status["gpu"]["temperature_c"])
        self.assertEqual(status["cpu"]["temperature_c"], 61)

    def test_missing_eio_and_stale_data_not_cached_as_zero(self):
        root, node = self.sysfs()
        hardware = Hardware(sys_root=root)
        clock = Clock()
        telemetry = Telemetry(hardware, clock=clock,
                              runner=Mock(return_value=subprocess.CompletedProcess([], 0, stdout="50")))
        telemetry.refresh()
        self.assertEqual(telemetry.cached_snapshot()["gpu"]["temperature_c"], 50)
        clock.advance(2.1)
        status = telemetry.cached_snapshot()
        self.assertIsNone(status["gpu"]["temperature_c"])
        self.assertIsNone(status["cpu"]["rpm"])
        with patch("pathlib.Path.read_text", side_effect=OSError("EIO")):
            telemetry.refresh()
        self.assertIsNone(telemetry.cached_snapshot()["cpu"]["temperature_c"])
        self.assertIsNone(telemetry.cached_snapshot()["cpu"]["pwm"])

    def test_fallback_shows_untrusted_recovery_record_as_fault(self):
        runtime = Runtime(self.root / "runtime")
        runtime.acquire()
        self.addCleanup(runtime.close)
        runtime.write_state({"schema": 1, "state": "running"})
        telemetry = FakeTelemetry(FakeHardware(), runtime_dir=runtime.path)
        status = telemetry.snapshot()
        self.assertEqual(status["recovery"]["state"], "fault")
        self.assertIn("untrusted", status["recovery"]["error"])
        self.assertFalse(status["backend"]["connected"])
        self.assertFalse(status["backend"]["hardware_verified"])


class AuthWireTests(unittest.TestCase):
    def test_socket_credentials_and_pid_start(self):
        first, second = socket.socketpair()
        with first, second:
            peer = socket_peer(first)
            self.assertEqual(peer, process_identity(os.getpid()))
            self.assertEqual(peer.uid, os.getuid())
            self.assertGreater(peer.start_ticks, 0)

    def test_pkcheck_fixed_subject_timeout_and_fail_closed(self):
        peer = process_identity(os.getpid())
        runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        self.assertTrue(authorize(peer, runner=runner))
        command = runner.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/pkcheck", "--action-id", "org.nitrocontrol.control",
                                   "--process", f"{peer.pid},{peer.start_ticks},{peer.uid}",
                                   "--allow-user-interaction"])
        self.assertEqual(runner.call_args.kwargs["timeout"], 15)
        runner.return_value.returncode = 1
        self.assertFalse(authorize(peer, runner=runner))
        runner.side_effect = subprocess.TimeoutExpired(command, 15)
        self.assertFalse(authorize(peer, runner=runner))
        runner.side_effect = FileNotFoundError()
        self.assertFalse(authorize(peer, runner=runner))
        runner.reset_mock()
        self.assertFalse(authorize(Peer(peer.uid, peer.pid, peer.start_ticks + 1), runner=runner))
        runner.assert_not_called()

    def test_malformed_strict_bounded_frames(self):
        for payload in (b"{}", b"[]\n", b"null\n", b'{"op":"status","op":"acquire"}\n',
                        b'{"op":"set","cpu":{"percent":NaN}}\n', b"\xff\n", b"{}\n{}\n",
                        b'{"op":"status"}\nextra', b" " * MAX_MESSAGE + b"\n"):
            with self.subTest(payload=payload[:80]), self.assertRaises(ValueError):
                decode_message(payload)
        self.assertEqual(decode_message(b'{"op":"status"}\n'), {"op": "status"})


class RecoverySupervisorTests(WorkspaceTest):
    def runtime(self):
        runtime = Runtime(self.root / "runtime")
        runtime.acquire()
        self.addCleanup(runtime.close)
        return runtime

    def test_exclusive_lock_and_safe_atomic_state(self):
        runtime = self.runtime()
        other = Runtime(runtime.path)
        self.addCleanup(other.close)
        with self.assertRaises(OwnerBusy):
            other.acquire()
        runtime.write_state({"schema": 1, "state": "auto", "worker": None})
        self.assertEqual(read_state(runtime.path)["state"], "auto")
        self.assertEqual((runtime.path / "recovery.json").stat().st_mode & 0o777, 0o644)
        target = runtime.path / "recovery.json"
        target.unlink()
        target.symlink_to(self.root / "victim")
        with self.assertRaises(ValueError):
            runtime.write_state({"schema": 1, "state": "auto"})
        self.assertFalse((self.root / "victim").exists())

    def test_unsafe_runtime_and_control_override_rejected(self):
        path = self.root / "runtime"
        path.mkdir(mode=0o777)
        path.chmod(0o777)
        with self.assertRaises(ValueError):
            Runtime(path)
        with self.assertRaises(ValueError):
            Runtime(self.root / "other", control=True)

    def test_auto_helper_quiesces_before_both_writes(self):
        runtime = self.runtime()
        runtime.write_state({"schema": 1, "state": "running", "worker": {"record": "test"}})
        hardware = FakeHardware()
        order = []
        original = hardware.write_mode

        def write(channel, mode):
            order.append("write")
            original(channel, mode)

        hardware.write_mode = write
        with patch("nitro_control.recovery.quiesce", side_effect=lambda _ref: order.append("stop") or True):
            self.assertTrue(auto_recover(runtime, hardware)["ok"])
        self.assertEqual(order, ["stop", "write", "write"])
        hardware.operations.clear()
        with patch("nitro_control.recovery.quiesce", return_value=False):
            self.assertFalse(auto_recover(runtime, hardware)["ok"])
        self.assertFalse(hardware.operations)

    def test_auto_helper_requires_lock_and_known_exit_even_on_proc_read_error(self):
        runtime = Runtime(self.root / "runtime")
        hardware = FakeHardware()
        with self.assertRaises(RuntimeError):
            auto_recover(runtime, hardware)
        self.assertFalse(hardware.operations)
        runtime.acquire()
        self.addCleanup(runtime.close)
        runtime.write_state({"schema": 1, "state": "running", "worker": {"record": "test"}})
        with patch("nitro_control.recovery.quiesce", side_effect=ValueError("proc identity unreadable")):
            self.assertFalse(auto_recover(runtime, hardware)["ok"])
        self.assertFalse(hardware.operations)
        self.assertEqual(read_state(runtime.path)["state"], "fault")

    def test_competing_owner_cli_never_even_constructs_hardware(self):
        runtime = self.runtime()
        from nitro_control.recovery import main
        with patch("nitro_control.recovery.Hardware") as hardware, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["--auto", "--runtime-dir", str(runtime.path)]), 1)
        hardware.assert_not_called()
        self.assertIn("Another supervisor", json.loads(output.getvalue())["error"])

    def test_recovery_readonly_never_writes_even_auto(self):
        runtime = self.runtime()
        hardware = FakeHardware(enabled=False)
        self.assertFalse(auto_recover(runtime, hardware)["ok"])
        self.assertFalse(hardware.operations)

    def test_quiesce_refuses_unknown_identity_instead_of_assuming_exit(self):
        ref = {"uid": os.geteuid(), "pid": 10001, "start_ticks": 123, "pgid": 10001}
        with patch("nitro_control.recovery.process_identity", side_effect=ValueError("EIO")):
            with self.assertRaises(ValueError):
                quiesce(ref)

    def test_recovery_attempts_both_before_readback(self):
        hardware = FakeHardware()
        hardware.fail[("mode", "cpu")] = "EIO"
        result = reset_auto(hardware)
        self.assertFalse(result.ok)
        self.assertEqual(hardware.operations,
                         [("mode", "cpu", "auto"), ("mode", "gpu", "auto"),
                          ("read", "cpu", None), ("read", "gpu", None)])

    def test_slow_auto_readback_is_not_reported_as_fresh_confirmation(self):
        hardware = FakeHardware()
        clock = Clock()

        def slow(_channel, data):
            clock.advance(3)
            return data

        hardware.read_transform = slow
        result = reset_auto(hardware, clock)
        self.assertFalse(result.ok)
        self.assertTrue(any("stale" in error for error in result.errors))
        self.assertEqual(len([op for op in hardware.operations if op[0] == "mode"]), 2)

    def test_pid_reuse_never_signals(self):
        peer = process_identity(os.getpid())
        ref = {"uid": peer.uid, "pid": peer.pid, "start_ticks": peer.start_ticks + 1, "pgid": peer.pid}
        with patch("os.pidfd_open") as opened:
            self.assertTrue(quiesce(ref))
            opened.assert_not_called()

    def test_uninterruptible_worker_never_competes_or_restarts(self):
        hardware = FakeHardware()
        supervisor = Supervisor(self.root / "runtime", hardware=hardware)
        supervisor.runtime.acquire()
        self.addCleanup(supervisor.runtime.close)
        supervisor.worker_ref = {"known": "worker"}
        with patch("nitro_control.supervisor.quiesce", return_value=False), patch.object(supervisor, "_spawn") as spawn:
            self.assertFalse(supervisor.stop_worker("fault"))
            self.assertFalse(supervisor.resume())
            self.assertEqual(supervisor.state, "fault")
            self.assertIn("NO recovery writes", supervisor.error)
            self.assertFalse(hardware.operations)
            spawn.assert_not_called()

    def test_suspend_resume_lifecycle_and_partial_auto_fault(self):
        hardware = FakeHardware()
        supervisor = Supervisor(self.root / "runtime", hardware=hardware)
        supervisor.runtime.acquire()
        self.addCleanup(supervisor.runtime.close)
        supervisor.worker_ref = {"known": "worker"}
        with patch("nitro_control.supervisor.quiesce", return_value=True) as stop:
            self.assertTrue(supervisor.suspend())
            stop.assert_called_once()
        self.assertEqual(supervisor.state, "suspended")
        self.assertIsNone(supervisor.worker_ref)
        with patch.object(supervisor, "_spawn") as spawn:
            self.assertTrue(supervisor.resume())
            spawn.assert_called_once()
        hardware.fail[("mode", "cpu")] = "EIO"
        hardware.operations.clear()
        self.assertFalse(supervisor.suspend())
        self.assertIn(("mode", "gpu", "auto"), hardware.operations)
        self.assertEqual(supervisor.state, "fault")

    def test_resume_even_when_running_stops_and_requires_new_worker(self):
        supervisor = Supervisor(self.root / "runtime", hardware=FakeHardware())
        supervisor.runtime.acquire()
        self.addCleanup(supervisor.runtime.close)
        supervisor.state = "running"
        supervisor.worker_ref = {"known": "worker"}
        with patch("nitro_control.supervisor.quiesce", return_value=True) as stopped, patch.object(supervisor, "_spawn") as spawned:
            self.assertTrue(supervisor.resume())
        stopped.assert_called_once()
        spawned.assert_called_once()
        self.assertIsNone(supervisor.worker_ref)
        self.assertTrue(all(data["mode"] == "auto" for data in supervisor.hardware.channels.values()))

    def test_revoked_gate_cannot_resume_a_control_supervisor_without_auto(self):
        supervisor = Supervisor(self.root / "runtime", hardware=FakeHardware(enabled=False))
        supervisor.runtime.acquire()
        self.addCleanup(supervisor.runtime.close)
        supervisor.enable_control = True  # Inject the production policy, not a CLI bypass.
        with patch.object(supervisor, "_spawn") as spawned:
            self.assertFalse(supervisor.resume())
        spawned.assert_not_called()
        self.assertEqual(supervisor.state, "fault")
        self.assertFalse(supervisor.hardware.operations)

    def test_malformed_prior_record_is_never_clobbered_or_assumed_no_worker(self):
        runtime = self.runtime()
        runtime.write_state({"schema": 1, "state": "running"})
        old = (runtime.path / "recovery.json").read_bytes()
        runtime.close()
        supervisor = Supervisor(runtime.path, hardware=FakeHardware())
        try:
            with self.assertRaises(ValueError):
                supervisor.start()
        finally:
            supervisor.close()
        self.assertEqual((runtime.path / "recovery.json").read_bytes(), old)
        self.assertFalse(supervisor.hardware.operations)

    def test_startup_gate_error_preserves_unstoppable_old_worker_identity(self):
        runtime = self.runtime()
        ref = {"pid": 10001, "uid": os.geteuid(), "start_ticks": 123, "pgid": 10001}
        runtime.write_state({"schema": 1, "state": "running", "worker": ref})
        runtime.close()
        supervisor = Supervisor(runtime.path, hardware=FakeHardware(enabled=False))
        supervisor.enable_control = True
        with patch("nitro_control.supervisor.quiesce", return_value=False):
            try:
                with self.assertRaises(ValueError):
                    supervisor.start()
            finally:
                supervisor.close()
        self.assertEqual(read_state(runtime.path)["worker"], ref)
        self.assertEqual(read_state(runtime.path)["state"], "fault")
        self.assertFalse(supervisor.hardware.operations)

    def test_worker_exit_and_watchdog_require_stop_then_reset(self):
        for crashed in (True, False):
            with self.subTest(crashed=crashed):
                supervisor = Supervisor(self.root / ("runtime" + str(crashed)), hardware=FakeHardware())
                supervisor.runtime.acquire()
                self.addCleanup(supervisor.runtime.close)
                supervisor.state = "running"
                supervisor.worker = Mock()
                supervisor.worker.poll.return_value = 1 if crashed else None
                supervisor.worker_ref = {"known": "worker"}
                supervisor.last_heartbeat = time.monotonic() - 6
                with patch("nitro_control.supervisor.quiesce", return_value=True) as stopped:
                    supervisor.check_worker()
                stopped.assert_called_once()
                self.assertIsNone(supervisor.worker_ref)
                self.assertEqual(supervisor.state, "fault")
                self.assertEqual(len([op for op in supervisor.hardware.operations if op[0] == "mode"]), 2)

    def test_authorization_does_not_hold_controller_lock(self):
        runtime = self.runtime()
        hardware = FakeHardware()
        telemetry = FakeTelemetry(hardware)
        controller = Controller(hardware, telemetry)
        controller.tick()
        peer = process_identity(os.getpid())
        token = controller.handle({"op": "acquire"}, peer, authorized=True)["token"]
        entered, release = threading.Event(), threading.Event()

        def slow_auth(_peer):
            entered.set()
            release.wait(3)
            return False

        server = IPCServer(runtime, controller, authorize=slow_auth)
        server.start()
        self.addCleanup(server.close)
        client = connect_socket(runtime)
        self.addCleanup(client.close)
        send_message(client, {"op": "acquire"})
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        controller.tick()
        self.assertTrue(controller.handle({"op": "heartbeat", "token": token}, peer)["ok"])
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(controller.lock.acquire(timeout=0.1))
        controller.lock.release()
        release.set()
        for _ in range(50):
            controller.process_one()
            if not controller.tasks.empty():
                continue
            time.sleep(0.005)
        client.settimeout(1)
        self.assertFalse(receive_message(client)["ok"])


if __name__ == "__main__":
    unittest.main()
