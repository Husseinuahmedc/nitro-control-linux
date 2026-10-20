"""Safe runtime ownership, independent Auto recovery, and sleep/stop helpers."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import time

from .auth import Peer, process_identity, socket_peer
from .hardware import Hardware, reset_auto

RUNTIME = Path("/run/nitro-control")
MAX_MESSAGE = 16384
SOCKET_MODES = {"control.sock": 0o666, "supervisor.sock": 0o600}


class OwnerBusy(RuntimeError):
    pass


def _safe_file(info, uid):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == uid
            and not info.st_mode & 0o022 and info.st_nlink == 1)


def _directory_fd(directory, *, create=False):
    """Walk pinned, no-follow directories; root never trusts a user ancestor."""
    path = Path(directory).absolute()
    if ".." in path.parts:
        raise ValueError("Runtime path must not contain '..'")
    uid = os.geteuid()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for index, name in enumerate((None, *path.parts[1:])):
            if name is not None:
                created = False
                try:
                    child = os.open(name, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create or index != len(path.parts) - 1:
                        raise
                    os.mkdir(name, 0o755, dir_fd=fd)
                    created = True
                    child = os.open(name, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                if created:
                    info = os.fstat(fd)
                    if info.st_uid != uid:
                        raise ValueError("New runtime directory has unexpected owner")
                    os.fchmod(fd, 0o755)
            info = os.fstat(fd)
            if info.st_uid not in (0, uid) or info.st_mode & 0o022:
                raise ValueError("Untrusted runtime directory ancestry")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_state(directory):
    try:
        directory_fd = os.dup(directory.dir_fd) if isinstance(directory, Runtime) else _directory_fd(directory)
    except FileNotFoundError:
        return None
    try:
        parent = os.fstat(directory_fd)
        try:
            fd = os.open("recovery.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=directory_fd)
        except FileNotFoundError:
            return None
    finally:
        os.close(directory_fd)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not _safe_file(info, parent.st_uid) or info.st_size > MAX_MESSAGE:
            raise ValueError("Unsafe recovery record")
        try:
            data = json.loads(stream.read(MAX_MESSAGE + 1))
        except RecursionError as exc:
            raise ValueError("Invalid recovery record nesting") from exc
    if (not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1
            or "worker" not in data
            or not isinstance(data.get("state"), str)
            or (data.get("error") is not None and not isinstance(data["error"], str))):
        raise ValueError("Invalid recovery record")
    return data


class Runtime:
    def __init__(self, directory=RUNTIME, *, control=False, create=True):
        self.path = Path(directory).absolute()
        self.uid = os.geteuid()
        self.lock_fd = None
        self.dir_fd = None
        self.socket_ids = {}
        if control and (self.uid != 0 or self.path != RUNTIME):
            raise ValueError("Control requires root and the fixed /run/nitro-control runtime")
        self.dir_fd = _directory_fd(self.path, create=create)
        info = os.fstat(self.dir_fd)
        if info.st_uid != self.uid or stat.S_IMODE(info.st_mode) != 0o755:
            self.close()
            raise ValueError("Runtime directory must be owned by the daemon and mode 0755")

    def acquire(self):
        if self.dir_fd is None:
            raise RuntimeError("Runtime is closed")
        fd = os.open("owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.dir_fd)
        if not _safe_file(os.fstat(fd), self.uid):
            os.close(fd)
            raise ValueError("Unsafe owner.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise OwnerBusy("Another supervisor/recovery owner holds owner.lock; no writes attempted") from exc
        self.lock_fd = fd

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.dir_fd is not None:
            os.close(self.dir_fd)
            self.dir_fd = None

    def write_state(self, state):
        if self.lock_fd is None:
            raise RuntimeError("State writes require the owner lock")
        try:
            if not _safe_file(os.stat("recovery.json", dir_fd=self.dir_fd, follow_symlinks=False), self.uid):
                raise ValueError("Unsafe recovery.json target")
        except FileNotFoundError:
            pass
        payload = json.dumps(state, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if len(payload) > MAX_MESSAGE:
            raise ValueError("Recovery record too large")
        name = ".recovery-" + secrets.token_hex(12)
        directory_fd = self.dir_fd
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o644, dir_fd=directory_fd)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, "recovery.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def socket(self, name, mode):
        if self.dir_fd is None:
            raise RuntimeError("Runtime is closed")
        if SOCKET_MODES.get(name) != mode:
            raise ValueError("Unknown runtime socket or permissions")
        if self.lock_fd is None and name != "control.sock":
            raise RuntimeError("Supervisor socket requires owner lock")
        try:
            info = os.stat(name, dir_fd=self.dir_fd, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid:
                raise ValueError("Refusing to replace non-socket or foreign socket")
            os.unlink(name, dir_fd=self.dir_fd)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(f"/proc/self/fd/{self.dir_fd}/{name}")
            info = os.stat(name, dir_fd=self.dir_fd, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid:
                raise ValueError("Runtime socket substituted during bind")
            os.chmod(name, mode, dir_fd=self.dir_fd, follow_symlinks=False)
            after = os.stat(name, dir_fd=self.dir_fd, follow_symlinks=False)
            if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("Runtime socket substituted during permission update")
            self.socket_ids[name] = (info.st_dev, info.st_ino)
            server.listen(16)
            return server
        except BaseException:
            server.close()
            raise

    def remove_socket(self, name):
        identity = self.socket_ids.pop(name, None)
        if identity is None or self.dir_fd is None:
            return
        try:
            info = os.stat(name, dir_fd=self.dir_fd, follow_symlinks=False)
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                os.unlink(name, dir_fd=self.dir_fd)
        except FileNotFoundError:
            pass


def process_ref(pid):
    peer = process_identity(pid)
    if peer is None or os.getpgid(pid) != pid or os.getsid(pid) != pid:
        raise ValueError("Worker is not the leader of its own process group/session")
    return {"pid": pid, "uid": peer.uid, "start_ticks": peer.start_ticks, "pgid": pid}


def ref_peer(ref):
    if (not isinstance(ref, dict) or set(ref) != {"pid", "uid", "start_ticks", "pgid"}
            or any(type(value) is not int for value in ref.values()) or ref["pid"] <= 1
            or ref["uid"] != os.geteuid() or ref["start_ticks"] < 0 or ref["pgid"] != ref["pid"]):
        raise ValueError("Invalid trusted worker identity")
    return Peer(ref["uid"], ref["pid"], ref["start_ticks"])


def quiesce(ref, *, term_timeout=2.0, kill_timeout=1.0):
    """Signal a pinned PID, never a PID reused since the recovery record."""
    if ref is None:
        return True
    peer = ref_peer(ref)

    def still_running():
        return process_identity(peer.pid, strict=True) == peer

    for sig, timeout in ((signal.SIGTERM, term_timeout), (signal.SIGKILL, kill_timeout)):
        if not still_running():
            return True
        try:
            if os.getpgid(peer.pid) != ref["pgid"] or os.getsid(peer.pid) != peer.pid:
                raise ValueError("Worker group ownership changed; refusing recovery writes")
        except ProcessLookupError:
            if not still_running():
                return True
            raise
        try:
            fd = os.pidfd_open(peer.pid)
        except ProcessLookupError:
            return True
        try:
            if not still_running():
                return True
            signal.pidfd_send_signal(fd, sig)
        except ProcessLookupError:
            return True
        finally:
            os.close(fd)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not still_running():
                return True
            time.sleep(0.02)
    return not still_running()


def decode_message(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("Non-finite JSON number")

    if len(payload) > MAX_MESSAGE or not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise ValueError("Expected one newline-framed JSON object, at most 16384 bytes")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("Malformed JSON") from exc
    if type(value) is not dict:
        raise ValueError("Request must be a JSON object")
    return value


def receive_message(connection):
    payload = bytearray()
    while len(payload) <= MAX_MESSAGE:
        chunk = connection.recv(min(4096, MAX_MESSAGE + 1 - len(payload)))
        if not chunk:
            break
        payload.extend(chunk)
        if b"\n" in chunk:
            break
    return decode_message(bytes(payload))


def send_message(connection, response):
    payload = json.dumps(response, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    if len(payload) > MAX_MESSAGE:
        raise ValueError("Response exceeds protocol limit")
    connection.sendall(payload)


def connect_socket(directory, name="control.sock", *, timeout=15):
    """Connect through a pinned directory, retaining ownership and peer checks."""
    if name not in SOCKET_MODES:
        raise ValueError("Unknown runtime socket")
    if isinstance(directory, Runtime):
        if directory.dir_fd is None:
            raise RuntimeError("Runtime is closed")
        directory_fd = os.dup(directory.dir_fd)
    else:
        directory_fd = _directory_fd(directory)
    try:
        info = os.fstat(directory_fd)
        target = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) != 0o755
                or not stat.S_ISSOCK(target.st_mode) or target.st_uid != info.st_uid
                or stat.S_IMODE(target.st_mode) != SOCKET_MODES[name]
                or (name == "supervisor.sock" and info.st_uid != os.geteuid())):
            raise ValueError("Untrusted runtime socket")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(timeout)
            connection.connect(f"/proc/self/fd/{directory_fd}/{name}")
            if socket_peer(connection).uid != info.st_uid:
                raise ValueError("Unexpected runtime socket peer")
            return connection
        except BaseException:
            connection.close()
            raise
    finally:
        os.close(directory_fd)


def supervisor_request(op, directory=RUNTIME):
    if op not in ("status", "suspend", "resume"):
        raise ValueError("Unknown supervisor operation")
    with connect_socket(directory, "supervisor.sock") as connection:
        send_message(connection, {"op": op})
        return receive_message(connection)


def auto_recover(runtime, hardware):
    """Caller holds the exclusive lock. A competing owner is never bypassed."""
    if runtime.lock_fd is None:
        raise RuntimeError("Auto recovery requires the exclusive owner lock")
    old = read_state(runtime)
    worker = old.get("worker") if old else None
    stop_error = "Worker cannot be stopped; NO recovery writes attempted"
    try:
        stopped = quiesce(worker)
    except (OSError, ValueError) as exc:
        stopped = False
        stop_error = f"Cannot confirm worker exit: {exc}; NO recovery writes attempted"
    if not stopped:
        result = {"ok": False, "error": stop_error}
    elif not hardware.recovery_enabled:
        result = {"ok": False, "error": hardware.reason}
    else:
        reset = reset_auto(hardware)
        result = {"ok": reset.ok, "error": "; ".join(reset.errors) or None}
    runtime.write_state({"schema": 1, "state": "auto" if result["ok"] else "fault",
                         "error": result["error"], "worker": worker if not result["ok"] else None,
                         "supervisor": None, "monotonic": time.monotonic()})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_mutually_exclusive_group(required=True)
    for op in ("auto", "suspend", "resume", "status"):
        operations.add_argument("--" + op, action="store_true")
    parser.add_argument("--enable-control", action="store_true")
    parser.add_argument("--validation-mode", action="store_true")
    parser.add_argument("--runtime-dir", default=str(RUNTIME))
    args = parser.parse_args(argv)
    runtime = None
    try:
        if args.auto:
            runtime = Runtime(args.runtime_dir, control=args.enable_control, create=False)
            runtime.acquire()
            result = auto_recover(runtime, Hardware(enable_control=args.enable_control,
                                                   validation_mode=args.validation_mode))
        else:
            op = "suspend" if args.suspend else "resume" if args.resume else "status"
            result = supervisor_request(op, args.runtime_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        result = {"ok": False, "error": str(exc)}
    finally:
        if runtime is not None:
            runtime.close()
    print(json.dumps(result, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
