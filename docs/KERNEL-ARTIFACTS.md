# Resumable Kernel Artifacts

`scripts/kernel-pipeline.py` continues only the existing configured tree at
`build/makepkg/linux-nitro-test/src/linux-7.2.9`. It does not prepare or extract
sources, clean the tree, change the stock configuration, install anything,
load modules, access fan controls, alter NVIDIA configuration, or publish boot
files. All output stays in this workspace. No commit or push is performed.

## Supervision

Start once with `/usr/bin/python scripts/kernel-pipeline.py --start`. This
detaches an ordinary process with a new session and file-backed output, not a
service. Do not start another kernel build alongside it.

The machine-readable state is `plan/kernel-pipeline-state.json`. It contains
the fixed stages, their commands/status/results, pipeline/guard/child PIDs,
log paths, immutable input pins, finish reason, and completed artifact paths.
Each stage has separate resource JSONL, compiler output, and guard summary
logs under `logs/kernel-pipeline-*`. Existing and partial logs are preserved.

Monitor with `/usr/bin/python scripts/kernel-pipeline.py --status` or
`watch -n 5 '/usr/bin/python -m json.tool plan/kernel-pipeline-state.json'`.
Read the current output log identified by `logs.output`. The pipeline log
identified by `logs.pipeline` records stage transitions. Send `SIGTERM` only
to `pipeline_pid` for an orderly stop through the existing guard.

Every stage is wrapped by the unchanged `scripts/resource-guard.py`, with
exactly `-j2`, 12 GiB filesystem-available space, 2 GiB host MemAvailable,
and a 3600-second maximum, including its invariant checks. These are polling
limits, not a quota or a memory sandbox. Any guard stop, tool failure, input
change, or missing result stops the whole pipeline. There is no automatic
retry, timeout reset, cleanup, or repartitioning after failure.

`--resume` is only for continuing after successful completed stages. A lost
supervisor can recover a successful guard summary plus stage result; it will
not rerun an incomplete, timed-out, failed, or resource-stopped stage. An
advisory lock remains with a live guard even if its supervisor is killed.
Stopped/failed pipelines require operator review and a fresh state, not a
reset or deletion of the state file.

## Controlled Reboot Recovery

`/usr/bin/python scripts/kernel-pipeline.py --recover-reboot` is an explicitly
requested, detached recovery mode, not an automatic retry. It takes the same
exclusive owner lock, checks that stale PIDs are absent and no other kernel
job exists, and snapshots the original state, stage logs (including empty
files), and successful result records under `plan/kernel-pipeline-recoveries/`.
The original evidence is never rewritten or supplied with invented summaries.
A denied recovery leaves the original state intact and writes a blocker report.

Recovery requires the immediately previous/current journal boot IDs to match
the interrupted stage's UTC timestamps and current `/proc` boot ID/btime.
Journal clock evidence must corroborate that boundary. Recorded resource/tool
failure, any terminal guard summary, incomplete evidence, exhausted budget,
changed stage plan, or a changed immutable input is a blocker. Missing log
records are classified as missing, not proof of success or of the reboot cause.

The interrupted stage is charged `ceil(new kernel btime + 1 - original active
UTC start)` seconds. This conservative bound includes downtime and a second
for integer btime precision. Its new guard timeout is at most `3600 - charged
seconds`, never a fresh hour. The original start and previous attempt remain
in `attempts`; a new log namespace preserves all original files. Future pending
stages retain their original 3600-second budgets and order. The guard itself,
12 GiB disk / 2 GiB MemAvailable thresholds, `-j2`, and package recipe do not change.

Only this explicit mode can re-pin the recognized legacy pipeline script to
its recovery-capable version, recording old/new hashes and the new helper pin.
A checkpoint device-number change is permitted only after confirmed reboot,
full matching `vmlinux.o` SHA-256, and unchanged inode, size, mtime and ctime;
the old/new stat records are retained. The prepared driver and patch are also
hash-pinned. No configuration or source preparation is rerun.

Before the recovered compile, within that stage's remaining guard budget,
`scripts/kernel_reboot_recovery.py` checks regular generated `.o`/`.a` files
inside the existing tree without following symlinks. It checks ELF headers,
section bounds and archive member bounds for obvious truncation/zero files,
not arbitrary content corruption in otherwise well-formed unpinned objects.
Invalid outputs are moved, not deleted, into the run's workspace `quarantine/`
with hashes and a durable inspection manifest. Valid objects are not rewritten.
The pinned `vmlinux.o` can never be quarantined or silently rebuilt. There is
no full clean or re-extraction.

New states and attempts durably record boot ID, kernel btime, monotonic and
boottime starts, and PID start-time identities. The stage worker waits for
its identity to be saved before compiling. State replacements also fsync their
parent directory; the supervisor fsyncs guard/output logs at the identity
handshake, every ten seconds, and stage completion, without editing the guard.
This improves future interruption evidence but cannot prove that no diagnostic
was lost during a power failure.

## Explicit Validation Repair

`/usr/bin/python -B scripts/kernel-pipeline.py --repair-validation` is a narrowly
scoped, explicitly requested repair for the original validator's incorrect
expectation of `build/tools/bpf/bpftool/vmlinux.h`. The unchanged official headers
function flattens this input to `build/vmlinux.h`. The corrected validator checks
that root file against the source tree's generated header by SHA-256, alongside
the original required files, stock config, release, Rust, BTF and safe-path checks.
Missing files and hash mismatches now identify the actual paths and digests.
Only decompression drops the unsupported `zstd -T2` option; compression stays at
two threads.

Repair requires the exact known old script fingerprint and package hashes,
matching durable `child_exit`/exit-1 summaries, the known checker diagnostic,
no resource violation or signal, unchanged guard/recipe/config/checkpoint/input
pins, completed build/package stages, and only the input-staging stage pending.
It never retries a timeout or resource stop, recompiles, cleans, prepares sources,
or repackages. A lock and absent prior process identities prevent duplicate owners.

The failed state and logs are copied under `plan/kernel-pipeline-validation-repairs/`;
the failed attempt and existing reboot history remain intact. The audit records
old/new self-script fingerprints and unchanged package hashes/sizes/timestamps.
Only validation and the original remaining input-staging stage execute, under
the existing resource guard and a new log namespace. The prior validation's
2.501668 seconds are conservatively charged as 3 seconds, leaving 3597 seconds
of its original 3600-second budget. There is no automatic repair/retry.

## Declared Stages

1. `vmlinux-btf-link`: incremental `make -j2 vmlinux`, reusing pinned `vmlinux.o`.
2. `gpu-amdgpu`: `drivers/gpu/drm/amd/amdgpu/`.
3. `gpu-i915`: `drivers/gpu/drm/i915/`.
4. `gpu-xe`: `drivers/gpu/drm/xe/`.
5. `gpu-nouveau`: `drivers/gpu/drm/nouveau/`.
6. `gpu-remaining`: `drivers/gpu/`.
7. `net-ethernet`: `drivers/net/ethernet/`.
8. `net-wireless`: `drivers/net/wireless/`.
9. `net-drivers-remaining`: `drivers/net/`.
10. `media`: `drivers/media/`.
11. `scsi`: `drivers/scsi/`.
12. `usb`: `drivers/usb/`.
13. `infiniband`: `drivers/infiniband/`.
14. `staging`: `drivers/staging/`.
15. `iio`: `drivers/iio/`.
16. `sound`: `sound/`.
17. `drivers-remaining`: `drivers/`.
18. `filesystems`: `fs/`.
19. `network-stack`: `net/`.
20. `remaining-core`: `arch/x86/ block/ crypto/ io_uring/ kernel/ lib/ mm/ rust/ security/ virt/`.
21. `modules-final`: incremental `make -j2 modules`, including modpost/module BTF.
22. `kernel-image`: incremental `make -j2 bzImage`.
23. `bpftool-vmlinux-header`: the recipe's `vmlinux.h feature-clang-bpf-co-re=1` target.
24. `package-kernel`: isolated fakeroot/pkgdir, existing sources, repackage mode.
25. `package-headers`: separate fakeroot/pkgdir, unchanged official headers function.
26. `validate-archives`: inspect actual archives, identities, payloads and releases.
27. `compatibility-uki-inputs`: workspace-only kernel/config/cmdline/manifest staging.

The parent-directory stages perform normal incremental Kbuild traversal after
their larger subsystems. They do not clean or deliberately recompile unchanged
objects. Large stages can still hit a limit; completion is not guaranteed by
the available disk or the predeclared boundaries.

## Identity And Packaging

The release is always `7.2.9-arch1-1-nitro-test`. Both source `.config` and
`packaging/config.x86_64` must retain SHA-256
`37e9b95c827d45cfb16b26bfdd236dd3b73f1064b6cb10c2955f59bd527e39ad`.
The initial guarded link verifies `vmlinux.o` SHA-256
`f85aeaf862734af84361205e4c355b1108e29ce9b7f0b495f610b2c63fe8a8ea`;
its inode/size/timestamps must remain unchanged throughout. Pins are checked
before and after every stage, including `make kernelrelease` after each stage.
DWARF5, BTF/module BTF, Rust, and the authoritative recipe are not changed.

The sanitized environment uses `PATH=/usr/bin:/bin`, `MAKEFLAGS=-j2`,
`SOURCE_DATE_EPOCH=1791027808`, `KBUILD_BUILD_HOST=archlinux`,
`KBUILD_BUILD_USER=linux-nitro-test`, and the UTC `date -Ru -d @1791027808`
timestamp. Temporary files and caches use `build/tmp` and `build/cache`.
The existing `scripts/Makefile.btf` derives pahole's two threads from `-j2`,
retaining its BTF feature flags and Rust language exclusion.

The workspace selector `scripts/kernel-pipeline.PKGBUILD` sources the pinned
`packaging/PKGBUILD` and selects one binary package per stage. It does not edit
either packaging function. Each package uses its own generated build/pkgdir
and a source-directory symlink to the existing tree. `makepkg --repackage
--noextract --noprepare --nocheck --nosign` never reruns build/prepare or source
acquisition. Only the workspace makepkg config sets archive compression to
`zstd -T2`. No global configuration is edited and no install/dependency-sync
flags are used.

Expected binary archives, not considered validated until stage 26 succeeds:

- `packages/linux-nitro-test-7.2.9.arch1-1-x86_64.pkg.tar.zst`
- `packages/linux-nitro-test-headers-7.2.9.arch1-1-x86_64.pkg.tar.zst`

Validation streams the actual archives without extraction. It checks internal
package names/versions/architecture, unique module-tree paths, no stock package
aliases, no boot/etc/hook or `.INSTALL` payload, safe links, exact module list,
kernel image/config hashes, headers release files and Rust metadata, final
vmlinux BTF/DWARF sections, and packaged acer-wmi vermagic. The unique headers
alias is `usr/src/linux-nitro-test`. A validation report with archive hashes is
written under `build/kernel-pipeline/<run-id>/archive-validation.json`.

## Deferred Work

The final stage stages only local UKI inputs under
`build/boot/7.2.9-arch1-1-nitro-test-<run-id>/`, retaining
`acer_wmi.predator_v4=0` in the command line. Its manifest identifies validated
packages and the staged headers build tree for later compatibility work.
It does not create an initramfs or claim a bootable UKI. NVIDIA external-module
compatibility, initramfs generation, and final local UKI assembly remain for
main after the artifacts exist. No DKMS global state, system NVIDIA files,
system UKIs, ESP, boot entries, or installed module trees are changed. All
privileged or hardware operations require separate explicit approval.
