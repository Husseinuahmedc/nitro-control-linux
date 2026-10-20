# Root Deployment

This is an administrator CLI, not a GUI action or backend IPC request. The
implementation is `application/nitro_control/deployment.py` (`main(argv=None)`).
It uses Linux Python's standard library and installed native tools. The original
project's GPL-3.0-or-later license covers this code. NVIDIA binaries retain the
license included in their validated DKMS archive.

## Operator Workflow

No deployment, privileged command, hardware test, module load, service start,
firmware change or reboot is performed by the offline tests. The operator must review
this implementation before executing it.

1. Finish the kernel pipeline and NVIDIA/UKI pipeline. Both JSON states must say
   `completed`, with every stage succeeded and successful guard results.
2. Build, review and install app `nitro-control 0.1.0-9` separately. Its installed
   bytes, including `deployment.py` and the admin launcher, must match its actual
   package. Keep both application services disabled and inactive. The operator owns the
   `nitro-control-deploy` launcher alias; it must import `deployment.main` from
   `/usr/lib/nitro-control`, use isolated Python, and disable bytecode writes
   before importing modules (for example, `#!/usr/bin/python -IB`). Never import
   workspace Python into a privileged process.
3. Generate and review a new manifest as the ordinary user. Missing or incomplete
   artifacts, app pkgrel1, changed stock inputs or insufficient space are blockers,
   not permission to infer future filenames or weaken validation.
4. Run the installed admin CLI without `--apply` to repeat read-only preflight.
   Record the independently reviewed manifest SHA-256 outside the editable JSON.
5. Only after review, explicitly invoke the installed CLI as UID 0 with both
   `--apply` and the pinned `--manifest-sha256`. The helper never elevates itself,
   stores credentials or invokes pacman through sudo/pkexec. The operator may use
   pkexec to launch the installed helper; this document does not execute it.
6. Inspect the root-owned summary. Stop for manual test boot and the separate
   post-boot acceptance gate. Nothing here grants hardware approval or enables
   control.

Example ordinary-user commands, with an operator-chosen workspace:

```bash
WORKSPACE=/absolute/path/to/nitro-anv15-51-backport
/usr/bin/python -I -B "$WORKSPACE/scripts/create-deployment-plan.py" \
  --workspace "$WORKSPACE" --output "$WORKSPACE/plan/deployment.json"
/usr/bin/nitro-control-deploy --manifest "$WORKSPACE/plan/deployment.json" --preflight
```

The privileged invocation's arguments are
`--manifest /absolute/workspace/plan/deployment.json --manifest-sha256 <reviewed-64-lowercase-hex-digest> --apply`.
No password belongs in arguments, environment, manifest or logs. There is no
`--force`, arbitrary destination, extra package, automatic retry, oneshot-ID guess,
default-entry change, reboot or rollback-execution option.

## Manifest API

The generator's `--output` creates a **new** workspace file, never overwriting an
existing plan. Without `--output`, JSON goes to stdout. It does not create missing
directories. `--workspace` defaults to the script's project root, not a published
machine-specific home directory. The current build helper's actual report is
`plan/application-package.json` (not `app-package.json`); its pkgrel9 schema is
required. The kernel report comes from
`kernel-pipeline-state.json.artifact_paths["archive-validation"]`; its actual
filename is `archive-validation.json`, not a guessed `kernel-archive-validation.json`.

Schema version 1 has exactly these top-level members:

```json
{
  "schema_version": 1,
  "created_at_utc": "UTC timestamp",
  "workspace": "/operator/chosen/workspace",
  "target": {
    "kernel_release": "7.2.9-arch1-1-nitro-test",
    "kernel_config_sha256": "37e9b95c827d45cfb16b26bfdd236dd3b73f1064b6cb10c2955f59bd527e39ad",
    "nvidia_driver": "615.78.08",
    "package_version": "7.2.9.arch1-1",
    "application_version": "0.1.0-9",
    "signing_certificate_sha256": "aef06000e5edf2cf2d91b69d440248ecf137818d7c5438f00e8bdd4943cc076b",
    "module_signing_key": "09:52:C9:19:B3:CC:45:AA:F5:F8:5D:32:2E:06:6A:04:E4:69:3B:83"
  },
  "authorization": "operator-sha256-local-unsigned-archives",
  "artifacts": {
    "uki": {
      "path": "build/nvidia-uki/boot/omarchy_linux-nitro-test.efi",
      "sha256": "actual validated SHA-256, not this example text",
      "bytes": 260766208
    }
  },
  "baseline": {
    "running_kernel": "7.2.9-arch1-1",
    "secure_boot": false,
    "target_absent": true,
    "packages": {"nitro-control": "0.1.0-9"},
    "protected": {}
  }
}
```

The example omits required inventories for readability and is **not** deployable.
`artifacts` contains `kernel_state`, `nvidia_state`, `archive_validation`,
`nvidia_manifest`, `import_status`, `app_report`, `cmdline`, `kernel_package`,
`headers_package`, `app_package`, `uki`, `dkms_archive`, and exactly the source/script
`pin:<relative-path>` entries declared by the completed pipelines. Every descriptor
has only `path`, `sha256`, `bytes`. Paths are workspace-relative, canonical and
symlink-free; artifact roles and permitted source locations are constrained in code.
Private signing keys are never read, hashed, copied, generated or enrolled.

`baseline.packages` is the **complete** installed name/version inventory, not just
the example app entry. `protected` has exactly the fixed `PROTECTED` roots from the
module. Each root has a recursive relative-path inventory with kind, owner, group,
permissions, regular-file size/SHA-256 or symlink text, including explicit absence.
It covers all `/boot`, the stock release tree and NVIDIA DKMS state, Limine,
pacman/hooks, mkinitcpio/initcpio, NVIDIA/module configuration and installed app
code. Application `__pycache__` is not an integrity input; installed source files
are. Snapshots contain no file contents. The separately hashed `cmdline` input
contains the complete current working encrypted-root command line with exactly one
`acer_wmi.predator_v4=0`; the stock Limine entry must corroborate its root arguments.

The CLI returns JSON to stdout on success and a JSON blocker to stderr on failure.
`preflight()` returns `status=preflight-passed`, targets and `writes_performed=false`.
`apply_deployment()` returns the same summary persisted under its root-owned cache.
Apply validates the raw JSON's operator hash before any privileged writes, rejects
unknown fields/duplicate keys and checks fixed target/configuration/fingerprints.

## Transaction Boundaries

- All source inputs are copied into
  `/var/lib/nitro-control/deployment/<UTC-timestamp>/inputs` under UID 0, mode 0700
  directories and a restrictive umask. Every copy is rehashed before use; all
  privileged consumers use cached paths, not workspace paths. A cached manifest
  and input index preserve provenance.
- Root needs a 12 GiB reserve plus twice the input and full-boot sizes and the
  declared package installed sizes. ESP free space must be **greater than** the
  staged UKI size plus 128 MiB. Space is rechecked at boot staging. Only the fixed
  `/boot` ESP and off-ESP `/var/lib/nitro-control` destinations are supported.
- Both archives must have exact package/base/version/architecture metadata, no
  `.INSTALL`, replacements/conflicts, stock aliases, system configuration, hooks,
  ESP payloads, escaping links, special files, writable/set-ID payloads, duplicate
  members or existing file collisions. Shared directory entries are allowed.
- Pacman runs directly as `/usr/bin/pacman`, using a root-private minimal config
  with no repositories, `VerbosePkgLists`, optional local signatures and native
  dependency checks. Operator-pinned hashes authorize these local unsigned
  archives; system signature policy is not edited. Temporary root-owned `/dev/null`
  masks suppress exactly `10-linux-modules-pre/post`, `70-dkms-install` and
  `90-mkinitcpio-install`. `/etc/pacman.d/hooks` precedes the private mask directory.
  No persistent mask, `--noconfirm`, `--overwrite` or force flag is used.
- The helper parses the **actual transaction's** C-locale, colorless verbose
  package table before writing `y` to that same process's final confirmation
  prompt. Exactly two NEW kernel packages with no Old Version column are
  required. Extra dependencies, upgrades, removals, replacements, key questions,
  additional prompts or unfamiliar output fail closed. The baseline and input
  hashes are checked again at that prompt while ALPM holds its transaction lock.
- Applicable hooks are checked against the package/path inventory. Only the
  transaction mutex `10-limine-snapper-lock`, standard `35-systemd-update`
  ConditionNeedsUpdate arming and new-tree `60-depmod` may run.
  The helper does **not** hold the external boot lock during pacman and does not
  suppress the ALPM transaction mutex hook.

## NVIDIA Import

The inspected installed DKMS 3.4.4 implementation is pinned by SHA-256.
Its `load_tarball` rejects an already-added module/version unless forced. Therefore
the helper does **not** run `ldtarball` against the stock registration and does not
use `--force`. It imports with native `dkms ldtarball` into a fresh private tree,
using existing `/usr/src/nvidia-615.78.08` read-only. Native `add_module` registers
that source only in the private tree. The existing source `dkms.conf` is pinned,
not edited. The tarball must have exactly its licensed, target-only native layout,
and its configuration and compressed module hashes must match the audited inputs.

After private `status` proves `built` and native modinfo verifies every binary's
version, vermagic and signing-key fingerprint, only the absent
`/var/lib/dkms/nvidia/615.78.08/7.2.9-arch1-1-nitro-test` subtree is copied into the
existing registration. Stock source links, versions and kernel records are not
replaced. Native target `status` must again prove `built`, then the helper invokes
`dkms install nvidia/615.78.08 -k 7.2.9-arch1-1-nitro-test -a x86_64 --no-depmod`
and runs `depmod` for that release only.

Both native DKMS phases run inside bubblewrap with a read-only host, isolated
temporary framework configuration, no network, no capabilities, and compiler/make
executables masked by `/usr/bin/false`. Only the private cache, target module tree
and narrowly scoped NVIDIA bookkeeping are writable; existing stock DKMS
directories are read-only mounts. Signing, post-transaction commands, module
loading and weak-module operations are disabled in the private namespace only.
Missing built markers or any implementation mismatch abort rather than building
as root. Installed module hashes and unchanged stock state are verified. Changed
DKMS/source/configuration semantics require a reviewed implementation update, not
a generic compatibility fallback.

## Boot And Recovery

Only after package and NVIDIA completion does the helper acquire a fail-closed
exclusive `flock` on `/run/lock/boot-partition.lock`. It never unlinks/replaces that
lock or retries unlocked. While held, it creates and verifies a complete
off-ESP backup at `/var/lib/nitro-control/boot-backups/<timestamp>/boot`, with a
mode-0700 parent and root-owned inventory. Secure credential files are copied
without logging their contents.

It creates only the absent
`/etc/limine-entry-tool.d/90-nitro-control-test.conf`, containing the COMPLETE
`KERNEL_CMDLINE[linux-nitro-test]` assignment. It never changes global/default/stock
command lines, NVIDIA configuration, signing keys or enrollment. Secure Boot must
already be disabled and Limine config enrollment/sorting disabled.

Inside the held external lock it calls exactly the installed wrapper's
`--add-uki linux-nitro-test <root-cached-staged-UKI> --comment ... --no-hooks --no-mutex`.
Skipping its internal mutex is safe only because the external lock is held.
No common EFI deployment, UEFI BootOrder, signing, enrollment, snapshot or
`limine-update` command runs. Only the new test UKI, Limine config and possible
`.old` may change. The new destination is
`/boot/EFI/Linux/omarchy_linux-nitro-test.efi`. The copied hash must match the staged
UKI; PE section lengths/hashes and root command line are audited. Stock UKI SHA-256
`495b147276e33d253fdc71460853a97ecacb0e83740668c1cd1b558e2fac5925`, existing entries,
OS order, first stock kernel and `default_entry: 2` must remain unchanged.

The completed summary contains one manual instruction: choose **Omarchy ->
linux-nitro-test** for one boot, then require the exact test release and effective
`predator_v4=N`. No `bootctl set-oneshot` is attempted because no Limine BLI entry
identifier has been proven. The caller stops for interactive boot. Services remain
disabled and no hardware-approved marker, module load or fan write occurs.

`summary.json` records each write intent, completed package/NVIDIA target, owned
fragment before/after hashes, entry/UKI hashes and verified backup. Failures name
the precise step and retained summary path and attempt an observed-owned-state
inventory. Nothing is automatically rolled back or cleaned up. A partial install
blocks a new apply: targets are no longer absent. Recovery is a separately reviewed
operation, never a blind restore of `/boot`, package removal or DKMS `--all`.

For later rollback, first boot stock and verify the test release is not running.
Reconcile live package/DKMS state and compare every candidate owned file against
the recorded **after** hash; mismatches mean stop for concurrent-change review.
Use the verified full backup as evidence, not permission to overwrite newer stock
files. Entry removal needs the same fail-closed external boot lock. Remove only
the exact owned fragment/entry/UKI, exact NVIDIA kernel target and two test
packages under separately audited removal hooks. Never remove the installed app,
stock kernel or stock NVIDIA registration as a side effect of deployment rollback.

## Offline Tests

```bash
WORKSPACE=/absolute/path/to/nitro-anv15-51-backport
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$WORKSPACE/application" \
  /usr/bin/python -B -m unittest discover -s "$WORKSPACE/application/tests" \
  -p test_deployment.py -v
```

Tests use disposable temporary files (verification here uses `TMPDIR=/tmp/opencode`) and mock host subprocesses,
ownership/UID checks and the shared boot lock. They do not invoke deployment,
native package installation, DKMS, boot tools or hardware operations.
