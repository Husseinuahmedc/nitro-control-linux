# Hardware evidence

Raw observations from the real-hardware acceptance of Nitro Control on an
Acer Nitro ANV15-51 (board `Sportage_RTH`, BIOS V1.60, i7-13620H, RTX 3050
6GB Laptop GPU). Recorded 2026-10-19/20 under Linux
`7.2.9-arch1-1-nitro-test` with NVIDIA 615.78.08 modules. The contract for
these tests is [docs/HARDWARE-ACCEPTANCE.md](../../docs/HARDWARE-ACCEPTANCE.md).

These files are measurements, not calibration or causality proof. They were
produced by the attended, explicitly approved test procedures described in
that document; no acceptance record was created by the tools themselves.

## Contents

| Directory | File | What it is |
| --- | --- | --- |
| `steady-state/` | `result.json` | Final report of the conservative steady-state sequence (`steady-state-sequence-passed`): verified Auto baseline, both-fan Max, independent CPU/GPU Manual 80 % tests, final both-Auto and lease release. |
| `steady-state/` | `events.jsonl` | Timestamped RPC-level log of the same run: every `status`/`acquire`/`auto`/`max`/`set`/`release`/heartbeat round-trip with elapsed times and full observed telemetry. |
| `gate4-lease-death/` | `events.jsonl` | Probe that acquires a lease, sets CPU Manual 80 % (real `pwm1_enable=1` write, firmware-confirmed) and then SIGKILLs itself; the trailing record shows the supervisor restored both-Auto (`pwm1_enable=2`, `pwm2_enable=2`) within the watchdog window. |
| `gate4-controller-kill/` | `events.jsonl` | Abrupt SIGKILL of the controller worker while a lease could be outstanding: both fans stayed/left Auto, supervisor published a visible fail-closed fault. |
| `gate4-suspend/` | `events.jsonl` | Real S3 suspend/resume cycle with a Manual setpoint active before the `--suspend` hook: both fans Auto before suspend, Auto after resume, no manual replay. |

## Selected measurements

| Phase | Requested | Baseline RPM | Observed RPM | Response attributed to |
| --- | --- | --- | --- | --- |
| both_max | Max both | CPU 2207 / GPU 1953 | CPU 2829 / GPU 2548 | CPU and GPU |
| cpu_manual_80 | CPU Manual 80 %, GPU Auto | CPU 3776 / GPU 3511 | CPU 4739 / GPU 4427 | CPU |
| gpu_manual_80 | GPU Manual 80 %, CPU Auto | CPU 5350 / GPU 5002 | CPU 6006 / GPU 5811 | GPU |

Both fans were still settling from the previous phase during the Manual
stages, so the non-requested channel also drifted; the runner attributes
response only to the requested channel, matching the firmware-confirmed
setpoint change.

Steady-state round-trips stayed in the milliseconds; `acquire` (polkit
authorization) took about 6 s on first use.

## Known gaps

Controller-loop hang, telemetry invalidation, and
first-write-success/second-write-failure were not injected against real
firmware; they are covered by the application unit tests only. Zero RPM at a
~45 °C cool idle is normal firmware-Auto behavior on this model.
