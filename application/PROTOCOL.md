# Nitro Control local protocol, version 1

Transport: one JSON request and JSON response per connection, newline framed,
maximum 16384 bytes, Unix socket `/run/nitro-control/control.sock`. GUI is
unprivileged. Requests use `SO_PEERCRED`; no client-provided UID/PID or paths
are trusted. Socket directory is root-owned and not group-writable.

Requests:

```json
{"op":"status"}
{"op":"acquire"}
{"op":"heartbeat","token":"opaque session token"}
{"op":"set","token":"opaque session token","cpu":{"mode":"manual","percent":70},"gpu":{"mode":"auto"}}
{"op":"max","token":"opaque session token"}
{"op":"auto","token":"opaque session token"}
{"op":"release","token":"opaque session token"}
```

`acquire` uses polkit authorization in production and returns `token` only to
its caller. The lease is bound to the kernel-provided UID, PID and process
start identity, expires without heartbeats, and is not persisted. Status never
exposes the token. Active non-Auto state requires an active lease. A production
daemon without explicit hardware-write enablement never writes, even Auto.

Response: `{"ok":true,"status":{...}}`, optionally `token` on acquire.
Errors: `{"ok":false,"error":"explanation","status":{...}}`.

Status shape (numbers shown as null are unavailable, not sample telemetry):

```json
{
  "schema":1,
  "backend":{"connected":true,"control_available":false,"control_enabled":false,"hardware_verified":false,"reason":"Fan PWM interface unavailable"},
  "cpu":{"temperature_c":null,"rpm":null,"mode":null,"pwm":null,"percent":null,"requested_mode":null,"requested_percent":null,"confirmation":"unsupported","rpm_response":"unverified"},
  "gpu":{"temperature_c":null,"rpm":null,"mode":null,"pwm":null,"percent":null,"requested_mode":null,"requested_percent":null,"confirmation":"unsupported","rpm_response":"unverified"},
  "lease":{"active":false,"owned_by_client":false,"owner_pid":null,"remaining_seconds":0},
  "recovery":{"state":"read_only","error":null},
  "error":null,
  "sample_age_seconds":0,
  "events":[]
}
```

Modes: `auto`, `max`, `manual`. The supported Acer sysfs ABI is
`pwmN_enable=2/0/1` respectively; raw PWM 0..255 maps to firmware percentages.
CPU channel 1, GPU-side fan channel 2. Integer-percent quantization applies.
Manual software floor is conservatively 50%, not a calibrated hardware limit.

Confirmation: `unsupported`, `pending`, `firmware-confirmed`, `failed`.
`rpm_response`: `unverified`, `pending`, `observed`, `not-observed`.
A matching mode/setpoint acknowledges firmware state, not motor response or
full hardware compatibility. Mode acceptance must not become a false GUI
"fan control verified" claim. No invented maximum RPM or RPM-from-percent.

Independent supervisor owns the singleton lock, watches completed-worker-loop
heartbeats, quiesces the worker before Auto recovery, and reports recovery in
`/run/nitro-control/recovery.json`. On normal stop, fault, lease expiry and
suspend, both Auto requests/readbacks must be attempted; partial reset is a
fault. No manual replay after restart/resume. A wedged kernel/WMI operation
cannot be hidden or guaranteed recoverable by userspace.

Backend Python API shared with the GUI: `Telemetry().snapshot()` supplies the
same channel fields for read-only fallback without controlling hardware.
`python -m nitro_control.gui` is the graphical entry point. Package lives at
`application/nitro_control`; CLI launchers set that package path.
