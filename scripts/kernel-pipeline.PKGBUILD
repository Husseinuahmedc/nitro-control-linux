# Workspace-only selector; all official package functions remain unchanged.
[[ -n $NITRO_PIPELINE_ROOT ]] || return 1
source "$NITRO_PIPELINE_ROOT/packaging/PKGBUILD"
case $NITRO_PIPELINE_PACKAGE in
  linux-nitro-test|linux-nitro-test-headers) pkgname=("$NITRO_PIPELINE_PACKAGE") ;;
  *) return 1 ;;
esac
