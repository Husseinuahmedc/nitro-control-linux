#!/usr/bin/env bash
set -euo pipefail

root=$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")
mode=${1:---preflight}
if (( EUID == 0 )) || [[ $mode != --preflight && $mode != --build ]]; then
  printf 'Usage, as ordinary user: bash scripts/build-kernel.sh [--preflight|--build]\n' >&2
  exit 1
fi

export GNUPGHOME="$root/build/gnupg"
export SRCDEST="$root/downloads" BUILDDIR="$root/build/makepkg"
export PKGDEST="$root/packages" SRCPKGDEST="$root/packages" LOGDEST="$root/logs"
export TMPDIR="$root/build/tmp" XDG_CACHE_HOME="$root/build/cache"
export MAKEFLAGS=${MAKEFLAGS:--j4}
export SOURCE_DATE_EPOCH=1791027808
mkdir -p "$BUILDDIR" "$PKGDEST" "$LOGDEST" "$TMPDIR" "$XDG_CACHE_HOME"

(
  source "$root/packaging/PKGBUILD"
  pacman -T "${makedepends[@]}" > "$root/logs/missing-build-dependencies.txt" \
    || [[ $? == 127 ]]
)
blocked=0
if [[ -s $root/logs/missing-build-dependencies.txt ]]; then
  printf 'Missing installed build dependencies (see logs/missing-build-dependencies.txt).\n' >&2
  blocked=1
fi

# DWARF5/BTF and the stock config are retained; no measured full-build size yet.
mapfile -t space < <(df --block-size=1 --output=avail "$root")
available=${space[1]//[[:space:]]/}
if (( available < 80 * 1024 * 1024 * 1024 )); then
  printf 'Full-build guard: require 80 GiB free for the stock debug-enabled config.\n' >&2
  blocked=1
fi
(( blocked == 0 )) || exit 2

if [[ $mode == --preflight ]]; then
  printf 'Preflight passed. Nothing compiled, installed, or activated.\n'
  exit 0
fi

# This removes only an earlier makepkg source tree inside this workspace.
# Deliberately never pass --syncdeps or --install.
cd "$root/packaging"
exec makepkg --cleanbuild --log
