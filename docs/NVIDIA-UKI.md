# NVIDIA Compatibility and UKI Pipeline

This ordinary-user, single-run helper waits for the kernel pipeline to
complete **all** declared stages, including a positive `validate-archives`
result. It never starts, resumes, duplicates, or edits that pipeline. A
kernel stop/failure, process identity change, changed pinned input, resource
event, timeout, or tool failure blocks this run without retries or weakened
limits. Waiting is not a compilation attempt and has no build timeout; each
actual stage has its own unchanged 3600-second guard budget.

## Launch and Monitor

```bash
/usr/bin/python -B scripts/prepare-nvidia-uki.py --self-test
/usr/bin/python -B scripts/prepare-nvidia-uki.py --start
/usr/bin/python -B scripts/prepare-nvidia-uki.py --status
```

`--start` detaches and immediately returns the PID, state, log, work directory,
and fixed stages. Only start once. Existing state or work directories are never
reused. The operator monitors `plan/nvidia-uki-state.json` after application integration.
A stopped run is evidence, not permission to delete state and retry. To cancel,
send TERM to the recorded helper PID; its resource guard terminates its own child
process group. No service, cron job, root shell, package manager, network fetch,
or additional dependency is involved.

### Blocked State Recovery

If the helper blocked before any NVIDIA stage because the upstream validator
expecting `build/tools/bpf/bpftool/vmlinux.h` was corrected to the official
flattened `build/vmlinux.h`, resume that one recorded dependency failure only:

```bash
/usr/bin/python -B scripts/prepare-nvidia-uki.py --resume-after-kernel-fix
```

This refuses any previous NVIDIA stage/guard/output, resource stop, changed limit,
different blocker, or second repair resume. It verifies the old-to-new kernel
checker hash against the durable repair record and snapshotted original state;
unchanged completed compile/package stages, stock configuration, recipe and source
pins; actual `f85aeaf...` **vmlinux.o checkpoint** bytes/stat; and matching archive
hashes in the repair, completed state, positive validation and final input manifest.
The replacement kernel validator PID is accepted only through this lineage.
The upstream final stage is explicitly **inputs-only**, not a bootability claim.

Before detaching, the blocked NVIDIA state/log are preserved under
`build/nvidia-uki/history/kernel-fix-*/` with hashes and a `resume.json` lineage
record. The original log remains untouched; the resumed run uses a new log prefix.
The existing host/configuration pins are not refreshed. Private header extraction
strictly verifies `build/vmlinux.h` against both archive validation and the completed
kernel source's generated header. No compilation budget is reset because no NVIDIA
stage had ever started. All subsequent stages retain the original guard limits.

All generated build/staging data is under `build/nvidia-uki/`; new logs have the
`logs/nvidia-uki-` prefix. The only other runtime write is the helper's dedicated
`plan/nvidia-uki-state.json`. The two manually owned files are this document and
`scripts/prepare-nvidia-uki.py`. Kernel pins and the existing command-line input
are read-only.

## Fixed Stages

1. Wait for the same kernel pipeline to complete and exit, verify its durable
   archive-validation result, then acquire its existing lock without writing it.
   Keep that lock throughout NVIDIA/UKI work, including in the guard. Reject a
   concurrent kernel compiler. No NVIDIA/kernel compilation overlaps.
2. Re-hash the validated kernel and usable complete headers archives, safely
   extract them into a private module root, and copy only the installed source
   root `/usr/src/nvidia-615.78.08` into a private source tree. Preserve upstream
   `COPYING`, record source hashes, and verify header configuration, release,
   `Module.symvers`, build tools, and `vmlinux`.
3. Build native DKMS for exactly `7.2.9-arch1-1-nitro-test`, non-root, with explicit
   `--dkmstree`, `--sourcetree`, `--installtree`, and `--kernelsourcedir`. Override
   the installed recipe's `nproc` parallelism only in the private framework
   directory: use `make -j2 SYSSRC=... SYSOUT=... KERNEL_UNAME=... modules`, with no
   compatibility patches, PREEMPT overrides, forced compiler flags, excluded
   modules, or weakened modpost checks.
4. Audit all five NVIDIA modules, exact full vermagic against packaged acer-wmi,
   the core driver version, successful normal modpost, and test-kernel signature.
   Put uncompressed `.ko` files into the new release's private `updates/dkms`.
   Run `depmod -b PRIVATE_ROOT -e -E PRIVATE_HEADERS/Module.symvers KREL` and reject
   unresolved/export-version warnings. This is compilation compatibility only.
5. Produce a licensed, binaries-only native DKMS tarball containing **only** the
   new KREL and x86_64. Verify its member layout and perform an actual non-root
   `ldtarball` into another empty private DKMS tree, then verify built status and
   every imported module hash. No global DKMS registration or installation occurs.
6. Run the real `/usr/bin/mkinitcpio` inside fakeroot **below** the ordinary-user
   resource guard. Use `--moduleroot`, `--kernel`, `--kernelimage`, `--cmdline`,
   `--uki`, `--generate`, `--builddir`, `--nopost`, and `--no-ukify`. There is no
   `--config`: original Omarchy/NVIDIA drop-ins remain active. Outputs are real
   workspace files, not an ESP publication or a dry run.
7. Audit the resulting PE sections, exact `.linux` hash and embedded version,
   `.uname`, complete `.cmdline`, `.initrd` hash and initramfs NVIDIA modules and
   cryptroot tooling. Emit an artifact manifest and a workspace-only Limine entry
   fragment. Verify recorded stock UKI, boot configuration, and stock NVIDIA
   module hashes remain unchanged.

Every post-wait stage is protected by the existing `resource-guard.py`, with cwd
inside `build/nvidia-uki`, 12 GiB filesystem availability, 2 GiB **host-wide**
`MemAvailable`, a 0.5-second poll, and a 3600-second budget. Compilation uses two
jobs, including recursive make. These are polling safeguards, not hard quotas.

Installed DKMS 3.4.4 hard-codes `/etc/dkms/framework.conf`; it has no alternate
framework-config CLI option. An existing unprivileged Bubblewrap namespace makes
the host filesystem read-only, exposes only this private work tree writable,
uses a private minimal `/dev`, disables networking, and overlays the workspace
framework directory onto `/etc/dkms` **inside the namespace only**. Nothing is
written to host `/etc/dkms`. The guard remains outside both the namespace worker
and fakeroot and rejects EUID 0 as designed. No privileged helper or root guard
is invoked. If this isolation stops working, stop; do not substitute a global
DKMS build. A successful non-root Bubblewrap UID probe was performed during
preparation.

Signing uses only the existing test kernel's workspace
`certs/signing_key.pem` and `certs/signing_key.x509`, through the private headers'
`scripts/sign-file`. The helper checks existence but never reads, hashes, or logs
private-key contents and never generates keys, enrolls certificates, or changes
global trust. It requires successful matching module signatures, even though
Secure Boot is currently disabled. EFI signing, ukify, TPM operations, enrollment,
presets, all-kernel regeneration, the `/usr/local/bin/mkinitcpio` wrapper, and
Limine update/deploy/entry tools are not executed.

## Protected Command Line

The existing `plan/test-kernel.cmdline` must match the complete working
`/proc/cmdline` and the stock `//linux` Limine entry, except for exactly one
`acer_wmi.predator_v4=0`. The helper reads these again at execution, rejects drift,
and copies the protected line into its own inputs. It never guesses or embeds a
root UUID in code, refreshes the shared file, edits global module options, or
changes the stock/default entry. Any changed working root/encryption arguments
require operator review of the source input, not an automatic workaround.

The fragment at `build/nvidia-uki/boot/limine-nitro-test.conf.fragment` is a new
test-entry proposal only. The operator owns backups, privileged installation, activation,
and the later one-time boot. With Secure Boot disabled, the Limine entry's complete
effective command line must retain the opt-out as well as the embedded line.
Neither a successful build nor an embedded opt-out proves runtime safety.

## Expected Actual Outputs

These paths are candidates until their stages succeed; only the final manifest
and state establish which actual artifacts exist:

- `build/nvidia-uki/artifacts/manifest.json`: package, driver, module, EFI section,
  UKI, initramfs, and input hashes, audit evidence, and explicit no-runtime claim.
- `build/nvidia-uki/artifacts/nvidia-615.78.08-7.2.9-arch1-1-nitro-test-x86_64.dkms.tar.gz`:
  native, binaries-only, new-target-only archive with upstream `NVIDIA-COPYING`.
- `build/nvidia-uki/dkms-import/nvidia/615.78.08/7.2.9-arch1-1-nitro-test/x86_64/`:
  actual native import proof, including modules and make log, not global state.
- `build/nvidia-uki/module-root/usr/lib/modules/7.2.9-arch1-1-nitro-test/updates/dkms/`:
  validated uncompressed `.ko` files and private dependency indices nearby.
- `build/nvidia-uki/boot/omarchy_linux-nitro-test.efi` and
  `build/nvidia-uki/boot/initramfs-linux-nitro-test.img`: real staged images.
- `build/nvidia-uki/audit/`: normal modpost log reference, module/signature report,
  symbol audit, native import status, EFI section payloads, and initramfs listing.

## Future Root Integration

**Preparation only. Nothing in this section is run by the helper or now.** The
operator owns the privileged transaction, current backups and hook re-audit. Never run the
ordinary-user resource guard in root or fakeroot. Re-check current package/DKMS
state and artifact hashes against the final manifest immediately before use.
The kernel and headers must already have been installed through the audited,
new-release-only transaction, without automatic DKMS or boot regeneration.

Global `dkms ldtarball` normally refuses when `nvidia/615.78.08` is already added,
even when the tarball contains only a new kernel. Do **not** use `--force` against
the existing driver to bypass this. The actual private native import proof offers
a target-only handoff that leaves the existing source registration and every
other release untouched. In a separately authorized native root shell, after
validating the existing global source link and module hashes, the exact proposed
target-only stage is:

```bash
W=/absolute/path/to/nitro-control-linux
KREL=7.2.9-arch1-1-nitro-test
V=615.78.08
PROOF="$W/build/nvidia-uki/dkms-import/nvidia/$V/$KREL"
TARGET="/var/lib/dkms/nvidia/$V/$KREL"
test "$KREL" != "$(uname -r)" || exit 1
test -d "/usr/lib/modules/$KREL/build" || exit 1
test "$(readlink -f "/var/lib/dkms/nvidia/$V/source")" = "/usr/src/nvidia-$V" || exit 1
test -d "$PROOF/x86_64/module" || exit 1
test ! -e "$TARGET" && test ! -L "$TARGET" || exit 1
# The operator must exclude concurrent DKMS/package writers before this no-overwrite copy.
/usr/bin/cp -a --no-clobber -T -- "$PROOF" "$TARGET" || exit 1
/usr/bin/dkms status "nvidia/$V" -k "$KREL" -a x86_64
# Stop unless status is exactly built for this new target and hashes still match.
# The operator must re-audit native installation hooks and forbid unexpected actions.
/usr/bin/dkms install "nvidia/$V" -k "$KREL" -a x86_64 --no-depmod
/usr/bin/depmod "$KREL"
/usr/bin/dkms status "nvidia/$V" -k "$KREL" -a x86_64
/usr/bin/modinfo -k "$KREL" -F vermagic nvidia
```

This uses the built-state native import, not root compilation. The status/hash
gate is mandatory before the install command: DKMS can otherwise fall back to
building. Do not load modules, change existing keys, use `autoinstall`, `--all`,
force flags, or run an unaudited post-transaction hook. The staged EFI remains
workspace-only until the operator separately installs a verified image/new entry while
preserving the stock/default entry and backups.

If private native DKMS is unavailable **before any compilation attempt**, the
ordinary-user alternative to prepare under the same guard is:

```bash
/usr/bin/make -j2 -C "$W/build/nvidia-uki/sources/nvidia-615.78.08" \
  SYSSRC="$W/build/nvidia-uki/module-root/usr/lib/modules/$KREL/build" \
  SYSOUT="$W/build/nvidia-uki/module-root/usr/lib/modules/$KREL/build" \
  KERNEL_UNAME="$KREL" modules
```

It must retain normal modpost, verify every `.ko`'s exact vermagic/exported symbols,
and use only the workspace test-kernel key for any private signing. It is not an
automatic fallback after a failure, and the current helper deliberately selects
the already-evaluated private DKMS route instead. If only normal-make outputs
exist, the exact later **native root DKMS** alternative is `dkms build
nvidia/615.78.08 -k 7.2.9-arch1-1-nitro-test -a x86_64 -j 2`, verify `built`, then
`dkms install nvidia/615.78.08 -k 7.2.9-arch1-1-nitro-test -a x86_64 --no-depmod`
and `depmod 7.2.9-arch1-1-nitro-test`, only after the operator's separate signing/hook/resource
review. That alternative would compile as root and is **not** executed or claimed
to avoid root compilation. Do not mix the alternatives or retry a failed build.

## Scope of Success

Success means NVIDIA 615.78.08 compiled against the validated private headers with
normal modpost/symbol checking, a target-only native DKMS binary archive survived
an actual private import, and a real staged UKI passed content audits. It does not
claim module loading, bootability on this host, GPU runtime compatibility, fan
safety, hardware acceptance, or privileged activation. No hardware/fan/path writer
is invoked, and the running kernel and installed driver remain untouched.
