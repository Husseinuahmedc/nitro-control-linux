# Backend Development And Operation

This is userspace source and simulated-test evidence, not hardware acceptance.
No marker, acceptance record, polkit policy, service, module, boot setting, or
NVIDIA setting is installed or changed by this backend's development tests.
`PROTOCOL.md` remains the shared API contract.

## Entry Points

Run from `application`, or arrange the installed package path externally:

```sh
python -m unittest discover -s tests -p 'test_backend*.py' -v
python -m nitro_control.supervisor --runtime-dir "$PWD/.backend-runtime"
python -m nitro_control.recovery --status --runtime-dir "$PWD/.backend-runtime"
```

The supervisor runs in the foreground. The second command is an unprivileged,
read-only workspace launch. It creates only that runtime directory and never
requests even Auto from hardware. Stop with SIGTERM/Ctrl-C. Do not use a
workspace runtime with `--enable-control`; that combination is rejected.
Root launches, including read-only launches, require root-owned, non-writable
ancestors all the way to the runtime. A root launch into a user's workspace is
therefore rejected. Unprivileged read-only workspace launches remain supported.

The GUI fallback API is:

```python
from nitro_control.telemetry import Telemetry

telemetry = Telemetry()
try:
    status = telemetry.snapshot()
finally:
    telemetry.close()
```

It returns the complete version-1 status, with `backend.connected=False`,
`control_enabled=False`, and no lease. It reads CPU **package** temperatures
from `coretemp`, GPU temperatures from a persistent read-only NVML helper,
and actual Acer tachometers. The helper uses the installed system
`/usr/lib/libnvidia-ml.so.1` through stdlib `ctypes`, binding only initialization,
device count/handle lookup and temperature sensor zero. It does not bind fan,
power or configuration APIs. GPU failure/zero/unavailable never falls back to
the CPU or the Acer GPU temperature. Missing/EIO/stale
readings are `None`, not invented zeros. A genuine tachometer zero remains zero.
Internally, temperature and fan samples have separate monotonic timestamps;
values older than two seconds are withheld. `sample_age_seconds` measures time
since the sampling loop began, not wall-clock file modification time.

Reuse one `Telemetry` in the sampler context. The first refresh starts at most
one helper and immediately publishes CPU/fan data with GPU `None` during native
initialization. The parent reads bounded newline JSON from a nonblocking pipe;
GPU work never blocks the controller's CPU/fan loop. After initialization the
same process queries at 1 Hz. Each GPU timestamp is taken immediately before
`nvmlDeviceGetTemperature`, not before initialization or on receipt/caching.
A native read taking over two seconds is discarded; a later fast read can
become available without making the old value appear fresh. Invalid frames,
missing NVML, helper exit and stale values produce `None`. No automatic helper
respawn occurs in the same `Telemetry`, even if initialization blocks or fails.

`Telemetry.close()` is read-only, idempotent and nonblocking. It prevents future
launches, closes the pipe, requests termination and delegates bounded-grace
kill/reaping to a daemon cleanup thread. A native D-state operation can remain
until the kernel permits exit, but cannot hold the caller or write fans. The
helper inherits the worker's process group; Linux parent-death SIGKILL is armed
before loading NVML, with parent PID/start identity checked. Broken pipes stop
the helper too. Controller shutdown closes telemetry. GUI shutdown should close
its persistent sampler-owned telemetry and must not recreate it per snapshot.

The helper uses system Python with safe import flags and a sanitized environment;
root launches additionally require protected installed code/interpreter. There
are no production fake-provider CLI switches. Explicit `runner=` injection keeps
the old simulated subprocess/FakeClock test interface only, not the production
provider. `gpu_factory=` is likewise an in-process offline test dependency.

`Controller`, `Hardware`, `Supervisor`, and the telemetry/authorization/worker
factories accept in-process test dependencies. None of these injections is
exposed as a production fake-hardware, arbitrary-path, or command-execution CLI.
Only the test modules contain filesystem fan simulators.
`Hardware.write_mode` and `write_pwm` accept an internal `before_write` callback.
The adapter invokes it after device validation/open, immediately before the
actual write; injected hardware implementations must honor that boundary too.

## Production Gates

These describe future separately authorized operation, not commands executed
during development:

- Supervisor: `python -m nitro_control.supervisor --enable-control`.
- Unverified validation additionally requires `--validation-mode`. It does not
  set `hardware_verified` and does not bypass any other gate.
- Stop recovery: `python -m nitro_control.recovery --auto --enable-control`,
  with the same `--validation-mode` flag if validation is still unverified.
- Sleep hooks: `python -m nitro_control.recovery --suspend` and
  `python -m nitro_control.recovery --resume`.

Control requires effective UID zero, the fixed `/run/nitro-control` runtime,
root-protected installed backend code/interpreter, and the separately supplied
`/etc/nitro-control/hardware-approved` regular file. The marker and every path
ancestor must be root-owned, non-symlink, and not group/world writable. An empty
marker can authorize **validation only**, with the explicit validation flag.
This code never creates either approval file.

The model must match DMI vendor `Acer`, product `Nitro ANV15-51`, board
`Sportage_RTH`, BIOS `V1.60`. Discovery accepts exactly one `name=acer` hwmon
below the actual `/sys/devices/platform/acer-wmi` device, bound to the real
platform `acer-wmi` driver. Forced `predator_v4`, non-default `force_caps`,
raw-EC mode, unavailable module parameters, or missing interfaces block normal
control. No hard-coded hwmon number, raw EC, NBFC, NV-CONTROL, thermal-profile
Turbo, or alternative firmware method is used.

Full `hardware_verified=True` additionally requires an externally reviewed
root-protected `/etc/nitro-control/hardware-verified.json` acceptance record:

| Field | Required Meaning |
| --- | --- |
| `schema` | Integer 1 |
| `accepted` | Boolean true, attested by the responsible hardware reviewer |
| `identity` | Exact four DMI keys/values listed above |
| `kernel_release` | Exact running kernel release |
| `evidence` | Nonempty reference to actual completed hardware acceptance evidence |

File existence, simulated readbacks, matching firmware modes, and observed RPM
changes never produce that record. Review acceptance again after a kernel or
hardware/BIOS change. The marker is an operator approval gate, not evidence that
fan motors obey a request. No acceptance record has been supplied here.

## IPC And Policy

The public socket is `control.sock`, mode 0666 in the owned 0755 runtime.
Status is open and does not authorize or write. All mutation requests require
the single lease acquired through fixed polkit action
`org.nitrocontrol.control`. Missing policy/agent/pkcheck, timeout, or denial
fails closed. Authorization has a 15-second timeout and runs outside the
controller lock, with two concurrent authorization slots and sixteen total
client slots. The control queue is bounded at 32 requests. JSON frames are
limited to 16384 bytes including newline, with duplicate keys, non-finite
numbers, unexpected keys, and booleans masquerading as integers rejected.

Tokens contain 256 random bits. A lease is bound to SO_PEERCRED UID/PID plus
`/proc` start ticks, works across socket-per-call requests from that same GUI
process, and expires after five seconds without a heartbeat. Other clients'
status never claims ownership; no status, event or recovery record includes a
token. Set/Max/Auto do not renew the heartbeat deadline. Keep GUI heartbeats at
one second. Status and authenticated lease heartbeats use only cached telemetry
and short-held metadata locks, bypassing the hardware command queue. A heartbeat
can renew only its still-live, unexpired lease; it cannot resurrect an expired
lease, perform recovery writes, or feed the parent watchdog.

Fan sampling, CPU reads, fan writes/readbacks and Auto recovery remain serialized
by the controller I/O lock in the actual control loop. Only the read-only GPU
temperature query runs in its isolated helper. Telemetry refresh runs
outside the metadata RLock and atomically publishes complete samples; slow
queries do not stamp cached values with a new timestamp. In-progress commands
report `pending` rather than reusing cached firmware confirmation. Status and
heartbeat remain responsive during a slow/hung readback, while the independent
five-second completed-loop watchdog still expires.

Recommended client deadlines: 1.5 seconds for status/heartbeat, 6.5 seconds for
mutations, and 22 seconds for acquisition (15-second authorization plus up to
six seconds of queue/operation wait and margin). These are transport deadlines,
not extensions of the five-second lease/watchdog. Continue independent
one-second GUI heartbeats during a long request; a single GUI request thread
blocked in a command must not prevent its process from sending heartbeats.
The server reports a six-second queued/running command timeout as an unconfirmed
error, never as success; a cancelled queued command is not executed later.

Manual accepts integer 50..100 percent, a **software safety policy**, not a
calibrated minimum. The write uses `ceil(percent*255/100)` so the driver's
integer conversion selects that percentage; manual confirmation expects
`floor(percent*255/100)` on readback. Modes use sysfs Auto=2, Max=0, Manual=1.
PWM is written before selecting Manual. CPU/GPU updates remain independent;
omitted channels are not rewritten. Max is mode 0, not PWM zero or a profile
change. Auto/Max have no invented PWM target: their mode must match and PWM
must be readable in 0..255; only Manual has a specified setpoint to compare.
Both channels are re-read after all command writes, including an omitted
channel, so unintended firmware coupling cannot pass on cached confirmation.
Owner UID/PID/start identity and the current monotonic deadline are checked
after blocking operations, before every new non-Auto PWM/mode write, again at
the adapter's final write boundary, and before a successful lease-dependent
reply. Authority lost during a command stops the next non-Auto write, revokes
the lease, attempts both Auto writes/readbacks, and returns an error/fault.
An already-issued WMI request cannot be cancelled by these userspace checks;
that request may finish after authority is lost, but no next non-Auto request
may be started. Successful release intentionally returns an inactive lease.

Every successful command requires fresh firmware mode/setpoint readback.
`firmware-confirmed` means only that acknowledgement, never motor calibration
or complete compatibility. Each non-Auto channel records its pre-command
actual RPM and monotonic timestamp in a bounded event log. `rpm_response`
starts `pending`, becomes `observed` after a later actual change of at least
100 RPM, or `not-observed` after three seconds without such a change. This
noise threshold is not a measured motor limit or RPM target and does not prove
causality. The two channels' observations remain independent.

While Manual is active, missing/stale CPU/GPU temperatures or tachometers,
CPU >=85 C, or GPU >=80 C cancel the lease and restore both fans to Auto.
Thermal cancellation requires explicit re-arming. A partial write, mismatching
readback, or externally changed firmware mode is a controller fault. Recovery
attempts both Auto writes before attempting both readbacks, even after an
individual error. A missing channel must not suppress an otherwise authorized
Auto recovery on the other channel. Partial reset remains a fault.

## Supervisor And Recovery

The independent supervisor alone owns the exclusive `owner.lock` flock. The
worker has a dedicated process group/session and is the sole normal writer.
Its subprocess has a fixed package path, sanitized environment, safe Python
import path, and no user-site imports; it does not inherit arbitrary code flags.
It cannot accept clients or write until its exact PID/start/group identity is
durably recorded by the supervisor, closing the crash-during-spawn gap.

Only the completed controller loop writes watchdog pipe timestamps. No IPC,
authorization, or timer thread can feed the five-second watchdog. Worker
failure/hang causes bounded SIGTERM, then SIGKILL through a pinned pidfd.
Recovery checks start identity and group/session ownership, confirms the
writer has exited, and only then permits supervisor Auto writes. Unknown
process identity or an unkillable worker is a visible fault: **no recovery
writes and no replacement worker**. The worker also releases to Auto if its
supervisor disappears. No manual request or lease is persisted or replayed.

`recovery.json` is atomically replaced, fsynced, mode 0644, safe-owned, and
token-free so the disconnected GUI can show faults. The private
`supervisor.sock` is mode 0600 and accepts only fixed `status`, `suspend`, and
`resume` operations from the supervisor's UID (root in production).
`--suspend` returns success only after confirmed worker exit and both Auto
acknowledgements. Read-only suspend quiesces the worker but returns failure
because it cannot claim a hardware Auto write. Resume resets Auto and starts a
fresh worker, including if requested while already running; the GUI must
acquire a new lease. A fault stays visible until an explicit private resume or
service restart; there is no automatic manual replay or restart loop.
An invalid prior recovery record is preserved and blocks writes/startup rather
than being replaced with a claim that no worker exists. Gate failures also
preserve a known, not-yet-stopped worker identity. Revoking approval cannot turn
failed control recovery into a successful resume.

Runtime traversal uses no-follow directory FDs, validating ancestry before any
creation or permission change. Lock files, state replacement, socket binding,
socket permission changes and cleanup stay relative to the pinned directory,
even if a pathname is renamed/replaced. New-directory permissions use `fchmod`;
socket permission updates never follow symlinks and verify the bound inode.
`Runtime.close()` releases both the owner lock and the directory FD; the closed
object must not be reused. Internal state readers accept that pinned `Runtime`
as well as the existing path form. The public JSON schema/operations and the
`Telemetry().snapshot()` API are unchanged.

The post-stop `--auto` helper must acquire the same lock and quiesce the exact
recorded worker before either write. A competing owner causes failure without
constructing a hardware writer. It has no force/profile override. Service
packaging must retain recovery state across stop, use `KillMode=control-group`,
and arrange the separately owned sleep hook's blocking suspend/resume order.
No services are enabled or installed by this work.

Userspace cannot guarantee cancellation of a wedged kernel/WMI operation,
including a supervisor's own Auto operation. If it cannot establish safe exit
or both Auto acknowledgements, stop the workload and obtain operator help;
do not start another writer or treat unload/reboot as a proven reset.

## Verification Scope

The backend tests use memory fakes and disposable directories **inside this
application workspace**. They cover independent modes, all allowed manual
percentages, input validation, ownership/expiry/death, stale/EIO/missing data,
partial writes/readbacks, polkit and peer credentials, framing, exclusive
locks, PID reuse, unknown/uninterruptible identities, completed-loop hangs,
worker/supervisor crashes, the spawn barrier, and suspend/resume ordering.
Regressions also substitute runtime paths/symlinks, expire/kill owners between
WMI steps, keep IPC heartbeats live while the control loop hangs, and measure
cached/queued socket latency under a one-second simulated GPU refresh.
GPU tests cover slow initialization/read discard, missing/invalid native results,
bounded nonblocking framing, one-helper lifetime, protected launch, cancellation,
and original per-key age. Real temperature-read smoke observations, when reported,
are not fan acceptance or evidence that control/motor response was validated.
Process tests SIGSTOP/SIGKILL only their own simulated workers and never
suspend the host. Passing them is not hardware acceptance.
