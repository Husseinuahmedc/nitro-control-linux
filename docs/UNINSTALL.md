# Uninstall And Rollback

**Restore and verify both fans in Auto before removing a used control backend
or leaving the test kernel.** Keep the installed recovery helper, approval
gates, runtime evidence, and test kernel available until that succeeds. An
unload, reboot, disconnected GUI, or service stop alone is not proof of Auto.

This is the scoped rollback procedure; the identity, backup, locking, and
non-running-kernel checks still apply. On the verified target machine the
deployment and acceptance records referenced below exist; adjust for your
own machine's actual state.

## Choose The Actual Scope

| Actual State | Required Cleanup |
| --- | --- |
| Source/read-only GUI only; no system installation or fan writes | Close the GUI. There is no system package, boot, or fan rollback to invent. Retain the workspace/evidence unless its removal is separately requested. |
| App installed but backend never armed; no fan writes | Remove only the app after confirming services are inactive and no control/sleep activation was left behind. Do not claim an Auto write on the stock read-only system. |
| Test kernel payload installed, never registered | Boot stock if necessary, then remove only the non-running test payload/targeted DKMS state. Skip entry removal. |
| Control used or state uncertain | Complete the Auto/quiescence procedure below while the test kernel and backend still work, before any backend/boot/package removal. |

Use installed package, service, DKMS, boot-entry, and project operation records,
not expected filenames, to determine which steps apply. Preserve another
operator's changed files. Do not stop, restart, clean, or delete the separate
running kernel-build pipeline as a side effect of this documentation runbook.

## Verify Auto First

Stop stress workloads. In the normal GUI, release ownership to Auto and inspect
fresh results for both fans. Then, in the already-authorized administrative
shell, ask the still-running installed supervisor to quiesce the writer and
confirm both Auto acknowledgements:

```sh
/usr/bin/nitro-control-recovery --suspend
/usr/bin/nitro-control-recovery --status
```

Here `--suspend` is a private supervisor request, **not** a host suspend command.
It succeeds only after the exact worker has exited and both Auto requests and
readbacks succeed. Record success, the quiescent state, and fresh
`pwm1_enable=2` and `pwm2_enable=2` from the validated/discovered Acer hwmon,
with readable PWM and timestamped evidence. Do not guess `hwmonN`, write sysfs
ad hoc, or treat a cached/stale mode, missing reading, or genuine zero RPM as
the required proof. Auto acknowledgement is not a promise of a particular RPM.

If the service is already down, the independent installed post-stop helper
must acquire the same owner lock and confirm the recorded worker's exit before
Auto. Its operation is `nitro-control-recovery --auto --enable-control`, adding
`--validation-mode` **only while operating the still-unverified trial**. Keep
the same approval marker and validation flags available as during activation.
Do not run this helper against a live owner or bypass a busy lock.

If a worker cannot be stopped, its identity is uncertain, or either Auto
acknowledgement fails, **stop rollback**. Keep the fault and recovery state,
stop the workload, and obtain operator recovery assistance. Do not delete
approval/runtime records, start a competing writer, unload the module, remove
the backend, or reboot on the assumption that firmware will reset. Userspace
cannot guarantee recovery from a wedged kernel/WMI call.

## Retire Both Units

Only after the preceding Auto check succeeds, disable and stop the sleep hook
and control service together, while the test kernel, approval marker, and
protected helper are still present:

```sh
/usr/bin/systemctl disable --now nitro-control-sleep.service nitro-control.service
```

The independent `ExecStopPost` recovery must still succeed, including the
validation flag in an unverified trial. Check final journal/recovery output,
both fresh Auto readbacks, inactive units, and absence of a surviving writer.
An error is a recovery stop, not permission to proceed. Do not leave the sleep
hook required by `sleep.target` after removing its backend; it intentionally
blocks sleep when safe quiescence/Auto cannot be confirmed.

Keep runtime records until that final recovery and worker-exit check is complete.
Do not clear a fault by deleting `/run/nitro-control` or the lock file. If the
GUI reported unconfirmed exit, resolve that condition instead of assuming its
window disappearing completed recovery.

## Boot Stock, Then Remove Test Artifacts

After verified Auto and clean unit retirement, manually reboot into the
unchanged working stock entry. This manual reboot is pending until actually
performed. Verify the actual running stock release and normal display/NVIDIA
operation. Require `uname -r` **not** to be `7.2.9-arch1-1-nitro-test` before
removing that release's modules, headers, or DKMS target. Do not downgrade or
remove a newer stock kernel just to match an old baseline.

Use [INSTALL-ROLLBACK.md](INSTALL-ROLLBACK.md#rollback-and-uninstall) for the
exact removal masks and commands. Re-audit live hooks and targets first; its
historical approval/status paragraphs do not establish current installed state.

1. If a test entry was registered, verify a usable current stock/recovery path, off-ESP backup, test-image hash, and live configuration. Quiesce boot/snapshot writers and hold the fail-closed external `/run/lock/boot-partition.lock` across the targeted entry-tool removal. Remove only `linux-nitro-test` and its `omarchy_linux-nitro-test.efi`; full removal must not leave a discoverable test UKI with `--keep-files`.
2. Verify stock UKI bytes, stock entry, boot default, and all unrelated entries are unchanged. Reconcile a partial registration/removal against live hashes and the operation log; do not restore an entire stale `limine.conf` or `.old` over user changes, deploy common EFI binaries, or modify firmware BootOrder/Secure Boot.
3. If DKMS inventory proves installation for this exact test release, remove only that `nvidia/<recorded-version> -k 7.2.9-arch1-1-nitro-test` target. The recorded baseline version is `615.78.08`; recheck actual state before using it. Never use `--all` or uninstall the global `nvidia-open-dkms` package.
4. Verify each prepared removal-hook mask, preview the transaction, and remove exactly `linux-nitro-test` and `linux-nitro-test-headers` using the kernel runbook. No recursive/cascade removal, stock `linux`/headers removal, unexpected dependency transaction, or global initramfs/UKI regeneration is acceptable.

If an artifact was never installed/registered, skip its removal. Inspect any
test-release residue and package ownership before deciding on targeted cleanup;
do not recursively delete a shared module, DKMS, source, ESP, or boot directory.
Workspace artifacts and backups can remain without affecting boot. Do not
purge unrelated snapshots, caches, dependency packages, or evidence.

## Remove The App And Local Activation

Only after control recovery/unit retirement and, when applicable, the verified
stock boot, remove the installed app with a reviewed non-recursive transaction:

```sh
/usr/bin/pacman -R nitro-control
```

Confirm exactly that package is removed. Do not remove Python, PySide6, Qt,
polkit, NVIDIA, or other shared dependencies as a shortcut. Package-owned
launchers and vendor units should be removed through the package manager, not
by hand. Review package-manager backup/config handling and retain evidence.

Inventory local activation files against the original creation log and hashes:

- The dedicated validation drop-in, if created for this project, and only its unchanged project-owned contents.
- `/etc/nitro-control/hardware-approved` and any reviewed `hardware-verified.json` supplied for this deployment, after preserving acceptance evidence.
- Any explicitly inventoried project bootstrap marker or manually created activation symlink that package removal does not own.
- Remaining runtime recovery state only after no writer/owner exists and final recovery evidence is retained.

Remove only those exact project-created files whose identity and expected hash
still match. A changed, pre-existing, symlinked, or unknown file requires review,
not deletion. Do not use a broad `/etc` directory removal or `systemctl revert`
that could destroy an operator's other drop-ins. Do not remove approval gates
early to make failed recovery appear read-only or successful.

Reload systemd after the targeted local-unit cleanup, then confirm both units
are inactive/disabled, no dangling required sleep dependency or GUI autostart
remains, and no backend writer survives. If persistent startup was never
enabled, do not invent additional settings to undo. Keep stock packages,
stock NVIDIA DKMS state, boot defaults, backup hashes, and final recovery logs
intact. This procedure never publishes, commits, pushes, or erases the project.
