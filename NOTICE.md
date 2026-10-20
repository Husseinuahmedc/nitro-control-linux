# Source And License Notices

This workspace combines a kernel backport, Arch-derived packaging, and a new,
separate userspace application. It is not an official Acer, Arch Linux, Linux
kernel, Qt, NVIDIA, or Omarchy release. Names identify provenance and interfaces,
not endorsement. See [LICENSE](LICENSE) for the local license grant and scope.

## Component Licenses

| Component | Applicable Notice |
| --- | --- |
| New original Python application, backend, native Qt frontend, application tests, launchers/service-policy definitions, original helpers under `scripts/`, root `tests/`, and original operator docs, excluding already upstream-licensed material | `GPL-3.0-or-later`; full version 3 text in [LICENSES/GPL-3.0.txt](LICENSES/GPL-3.0.txt) |
| Linux `drivers/platform/x86/acer-wmi.c` and local derived 16-line patch | `GPL-2.0-or-later`, as stated by the driver's SPDX header; full version 2 text in [LICENSES/GPL-2.0.txt](LICENSES/GPL-2.0.txt) |
| Complete Linux kernel and Arch kernel patch set | Retain kernel `COPYING`, all file-level licenses/exceptions, and upstream notices; the Arch kernel package declaration remains `GPL-2.0-only` |
| Arch recipe, configuration, and metadata covered by upstream `REUSE.toml` | `0BSD`, copyright Arch Linux Contributors; full terms with preserved attribution in [LICENSES/0BSD.txt](LICENSES/0BSD.txt); obtain the upstream [LICENSE](https://github.com/archlinux/svntogit-packages/blob/master/trunk/LICENSE) and [file-scope annotations](https://github.com/archlinux/svntogit-packages/blob/master/trunk/REUSE.toml) from the packaging repository |
| Python, Qt/PySide6, system utilities, NVIDIA components, firmware | Their own component/version-specific licenses; not relicensed by this workspace |

The inherited `license=(GPL-2.0-only)` in `packaging/PKGBUILD` describes the
packaged kernel, not the copyright license of the recipe. The vendored Arch
`REUSE.toml` explicitly places `PKGBUILD`, configuration, and listed metadata
under `0BSD`, and separately annotates kernel patches `GPL-2.0-only`. That
general patch annotation does not narrow the local Acer-derived patch's
`GPL-2.0-or-later` notice. Do not flatten these distinctions into a single
repository-wide GPL version.

## Linux Acer WMI

The driver's original notices identify:

- Carlos Corbacho, Acer WMI Laptop Extras, Copyright (C) 2007-2009.
- E.M. Smith, underlying `acer_acpi` work, Copyright (C) 2005-2007.
- Carlos Corbacho, underlying `acer_acpi` work, Copyright (C) 2007-2008.
- Other Linux Acer WMI contributors, whose individual notices and history remain applicable.

The source header is `SPDX-License-Identifier: GPL-2.0-or-later`; the broader
kernel package's `GPL-2.0-only` declaration does not replace that header.
The driver's `MODULE_LICENSE("GPL")` is module metadata, not a more precise
replacement license grant.

The source baseline is Linux `7.2.9` plus Arch `v7.2.9-arch1`, referenced with
signatures and hashes in [packaging/PKGBUILD](packaging/PKGBUILD). The full
prepared tree retains its upstream notices under
`build/makepkg/linux-nitro-test/src/linux-7.2.9/`; generated build/download
directories are not newly authored application code.

## ANV15-51 Backport

Justin Vargas authored the upstream ANV15-51 support change:

- Original object: [`71019d2a13e0766ca174846e68dc5c0bde2e6494`](https://github.com/kernelorg-mirror/linux_kernel_git_pdx86_platform-drivers-x86/commit/71019d2a13e0766ca174846e68dc5c0bde2e6494).
- Reapplied object: [`9319f8ff83941d63658d230b3f02138764fceadc`](https://github.com/kernelorg-mirror/linux_kernel_git_pdx86_platform-drivers-x86/commit/9319f8ff83941d63658d230b3f02138764fceadc).
- The upstream patch is retrievable from that repository; this repository
  carries only the adapted 16-line variant.

The local [16-line patch](packaging/0001-acer-wmi-anv15-51.patch), prepared in
October 2026, adapts that change to the older combined `.predator_v4` quirk
field. It adds one CPU fan, one GPU-side fan, PWM capability, and the DMI quirk;
it does not introduce new WMI methods or implement recovery. It is a modified,
kernel-derived work retaining `GPL-2.0-or-later`, not covered by the new
userspace license grant. The quirk field is distinct from the forced module
parameter.

Upstream sign-off, review, assistance, and test trailers remain part of the
original upstream patch. They are not sign-offs or hardware attestations for
this local backport, kernel build, application, BIOS, or recovery behavior.
No local `Signed-off-by` or `Tested-by` is asserted here.

## Arch Packaging

The original Arch Linux `linux` recipe maintainer is **Jan Alexander Steffens
(heftig)**, as retained in both PKGBUILDs. The packaging baseline is commit
`981148f11711b8602408371fb82802827a2cd424` (`7.2.9.arch1-1`) from
<https://gitlab.archlinux.org/archlinux/packaging/packages/linux>.

Arch Linux Contributors retain the original `0BSD` copyright/license notice.
The self-contained [0BSD license](LICENSES/0BSD.txt) preserves that notice and
the full canonical grant/disclaimer from SPDX; it does not invent an individual
recipe author from the license template.
Local changes create separate `linux-nitro-test` and
`linux-nitro-test-headers` identities, apply the model backport, and add build
invariant checks. They do not make the local preparer the original recipe
maintainer or change the inherited kernel package license declaration.

## Userspace And Dependencies

The new Python controller, independent supervisor/recovery processes, and native
Qt frontend are separate userspace work, licensed `GPL-3.0-or-later`. They use
the standard Linux sysfs hwmon ABI, Unix sockets, process credentials, and
ordinary system calls. They are not linked into the kernel; placing their
source beside a GPL version 2 kernel does not relicense either component.

PySide6 dynamically supplies Qt bindings. Qt, PySide6, Shiboken, Python, and
their transitive dependencies retain their own notices and applicable
LGPL/GPL/commercial or other license terms. Dynamic use is not a grant to
relicense them, nor an exemption from their distribution obligations. Check
the exact modules and versions before bundling; see
[Qt for Python licensing](https://doc.qt.io/qtforpython-6/licenses.html) and
[Python licensing](https://docs.python.org/3/license.html).

The app queries the separately installed `/usr/bin/nvidia-smi` for temperature;
this does not grant rights to redistribute NVIDIA utilities, drivers, or
firmware. No external dependency is claimed as original Nitro Control code.
Local participants' legal names or copyright ownership are not inferred from
usernames or development-tool output.

## Evidence And Distribution

The screenshot [nitro-control-readonly.png](docs/screenshots/nitro-control-readonly.png)
records a real unprivileged Wayland session with actual CPU/GPU temperatures
and unavailable RPM. The reported 74 backend and 49 GUI tests use software
fakes; neither they nor upstream test trailers prove local fan operation.
See [SUPPORTED-HARDWARE.md](docs/SUPPORTED-HARDWARE.md).

Before any future binary distribution, retain component notices, identify the
modified versions, include the applicable complete license texts, and provide
the required corresponding source, configuration, patches, and build/install
scripts under the applicable GPL provisions. A source archive that merely
points at remote kernel tarballs is not, by itself, a complete source delivery
for a distributed kernel binary. Include Arch's `0BSD` text if distributing
the derived recipe separately. This work performs no publication or upload.
