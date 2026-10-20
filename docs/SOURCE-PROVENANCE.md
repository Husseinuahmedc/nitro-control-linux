# Source Provenance

- Official Arch packaging tag `7.2.9.arch1-1`, commit
  `981148f11711b8602408371fb82802827a2cd424`:

  ```sh
  mkdir -p vendor
  git clone --branch 7.2.9.arch1-1 --depth 1 \
    https://github.com/archlinux/svntogit-packages.git \
    vendor/arch-linux-packaging
  git -C vendor/arch-linux-packaging checkout 981148f11711b8602408371fb82802827a2cd424
  ```
- Signed stable `linux-7.2.9.tar.xz` plus the signed official Arch
  `linux-v7.2.9-arch1.patch.zst`, corresponding to Arch source commit
  `811a830af7e795834eb65ea419afe08153d57720`.
- ANV15-51 maintainer-tree support from
  `71019d2a13e0766ca174846e68dc5c0bde2e6494`, later reapplied as
  `9319f8ff83941d63658d230b3f02138764fceadc`.
- The backport (`packaging/0001-acer-wmi-anv15-51.patch`, 16 lines,
  upstream-derived) translates newer `.hwmon`/`.platform_profile` quirk
  fields into 7.2.9's existing `.predator_v4` quirk field. It does not alter
  WMI methods, EC access or any unrelated driver behavior.
- The original loaded stock acer_wmi module source version is
  `3B2CBCF7ACAF98B534919E6`; the patched test-kernel build reports
  `689B92E5FB873DA56B8AC74`, distinguishing stock and patched binaries.
- The private NVIDIA/UKI pipeline (`scripts/prepare-nvidia-uki.py`,
  `docs/NVIDIA-UKI.md`) built NVIDIA 615.78.08 modules against the test
  kernel in a private tree and embedded them in the staged UKI; the host
  DKMS registration for the stock kernel remained untouched.
