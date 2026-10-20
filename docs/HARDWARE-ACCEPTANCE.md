# Fan backend acceptance gates

Gates 1–3 and the attended parts of gate 4 have been executed on the target
machine; results and raw evidence are summarized at the end of this document
and stored under `evidence/hardware/`. The gate definitions below are the
contract any future validation must follow.

## Interface contract

Use only the in-tree Acer hwmon interface after confirming its device path,
driver, and `name=acer`. Discover the hwmon number; never hard-code `hwmon4`.

| Channel | Firmware tachometer | Mode | Manual setpoint |
| --- | --- | --- | --- |
| CPU | `fan1_input` | `pwm1_enable` | `pwm1` |
| GPU-side fan | `fan2_input` | `pwm2_enable` | `pwm2` |

| `pwmN_enable` sysfs value | Acer driver meaning |
| --- | --- |
| `2` | Firmware Auto |
| `0` | Firmware full-speed fan mode, NOT fan stop |
| `1` | Manual/custom fan mode |

`pwmN` accepts 0-255, converted to firmware integer percentages. Readback is
quantized; it need not equal an arbitrary raw PWM request exactly. Requested
percentage, firmware-reported setpoint/mode, and measured RPM are different
values and must remain separate in logs and the eventual UI.
The mode numbers above are sysfs ABI values, not raw WMI encodings.

Thermal-profile `performance` means Turbo and is reported unsupported on this
model. Do not use it to implement Max Fan. Do not add EC access, NBFC profiles,
GPU power-limit changes, or unsupported capability masks.

## Gate 1: read-only test boot

1. Confirm `uname -r` is `7.2.9-arch1-1-nitro-test`, not the diagnostic
   module's stock-header release. Do not copy that diagnostic `.ko` into either
   installed kernel tree.
2. Verify effective `acer_wmi.predator_v4` is `N`, `force_caps` is `-1`, and
   `ec_raw_mode` is `N`. The quirk's `.predator_v4=1` field is intentional;
   the forced module parameter must not bypass DMI detection.
3. Confirm both RPM files, both PWM files, and both mode files exist and can
   be read. Record any firmware errors verbatim. Missing or rejected mode
   readback blocks further control tests.
4. Log timestamped CPU package temperature, NVIDIA GPU temperature, actual
   RPM, and reported modes/setpoints. Acer GPU temperature `0` is unavailable,
   not a substitute for the NVIDIA reading. NVIDIA fan `N/A` is not RPM.
5. Review active fan writers and profile managers. No competing fan-control
   daemon is permitted. Changes to power-profiles-daemon need separate
   approval if isolation is necessary; do not silently disable it.

The new quirk only exposes existing functionality. The driver still has no
ownership lease or automatic fan reset on process exit, unload, or suspend.

## Gate 2: recovery implementation review

Fan recovery must be a narrow, root-owned helper and supervisor, reviewed
before any Max or Manual test. Their required properties are:

- Fixed device validation and allowed operations, not arbitrary paths or
  shell commands received from the GUI.
- A single control owner and an expiring client lease.
- Firmware Auto before arming a manual session, and no saved manual request
  automatically reapplied after failure, daemon restart, or resume.
- A watchdog heartbeat from the actual control loop, not an unrelated thread.
- Recovery independent of the GUI and controller process, including a
  service stop/failure path such as `ExecStopPost`.
- Controller writes quiesced before recovery, so reset cannot race a restart.
- Auto attempted for both fans even if one operation fails; both mode
  readbacks required to report success. A partial reset is a visible fault.
- Auto before suspend and explicit user re-arming after resume.
- Missing/stale telemetry or unsafe temperature cancels manual ownership.
- Bounded operations, fault logging, and no assumption that killing a process
  can cancel a stuck kernel WMI operation.

Unit tests against a temporary simulated filesystem are useful for software
logic, but are not hardware tests and must never be presented as such.

## Gate 3: separately approved writes

Start at cool idle, connected to AC, without a stress workload. Agree on
conservative temperature limits and short test timeouts before proceeding.
Keep the recovery path immediately available.

1. Request Auto independently for both channels and query both modes.
   A profile change, a successful write alone, or a cached GUI state is not
   proof of Auto. If either mode cannot be verified as `2`, stop.
2. Request full-speed fan mode (`0`) and confirm reported mode plus physical
   RPM response on each fan. Do not promise a particular maximum RPM.
   Restore and recheck Auto before the next test.
3. Test a conservative high manual request on the CPU channel while keeping
   the GPU channel in verified Auto, then reverse the channels. Confirm
   independent mode, setpoint and tachometer behavior. No low-speed or
   zero-PWM sweep is authorized by this plan.
4. On a command/readback error or unexpected coupling, cancel the test,
   quiesce the controller, request Auto on both fans, and record the fault.

The test must account for firmware-percentage quantization. A matching
setpoint alone does not demonstrate that the motor obeyed the request.

## Gate 4: failure acceptance

With conservative high fan requests and continuous temperature observation,
separately authorize and test these cases:

| Fault | Required result |
| --- | --- |
| Control client exits or is killed | Lease expires; both fans report Auto |
| Controller exits normally | Independent reset verifies both modes as `2` |
| Controller is killed abruptly | Supervisor resets before any re-arming |
| Controller loop hangs | Watchdog triggers; reset verified; no manual replay |
| Telemetry becomes invalid | Manual ownership cancelled; explicit fault |
| Suspend/resume | Auto before suspend; stays Auto until user re-arms |
| First fan write succeeds, second fails | Recovery attempted for both; partial failure never labelled Auto |

A minimized window with a live tray/client is not necessarily client failure;
the UI must make active ownership and an explicit Exit/Return to Auto clear.

No software guarantee covers an unresponsive firmware interface or wedged
kernel. If an Auto write cannot be completed, stop the workload, stop further
control writes, and request user intervention. Unload, reboot and cold boot
are not proven fan-mode resets on this BIOS.

## Evidence required before declaring success

- Exact kernel, source patch hash, BIOS, model, controller and recovery versions.
- Timestamped real CPU/GPU temperatures, per-fan mode/setpoint and actual RPM.
- Independent CPU/GPU response, Max, Auto and all failure-case results.
- Explicit unsupported/stale indicators; no default or interpolated fake RPM.
- Verified Auto before removing a backend or booting back to stock.

Only after those gates pass should the graphical controller expose Manual as
verified. Until then, the correct result is "built, hardware not validated".

## Results on the target machine

All observations below are measurements, not calibration or causality proof.
Raw evidence: `evidence/hardware/` (steady-state runner result and per-stage
events, lease-death, controller-kill and suspend probes).

- Environment: Linux `7.2.9-arch1-1-nitro-test` (stock Arch `7.2.9.arch1-1`
  config, SHA-256 `37e9b95c…`), acer-wmi ANV15-51 quirk backport
  (`packaging/0001-acer-wmi-anv15-51.patch`), NVIDIA 615.78.08 modules, BIOS
  V1.60, controller/recovery from `nitro-control` 0.1.0.
- Gate 1: `predator_v4=N`, `force_caps=-1`, `ec_raw_mode=N`; both tachometers,
  PWM and mode files readable; no competing fan daemon.
- Steady-state sequence (`steady-state-sequence-passed`): verified Auto
  baseline; both Max — CPU 2207→2829 RPM, GPU 1953→2548 RPM; CPU Manual 80 %
  firmware-confirmed with GPU held in verified Auto; GPU Manual 80 %
  firmware-confirmed with CPU held in verified Auto; final both-Auto and
  lease release firmware-confirmed.
- Gate 4: lease-holder SIGKILL with an active CPU Manual setpoint ended in
  supervisor-restored both-Auto; abrupt controller kill left both fans Auto
  with a visible fail-closed fault; clean service stop ran post-stop
  recovery with both readbacks confirmed; real S3 suspend/resume kept both
  fans Auto before and after with no manual replay.
- Not injected against real firmware: controller-loop hang, telemetry
  invalidation, and first-write-success/second-write-failure. These are
  covered by the application unit tests; treat them as software-verified
  only.
