# Supported Hardware And Evidence

Fan control is verified on one exact configuration only (see
[HARDWARE-ACCEPTANCE.md](HARDWARE-ACCEPTANCE.md) and `evidence/hardware/`).
The userspace write gate deliberately refuses everything else.

## Exact Target

| DMI Field | Required Value |
| --- | --- |
| `sys_vendor` | `Acer` |
| `product_name` | `Nitro ANV15-51` |
| `board_name` | `Sportage_RTH` |
| `bios_version` | `V1.60` |

The userspace gate deliberately matches all four values. A different Nitro
model, board, BIOS revision, or merely similar fan layout is not supported for
writes. The kernel quirk's broader model match is not a userspace compatibility
promise. Read-only temperatures may work elsewhere without authorizing control.

The expected experimental kernel release is `7.2.9-arch1-1-nitro-test`, from
`linux-nitro-test` and `linux-nitro-test-headers` version `7.2.9.arch1-1`, installed
side by side with stock. Binary archives, NVIDIA compatibility, a bootable test
UKI, and a successful test boot are separate evidence gates, not consequences
of a successful object compile or source package.

## What Has Been Observed

| Feature | Current Evidence | Hardware Status |
| --- | --- | --- |
| CPU package temperature | Actual `coretemp` reading in the unprivileged session | Observed monitoring only |
| NVIDIA GPU temperature | Actual fixed `nvidia-smi` reading in that session | Observed monitoring only |
| CPU actual RPM | Reading path implemented; unavailable in the captured stock session | **UNVERIFIED** |
| GPU-side actual RPM | Reading path implemented; unavailable in the captured stock session | **UNVERIFIED** |
| Auto for both fans | Writes/readback and recovery logic tested with fakes | **UNVERIFIED** |
| Max for each fan | Sysfs mode `0` handling tested with fakes | **UNVERIFIED** |
| Independent CPU/GPU Manual | Mode/setpoint independence tested with fakes | **UNVERIFIED** |
| Motor response to requests | Actual-RPM observation logic only | **UNVERIFIED** |
| Client/backend failure recovery | Software fault/process tests only | **UNVERIFIED** |
| Service stop and independent post-stop Auto | Source/service integration requires real acceptance | **UNVERIFIED** |
| Suspend/resume and blocking unsafe sleep | Software ordering tests only | **UNVERIFIED** |

The reported software suites passed **74 backend tests and 49 GUI tests**.
They use in-memory/disposable filesystem fan fakes and simulated process or GUI
states. They do not drive the real fans, suspend the host, calibrate a minimum,
or prove that a WMI request can be interrupted. Counts describe the reported
software checkpoint, not a hardware certificate or a new run by these docs.

![Actual unprivileged read-only session: CPU/GPU temperatures present, both RPM readings unavailable, hardware unverified](screenshots/nitro-control-readonly.png)

This is an actual unprivileged Wayland screenshot, not a mockup or synthetic
RPM demonstration. Stock Acer telemetry did not supply usable fan RPM/PWM
control in that session. Unavailable RPM is not stopped fans, zero RPM, or
permission to invent a speed from the draft percentage.

## Required Interface

Discover exactly one `name=acer` hwmon directly below the genuine
`/sys/devices/platform/acer-wmi` device, bound to the platform `acer-wmi` driver.
Do not pin a `hwmonN` number. Both channels require actual tachometer, mode,
and PWM files:

| Channel | Actual RPM | Mode | Manual Setpoint |
| --- | --- | --- | --- |
| CPU, 1 | `fan1_input` | `pwm1_enable` | `pwm1` |
| GPU-side fan, 2 | `fan2_input` | `pwm2_enable` | `pwm2` |

Effective module parameters must be `predator_v4=N`, `force_caps=-1`, and
`ec_raw_mode=N`. Missing parameters/interfaces or firmware errors block
control. The patch's `.predator_v4=1` **quirk field** is intentional and is not
an instruction to force the module parameter. Keep `acer_wmi.predator_v4=0`
scoped to the one-time test boot, not a global module override.

Auto/Max/Manual use sysfs mode values `2/0/1`; raw `pwmN` is a separate
0..255 value quantized to firmware percent. Max is not fan stop. The app's
50% Manual floor is uncalibrated software policy, not proof that every value
in 50..100% is safe or obeyed. Firmware acknowledgement and actual motor RPM
remain separate facts.

No fan DKMS module, blacklist, guessed EC register, NBFC profile, forced
capability mask, GPU power-limit adjustment, or Turbo/performance thermal
profile is part of this design. The upstream report says this model rejects
Turbo; a profile switch is not a substitute for Max fan mode.

## Acceptance Status

The steady-state sequence and the attended failure/suspend probes have been
executed on the target machine; see [HARDWARE-ACCEPTANCE.md](HARDWARE-ACCEPTANCE.md)
and `evidence/hardware/` for results and their limits. Remaining gaps (loop
hang, telemetry invalidation, partial-write failure injection) are listed there.
6. Record exact source/package hashes, kernel, BIOS, timestamped measurements, each result, and final verified Auto. A responsible hardware reviewer must review the real evidence before creating an acceptance record.

[HARDWARE-ACCEPTANCE.md](HARDWARE-ACCEPTANCE.md) provides the detailed test
checklist. Its old approval/implementation-status paragraphs are historical;
the checklist is not a claim of completion. Code never writes
`/etc/nitro-control/hardware-verified.json` or turns a passed software test,
matching setpoint, or RPM change into automatic acceptance. A later hardware,
BIOS, or kernel change requires renewed review for that exact identity/release.

If either Auto acknowledgement cannot be obtained, stop the workload and keep
the backend/recovery path available. A reboot, module unload, or cold boot is
not a proven fan-mode reset. Follow [UNINSTALL.md](UNINSTALL.md) before removing
anything used for recovery.
