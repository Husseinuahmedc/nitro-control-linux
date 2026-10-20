"""Kernel peer identity and fail-closed, fixed-action polkit authorization."""

from dataclasses import dataclass
import os
from pathlib import Path
import socket
import struct
import subprocess

ACTION = "org.nitrocontrol.control"


@dataclass(frozen=True)
class Peer:
    uid: int
    pid: int
    start_ticks: int


def process_identity(pid, *, strict=False):
    try:
        path = Path(f"/proc/{pid}")
        data = (path / "stat").read_text()
        fields = data[data.rindex(")") + 2:].split()
        if fields[0] in ("Z", "X"):
            return None
        return Peer(path.stat().st_uid, pid, int(fields[19]))
    except (FileNotFoundError, ProcessLookupError):
        if strict:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return None
            except OSError as exc:
                raise ValueError("Cannot establish process exit") from exc
            raise ValueError("Process exists but its identity is unavailable")
        return None
    except (OSError, ValueError, IndexError) as exc:
        if strict:
            raise ValueError("Cannot establish process identity") from exc
        return None


def peer_alive(peer):
    return process_identity(peer.pid) == peer


def socket_peer(connection):
    pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    peer = process_identity(pid)
    if peer is None or peer.uid != uid:
        raise ValueError("Socket peer process identity unavailable")
    return peer


def authorize(peer, *, runner=subprocess.run):
    if not peer_alive(peer):
        return False
    try:
        result = runner(["/usr/bin/pkcheck", "--action-id", ACTION, "--process",
                         f"{peer.pid},{peer.start_ticks},{peer.uid}", "--allow-user-interaction"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=15, check=False)
        return result.returncode == 0 and peer_alive(peer)
    except (OSError, subprocess.SubprocessError):
        return False
