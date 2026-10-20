"""Offscreen software-only GUI/IPC tests. Every sample here is a TEST FIXTURE.

No real authorization, controller mutations, sysfs writes or NVIDIA queries.
Run with /usr/bin/python -m unittest discover -s tests -p test_gui.py -v.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QMetaObject, QObject, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from nitro_control.client import ACQUIRE_TIMEOUT, MUTATION_TIMEOUT, RPC_TIMEOUT, ClientError, DEFAULT_SOCKET, MAX_FRAME, SocketClient, validate_status
from nitro_control.gui import NitroControlWindow, unavailable_status
from nitro_control.palette import OKLCH, apply_theme, colors


def fixture_status(*, available=True, missing=False):
    status = unavailable_status("TEST FIXTURE: simulated controller, never hardware evidence")
    status["error"] = None
    status["backend"].update(connected=available, control_available=available, control_enabled=available)
    status["events"] = ["TEST FIXTURE / software simulation only"]
    if not missing:
        for name, temperature, rpm in (("cpu", 61, 2400), ("gpu", 49, 2200)):
            status[name].update(temperature_c=temperature, rpm=rpm, mode="auto", pwm=160,
                                percent=63, confirmation="firmware-confirmed")
    return status


class FixtureTelemetry:
    def snapshot(self):
        return fixture_status(available=False, missing=True)


class MeasuredFixtureTelemetry:
    def snapshot(self):
        return fixture_status(available=False)


class FixtureClient:
    """In-memory TEST FIXTURE; request() cannot reach hardware or a socket."""
    path = "/TEST-FIXTURE/no-real-socket"
    peer_uid = 0
    read_only = False

    def __init__(self, *, missing=False, available=True, read_only=False):
        self.current = fixture_status(available=available, missing=missing)
        self.records = []
        self.read_only = read_only
        self.pending = False
        self.bad_ack = False
        self.release_fails = False
        self.offline = False
        self.acquire_gate = None
        self.release_entered = threading.Event()

    def clone(self):
        # The fixture models one server shared by independently executing lanes.
        return self

    def request(self, op, **params):
        self.records.append((op, copy.deepcopy(params), threading.get_ident()))
        if self.read_only and op != "status":
            raise AssertionError("TEST FIXTURE: read-only transport was asked to mutate")
        if self.offline:
            raise ClientError("TEST FIXTURE: socket disconnected")
        if op == "acquire":
            if self.acquire_gate is not None:
                self.acquire_gate.wait(3)
            self.current["lease"].update(active=True, owned_by_client=True,
                                         owner_pid=os.getpid(), remaining_seconds=4)
            self.current["error"] = None
            return {"ok": True, "token": "TEST-FIXTURE-SECRET", "status": copy.deepcopy(self.current)}
        if op == "release":
            self.release_entered.set()
            self.current["lease"].update(active=False, owned_by_client=False, remaining_seconds=0)
            if self.release_fails:
                self.current["recovery"] = {"state": "fault", "error": "TEST FIXTURE: Auto readback failed"}
                return {"ok": False, "error": "TEST FIXTURE: Auto failed", "status": copy.deepcopy(self.current)}
            for name in ("cpu", "gpu"):
                self.current[name].update(mode="auto", confirmation="firmware-confirmed",
                                           requested_mode="auto", requested_percent=None, rpm_response="unverified")
        if op == "set":
            for name in ("cpu", "gpu"):
                if name not in params:
                    continue
                request = params[name]
                self.current[name].update(requested_mode=request["mode"], requested_percent=request.get("percent"),
                                           rpm_response="pending" if self.pending else "observed")
                if self.pending:
                    self.current[name]["confirmation"] = "pending"
                elif not self.bad_ack:
                    self.current[name].update(mode=request["mode"], percent=request.get("percent"),
                                               confirmation="firmware-confirmed")
        return {"ok": True, "status": copy.deepcopy(self.current)}


class FixtureSocket:
    def __init__(self, payload, *, uid=0, chunks=None):
        self.payload = payload
        self.uid = uid
        self.sent = []
        self.chunks = list(chunks) if chunks is not None else [payload]
        self.timeouts = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, path):
        self.path = path

    def getsockopt(self, *_):
        return struct.pack("3i", 123, self.uid, self.uid)

    def sendall(self, value):
        self.sent.append(value)

    def recv(self, count):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > count:
            self.chunks.insert(0, chunk[count:])
        return chunk[:count]


def response_frame():
    return json.dumps({"ok": True, "status": fixture_status()}).encode() + b"\n"


class NativeFixtureClient:
    """Native controller/IPC code with in-memory hardware and connections only."""
    path = "/TEST-FIXTURE/native-memory-ipc"
    peer_uid = 0
    read_only = False

    def __init__(self, *, timeout_acquire=False):
        from nitro_control.controller import Controller, IPCServer
        from test_backend import Clock, FakeHardware, FakeTelemetry

        class GatedHardware(FakeHardware):
            def __init__(self):
                super().__init__()
                self.block_auto = False
                self.entered = threading.Event()
                self.release = threading.Event()

            def read_channel(self, channel):
                if self.block_auto and channel == "cpu":
                    self.entered.set()
                    self.release.wait()
                return super().read_channel(channel)

        self.hardware = GatedHardware()
        self.clock = Clock()
        self.records = []
        self.replies = []
        self.wait_budgets = []
        hardware = self.hardware
        budgets = self.wait_budgets

        class NativeController(Controller):
            def submit(self, request, peer, authorized=False):
                future = super().submit(request, peer, authorized)
                if timeout_acquire and request["op"] == "acquire":
                    def short_wait(timeout):
                        budgets.append(timeout)
                        hardware.entered.wait(1)
                        return future.result(timeout=0.05)

                    proxy = Mock(wraps=future)
                    proxy.result.side_effect = short_wait
                    return proxy
                return future

        self.controller = NativeController(self.hardware, FakeTelemetry(self.hardware, clock=self.clock), clock=self.clock)
        self.controller.tick()
        runtime = Mock()
        runtime.socket.return_value = FixtureSocket(b"")
        self.server = IPCServer(runtime, self.controller, authorize=lambda _peer: True)
        self.stopped = threading.Event()

        def process():
            while not self.stopped.is_set():
                self.controller.process_one()
                self.stopped.wait(0.005)

        self.io_thread = threading.Thread(target=process, name="TEST-FIXTURE-native-controller", daemon=True)
        self.io_thread.start()

    def clone(self):
        return self

    def request(self, op, **params):
        class MemoryConnection(FixtureSocket):
            def getsockopt(self, *_):
                return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

        self.records.append((op, copy.deepcopy(params), threading.get_ident()))
        connection = MemoryConnection(json.dumps({"op": op, **params}).encode() + b"\n")
        if not self.server.clients.acquire(timeout=1):
            raise ClientError("TEST FIXTURE: IPC client capacity exhausted")
        self.server._client(connection)
        response = json.loads(b"".join(connection.sent))
        self.replies.append((op, copy.deepcopy(response)))
        return response

    def close(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        self.hardware.release.set()
        self.io_thread.join(2)
        if self.io_thread.is_alive():
            raise AssertionError("TEST FIXTURE: native controller did not stop")
        self.controller.shutdown()


class ClientTests(unittest.TestCase):
    def test_one_request_newline_no_id_and_fragmented_response(self):
        frame = response_frame()
        fake = FixtureSocket(frame, chunks=[frame[:5], frame[5:31], frame[31:]])
        with patch("nitro_control.client.socket.socket", return_value=fake) as constructor:
            result = SocketClient().request("status")
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(fake.sent[0]), {"op": "status"})
        self.assertTrue(fake.sent[0].endswith(b"\n"))
        constructor.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        self.assertGreater(max(fake.timeouts), 1)
        self.assertLessEqual(max(fake.timeouts), RPC_TIMEOUT)

    def test_invalid_requests_are_rejected_before_connecting(self):
        invalid = [
            ("bogus", {}), ("status", {"id": 1}), ("acquire", {"uid": 0}),
            ("release", {}), ("heartbeat", {"token": ""}), ("set", {"token": "test"}),
            ("set", {"token": "test", "cpu": {"mode": "manual", "percent": 49}}),
            ("set", {"token": "test", "cpu": {"mode": "manual", "percent": 101}}),
            ("set", {"token": "test", "cpu": {"mode": "manual", "percent": True}}),
            ("set", {"token": "test", "cpu": {"mode": "manual", "percent": 70.0}}),
            ("set", {"token": "test", "cpu": {"mode": "manual"}}),
            ("set", {"token": "test", "gpu": {"mode": "auto", "percent": 70}}),
            ("set", {"token": "test", "cpu": {"mode": "off"}}),
            ("set", {"token": "test", "cpu": {"mode": "max", "path": "/sys/anything"}}),
            ("max", {"token": "x" * MAX_FRAME}),
        ]
        with patch("nitro_control.client.socket.socket") as constructor:
            for op, params in invalid:
                with self.subTest(op=op, params=params):
                    with self.assertRaises(ClientError):
                        SocketClient().request(op, **params)
            constructor.assert_not_called()

    def test_read_only_never_connects_for_control(self):
        with patch("nitro_control.client.socket.socket") as constructor:
            for op, params in (("acquire", {}), ("auto", {"token": "TEST"}), ("release", {"token": "TEST"})):
                with self.assertRaises(ClientError):
                    SocketClient(read_only=True).request(op, **params)
            constructor.assert_not_called()

    def test_manual_policy_endpoints_are_valid_without_live_writes(self):
        for percent in (50, 100):
            fake = FixtureSocket(response_frame())
            with patch("nitro_control.client.socket.socket", return_value=fake):
                SocketClient().request("set", token="TEST-FIXTURE", cpu={"mode": "manual", "percent": percent})
            self.assertEqual(json.loads(fake.sent[0])["cpu"]["percent"], percent)

    def test_production_peer_must_be_root_even_for_status(self):
        fake = FixtureSocket(response_frame(), uid=1000)
        with patch("nitro_control.client.socket.socket", return_value=fake):
            with self.assertRaisesRegex(ClientError, "root"):
                SocketClient(DEFAULT_SOCKET).request("status")
        self.assertEqual(fake.sent, [])

    def test_nonroot_custom_socket_is_monitoring_only(self):
        fake = FixtureSocket(response_frame(), uid=1000)
        with patch("nitro_control.client.socket.socket", return_value=fake):
            client = SocketClient("/TEST-FIXTURE/socket")
            self.assertTrue(client.request("status")["ok"])
            self.assertEqual(client.peer_uid, 1000)
        fake = FixtureSocket(response_frame(), uid=1000)
        with patch("nitro_control.client.socket.socket", return_value=fake):
            with self.assertRaisesRegex(ClientError, "root"):
                SocketClient("/TEST-FIXTURE/socket").request("acquire")
        self.assertEqual(fake.sent, [])

    def test_framing_limits_and_bad_json(self):
        for frame in (b"{}", b"not json\n", b"{}\n{}\n", b"x" * MAX_FRAME + b"\n",
                      b'{"ok":true,"ok":false}\n', b'{"ok":NaN}\n'):
            with self.subTest(frame=frame[:30]):
                with patch("nitro_control.client.socket.socket", return_value=FixtureSocket(frame)):
                    with self.assertRaises(ClientError):
                        SocketClient().request("status")

    def test_malformed_status_cannot_be_confirmation(self):
        mutations = [
            lambda s: s.update(schema=2),
            lambda s: s["backend"].update(control_enabled="true"),
            lambda s: s["cpu"].update(rpm=float("nan")),
            lambda s: s["cpu"].update(rpm=-1),
            lambda s: s["gpu"].update(percent=101),
            lambda s: s["gpu"].update(mode="off"),
            lambda s: s["gpu"].update(confirmation="success"),
            lambda s: s["cpu"].update(rpm_response="verified"),
            lambda s: s["lease"].update(active=1),
            lambda s: s.update(sample_age_seconds=None),
        ]
        for mutate in mutations:
            status = fixture_status()
            mutate(status)
            with self.assertRaises(ClientError):
                validate_status(status)

    def test_authorization_has_separate_bounded_timeout(self):
        frame = json.dumps({"ok": True, "token": "TEST-FIXTURE", "status": fixture_status()}).encode() + b"\n"
        fake = FixtureSocket(frame)
        with patch("nitro_control.client.socket.socket", return_value=fake):
            SocketClient().request("acquire")
        self.assertGreater(max(fake.timeouts), 23)
        self.assertLessEqual(max(fake.timeouts), ACQUIRE_TIMEOUT)

    def test_concurrent_transports_do_not_share_peer_credentials(self):
        original = SocketClient("/TEST-FIXTURE/socket", read_only=True)
        original.peer_uid = 0
        other = original.clone()
        self.assertIsNot(original, other)
        self.assertIsNone(other.peer_uid)
        self.assertEqual((other.path, other.timeout, other.acquire_timeout, other.mutation_timeout, other.read_only),
                         (original.path, 2.0, 24.0, 6.5, True))
        customized = SocketClient(timeout=1.5, acquire_timeout=25, mutation_timeout=7).clone()
        self.assertEqual((customized.timeout, customized.acquire_timeout, customized.mutation_timeout), (1.5, 25, 7))

    def test_mutation_deadline_is_separate_from_status_heartbeat_and_acquire(self):
        for op, params, budget in (
            ("status", {}, RPC_TIMEOUT),
            ("heartbeat", {"token": "TEST-FIXTURE"}, RPC_TIMEOUT),
            ("acquire", {}, ACQUIRE_TIMEOUT),
            ("set", {"token": "TEST-FIXTURE", "cpu": {"mode": "auto"}}, MUTATION_TIMEOUT),
            ("auto", {"token": "TEST-FIXTURE"}, MUTATION_TIMEOUT),
            ("max", {"token": "TEST-FIXTURE"}, MUTATION_TIMEOUT),
            ("release", {"token": "TEST-FIXTURE"}, MUTATION_TIMEOUT),
        ):
            with self.subTest(op=op):
                frame = json.dumps({"ok": True, "token": "TEST-FIXTURE", "status": fixture_status()}).encode() + b"\n"
                fake = FixtureSocket(frame)
                with patch("nitro_control.client.socket.socket", return_value=fake):
                    SocketClient().clone().request(op, **params)
                self.assertLessEqual(max(fake.timeouts), budget)
                self.assertGreater(max(fake.timeouts), budget - 0.1)

    def test_real_readonly_socket_allows_normal_one_second_latency(self):
        with tempfile.TemporaryDirectory(prefix="nitro-gui-socket-") as directory:
            path = os.path.join(directory, "control.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            server.settimeout(3)
            requests = []

            def reply():
                try:
                    with server.accept()[0] as connection:
                        requests.append(json.loads(connection.recv(MAX_FRAME)))
                        time.sleep(1.1)
                        connection.sendall(response_frame())
                finally:
                    server.close()

            thread = threading.Thread(target=reply, daemon=True)
            thread.start()
            started = time.monotonic()
            self.assertTrue(SocketClient(path, read_only=True).request("status")["ok"])
            self.assertGreater(time.monotonic() - started, 1)
            self.assertEqual(requests, [{"op": "status"}])
            thread.join(1)
            self.assertFalse(thread.is_alive())


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        apply_theme(cls.app)

    def wait_for(self, condition, timeout=3000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            QTest.qWait(10)
        self.fail("TEST FIXTURE: timed out waiting for asynchronous Qt state")

    def make_window(self, client=None, **kwargs):
        client = client or FixtureClient()
        window = NitroControlWindow(client=client, socket_path=client.path,
                                    telemetry_factory=kwargs.pop("telemetry_factory", FixtureTelemetry), **kwargs)
        window.show()
        self.addCleanup(self.shutdown_window, window)
        self.wait_for(lambda: bool(client.records))
        self.wait_for(lambda: "_client" in window.status)
        return window, client

    def shutdown_window(self, window):
        window._allow_unconfirmed_exit = True
        window.close()
        self.wait_for(lambda: window._thread_done, timeout=5000)
        window._allow_unconfirmed_exit = True
        window.close()
        self.app.processEvents()
        if window._thread:
            self.assertFalse(window._thread.isRunning())
        window.deleteLater()
        self.app.processEvents()

    def acquire(self, window):
        self.wait_for(window.take_control.isEnabled)
        window.take_control.click()
        self.wait_for(lambda: window._owns() and not window._busy)

    def poll_now(self, window):
        QMetaObject.invokeMethod(window._worker, "poll", Qt.ConnectionType.QueuedConnection)

    def test_missing_data_is_unavailable_not_zero_and_controls_disabled(self):
        window, client = self.make_window(FixtureClient(missing=True, available=False))
        for panel in window.panels.values():
            self.assertEqual(panel.rpm.text(), "--")
            self.assertEqual(panel.temperature.text(), "-- C")
            self.assertEqual(panel.requested.text(), "--")
            self.assertEqual(panel.firmware.text(), "--")
            self.assertFalse(panel.slider.isEnabled())
            self.assertFalse(any(button.isEnabled() for button in panel.mode_buttons.values()))
            self.assertFalse(any(button.isChecked() for button in panel.mode_buttons.values()))
        self.assertFalse(window.take_control.isEnabled())
        self.assertFalse(window.all_auto.isEnabled())
        self.assertFalse(window.apply_button.isEnabled())
        self.assertFalse(window.state_title.property("autoConfirmed"))
        self.assertEqual([record[0] for record in client.records], ["status"])

    def test_no_startup_authorization_and_readonly_disables_control(self):
        window, client = self.make_window(FixtureClient(read_only=True), read_only=True)
        self.assertEqual([record[0] for record in client.records], ["status"])
        self.assertFalse(window.take_control.isEnabled())
        self.assertFalse(window.apply_button.isEnabled())
        window.take_control.click()
        self.assertEqual([record[0] for record in client.records], ["status"])

    def test_missing_required_sensor_locks_otherwise_enabled_backend(self):
        client = FixtureClient()
        client.current["cpu"]["temperature_c"] = None
        window, _ = self.make_window(client)
        self.assertEqual(window.panels["cpu"].temperature.text(), "-- C")
        self.assertIn("Telemetry incomplete", window.state_title.text())
        self.assertIn("CPU temperature", window.state_reason.text())
        self.assertFalse(window.take_control.isEnabled())
        self.assertFalse(window.apply_button.isEnabled())

    def test_recoverable_incident_requires_explicit_reacquisition_without_replay(self):
        client = FixtureClient()
        client.current["error"] = "TEST FIXTURE: former lease expired; firmware Auto recovered"
        client.current["recovery"] = {"state": "auto", "error": None}
        window, _ = self.make_window(client)
        self.assertIn("error", window.state_title.text())
        self.assertFalse(window.state_title.property("autoConfirmed"))
        self.assertTrue(window.take_control.isEnabled())
        self.acquire(window)
        self.assertFalse(any(op == "set" for op, *_ in client.records))
        self.assertFalse(window.apply_button.isEnabled())

    def test_fallback_preserves_sensor_values_but_not_auto_claims(self):
        client = FixtureClient()
        client.offline = True
        window = NitroControlWindow(client=client, socket_path=client.path, telemetry_factory=MeasuredFixtureTelemetry)
        window.show()
        self.addCleanup(self.shutdown_window, window)
        self.wait_for(lambda: window.status["cpu"]["temperature_c"] is not None)
        self.assertEqual(window.panels["cpu"].temperature.text(), "61 C")
        self.assertEqual(window.panels["gpu"].temperature.text(), "49 C")
        self.assertEqual(window.panels["cpu"].rpm.text(), "2,400")
        self.assertEqual(window.panels["cpu"].firmware.text(), "--")
        self.assertFalse(window.state_title.property("autoConfirmed"))
        self.assertFalse(window.take_control.isEnabled())

    def test_rejected_status_cannot_turn_cached_auto_green(self):
        class RejectedStatusClient(FixtureClient):
            def request(self, op, **params):
                response = super().request(op, **params)
                response.update(ok=False, error="TEST FIXTURE: status request rejected")
                return response

        window, _ = self.make_window(RejectedStatusClient())
        self.assertFalse(window.state_title.property("autoConfirmed"))
        self.assertIn("error", window.state_title.text())
        self.assertIn("status request rejected", window.state_reason.text())

    def test_nonroot_status_never_enables_controls_or_hardware_verification(self):
        client = FixtureClient()
        client.peer_uid = 1000
        client.current["backend"]["hardware_verified"] = True
        window, _ = self.make_window(client)
        self.assertFalse(window.state_title.property("autoConfirmed"))
        self.assertFalse(window.take_control.isEnabled())
        self.assertEqual(window.verification.text(), "Hardware unverified")
        self.assertIn("not root", window.state_reason.text())

    def test_independent_draft_slider_numeric_and_explicit_apply(self):
        window, client = self.make_window()
        self.acquire(window)
        cpu = window.panels["cpu"]
        cpu.mode_buttons["manual"].click()
        cpu.slider.setValue(76)
        cpu.percent.setValue(74)
        self.assertEqual(cpu.slider.value(), 74)
        self.assertTrue(cpu.dirty)
        self.assertIn("Not applied", cpu.draft_hint.text())
        self.assertFalse(any(op == "set" for op, *_ in client.records))
        self.assertTrue(window.apply_button.isEnabled())
        window.apply_button.click()
        self.wait_for(lambda: not window._busy and "Firmware accepted" in window.feedback.text())
        requests = [params for op, params, _ in client.records if op == "set"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["cpu"], {"mode": "manual", "percent": 74})
        self.assertNotIn("gpu", requests[0])
        self.assertIn("74%", cpu.requested.text())
        self.assertIn("74%", cpu.firmware.text())
        self.assertEqual(cpu.rpm.text(), "2,400")
        self.assertIn("observed", cpu.rpm_response.text())
        self.assertFalse(window.apply_button.isEnabled())
        self.assertTrue(all(thread != threading.get_ident() for _, _, thread in client.records))

    def test_global_modes_stage_both_but_do_not_send_until_apply(self):
        window, client = self.make_window()
        self.acquire(window)
        window.all_max.click()
        self.assertTrue(all(panel.draft() == {"mode": "max"} for panel in window.panels.values()))
        self.assertFalse(any(op == "set" for op, *_ in client.records))
        window.apply_button.click()
        self.wait_for(lambda: not window._busy)
        request = next(params for op, params, _ in client.records if op == "set")
        self.assertEqual(request["cpu"], {"mode": "max"})
        self.assertEqual(request["gpu"], {"mode": "max"})

    def test_pending_firmware_and_rpm_are_not_reported_as_completed(self):
        client = FixtureClient()
        client.pending = True
        window, _ = self.make_window(client)
        self.acquire(window)
        window.panels["cpu"].mode_buttons["manual"].click()
        window.panels["cpu"].slider.setValue(72)
        window.apply_button.click()
        self.wait_for(lambda: not window._busy)
        self.assertIn("readback pending", window.feedback.text())
        self.assertNotIn("Firmware accepted", window.feedback.text())
        self.assertIn("pending", window.panels["cpu"].confirmation.text())
        self.assertIn("pending", window.panels["cpu"].rpm_response.text())
        self.assertIn("72%", window.panels["cpu"].requested.text())
        self.assertIn("Auto", window.panels["cpu"].firmware.text())
        self.assertEqual(window.verification.text(), "Hardware unverified")
        client.current["cpu"].update(mode="manual", percent=72, confirmation="firmware-confirmed", rpm_response="pending")
        self.poll_now(window)
        self.wait_for(lambda: "firmware confirmed" in window.panels["cpu"].confirmation.text())
        self.assertIn("pending", window.panels["cpu"].rpm_response.text())
        self.assertIn("Firmware accepted / RPM response pending", window.feedback.text())

    def test_firmware_ack_does_not_claim_rpm_observed(self):
        window, client = self.make_window()
        self.acquire(window)
        client.pending = True
        window.panels["gpu"].stage("max")
        window.apply_button.click()
        self.wait_for(lambda: not window._busy)
        client.current["gpu"].update(mode="max", percent=100, confirmation="firmware-confirmed", rpm_response="not-observed")
        self.poll_now(window)
        self.wait_for(lambda: "not observed" in window.panels["gpu"].rpm_response.text())
        self.assertEqual(window.verification.text(), "Hardware unverified")
        self.assertIn("Firmware accepted / RPM response not observed", window.feedback.text())

    def test_later_confirmed_mode_cannot_acknowledge_a_different_pending_target(self):
        client = FixtureClient()
        client.pending = True
        window, _ = self.make_window(client)
        self.acquire(window)
        window.panels["cpu"].stage("manual")
        window.panels["cpu"].percent.setValue(72)
        window.apply_button.click()
        self.wait_for(lambda: not window._busy)
        self.assertIn("pending", window.feedback.text())
        client.current["cpu"].update(mode="auto", confirmation="firmware-confirmed")
        self.poll_now(window)
        self.wait_for(lambda: client.release_entered.is_set() and not window._busy
                      and "does not match the submitted" in window.feedback.text())
        self.assertIn("does not match the submitted", window.feedback.text())
        self.assertNotIn("Firmware accepted", window.feedback.text())
        self.assertFalse(window._owns())
        self.assertFalse(window.state_title.property("autoConfirmed"))

    def test_later_wrong_manual_percent_is_also_a_confirmation_fault(self):
        client = FixtureClient()
        client.pending = True
        window, _ = self.make_window(client)
        self.acquire(window)
        window.panels["gpu"].stage("manual")
        window.panels["gpu"].percent.setValue(72)
        window.apply_button.click()
        self.wait_for(lambda: not window._busy)
        client.current["gpu"].update(mode="manual", percent=80, confirmation="firmware-confirmed")
        self.poll_now(window)
        self.wait_for(lambda: client.release_entered.is_set() and not window._busy
                      and "GPU firmware readback does not match" in window.feedback.text())
        self.assertIn("GPU firmware readback does not match", window.feedback.text())
        self.assertFalse(window.state_title.property("autoConfirmed"))

    def test_mismatched_firmware_ack_forces_auto_recovery(self):
        window, client = self.make_window()
        self.acquire(window)
        client.bad_ack = True
        window.panels["cpu"].stage("manual")
        window.apply_button.click()
        self.wait_for(lambda: client.release_entered.is_set() and not window._busy)
        self.assertIn("does not match", window.feedback.text())
        self.assertFalse(window._owns())

    def test_auto_confirmation_requires_both_fresh_actual_modes(self):
        window, client = self.make_window()
        self.assertTrue(window.state_title.property("autoConfirmed"))
        client.current["gpu"].update(mode="max", requested_mode="auto", confirmation="pending")
        self.poll_now(window)
        self.wait_for(lambda: window.status["gpu"]["mode"] == "max")
        self.assertFalse(window.state_title.property("autoConfirmed"))
        self.assertNotIn("Auto confirmed", window.state_title.text())
        client.offline = True
        self.poll_now(window)
        self.wait_for(lambda: not window.status["backend"]["connected"])
        self.assertFalse(window.state_title.property("autoConfirmed"))
        self.assertEqual(window.panels["cpu"].rpm.text(), "--")

    def test_stale_samples_hide_live_values_and_lock_control(self):
        window, _ = self.make_window()
        self.acquire(window)
        window._last_update -= 10
        window._render()
        self.assertEqual(window.panels["cpu"].rpm.text(), "--")
        self.assertEqual(window.panels["cpu"].temperature.text(), "-- C")
        self.assertFalse(window.panels["cpu"].mode_buttons["manual"].isEnabled())
        self.assertFalse(window.state_title.property("autoConfirmed"))
        self.assertIn("stale", window.state_title.text().lower())

    def test_heartbeat_one_second_and_stop_after_connection_fault(self):
        window, client = self.make_window()
        self.acquire(window)
        self.wait_for(lambda: any(op == "heartbeat" for op, *_ in client.records), timeout=1800)
        self.assertEqual(window._worker._timer.interval(), 1000)
        client.offline = True
        self.poll_now(window)
        self.wait_for(lambda: window.status["recovery"]["state"] == "fault")
        heartbeat_count = sum(op == "heartbeat" for op, *_ in client.records)
        self.assertFalse(window.apply_button.isEnabled())
        self.assertFalse(window.state_title.property("autoConfirmed"))
        QTest.qWait(1200)
        self.assertEqual(heartbeat_count, sum(op == "heartbeat" for op, *_ in client.records))
        self.assertIn("not confirmed", window.ownership.text())

    def test_heartbeat_cadence_does_not_add_rpc_duration_to_each_period(self):
        class DelayedHeartbeatClient(FixtureClient):
            def __init__(self):
                super().__init__()
                self.pulse_times = []

            def request(self, op, **params):
                if op == "heartbeat":
                    self.pulse_times.append(time.monotonic())
                    time.sleep(0.2)
                return super().request(op, **params)

        client = DelayedHeartbeatClient()
        window, _ = self.make_window(client)
        self.acquire(window)
        self.wait_for(lambda: len(client.pulse_times) >= 3, timeout=3800)
        for before, after in zip(client.pulse_times, client.pulse_times[1:]):
            self.assertGreater(after - before, 0.85)
            self.assertLess(after - before, 1.15)

    def test_slow_apply_and_release_do_not_hold_up_same_process_heartbeats(self):
        class SlowCommandsClient(FixtureClient):
            def __init__(self):
                super().__init__()
                self.entries = []
                self.deadline = float("inf")

            def request(self, op, **params):
                entered = time.monotonic()
                self.entries.append((op, entered, threading.get_ident(), os.getpid()))
                if op in {"set", "release"}:
                    time.sleep(1.65)
                if op == "heartbeat":
                    if time.monotonic() >= self.deadline:
                        raise ClientError("TEST FIXTURE: five-second lease expired")
                    self.deadline = time.monotonic() + 5
                result = super().request(op, **params)
                if op == "acquire":
                    self.deadline = time.monotonic() + 5
                return result

        client = SlowCommandsClient()
        window, _ = self.make_window(client)
        self.acquire(window)
        window.panels["cpu"].stage("manual")
        window.apply_button.click()
        self.wait_for(lambda: any(op == "set" for op, *_ in client.entries))
        apply_start = next(at for op, at, *_ in client.entries if op == "set")
        before = time.monotonic()
        window.diagnostics_toggle.click()
        self.assertLess(time.monotonic() - before, 0.2)
        self.wait_for(lambda: not window._busy)
        during_apply = [entry for entry in client.entries if entry[0] == "heartbeat" and entry[1] > apply_start]
        self.assertTrue(during_apply)
        apply_thread = next(thread for op, _, thread, _ in client.entries if op == "set")
        self.assertTrue(all(entry[2] != apply_thread for entry in during_apply))
        window.close()
        self.wait_for(lambda: any(op == "release" for op, *_ in client.entries))
        release_start = next(at for op, at, *_ in client.entries if op == "release")
        self.wait_for(lambda: any(op == "heartbeat" and at > release_start for op, at, *_ in client.entries))
        self.wait_for(lambda: window._thread_done)
        self.assertFalse(window._shutdown_failed)
        self.assertEqual({pid for _, _, _, pid in client.entries}, {os.getpid()})
        pulses = [at for op, at, *_ in client.entries if op == "heartbeat"]
        self.assertLess(max(after - before for before, after in zip(pulses, pulses[1:])), 1.2)

    def test_mutation_longer_than_five_seconds_keeps_renewing_its_lease(self):
        class LongMutationClient(FixtureClient):
            def __init__(self):
                super().__init__()
                self.deadline = float("inf")
                self.started = threading.Event()
                self.pulses = []

            def request(self, op, **params):
                if op == "set":
                    self.started.set()
                    time.sleep(5.2)
                if op == "heartbeat":
                    if time.monotonic() >= self.deadline:
                        raise ClientError("TEST FIXTURE: five-second lease expired")
                    self.pulses.append(time.monotonic())
                    self.deadline = time.monotonic() + 5
                response = super().request(op, **params)
                if op == "acquire":
                    self.deadline = time.monotonic() + 5
                return response

        window, client = self.make_window(LongMutationClient())
        self.acquire(window)
        window.panels["cpu"].stage("manual")
        started = time.monotonic()
        window.apply_button.click()
        self.wait_for(client.started.is_set)
        self.wait_for(lambda: not window._busy and "Firmware accepted" in window.feedback.text(), timeout=6500)
        self.assertGreater(time.monotonic() - started, 5)
        self.assertGreaterEqual(len([pulse for pulse in client.pulses if pulse > started]), 5)
        self.assertTrue(window._owns())
        self.assertFalse(window._worker._recovery_unconfirmed)

    def test_native_release_stops_heartbeats_while_auto_reset_is_pending(self):
        client = NativeFixtureClient()
        self.addCleanup(client.close)
        window, _ = self.make_window(client)
        self.acquire(window)
        window.all_max.click()
        window.apply_button.click()
        self.wait_for(lambda: not window._busy)
        client.hardware.block_auto = True
        window.release_button.click()
        self.wait_for(client.hardware.entered.is_set)
        self.wait_for(lambda: window._worker._renew_suspended and any(
            op == "heartbeat" and not reply["ok"] for op, reply in client.replies
        ), timeout=1800)
        heartbeat_reply = next(reply for op, reply in client.replies if op == "heartbeat" and not reply["ok"])
        self.assertEqual(heartbeat_reply["error"], "No matching active lease for this process")
        self.assertFalse(heartbeat_reply["status"]["lease"]["active"])
        self.assertEqual(heartbeat_reply["status"]["cpu"]["confirmation"], "pending")
        self.assertTrue(window._busy)
        self.assertFalse(window._worker._recovery_unconfirmed)
        self.assertEqual(window._worker._fault_reason, "")
        self.assertFalse(window.state_title.property("autoConfirmed"))
        count = sum(op == "heartbeat" for op, *_ in client.records)
        self.poll_now(window)
        QTest.qWait(150)
        self.assertEqual(count, sum(op == "heartbeat" for op, *_ in client.records))
        client.hardware.release.set()
        self.wait_for(lambda: not window._busy and window.feedback.text().startswith("Lease released."))
        self.assertFalse(window._message_error)
        self.assertNotIn("Request failed", window.state_title.text())
        self.assertEqual(window._worker._fault_reason, "")
        self.assertFalse(window._owns())

    def test_release_does_not_hide_invalid_heartbeat_errors_or_status(self):
        for failure in ("Invalid lease token", "Authorization denied or unavailable", "invalid schema", "inconsistent owner"):
            with self.subTest(failure=failure):
                class InvalidHeartbeatClient(NativeFixtureClient):
                    def request(self, op, **params):
                        response = super().request(op, **params)
                        if op == "heartbeat" and not response["ok"]:
                            if failure == "invalid schema":
                                response["status"]["schema"] = 99
                            elif failure == "inconsistent owner":
                                response["status"]["lease"]["owned_by_client"] = True
                            else:
                                response["error"] = failure
                        return response

                client = InvalidHeartbeatClient()
                self.addCleanup(client.close)
                window, _ = self.make_window(client)
                self.acquire(window)
                client.hardware.block_auto = True
                window.release_button.click()
                self.wait_for(client.hardware.entered.is_set)
                self.wait_for(lambda: "heartbeat" not in window._worker._inflight)
                self.poll_now(window)
                self.wait_for(lambda: window._worker._recovery_unconfirmed
                              and "Recovery requires attention" in window.state_title.text())
                self.assertTrue(window._worker._fault_reason)
                client.hardware.release.set()
                self.wait_for(lambda: not window._busy and window._message_error)
                window.close()
                self.wait_for(lambda: window._thread_done)
                client.close()

    def test_no_reauthorization_while_a_manual_lease_is_owned(self):
        window, client = self.make_window()
        self.acquire(window)
        window.panels["cpu"].stage("manual")
        window.apply_button.click()
        self.wait_for(lambda: not window._busy)
        acquisitions = sum(op == "acquire" for op, *_ in client.records)
        heartbeat_count = sum(op == "heartbeat" for op, *_ in client.records)
        window.command.emit("acquire", {})
        self.wait_for(lambda: "authorization will not run again" in window.feedback.text())
        self.wait_for(lambda: sum(op == "heartbeat" for op, *_ in client.records) > heartbeat_count, timeout=1500)
        self.assertEqual(acquisitions, sum(op == "acquire" for op, *_ in client.records))
        self.assertTrue(window._owns())
        self.assertEqual(client.current["cpu"]["mode"], "manual")

    def test_uncertain_acquire_close_warns_even_without_a_token(self):
        for invalid_reply in (False, True):
            with self.subTest(invalid_reply=invalid_reply):
                class LostAcquireClient(FixtureClient):
                    def request(self, op, **params):
                        response = super().request(op, **params)
                        if op == "acquire":
                            if invalid_reply:
                                response.pop("token")
                            else:
                                raise ClientError("TEST FIXTURE: acquisition reply lost after grant")
                        return response

                window, client = self.make_window(LostAcquireClient())
                window.take_control.click()
                self.wait_for(lambda: not window._busy and window._worker._recovery_unconfirmed)
                self.assertIsNone(window._worker._token)
                self.assertFalse(window.take_control.isEnabled())
                self.poll_now(window)
                self.wait_for(lambda: window.status["backend"]["connected"])
                self.assertTrue(window._worker._recovery_unconfirmed)
                self.assertFalse(window.state_title.property("autoConfirmed"))
                self.assertTrue(client.current["lease"]["active"])
                client.offline = True
                self.poll_now(window)
                self.wait_for(lambda: not window.status["backend"]["connected"])
                self.assertTrue(window._worker._recovery_unconfirmed)
                window.close()
                self.wait_for(lambda: window._thread_done)
                self.assertTrue(window._shutdown_failed)
                self.assertTrue(window.isVisible())
                self.assertIn("without Auto confirmation", window.exit_button.text())
                self.assertFalse(any(op == "release" for op, *_ in client.records))
                window.exit_button.click()

    def test_native_acquire_timeout_before_grant_requires_uncertain_close(self):
        client = NativeFixtureClient(timeout_acquire=True)
        self.addCleanup(client.close)
        window, _ = self.make_window(client)
        client.hardware.block_auto = True
        window.take_control.click()
        self.wait_for(lambda: not window._busy and any(op == "acquire" for op, _ in client.replies))
        reply = next(reply for op, reply in client.replies if op == "acquire")
        self.assertEqual(client.wait_budgets, [6])
        self.assertTrue(client.hardware.entered.is_set())
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "Controller did not complete request; outcome unconfirmed")
        self.assertFalse(reply["status"]["lease"]["active"])
        self.assertEqual(reply["status"]["cpu"]["confirmation"], "pending")
        self.assertIsNone(client.controller.lease)
        self.assertIsNone(window._worker._token)
        self.assertTrue(window._worker._recovery_unconfirmed)
        self.poll_now(window)
        self.wait_for(lambda: window.status["cpu"]["confirmation"] == "pending")
        self.assertTrue(window._worker._recovery_unconfirmed)
        self.assertFalse(window.take_control.isEnabled())
        self.assertFalse(window.state_title.property("autoConfirmed"))
        window.close()
        self.wait_for(lambda: window._thread_done)
        self.assertTrue(window._shutdown_failed)
        self.assertTrue(window.isVisible())
        self.assertIn("without Auto confirmation", window.exit_button.text())

    def test_uncertain_acquire_clears_only_after_fresh_inactive_lease_auto(self):
        class LostAcquireClient(FixtureClient):
            def request(self, op, **params):
                response = super().request(op, **params)
                if op == "acquire":
                    raise ClientError("TEST FIXTURE: grant reply lost")
                return response

        window, client = self.make_window(LostAcquireClient())
        window.take_control.click()
        self.wait_for(lambda: not window._busy and window._worker._recovery_unconfirmed)
        client.current["lease"].update(active=False, owned_by_client=False)
        client.current["sample_age_seconds"] = 10
        self.poll_now(window)
        self.wait_for(lambda: window.status["sample_age_seconds"] >= 10)
        self.assertTrue(window._worker._recovery_unconfirmed)
        client.current["sample_age_seconds"] = 0
        client.current["gpu"]["confirmation"] = "pending"
        self.poll_now(window)
        self.wait_for(lambda: window.status["gpu"]["confirmation"] == "pending")
        self.assertTrue(window._worker._recovery_unconfirmed)
        client.current["gpu"]["confirmation"] = "firmware-confirmed"
        self.poll_now(window)
        self.wait_for(lambda: window.take_control.isEnabled() and "Recovery observed" in window.feedback.text())
        self.assertFalse(window._worker._recovery_unconfirmed)
        self.assertIn("Recovery observed", window.feedback.text())
        window.close()
        self.wait_for(lambda: window._thread_done)
        self.assertFalse(window._shutdown_failed)

    def test_old_inflight_status_cannot_clear_an_unknown_acquisition(self):
        class OldStatusClient(FixtureClient):
            def __init__(self):
                super().__init__()
                self.hold_status = False
                self.status_entered = threading.Event()
                self.status_release = threading.Event()

            def request(self, op, **params):
                response = super().request(op, **params)
                if op == "status" and self.hold_status:
                    self.status_entered.set()
                    self.status_release.wait(4)
                if op == "acquire":
                    raise ClientError("TEST FIXTURE: acquisition granted but reply lost")
                return response

        client = OldStatusClient()
        window, _ = self.make_window(client)
        self.addCleanup(client.status_release.set)
        client.hold_status = True
        self.poll_now(window)
        self.wait_for(client.status_entered.is_set)
        window.take_control.click()
        self.wait_for(lambda: window._worker._recovery_unconfirmed and not window._busy)
        client.status_release.set()
        QTest.qWait(150)
        self.assertTrue(window._worker._recovery_unconfirmed)
        self.assertFalse(window.take_control.isEnabled())
        self.assertFalse(window.state_title.property("autoConfirmed"))

    def test_one_second_heartbeat_latency_is_bounded_without_overlapping_renewals(self):
        class SlowHeartbeatClient(FixtureClient):
            def __init__(self):
                super().__init__()
                self.pulses = []
                self.active_heartbeats = 0
                self.max_heartbeats = 0

            def request(self, op, **params):
                if op == "heartbeat":
                    self.active_heartbeats += 1
                    self.max_heartbeats = max(self.max_heartbeats, self.active_heartbeats)
                    self.pulses.append(time.monotonic())
                    try:
                        time.sleep(1.05)
                        return super().request(op, **params)
                    finally:
                        self.active_heartbeats -= 1
                if op == "set":
                    time.sleep(1.65)
                return super().request(op, **params)

        client = SlowHeartbeatClient()
        window, _ = self.make_window(client)
        self.acquire(window)
        window.panels["cpu"].stage("manual")
        window.apply_button.click()
        self.wait_for(lambda: not window._busy)
        self.wait_for(lambda: len(client.pulses) >= 2, timeout=1500)
        self.assertTrue(window._owns())
        self.assertEqual(client.max_heartbeats, 1)
        self.assertLess(client.pulses[1] - client.pulses[0], 2.2)

    def test_fault_during_apply_is_immediate_and_recovery_is_serialized(self):
        class GatedApplyClient(FixtureClient):
            def __init__(self):
                super().__init__()
                self.apply_entered = threading.Event()
                self.apply_release = threading.Event()
                self.fail_heartbeat = False
                self.order = []

            def request(self, op, **params):
                self.order.append(op)
                if op == "set":
                    self.apply_entered.set()
                    self.apply_release.wait(4)
                if op == "heartbeat" and self.fail_heartbeat:
                    raise ClientError("TEST FIXTURE: heartbeat failed while Apply was in flight")
                return super().request(op, **params)

        client = GatedApplyClient()
        window, _ = self.make_window(client)
        self.addCleanup(client.apply_release.set)
        self.acquire(window)
        window.panels["cpu"].stage("manual")
        window.apply_button.click()
        self.wait_for(client.apply_entered.is_set)
        self.wait_for(lambda: "heartbeat" not in window._worker._inflight)
        client.fail_heartbeat = True
        started = time.monotonic()
        self.poll_now(window)
        self.wait_for(lambda: window.status["recovery"]["state"] == "fault", timeout=600)
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertTrue(window._busy)
        self.assertIn("Recovery requires attention", window.state_title.text())
        self.assertFalse(window.apply_button.isEnabled())
        self.assertNotIn("release", client.order)
        window.close()
        client.apply_release.set()
        self.wait_for(lambda: window._thread_done)
        self.assertTrue(client.release_entered.is_set())
        self.assertFalse(window._shutdown_failed)
        self.assertGreater(client.order.index("release"), client.order.index("set"))

    def test_new_lease_heartbeats_do_not_wait_for_an_older_status_query(self):
        class HeldStatusClient(FixtureClient):
            def __init__(self):
                super().__init__()
                self.hold_status = False
                self.status_entered = threading.Event()
                self.status_release = threading.Event()

            def request(self, op, **params):
                response = super().request(op, **params)
                if op == "status" and self.hold_status:
                    self.status_entered.set()
                    self.status_release.wait(4)
                return response

        client = HeldStatusClient()
        window, _ = self.make_window(client)
        self.addCleanup(client.status_release.set)
        client.hold_status = True
        self.poll_now(window)
        self.wait_for(client.status_entered.is_set)
        self.acquire(window)
        self.wait_for(lambda: any(op == "heartbeat" for op, *_ in client.records), timeout=600)
        self.assertIn("status", window._worker._inflight)
        self.assertFalse(client.status_release.is_set())
        client.status_release.set()
        self.wait_for(lambda: "status" not in window._worker._inflight)
        self.assertTrue(window._owns())

    def test_blocked_fallback_is_one_daemon_and_does_not_delay_shutdown(self):
        class GatedSensors:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.calls = 0

            def snapshot(self):
                self.calls += 1
                self.entered.set()
                self.release.wait()
                return fixture_status(available=False)

        sensors = GatedSensors()
        client = FixtureClient()
        client.offline = True
        started = time.monotonic()
        window, _ = self.make_window(client, telemetry_factory=lambda: sensors)
        self.addCleanup(sensors.release.set)
        self.wait_for(sensors.entered.is_set)
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertFalse(window.take_control.isEnabled())
        self.assertEqual(window.panels["cpu"].rpm.text(), "--")
        self.wait_for(lambda: window._worker._sample_timed_out, timeout=2500)
        sampler = window._worker._sampler
        self.assertTrue(sampler.daemon)
        self.poll_now(window)
        QTest.qWait(150)
        self.assertEqual(sensors.calls, 1)
        started = time.monotonic()
        window.close()
        self.wait_for(lambda: window._thread_done, timeout=600)
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertTrue(sampler.is_alive())
        sensors.release.set()

    def test_fallback_reuses_one_provider_through_gpu_warmup_and_closes_it(self):
        instances = []

        class WarmingSensors:
            def __init__(self):
                self.calls = 0
                self.threads = [threading.get_ident()]
                self.closed = threading.Event()
                self.close_calls = 0
                instances.append(self)

            def snapshot(self):
                self.calls += 1
                self.threads.append(threading.get_ident())
                measured = fixture_status(available=False)
                if self.calls == 1:
                    measured["gpu"]["temperature_c"] = None
                return measured

            def close(self):
                self.close_calls += 1
                self.threads.append(threading.get_ident())
                self.closed.set()

        client = FixtureClient()
        client.offline = True
        started = time.monotonic()
        window, _ = self.make_window(client, telemetry_factory=WarmingSensors)
        self.wait_for(lambda: window.panels["cpu"].temperature.text() == "61 C", timeout=600)
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(window.panels["gpu"].temperature.text(), "-- C")
        self.assertEqual(window.panels["cpu"].rpm.text(), "2,400")
        self.assertEqual(len(instances), 1)
        self.assertFalse(window.take_control.isEnabled())
        context = window._worker._sensor_context
        self.assertNotIsInstance(context, QObject)
        sampler = context.thread
        self.wait_for(lambda: window.panels["gpu"].temperature.text() == "49 C", timeout=2800)
        self.assertEqual(len(instances), 1)
        self.assertGreaterEqual(instances[0].calls, 2)
        self.assertIs(window._worker._sampler, sampler)
        self.assertEqual(len(set(instances[0].threads)), 1)
        self.assertNotEqual(instances[0].threads[0], threading.get_ident())
        started = time.monotonic()
        window.close()
        self.wait_for(lambda: window._thread_done, timeout=600)
        self.assertLess(time.monotonic() - started, 0.6)
        self.wait_for(instances[0].closed.is_set, timeout=600)
        self.wait_for(lambda: not sampler.is_alive(), timeout=600)
        self.assertEqual(instances[0].close_calls, 1)
        self.assertEqual(len(set(instances[0].threads)), 1)

    def test_stop_during_sensor_construction_does_not_wait_or_take_a_snapshot(self):
        entered = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        snapshots = []
        threads = []

        class LateSensors:
            def __init__(self):
                threads.append(threading.get_ident())
                entered.set()
                release.wait()

            def snapshot(self):
                snapshots.append(True)
                return fixture_status(available=False)

            def close(self):
                threads.append(threading.get_ident())
                closed.set()

        client = FixtureClient()
        client.offline = True
        window, _ = self.make_window(client, telemetry_factory=LateSensors)
        self.addCleanup(release.set)
        self.wait_for(entered.is_set)
        context = window._worker._sensor_context
        self.assertNotIsInstance(context.thread._target.__self__, QObject)
        window.close()
        self.wait_for(lambda: window._thread_done, timeout=600)
        self.assertTrue(context.thread.is_alive())
        self.assertFalse(closed.is_set())
        release.set()
        self.wait_for(closed.is_set, timeout=600)
        self.wait_for(lambda: not context.thread.is_alive(), timeout=600)
        self.assertEqual(snapshots, [])
        self.assertEqual(len(set(threads)), 1)

    def test_provider_cleanup_cannot_block_gui_shutdown(self):
        entered = threading.Event()
        release = threading.Event()
        close_calls = []

        class SlowClosingSensors:
            def snapshot(self):
                return fixture_status(available=False)

            def close(self):
                close_calls.append(threading.get_ident())
                entered.set()
                release.wait()

        client = FixtureClient()
        client.offline = True
        window, _ = self.make_window(client, telemetry_factory=SlowClosingSensors)
        self.addCleanup(release.set)
        self.wait_for(lambda: window.panels["cpu"].temperature.text() == "61 C")
        sampler = window._worker._sampler
        window.close()
        self.wait_for(lambda: window._thread_done, timeout=600)
        self.wait_for(entered.is_set, timeout=600)
        self.assertTrue(sampler.is_alive())
        self.assertEqual(len(close_calls), 1)
        self.assertNotEqual(close_calls[0], threading.get_ident())
        release.set()
        self.wait_for(lambda: not sampler.is_alive(), timeout=600)

    def test_screenshot_delay_allows_warmup_without_outliving_quit_after(self):
        from nitro_control.gui import main

        with tempfile.TemporaryDirectory(prefix="nitro-gui-capture-test-") as directory:
            path = os.path.join(directory, "TEST-FIXTURE-window.png")
            for seconds, expected in ((5, 4750), (1, 750), (0.1, 0), (10, 6000), (None, 6000)):
                with self.subTest(seconds=seconds), \
                        patch("nitro_control.gui.QApplication") as application, \
                        patch("nitro_control.gui.NitroControlWindow") as constructor, \
                        patch("nitro_control.gui.apply_theme"), \
                        patch("nitro_control.gui.signal.signal"), \
                        patch("nitro_control.gui.QTimer.singleShot") as scheduled:
                    application.instance.return_value.exec.return_value = 0
                    constructor.return_value._thread = None
                    constructor.return_value.grab.return_value.save.return_value = True
                    args = ["--read-only", "--screenshot", path]
                    if seconds is not None:
                        args += ["--quit-after", str(seconds)]
                    self.assertEqual(main(args), 0)
                    self.assertEqual(scheduled.call_args_list[0].args[0], expected)
                    if seconds is not None:
                        self.assertLess(expected, seconds * 1000)
                    scheduled.call_args_list[0].args[1]()
                    constructor.return_value.grab.return_value.save.assert_called_once_with(path)

    def test_lease_fault_is_visible_before_a_gated_fallback_finishes(self):
        gate = threading.Event()
        entered = threading.Event()

        class GatedSensors:
            def snapshot(self):
                entered.set()
                gate.wait()
                return fixture_status(available=False)

        window, client = self.make_window(telemetry_factory=GatedSensors)
        self.addCleanup(gate.set)
        self.acquire(window)
        client.offline = True
        started = time.monotonic()
        self.poll_now(window)
        self.wait_for(lambda: window.status["recovery"]["state"] == "fault", timeout=600)
        self.wait_for(entered.is_set)
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertIn("Recovery requires attention", window.state_title.text())
        self.assertFalse(window.apply_button.isEnabled())
        self.assertTrue(window._worker._sampler.is_alive())
        window.close()
        self.wait_for(lambda: window._thread_done, timeout=600)
        self.assertTrue(window._shutdown_failed)
        self.assertTrue(window.isVisible())
        gate.set()

    def test_late_fallback_result_cannot_overwrite_reconnected_status(self):
        gate = threading.Event()
        entered = threading.Event()

        class GatedSensors:
            def snapshot(self):
                entered.set()
                gate.wait()
                measured = fixture_status(available=False)
                measured["cpu"]["temperature_c"] = 99
                return measured

        client = FixtureClient()
        client.offline = True
        window, _ = self.make_window(client, telemetry_factory=GatedSensors)
        self.addCleanup(gate.set)
        self.wait_for(entered.is_set)
        client.offline = False
        self.poll_now(window)
        self.wait_for(lambda: window.status["backend"]["connected"])
        gate.set()
        QTest.qWait(200)
        self.assertTrue(window.status["backend"]["connected"])
        self.assertEqual(window.panels["cpu"].temperature.text(), "61 C")

    def test_authorization_and_close_do_not_block_gui_and_release_late_token(self):
        client = FixtureClient()
        client.acquire_gate = threading.Event()
        window, _ = self.make_window(client)
        window.take_control.click()
        self.wait_for(lambda: any(op == "acquire" for op, *_ in client.records))
        self.assertTrue(window._busy)
        start = time.monotonic()
        window.diagnostics_toggle.click()
        self.assertTrue(window.diagnostics.isVisible())
        window.close()
        self.app.processEvents()
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertTrue(window.isVisible())
        self.assertFalse(window._thread_done)
        client.acquire_gate.set()
        self.wait_for(lambda: window._thread_done)
        self.assertTrue(client.release_entered.is_set())
        self.assertFalse(window._thread.isRunning())

    def test_repeated_late_authorization_shutdown_destroys_workers_before_windows(self):
        for _ in range(12):
            client = FixtureClient()
            client.acquire_gate = threading.Event()
            window = NitroControlWindow(client=client, socket_path=client.path,
                                        telemetry_factory=FixtureTelemetry)
            window.show()
            try:
                self.wait_for(window.take_control.isEnabled)
                window.take_control.click()
                self.wait_for(lambda: any(op == "acquire" for op, *_ in client.records))
                window.close()
                client.acquire_gate.set()
                self.wait_for(lambda: window._thread_done)
                self.assertTrue(client.release_entered.is_set())
                self.assertFalse(window._thread.isRunning())
            finally:
                client.acquire_gate.set()
                self.shutdown_window(window)

    def test_close_releases_owned_lease_and_stops_thread(self):
        window, client = self.make_window()
        self.acquire(window)
        window.close()
        self.wait_for(lambda: window._thread_done and not window.isVisible())
        self.assertTrue(client.release_entered.is_set())
        self.assertFalse(window._thread.isRunning())

    def test_failed_close_recovery_is_visible_and_requires_explicit_exit(self):
        window, client = self.make_window()
        self.acquire(window)
        client.release_fails = True
        window.close()
        self.wait_for(lambda: window._thread_done)
        self.assertTrue(window.isVisible())
        self.assertIn("Recovery requires attention", window.state_title.text())
        self.assertIn("without Auto confirmation", window.exit_button.text())
        window.exit_button.click()
        self.wait_for(lambda: not window.isVisible())

    def test_narrow_layout_stacks_and_scrolls_without_horizontal_overflow(self):
        window, _ = self.make_window()
        for width, stacked in ((900, False), (640, True), (900, False), (640, True)):
            window.resize(width, 620)
            self.app.processEvents()
            self.assertEqual(window._stacked, stacked)
            self.assertEqual(window.scroll.horizontalScrollBar().maximum(), 0)
            self.assertLessEqual(window.hardware.width(), window.scroll.viewport().width())
            if stacked:
                self.assertGreater(window.panels["gpu"].y(), window.panels["cpu"].y())
                self.assertGreater(window.scroll.verticalScrollBar().maximum(), 0)
        window.diagnostics_toggle.click()
        self.app.processEvents()
        self.assertEqual(window.scroll.horizontalScrollBar().maximum(), 0)
        self.assertGreater(window.scroll.verticalScrollBar().maximum(), 0)

    def test_default_window_core_actions_fit_without_scrolling(self):
        window, _ = self.make_window(FixtureClient(missing=True, available=False))
        window.resize(900, 680)
        self.app.processEvents()
        self.assertEqual(window.scroll.verticalScrollBar().maximum(), 0)
        self.assertTrue(window.apply_button.isVisible())
        self.assertTrue(window.diagnostics_toggle.isVisible())

    def test_keyboard_slider_focus_and_diagnostics_no_token(self):
        window, _ = self.make_window()
        self.acquire(window)
        panel = window.panels["cpu"]
        panel.mode_buttons["manual"].click()
        panel.slider.setFocus()
        before = panel.slider.value()
        QTest.keyClick(panel.slider, Qt.Key.Key_Right)
        self.assertEqual(panel.slider.value(), before + 1)
        self.assertEqual(panel.percent.value(), before + 1)
        self.assertTrue(panel.slider.accessibleName())
        window.diagnostics_toggle.click()
        self.app.processEvents()
        self.assertIn("hardware acceptance", window.diagnostics.toPlainText())
        self.assertNotIn("TEST-FIXTURE-SECRET", window.diagnostics.toPlainText())

    def test_accessible_labels_expose_actual_values_and_live_command_feedback(self):
        window, client = self.make_window()
        self.acquire(window)
        panel = window.panels["cpu"]
        panel.stage("manual")
        panel.percent.setValue(72)
        window.apply_button.click()
        self.wait_for(lambda: not window._busy)
        for widget in (panel.rpm, panel.temperature, panel.requested, panel.firmware,
                       panel.confirmation, panel.rpm_response, window.feedback, window.verification):
            accessible = QAccessible.queryAccessibleInterface(widget)
            self.assertIsNotNone(accessible)
            self.assertEqual(accessible.text(QAccessible.Text.Name), widget.text())
            self.assertTrue(accessible.text(QAccessible.Text.Description))
        self.assertEqual(QAccessible.queryAccessibleInterface(panel.rpm).text(QAccessible.Text.Name), "2,400")
        self.assertIn("72%", QAccessible.queryAccessibleInterface(panel.firmware).text(QAccessible.Text.Name))
        self.assertIn("Firmware accepted", QAccessible.queryAccessibleInterface(window.feedback).text(QAccessible.Text.Name))
        self.assertEqual(QAccessible.queryAccessibleInterface(panel.slider).valueInterface().currentValue(), 72)
        client.current["cpu"].update(rpm=None, temperature_c=None)
        self.poll_now(window)
        self.wait_for(lambda: not window._owns())
        self.assertEqual(QAccessible.queryAccessibleInterface(panel.rpm).text(QAccessible.Text.Name), "--")
        self.assertEqual(QAccessible.queryAccessibleInterface(panel.temperature).text(QAccessible.Text.Name), "-- C")
        self.assertNotEqual(QAccessible.queryAccessibleInterface(window.feedback).text(QAccessible.Text.Name),
                            "Command result and unapplied changes")

    def test_palette_is_canonical_and_readable(self):
        self.assertEqual(OKLCH["primary"], (0.650, 0.160, 0))
        palette = colors()

        def luminance(color):
            rgb = (color.redF(), color.greenF(), color.blueF())
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
            return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

        for foreground, background in (("ink", "surface"), ("muted", "surface"),
                                      ("muted", "raised"), ("background", "primary"),
                                      ("primary", "surface"), ("warning", "background")):
            light, dark = sorted((luminance(palette[foreground]), luminance(palette[background])), reverse=True)
            self.assertGreaterEqual((light + 0.05) / (dark + 0.05), 4.5, (foreground, background))


if __name__ == "__main__":
    unittest.main()
