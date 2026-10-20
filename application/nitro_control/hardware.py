"""Narrow Acer kernel hwmon adapter; no alternative firmware access methods."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import platform
import stat
import time

CHANNELS = {"cpu": 1, "gpu": 2}
MODES = {"auto": 2, "max": 0, "manual": 1}
MAX_SAMPLE_AGE = 2.0
IDENTITY = {"sys_vendor": "Acer", "product_name": "Nitro ANV15-51",
            "board_name": "Sportage_RTH", "bios_version": "V1.60"}
APPROVAL = Path("/etc/nitro-control/hardware-approved")
VERIFICATION = Path("/etc/nitro-control/hardware-verified.json")


class HardwareError(RuntimeError):
    pass


def trusted_path(path, uid=0, directory=False):
    """Reject symlinks and writable ancestors, including the final component."""
    path = Path(path).absolute()
    try:
        for part in (*reversed(path.parents), path):
            info = part.lstat()
            if info.st_uid != uid or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
                return False
            if part != path or directory:
                if not stat.S_ISDIR(info.st_mode):
                    return False
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return False
        return True
    except OSError:
        return False


def trusted_json(path):
    if not trusted_path(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid or info.st_mode & 0o022 or info.st_size > 16384:
                return None
            return json.loads(stream.read(16385))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def percent_to_pwm(percent):
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError("percent must be an integer in 0..100")
    # The driver truncates raw*100/255 on write, and percent*255/100 on read.
    return (percent * 255 + 99) // 100


def pwm_to_percent(pwm):
    return (pwm * 100 + 254) // 255


def expected_pwm(percent):
    return percent * 255 // 100


def read_integer(path, low, high):
    try:
        value = Path(path).read_text(encoding="ascii").strip()
        if not value or len(value) > 16 or not value.lstrip("-").isdigit():
            return None
        number = int(value)
        return number if low <= number <= high else None
    except (OSError, ValueError, UnicodeError):
        return None


@dataclass
class AutoResult:
    errors: list[str] = field(default_factory=list)
    channels: dict = field(default_factory=dict)
    sampled_at: dict = field(default_factory=dict)

    @property
    def ok(self):
        return not self.errors


def reset_auto(hardware, clock=time.monotonic):
    """Always attempt each write AND each readback, even after a failed write."""
    result = AutoResult()
    for channel in CHANNELS:
        try:
            hardware.write_mode(channel, "auto")
        except Exception as exc:
            result.errors.append(f"{channel} Auto write: {exc}"[:240])
    for channel in CHANNELS:
        result.sampled_at[channel] = clock()
        try:
            data = hardware.read_channel(channel)
            result.channels[channel] = data
            if (data["mode"] != "auto" or type(data["pwm"]) is not int
                    or not 0 <= data["pwm"] <= 255):
                result.errors.append(f"{channel} Auto readback unavailable or mismatched")
        except Exception as exc:
            result.errors.append(f"{channel} Auto readback: {exc}"[:240])
    for channel in result.channels:
        if clock() - result.sampled_at[channel] > MAX_SAMPLE_AGE:
            result.errors.append(f"{channel} Auto readback stale")
    return result


class Hardware:
    def __init__(self, *, enable_control=False, validation_mode=False, sys_root="/sys"):
        if type(enable_control) is not bool or type(validation_mode) is not bool:
            raise ValueError("Control/validation flags must be booleans")
        self.sys_root = Path(sys_root)
        self.enable_requested = enable_control
        self.validation_mode = validation_mode
        self.hwmon = self._discover()
        self.identity = self._identity()
        self.hardware_verified = self._verified()
        self.control_available = False
        self.control_enabled = False
        self.reason = ""
        self._evaluate()

    def _identity(self):
        result = {}
        for name in IDENTITY:
            try:
                result[name] = (self.sys_root / "class/dmi/id" / name).read_text().strip()
            except (OSError, UnicodeError):
                result[name] = None
        return result

    def _discover(self):
        device = self.sys_root / "devices/platform/acer-wmi"
        driver = self.sys_root / "bus/platform/drivers/acer-wmi"
        try:
            if device.resolve(strict=True) != device or (device / "driver").resolve(strict=True) != driver:
                return None
            matches = []
            for node in (device / "hwmon").glob("hwmon*"):
                if node.resolve(strict=True).parent != device / "hwmon":
                    continue
                if (node / "name").read_text().strip() == "acer":
                    matches.append(node)
            return matches[0] if len(matches) == 1 else None
        except (OSError, UnicodeError):
            return None

    def _safe_parameters(self):
        try:
            base = self.sys_root / "module/acer_wmi/parameters"
            return all((base / name).read_text().strip() == value for name, value in
                       (("predator_v4", "N"), ("force_caps", "-1"), ("ec_raw_mode", "N")))
        except (OSError, UnicodeError):
            return False

    def _verified(self):
        # Only an externally reviewed, root-protected acceptance record counts.
        if self.sys_root != Path("/sys"):
            return False
        record = trusted_json(VERIFICATION)
        return bool(isinstance(record, dict) and type(record.get("schema")) is int and record["schema"] == 1
                    and record.get("accepted") is True
                    and record.get("identity") == IDENTITY == self.identity
                    and record.get("kernel_release") == platform.release()
                    and isinstance(record.get("evidence"), str) and record["evidence"].strip())

    def _evaluate(self):
        self.identity = self._identity()
        if self.enable_requested:
            self.hardware_verified = self._verified()
        self.control_available = False
        self.control_enabled = False
        self.recovery_enabled = False
        if self.identity != IDENTITY:
            self.reason = "Unsupported DMI/BIOS; expected Acer Nitro ANV15-51 Sportage_RTH V1.60"
        elif self.hwmon is None or self._discover() != self.hwmon:
            self.reason = "Acer hwmon under the acer-wmi platform driver unavailable"
        elif not self._safe_parameters():
            self.reason = "Unsafe or unavailable acer_wmi module parameters"
        else:
            self.control_available = all(self._interface_present(channel) for channel in CHANNELS)
            if not self.enable_requested:
                self.reason = "Read-only: --enable-control not supplied"
            elif os.geteuid() != 0:
                self.reason = "Read-only: hardware control requires root"
            elif self.sys_root != Path("/sys"):
                self.reason = "Read-only: non-production sysfs tree"
            elif not trusted_path(APPROVAL):
                self.reason = "Read-only: trusted hardware-approved marker missing"
            elif not self._trusted_code():
                self.reason = "Read-only: backend code/interpreter is not root-protected"
            elif not self.hardware_verified and not self.validation_mode:
                self.reason = "Read-only: acceptance record missing; validation requires --validation-mode"
            else:
                self.recovery_enabled = True
                self.control_enabled = self.control_available
                self.reason = ("Hardware acceptance record present" if self.hardware_verified else
                               "Validation mode: hardware NOT verified")
            if not self.control_available:
                self.reason = "Fan RPM/PWM/mode interfaces unavailable"

    @staticmethod
    def _trusted_code():
        import sys
        package = Path(__file__).resolve().parent
        return (trusted_path(package, directory=True)
                and trusted_path(Path(sys.executable).resolve())
                and all(trusted_path(path) for path in package.glob("*.py")))

    def _interface_present(self, channel):
        index = CHANNELS[channel]
        for filename in (f"fan{index}_input", f"pwm{index}", f"pwm{index}_enable"):
            path = self.hwmon / filename
            try:
                if path.is_symlink() or not path.is_file():
                    return False
            except OSError:
                return False
        return True

    def read_channel(self, channel):
        index = CHANNELS[channel]
        if self.hwmon is None:
            return {"mode": None, "pwm": None, "percent": None, "rpm": None}
        mode = read_integer(self.hwmon / f"pwm{index}_enable", 0, 2)
        pwm = read_integer(self.hwmon / f"pwm{index}", 0, 255)
        rpm = read_integer(self.hwmon / f"fan{index}_input", 0, 100000)
        return {"mode": {value: name for name, value in MODES.items()}.get(mode),
                "pwm": pwm, "percent": None if pwm is None else pwm_to_percent(pwm), "rpm": rpm}

    def _write(self, channel, suffix, value, *, before_write=None):
        # A broken first channel must not suppress an otherwise safe second Auto write.
        self._evaluate()
        allowed = self.recovery_enabled if suffix == "_enable" and value == 2 else self.control_enabled
        if not allowed:
            raise HardwareError(self.reason)
        index = CHANNELS[channel]
        path = self.hwmon / f"pwm{index}{suffix}"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                payload = f"{value}\n".encode("ascii")
                if before_write is not None:
                    before_write()
                if os.write(fd, payload) != len(payload):
                    raise HardwareError(f"{channel} {path.name}: incomplete write")
            finally:
                os.close(fd)
        except OSError as exc:
            raise HardwareError(f"{channel} {path.name}: {exc.strerror}") from exc

    def write_mode(self, channel, mode, *, before_write=None):
        if channel not in CHANNELS or type(mode) is not str or mode not in MODES:
            raise ValueError("invalid channel or mode")
        self._write(channel, "_enable", MODES[mode], before_write=before_write)

    def write_pwm(self, channel, pwm, *, before_write=None):
        if channel not in CHANNELS or type(pwm) is not int or not 0 <= pwm <= 255:
            raise ValueError("PWM must be an integer in 0..255")
        self._write(channel, "", pwm, before_write=before_write)
