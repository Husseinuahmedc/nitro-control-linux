# Installation and Rollback Runbook

Manual runbook for building and installing the test kernel alongside the stock
kernel. The repository also ships `scripts/create-deployment-plan.py` and the
installed `nitro-control-deploy` helper, which automate the audited version of
this procedure; this runbook documents the underlying steps and rollback.
Stop at every failed check or changed assumption.

## Recorded Initial Baseline

- Baseline and patched driver compile checks passed cleanly with `W=1`, modpost, module linking, and BTF. These are not a full kernel/package build or hardware acceptance.
- Separate packages are `linux-nitro-test` and `linux-nitro-test-headers`, version `7.2.9.arch1-1`; test release is `7.2.9-arch1-1-nitro-test`. Stock source configuration is unchanged.
- `scripts/build-kernel.sh` produces the source package in `packages/`; a source archive is not installable payload.
- Full-build dependencies: `bc`, `rust-bindgen`, `rust-src` on top of a standard kernel build environment. Recheck actual free space before building; the stock UKI is approximately 248 MiB.
- The build script's 80 GiB free-space guard is conservative, not a measured full-build requirement. Do not bypass it or disable stock debug/Rust configuration to force a build; resolve dependencies/space first.
- Existing `nvidia-open-dkms` version `615.78.08` and stock release `7.2.9-arch1-1` stay untouched. No fan DKMS driver or blacklist is needed or authorized.
- Current `acer_wmi.predator_v4` runtime value is `Y`; no persistent override was found, including in the stock UKI. The patch's old quirk field `.predator_v4 = 1` is distinct from the forced module-parameter path that bypasses DMI selection. Test only with effective `acer_wmi.predator_v4=0`, never a global modprobe setting.

## Recovery Gates And Separation Of Stages

- Treat each stage (prerequisites, full build, payload installation, targeted NVIDIA DKMS, UKI staging, ESP registration, test boot/read-only inspection, control experiments, rollback) as its own decision with its own checks. Never chain them blindly.
- Before future system writes, make and verify a dedicated off-ESP backup of the ESP, stock UKI, current `/boot/limine.conf` and any `.old`; retain checksums, package/DKMS inventory, full working LUKS/root command line, and a usable stock/recovery boot path. Retaining the stock image or home/root snapshots is not an ESP backup.
- Re-audit installed tools, ALPM hooks, initcpio configuration/drop-ins, boot configuration, firmware settings, and artifacts immediately before use. Secure Boot must still be disabled and configuration-driven enrollment disabled. If either changed, stop and reassess; do not disable/enroll anything to proceed.
- Preserve concurrent legitimate changes. Neither `.old` nor a saved configuration authorizes blindly replacing a newer live configuration. Record each command, target, exit status, and before/after evidence; freeze or re-hash audited inputs before root use.

## Future Variables

Use these in the ordinary-user build shell and redefine them explicitly in each **already-authorized root shell**. Do not acquire privileges as part of this runbook. Binary filenames below are expected candidates only: require actual build outputs and metadata to match, without guessing/globbing alternatives.

```bash
W=/absolute/path/to/nitro-control-linux
TESTKREL=7.2.9-arch1-1-nitro-test
STOCKKREL=7.2.9-arch1-1
STAGEDUKI="$W/build/boot/omarchy_linux-nitro-test.efi"
KERNEL_PKG="$W/packages/linux-nitro-test-7.2.9.arch1-1-x86_64.pkg.tar.zst"
HEADERS_PKG="$W/packages/linux-nitro-test-headers-7.2.9.arch1-1-x86_64.pkg.tar.zst"
```

`STOCKKREL` records this audit, not permission to downgrade a newer official kernel. A changed stock/toolchain/driver baseline requires revalidation.

## Full Build and Archive Audit

Run as the ordinary user only:

```bash
bash "$W/scripts/build-kernel.sh" --build
```

This invokes a full build, never automatic installation or dependency installation. Do not add `--install`, `--syncdeps`, or a privileged makepkg invocation. Keep complete build logs and audit both actual binary archives, not just the PKGBUILD/source package:

```bash
sha256sum "$KERNEL_PKG" "$HEADERS_PKG"
for p in "$KERNEL_PKG" "$HEADERS_PKG"; do
  /usr/bin/pacman -Qip "$p"
  /usr/bin/bsdtar -xOf "$p" .PKGINFO
  /usr/bin/bsdtar -tf "$p"
done
/usr/bin/bsdtar -xOf "$KERNEL_PKG" "usr/lib/modules/$TESTKREL/pkgbase"
```

- Require exactly the two expected internal package names, version `7.2.9.arch1-1`, architecture `x86_64`, and package base `linux-nitro-test`. Reject stock `linux`/`linux-headers` aliases, replacements/conflicts, unexpected dependencies, or extra transaction targets.
- Require kernel/modules only under `/usr/lib/modules/$TESTKREL`, pkgbase contents `linux-nitro-test`, matching kernel/module release and header release/configuration. Verify `/usr/src/linux-nitro-test` resolves to this release's headers/build tree, never a stock alias.
- Reject `.INSTALL` scriptlets, ALPM/initcpio hook payloads, `/boot` or other ESP payloads, and system configuration payloads such as `/etc`. The headers' stock build `.config` is expected, not a system configuration change.
- Audit all paths, symlink destinations, archive filenames, and existing file ownership/collisions. The test packages, release tree, and source alias must not already exist. Shared directories are not file collisions; never use overwrite/force flags. Retain hashes and a complete reviewed file inventory.

## Payload Installation Only

Create a temporary hook-mask directory with `/dev/null` symlinks for the four
hooks that would otherwise regenerate initramfs/DKMS state for the new release,
and verify each one before the root transaction:

```bash
mkdir -p "$W/plan/install-hooks"
for h in 10-linux-modules-pre.hook 10-linux-modules-post.hook 70-dkms-install.hook 90-mkinitcpio-install.hook; do
  ln -sfn /dev/null "$W/plan/install-hooks/$h" &&
    test "$(readlink -- "$W/plan/install-hooks/$h")" = /dev/null || exit 1
done
```

An empty hook directory does not disable vendor hooks. `--noscriptlet` disables package scriptlets, **not ALPM hooks**. Later hook directories override same-named hooks; re-audit `/usr/share/libalpm/hooks`, `/etc/pacman.d/hooks`, and the transaction's applicable hooks. Keep `60-depmod.hook` enabled, with targets restricted to the new release only. Stop for any other unexpected triggered action.

```bash
/usr/bin/pacman --hookdir /etc/pacman.d/hooks --hookdir "$W/plan/install-hooks" -U --needed --noscriptlet "$KERNEL_PKG" "$HEADERS_PKG"
```

At the confirmation prompt, allow exactly those two new packages, no additional targets, dependency installs, replacements, or upgrades; otherwise cancel. Do not use `--noconfirm`. Verify installed names/version, pkgbase, headers link, new-release depmod output, and unchanged stock package files, modules, boot/configuration bytes. This stage changes package state/new payload only, not the ESP or running kernel.

## Targeted NVIDIA Build

Record DKMS state first and require a unique, non-running test release. Use existing NVIDIA sources only, no driver package install/reinstall, `autoinstall`, or `--all`:

```bash
test "$TESTKREL" != "$(uname -r)" || exit 1
/usr/bin/dkms install nvidia/615.78.08 -k "$TESTKREL"
/usr/bin/dkms status nvidia/615.78.08 -k "$TESTKREL"
/usr/bin/modinfo -k "$TESTKREL" -F vermagic nvidia
```

Verify successful installation and matching vermagic for this release; record all targeted actions and unchanged stock modules/configuration. DKMS may update shared state and create/use signing keys: account for that; this is not a claim of no system writes. Do not load any module or change NVIDIA configuration.

## Stage UKI in Workspace

After the package/DKMS audit, refresh the command-line source from the current working stock boot: compare `/proc/cmdline`, the stock Limine entry, and inherited/default command line. Prepare `$W/plan/test-kernel.cmdline` as one complete line retaining **all** LUKS/root/decryption, rootflags, resume, and other required arguments, with exactly one `acer_wmi.predator_v4=0` and no conflicting value. Do not edit stock/default command lines or global module options.

Require existing workspace directories, an absent staged filename (including dangling symlinks), unchanged audited mkinitcpio settings, and the Secure Boot/enrollment prerequisites above. Active initcpio hooks were audited to copy files/modules only, without TPM/trust writes; revalidate this. Post-hook directories were empty, but suppress post hooks explicitly against future additions. Use the real `/usr/bin/mkinitcpio`, not the local wrapper, and **no `--config` flag**: Omarchy/NVIDIA drop-ins must load.

```bash
test -d "$W/build/boot" && test -d "$W/build/tmp" &&
  test ! -e "$STAGEDUKI" && test ! -L "$STAGEDUKI" || exit 1
/usr/bin/mkinitcpio --kernel "$TESTKREL" --kernelimage "/usr/lib/modules/$TESTKREL/vmlinuz" --cmdline "$W/plan/test-kernel.cmdline" --uki "$W/build/boot/omarchy_linux-nitro-test.efi" --builddir "$W/build/tmp" --nopost --no-ukify
```

This stages in the workspace, not on the ESP. Audit the resulting UKI's release, embedded command line, initramfs and required NVIDIA modules; retain its hash/build log and verify stock/ESP bytes unchanged. Do not run presets, global regeneration, `limine-update`, deploy, signing, or enrollment commands.

## Register Separate Test Entry

Before registration, recheck the verified ESP backup, mount/destination, actual free space versus staged size, and absence of both the test entry and `/boot/EFI/Linux/omarchy_linux-nitro-test.efi`. Quiesce concurrent boot/snapshot writers for registration and removal. The entry-tool wrapper fails open on mutex failure, so use an external, fail-closed `flock` on the same lock file. The inner `--no-mutex` below is valid ONLY inside that outer lock; never run that inner command unlocked.

```bash
/usr/bin/flock --exclusive --timeout 10 /run/lock/boot-partition.lock /usr/bin/limine-entry-tool --add-uki linux-nitro-test "$STAGEDUKI" --comment "Kernel version: $TESTKREL (nitro test)" --no-hooks --no-mutex
```

If the outer lock times out, no inner operation runs: stop rather than retrying without it. Expected boot-file changes are the computed test UKI destination and `/boot/limine.conf`, possibly its `.old`. Verify the copied image hash and configuration diff. Do not deploy common EFI binaries, modify firmware BootOrder, enroll keys, or change Secure Boot. Require stock entry `linux` still first, `default_entry: 2` unchanged, and sorting disabled; verify these after registration, not merely before it. Preserve any legitimate concurrent changes when repairing a partial failure.

## One-Time Boot and Read-Only Test

With Secure Boot disabled, this menu's external/default command line overrides the embedded UKI command line. At the menu, one-time edit **only the test entry**, retaining the complete working root/decryption arguments and appending `acer_wmi.predator_v4=0` (remove any conflicting occurrence in that temporary test line). Never alter the shared/default or stock entry. A staged embedded parameter alone does not prove the effective parameter.

After boot, verify `uname -r` equals `$TESTKREL`, inspect the entire `/proc/cmdline`, and require the module parameter readback to be `N` before considering any control writes:

```bash
test "$(uname -r)" = "$TESTKREL" || exit 1
test "$(< /sys/module/acer_wmi/parameters/predator_v4)" = N || exit 1
```

A missing parameter, runtime `Y`, wrong release, missing expected interface, or firmware-method/read failure aborts acceptance. Do not work around it with live unload/reload or global options. Boot itself can cause systemd to measure the new UKI or run TPM setup services; staging's limited scope is **not** a guarantee of unchanged TPM state after boot.

First inspect only RPM, temperatures, `pwm1`/`pwm2`, and both enable-mode readbacks, matching the patched source's firmware methods/interfaces. No fan writes happen in this stage. The [hardware acceptance runbook](HARDWARE-ACCEPTANCE.md) governs any later writes; neither this document nor build success establishes hardware safety.

## Later Control Safety Gate

- Any control trial must establish firmware Auto baseline before Max or Manual, testing channels independently at cool idle. The `pwmN_enable` sysfs values are Auto `2`, Max `0` (**not stop**), Manual `1`, not raw WMI mode encodings; manual `pwmN` setpoints range `0..255`. A thermal-profile `performance`/turbo setting is not fan Max.
- Do not promise kernel unload or reboot restores Auto. Restore while the test backend is still available using the audited, root-owned helper; verify **both** modes are `2`, and return a fault on restoration/readback failure.
- The supervisor/recovery implementation lives in `application/nitro_control/` and its hardware behavior is documented in [HARDWARE-ACCEPTANCE.md](HARDWARE-ACCEPTANCE.md). Do not provide or use ad hoc sysfs-write commands as a substitute.

## Rollback and Uninstall

**If control was used**, restore and verify both fans in Auto **before**
reboot or removal, while the test backend works; a failure blocks removal and
requires recovery, not an assumption about reboot. Boot the unchanged
stock/default entry and verify the running release is not `$TESTKREL`. Do not
remove the running kernel's modules.

If only payload was installed, skip entry removal entirely. If registration occurred, first recheck live configuration/backup and quiesce writers as above; under the same fail-closed outer lock remove only the test entry:

```bash
/usr/bin/flock --exclusive --timeout 10 /run/lock/boot-partition.lock /usr/bin/limine-entry-tool --remove-uki linux-nitro-test --no-hooks --no-mutex
```

This deletes the computed test UKI too. Do **not** use `--keep-files` for full rollback: it leaves a UKI on the ESP that can remain discoverable. Verify the stock/default entry and stock image remain intact. A partial registration/removal needs targeted reconciliation of live state, not blind restoration of an old whole configuration.

If DKMS status records this exact test target, remove it only after the non-running check; skip the remove command if this target was never installed:

```bash
test "$TESTKREL" != "$(uname -r)" || exit 1
/usr/bin/dkms status nvidia/615.78.08 -k "$TESTKREL"
/usr/bin/dkms remove nvidia/615.78.08 -k "$TESTKREL"
```

Re-audit removal hooks and prepare the five removal masks before removing exactly the two test packages:

```bash
mkdir -p "$W/plan/remove-hooks"
for h in 60-limine-mkinitcpio-remove-pre.hook 60-mkinitcpio-remove.hook 71-dkms-remove.hook 90-limine-mkinitcpio-remove-post.hook 90-mkinitcpio-install.hook; do
  ln -sfn /dev/null "$W/plan/remove-hooks/$h" &&
    test "$(readlink -- "$W/plan/remove-hooks/$h")" = /dev/null || exit 1
done
/usr/bin/pacman --hookdir /etc/pacman.d/hooks --hookdir "$W/plan/remove-hooks" -R --noscriptlet linux-nitro-test linux-nitro-test-headers
```

Inspect the confirmation: no recursive/cascade removal, additional packages, upgrades, NVIDIA package uninstall, or DKMS `--all`. Verify test packages/headers alias/entry/image and targeted DKMS record are gone; inspect residual test-release files rather than deleting shared state blindly. Verify stock files, stock DKMS installation and boot defaults remain unchanged. Retain logs/backups until recovery is proven.

Snapshot/history copies of future test images are not automatically purged by this rollback. Inventory them separately if needed; do not alter unrelated snapshots. Workspace artifacts can remain without affecting boot.

## Maintenance and Sources

`7.2.9` is not a safe long-term pin. Do not block official stock kernel/security updates to preserve this test. Revalidate the runbook after updates; when the quirk reaches an official kernel, upgrade, revalidate hardware/control safety manually, and retire the backport through targeted removal.

Local source pointers for re-audit: `packaging/PKGBUILD`, `scripts/build-kernel.sh`, `/usr/share/libalpm/scripts/limine-mkinitcpio-install` (target collection/build behavior), `/usr/bin/mkinitcpio`, `/usr/local/bin/mkinitcpio` (wrapper), `/usr/bin/limine-entry-tool:86-106` (fail-open mutex), and `/usr/lib/limine/limine-entry-tool` (entry/file operations). The installed versions, not these recorded assumptions, govern future execution.
