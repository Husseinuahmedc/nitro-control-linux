"""Bounded, one-request-per-connection client for PROTOCOL.md version 1.

This module never authorizes implicitly, retries writes, or reads hardware.
Call it from a worker, not from the Qt GUI thread.
"""

from __future__ import annotations

import json
import math
import os
import socket
import struct
import time
from typing import Any


DEFAULT_SOCKET = "/run/nitro-control/control.sock"
MAX_FRAME = 16384
RPC_TIMEOUT = 2.0
ACQUIRE_TIMEOUT = 24.0
MUTATION_TIMEOUT = 6.5
MODES = {"auto", "max", "manual"}
CONFIRMATIONS = {"unsupported", "pending", "firmware-confirmed", "failed"}
RPM_RESPONSES = {"unverified", "pending", "observed", "not-observed"}


class ClientError(Exception):
    """A local validation, trust, transport or response-schema failure."""


def validate_request(op: str, params: dict[str, Any]) -> None:
    allowed = {
        "status": set(), "acquire": set(),
        "heartbeat": {"token"}, "set": {"token", "cpu", "gpu"},
        "auto": {"token"}, "max": {"token"}, "release": {"token"},
    }
    if not isinstance(op, str) or op not in allowed:
        raise ClientError("Unsupported controller operation")
    if set(params) - allowed[op]:
        raise ClientError("Unexpected request parameter")
    if "token" in allowed[op]:
        if not isinstance(params.get("token"), str) or not params["token"]:
            raise ClientError("An active session token is required")
    if op != "set":
        return
    if not ({"cpu", "gpu"} & set(params)):
        raise ClientError("Select at least one fan channel")
    for channel in ("cpu", "gpu"):
        if channel not in params:
            continue
        value = params[channel]
        if not isinstance(value, dict) or not isinstance(value.get("mode"), str):
            raise ClientError(f"{channel.upper()}: a valid mode is required")
        if value["mode"] not in MODES or set(value) - {"mode", "percent"}:
            raise ClientError(f"{channel.upper()}: unsupported mode or parameter")
        if value["mode"] == "manual":
            percent = value.get("percent")
            if type(percent) is not int or not 50 <= percent <= 100:
                raise ClientError("Manual requests must be integer percentages from 50 to 100")
        elif "percent" in value:
            raise ClientError("Only Manual accepts a percentage")


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_status(status: Any) -> None:
    """Reject malformed status rather than letting it imply a successful mode."""
    if not isinstance(status, dict) or type(status.get("schema")) is not int or status["schema"] != 1:
        raise ClientError("Unsupported or missing controller status schema")
    backend = status.get("backend")
    if not isinstance(backend, dict) or any(
        type(backend.get(key)) is not bool
        for key in ("connected", "control_available", "control_enabled", "hardware_verified")
    ):
        raise ClientError("Invalid controller capability status")
    if backend.get("reason") is not None and not isinstance(backend["reason"], str):
        raise ClientError("Invalid controller reason")
    for name in ("cpu", "gpu"):
        channel = status.get(name)
        if not isinstance(channel, dict):
            raise ClientError(f"Missing {name.upper()} status")
        for key in ("temperature_c", "rpm", "pwm", "percent", "requested_percent"):
            if key not in channel or (channel[key] is not None and not _number(channel[key])):
                raise ClientError(f"Invalid {name.upper()} {key}")
        for key in ("rpm", "pwm", "percent", "requested_percent"):
            if channel[key] is not None and channel[key] < 0:
                raise ClientError(f"Invalid negative {name.upper()} {key}")
        for key in ("percent", "requested_percent"):
            if channel[key] is not None and channel[key] > 100:
                raise ClientError(f"Invalid {name.upper()} percentage")
        if channel["pwm"] is not None and channel["pwm"] > 255:
            raise ClientError(f"Invalid {name.upper()} PWM")
        for key in ("mode", "requested_mode"):
            if key not in channel or (channel[key] is not None and channel[key] not in MODES):
                raise ClientError(f"Invalid {name.upper()} mode")
        if channel.get("confirmation") not in CONFIRMATIONS:
            raise ClientError(f"Invalid {name.upper()} mode confirmation")
        if channel.get("rpm_response") not in RPM_RESPONSES:
            raise ClientError(f"Invalid {name.upper()} RPM response")
    lease = status.get("lease")
    if not isinstance(lease, dict) or any(
        type(lease.get(key)) is not bool for key in ("active", "owned_by_client")
    ):
        raise ClientError("Invalid controller lease status")
    if lease.get("owner_pid") is not None and type(lease["owner_pid"]) is not int:
        raise ClientError("Invalid controller owner PID")
    if not _number(lease.get("remaining_seconds")) or lease["remaining_seconds"] < 0:
        raise ClientError("Invalid controller lease expiry")
    recovery = status.get("recovery")
    if not isinstance(recovery, dict) or not isinstance(recovery.get("state"), str):
        raise ClientError("Invalid controller recovery status")
    if recovery.get("error") is not None and not isinstance(recovery["error"], str):
        raise ClientError("Invalid controller recovery error")
    if status.get("error") is not None and not isinstance(status["error"], str):
        raise ClientError("Invalid controller error")
    if not _number(status.get("sample_age_seconds")) or status["sample_age_seconds"] < 0:
        raise ClientError("Invalid telemetry sample age")
    if not isinstance(status.get("events"), list):
        raise ClientError("Invalid controller events")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ClientError("Duplicate JSON response key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ClientError(f"Non-finite JSON value: {value}")


class SocketClient:
    def __init__(
        self, path: str | os.PathLike[str] = DEFAULT_SOCKET, *,
        timeout: float = RPC_TIMEOUT, acquire_timeout: float = ACQUIRE_TIMEOUT,
        mutation_timeout: float = MUTATION_TIMEOUT, read_only: bool = False,
    ) -> None:
        self.path = os.fspath(path)
        self.timeout = timeout
        self.acquire_timeout = acquire_timeout
        self.mutation_timeout = mutation_timeout
        self.read_only = read_only
        self.peer_uid: int | None = None

    def clone(self) -> SocketClient:
        """Independent peer credentials and sockets for concurrent RPC lanes."""
        return SocketClient(self.path, timeout=self.timeout, acquire_timeout=self.acquire_timeout,
                            mutation_timeout=self.mutation_timeout, read_only=self.read_only)

    def request(self, op: str, **params: Any) -> dict[str, Any]:
        validate_request(op, params)
        if self.read_only and op != "status":
            raise ClientError("This window was started with --read-only; control requests are disabled")
        try:
            frame = json.dumps({"op": op, **params}, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ClientError("Request is not valid JSON") from exc
        if len(frame) > MAX_FRAME:
            raise ClientError("Request exceeds the 16 KiB protocol limit")
        timeout = (self.acquire_timeout if op == "acquire" else self.mutation_timeout
                   if op in {"set", "auto", "max", "release"} else self.timeout)
        deadline = time.monotonic() + timeout
        self.peer_uid = None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                connection.connect(self.path)
                if hasattr(socket, "SO_PEERCRED"):
                    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    _, self.peer_uid, _ = struct.unpack("3i", credentials)
                if self.peer_uid != 0 and (self.path == DEFAULT_SOCKET or op != "status"):
                    raise ClientError("Controller peer is not verified as root; refusing this request")
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                connection.sendall(frame)
                received = bytearray()
                while b"\n" not in received:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ClientError("Controller request timed out")
                    connection.settimeout(remaining)
                    part = connection.recv(min(4096, MAX_FRAME + 1 - len(received)))
                    if not part:
                        raise ClientError("Controller closed the connection before a complete response")
                    received.extend(part)
                    if len(received) > MAX_FRAME:
                        raise ClientError("Controller response exceeds the 16 KiB protocol limit")
                line, extra = bytes(received).split(b"\n", 1)
                if extra:
                    raise ClientError("Controller returned more than one response frame")
            response = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise ClientError("Invalid controller response")
            validate_status(response.get("status"))
            if not response["ok"] and not isinstance(response.get("error"), str):
                raise ClientError("Controller rejected the request without an explanation")
            if op == "acquire" and response["ok"]:
                if not isinstance(response.get("token"), str) or not response["token"]:
                    raise ClientError("Controller did not return a session token")
            return response
        except ClientError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ClientError(f"{type(exc).__name__}: {exc}") from exc
