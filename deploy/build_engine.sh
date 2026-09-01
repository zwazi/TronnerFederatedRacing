#!/usr/bin/env bash
set -euo pipefail

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install_prefix=${TRONNER_ENGINE_PREFIX:-/opt/armagetronad}
jobs=${TRONNER_BUILD_JOBS:-1}
upstream_url=${TRONNER_ENGINE_UPSTREAM_URL:-https://github.com/ArmagetronAd/armagetronad.git}
upstream_commit=$(tr -d '[:space:]' < "$repository_dir/engine/UPSTREAM_COMMIT")
patch_file="$repository_dir/engine/patches/tronner-federation.patch"
engine_variant=${TRONNER_ENGINE_VARIANT:-federated}
temporary_workspace=

case "$engine_variant" in
    federated|vanilla) ;;
    *)
        echo "TRONNER_ENGINE_VARIANT must be federated or vanilla." >&2
        exit 2
        ;;
esac

if [[ -n ${TRONNER_ENGINE_SOURCE_DIR:-} || -n ${TRONNER_ENGINE_BUILD_DIR:-} ]]; then
    [[ -n ${TRONNER_ENGINE_SOURCE_DIR:-} && -n ${TRONNER_ENGINE_BUILD_DIR:-} ]] || {
        echo "Set both TRONNER_ENGINE_SOURCE_DIR and TRONNER_ENGINE_BUILD_DIR, or neither." >&2
        exit 2
    }
    source_dir=$TRONNER_ENGINE_SOURCE_DIR
    build_dir=$TRONNER_ENGINE_BUILD_DIR
else
    temporary_workspace=$(mktemp -d -t tronner-engine-build.XXXXXXXX)
    source_dir=$temporary_workspace/source
    build_dir=$temporary_workspace/build
    cleanup() {
        rm -rf -- "$temporary_workspace"
    }
    trap cleanup EXIT
fi

if [[ ! "$jobs" =~ ^[1-9][0-9]*$ ]]; then
    echo "TRONNER_BUILD_JOBS must be a positive integer." >&2
    exit 2
fi

new_source=0
if [[ ! -d "$source_dir/.git" ]]; then
    git clone --filter=blob:none --no-checkout "$upstream_url" "$source_dir"
    new_source=1
fi

if ((!new_source)) && [[ -n $(git -C "$source_dir" status --porcelain) ]]; then
    echo "Engine source directory has local changes; refusing to overwrite it." >&2
    exit 2
fi
git -C "$source_dir" fetch --depth 1 origin "$upstream_commit"
git -C "$source_dir" checkout --detach "$upstream_commit"
if [[ "$engine_variant" == federated ]]; then
    git -C "$source_dir" apply --check "$patch_file"
    git -C "$source_dir" apply "$patch_file"
fi

(
    cd "$source_dir"
    ./bootstrap.sh
)

mkdir -p "$build_dir"
cd "$build_dir"
"$source_dir/configure" \
    --prefix="$install_prefix" \
    --enable-dedicated \
    --enable-authentication \
    --disable-glout \
    --disable-sysinstall \
    --disable-useradd \
    --disable-initscripts \
    --disable-etc \
    --disable-desktop \
    --disable-uninstall \
    --disable-restoreold \
    --disable-migratestate \
    CXXFLAGS=-O2
make -j"$jobs"
make install

binary="$install_prefix/bin/armagetronad-dedicated"
test -x "$binary"
sha256sum "$binary"
"$binary" --version
echo "Built $engine_variant engine from upstream commit $upstream_commit."
