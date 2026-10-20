# License Texts

`../LICENSE` defines scope; a license text alone does not assign every file in
this workspace to that license. `../NOTICE.md` records component provenance.

| File | Official HTTPS Source | Use |
| --- | --- | --- |
| `0BSD.txt` | <https://spdx.org/licenses/0BSD.txt> | Full 0BSD grant/disclaimer with the preserved Arch Linux Contributors copyright notice |
| `GPL-2.0.txt` | <https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt> | GPL version 2; used with the component's `-only` or `-or-later` notice |
| `GPL-3.0.txt` | <https://www.gnu.org/licenses/gpl-3.0.txt> | GPL version 3; new original application, scripts, tests, and operator docs choose `GPL-3.0-or-later`, excluding already upstream-licensed material |

The GPL files are complete, unedited license documents, including application
instructions and warranty provisions, not summaries. `0BSD.txt` reproduces the
full canonical SPDX grant and disclaimer, with its example copyright line
replaced by the existing `Copyright Arch Linux Contributors` notice from the
vendored Arch license. Arch's `REUSE.toml` identifies the upstream contributors
and file scope; the SPDX example's named holder is not attributed as the Arch
recipe's author. No new legal name or copyright ownership is inferred.

HTTPS text origins are recorded here; digests identify exact local bytes, not
a detached signature or a claim about source authorship. No package installation
is needed to read these licenses.

The GPL texts were downloaded from GNU and the 0BSD terms retrieved from SPDX
on 2026-10-19. All three local license files are ASCII. Verify their recorded
SHA-256 digests from the project root:

```sh
sha256sum --check LICENSES/SHA256SUMS
```

Arch's original `0BSD` text and copyright attribution are part of the upstream
packaging repository (see the commit pinned in `scripts/verify-sources.sh`).
The self-contained local copy is [`0BSD.txt`](0BSD.txt).

The complete kernel source also retains its `COPYING`, `LICENSES/`, and
per-file notices. Do not replace those with this directory or assume all
kernel files are licensed identically. Dependency packages retain their own
license files; no Qt/PySide6 or NVIDIA binary license is replaced here.
