# Application Installation And Activation

This is an operator runbook for `nitro-control` **0.1.0**. It documents how to
build, install, activate and remove the application safely; it is not a record
of what any particular machine has done.

Unrelated settings, disabling Secure Boot, partition changes, unrelated cache
deletion, global NVIDIA replacement, fan DKMS, blacklists, and guessed EC access
remain outside scope. Keep the stock kernel, UKI, entry, and boot default intact.

## Start Read-Only

From a checked-out repository, once the executable source
launcher is present:

```sh
./bin/nitro-control
./bin/nitro-control --read-only
```

Run as the normal desktop user, **not** with `sudo`, `pkexec`, or a root GUI.
The default launch does not elevate, authorize, or write on startup. The explicit
read-only flag also forbids taking a lease later. If the launcher has not yet
been integrated, the existing source entry point is:

```sh
PYTHONPATH="$PWD/application" /usr/bin/python -m nitro_control.gui --read-only
```

Python and the system's PySide6 dependency must already be available. A missing
dependency is not permission for an unreviewed package transaction. Monitoring
can work on the stock kernel with no backend installed: CPU/GPU temperatures
may be actual while RPM and control remain unavailable. See the actual
[read-only screenshot and evidence limits](SUPPORTED-HARDWARE.md).

## Keep The Existing Build

The kernel pipeline is already running separately. Inspect it without starting
another build:

```sh
/usr/bin/python scripts/kernel-pipeline.py --status
```

[KERNEL-ARTIFACTS.md](KERNEL-ARTIFACTS.md) defines the pinned incremental stages,
logs, reproduction environment, and recovery rules. Keep the original `.config`,
two jobs, 12 GiB filesystem/2 GiB host-memory guards, and one-hour-per-stage
limit. Do not edit pinned scripts, restart a failed stage, clean/re-extract,
launch another kernel `make`/makepkg build, use `--cleanbuild`, or invoke the legacy
full-build wrapper from the older installation document.

Require successful `validate-archives` and
`build/kernel-pipeline/<run-id>/archive-validation.json` for the **actual**
kernel/header archives. Recheck their recorded hashes immediately before root
use. A source archive, compile checkpoint, expected filename, or successful
`compatibility-uki-inputs` stage is not proof of installable packages or a
bootable UKI. That final stage only stages inputs.

## Audit The Application Archive

The application package/launchers are integrated separately by main. Do not
assume their archive exists. Set `APP_PKG` to the exact reviewed existing archive
path from that build, not a guessed filename or an automatically selected glob.
Inspect as an ordinary user:

```sh
: "${APP_PKG:?Set APP_PKG to the exact existing application archive}"
sha256sum "$APP_PKG"
/usr/bin/pacman -Qip "$APP_PKG"
/usr/bin/bsdtar -xOf "$APP_PKG" .PKGINFO
/usr/bin/bsdtar -tf "$APP_PKG"
```

Require internal package name `nitro-control`, version `0.1.0` with the reviewed
package release, expected dependencies, safe paths/links, no file collisions,
and retained license/notice files. Review any scriptlets, hooks, desktop entries,
and the entire proposed dependency transaction. The package must not autostart
the GUI or start/enable services. Expected installed application identities are:

| Purpose | Path Or Name |
| --- | --- |
| Python package | `/usr/lib/nitro-control/nitro_control` |
| Unprivileged GUI launcher | `/usr/bin/nitro-control` |
| Root supervisor launcher | `/usr/bin/nitro-control-supervisor` |
| Root recovery launcher | `/usr/bin/nitro-control-recovery` |
| Control service | `nitro-control.service` |
| Required sleep hook | `nitro-control-sleep.service` |
| Fixed polkit action | `org.nitrocontrol.control` |

Root code/helpers and every trusted path ancestor must be root-owned and not
group/world writable. Do not install a root helper as a symlink into this user
checkout, run it from a user virtualenv, or use a writable import path. Record
the inspected archive hash and installed file ownership/permissions.

Only after that audit, use the already-authorized administrative shell, with
the same exact `APP_PKG` path and a matching rechecked hash:

```sh
/usr/bin/pacman -U --needed "$APP_PKG"
```

Read the confirmation; stop on unexpected targets, upgrades, replacements, or
service/boot actions. Do not use overwrite/force or noninteractive confirmation.
Installation alone does not approve hardware or activate control. An ordinary
user can then launch `/usr/bin/nitro-control --read-only`.

## Separate Test Kernel

| Item | Expected Identity |
| --- | --- |
| Kernel and headers packages | `linux-nitro-test`, `linux-nitro-test-headers`, version `7.2.9.arch1-1` |
| Release and separate module tree | `7.2.9-arch1-1-nitro-test`, `/usr/lib/modules/7.2.9-arch1-1-nitro-test` |
| Headers source alias | `/usr/src/linux-nitro-test`, never a stock alias |
| Separate test UKI | `omarchy_linux-nitro-test.efi` |

Use [INSTALL-ROLLBACK.md](INSTALL-ROLLBACK.md#payload-installation-only) for the
exact kernel ALPM hook masks, payload-only transaction, targeted NVIDIA DKMS,
workspace UKI assembly/audit, and separate Limine registration procedure. Its
introductory paragraphs describe an earlier stage of this project and are kept
for provenance; every technical stop condition still applies.

Before installation/boot writes, recheck actual archives and collisions,
current hooks/snapshot writers, capacities, and a verified off-ESP backup of
the ESP, stock UKI, and live Limine configuration. A root/home snapshot alone
does not back up the ESP. Record current hashes and preserve concurrent user
changes. Hold the fail-closed boot-writer lock for the live registration/removal
operation, not just for an earlier check. Never rely on the entry tool's
fail-open internal mutex or blindly replace live configuration with `.old`.

Target only the test release for the existing NVIDIA DKMS version after checking
inventory; do not reinstall/remove the global NVIDIA package, use `--all`, or
alter stock modules. Keep Secure Boot/enrollment state unchanged; if the audited
prerequisites no longer hold, stop rather than disabling or enrolling anything.

The one-time test boot is a manual step: select only the separate test entry,
retain the full working
root/decryption command line, and apply the effective test-only
`acer_wmi.predator_v4=0` as described in the kernel runbook. Do not change the
stock/default entry. After boot, verify the exact release, DMI/BIOS, device
binding, parameters `N/-1/N`, both real tachometers/modes/PWM, and temperatures
read-only before starting the write-enabled backend. Any missing/error reading
is a stop, not a reason to force capabilities or unload/reload modules.

## Initial Hardware Validation

If no reviewed acceptance record exists yet, control writes must first go
through an attended validation trial:

1. Review the protected installed units and recovery path. The normal service starts the supervisor with `--enable-control` and runs independent post-stop `--auto --enable-control` recovery. It must preserve runtime state, use control-group termination, and not automatically replay/restart Manual.
2. Verify `nitro-control-sleep.service` has `RequiredBy=sleep.target` and `Before=sleep.target`, start-side `--suspend`, and stop-side `--resume`. A fault must fail the required pre-sleep job until worker exit and both Auto acknowledgements are confirmed. Do not ignore command failure or substitute a non-blocking hook.
3. Record existing files/hashes, then deliberately provision `/etc/nitro-control/hardware-approved` as an empty root-owned regular file with one link, no symlinks, no group/world writes, and protected ancestors. Do not overwrite a pre-existing marker or create an acceptance record to bypass validation.
4. Add a dedicated, inventoried validation drop-in for the control service, such as `/etc/systemd/system/nitro-control.service.d/10-validation.conf`, with **both** commands below. Do not change only startup and leave post-stop unable to recover unverified hardware.

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/nitro-control-supervisor --enable-control --validation-mode
ExecStopPost=
ExecStopPost=/usr/bin/nitro-control-recovery --auto --enable-control --validation-mode
```

Without a reviewed acceptance record, the empty marker permits only this
explicit validation mode. It never sets `hardware_verified=true` and bypasses
none of the other gates.

At cool idle, with the read-only gate passed and the recovery path ready, the
administrator can activate the reviewed units:

```sh
/usr/bin/systemctl daemon-reload
/usr/bin/systemctl enable nitro-control-sleep.service
/usr/bin/systemctl start nitro-control.service
/usr/bin/nitro-control-recovery --status
```

**Starting this backend attempts real Auto writes.** It is not a read-only
inspection step. Enable the sleep hook without `--now`; directly starting the
sleep unit would request quiescence rather than ordinary monitoring. During
initial validation, do not enable the control service for automatic startup;
once the acceptance record exists, `systemctl enable --now nitro-control.service`
is the normal production activation. Leave neither an armed backend without
its sleep hook nor a required sleep hook
behind after retiring the backend; follow [UNINSTALL.md](UNINSTALL.md).

Run the GUI as the desktop user. Taking control requires explicit polkit
authorization and a five-second UID/PID/start-bound lease with one-second UI
heartbeats. Editing or staging Auto/Max/Manual does not write until Apply.
Minimizing a live GUI is not lease release. Use Release to Auto/Exit and verify
the result; an unconfirmed-exit warning is not successful Auto recovery.

Follow the [acceptance status](SUPPORTED-HARDWARE.md#acceptance-status)
and detailed [hardware checklist](HARDWARE-ACCEPTANCE.md), recording actual
per-fan RPM separately from requested percentage and firmware acknowledgement.
On this project's target machine these results are verified and recorded in
`evidence/hardware/`; on any other machine everything must be revalidated
before trusting it.

## Acceptance Record

Only after real-test review may the responsible reviewer supply a protected
`/etc/nitro-control/hardware-verified.json` record with these fields:

| Field | Required Content |
| --- | --- |
| `schema` | Integer `1` |
| `accepted` | Boolean `true`, a real reviewer attestation, not a default |
| `identity` | Exact `sys_vendor`, `product_name`, `board_name`, `bios_version` values in [SUPPORTED-HARDWARE.md](SUPPORTED-HARDWARE.md#exact-target) |
| `kernel_release` | Exact running release that was tested |
| `evidence` | Nonempty reference to the completed real hardware results |

The record and marker must be root-owned regular files with protected paths;
no symlink or group-writable approval/helper is trusted. Code never generates
this acceptance record. A software test pass, firmware readback, or RPM change
alone cannot authorize it.

Restore/verify Auto and stop normally with validation recovery still available.
Then remove only the unchanged project validation drop-in, reload units, and
test normal startup against the real record. Enable persistent control-service
startup only if deliberately desired, keeping the required sleep hook paired.
No package install enables either unit or desktop autostart for the operator.
Renew acceptance after kernel/BIOS/hardware changes; do not pin an obsolete
kernel by blocking official security updates.
