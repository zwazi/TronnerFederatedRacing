#!/usr/bin/env bash
set -euo pipefail

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source_dir=${TRONNER_ENGINE_SOURCE_DIR:?Set TRONNER_ENGINE_SOURCE_DIR to the patched engine source tree.}
build_dir=${TRONNER_ENGINE_BUILD_DIR:?Set TRONNER_ENGINE_BUILD_DIR to its completed build tree.}
output_dir=${SMOKE_PROBE_DIR:-$build_dir/tronner-smoke-probes}

test -f "$source_dir/src/tron/gGame.cpp"
test -f "$build_dir/config.h"
for library in libtron libenginecore libengine libnetwork libui librender libtools; do
    test -f "$build_dir/src/$library.a"
done

mkdir -p "$output_dir"
for probe in network_hold player_name_probe server_info_probe; do
    g++ -std=c++11 -O2 -Wl,--allow-multiple-definition \
        -I"$build_dir" -I"$build_dir/src" \
        -iquote "$source_dir/src" \
        -iquote "$source_dir/src/tools" \
        -iquote "$source_dir/src/network" \
        -iquote "$source_dir/src/engine" \
        -iquote "$source_dir/src/tron" \
        -iquote "$source_dir/src/ui" \
        -iquote "$source_dir/src/render" \
        $(pkg-config --cflags libxml-2.0) \
        "$repository_dir/deploy/smoke/$probe.cpp" \
        -Wl,--start-group \
        "$build_dir/src/libtron.a" \
        "$build_dir/src/libenginecore.a" \
        "$build_dir/src/libengine.a" \
        "$build_dir/src/libnetwork.a" \
        "$build_dir/src/libui.a" \
        "$build_dir/src/librender.a" \
        "$build_dir/src/libtools.a" \
        -Wl,--end-group \
        $(pkg-config --libs libxml-2.0) -lpthread -lm -lz \
        -o "$output_dir/$probe"
done

printf 'built disposable smoke probes in %s\n' "$output_dir"
