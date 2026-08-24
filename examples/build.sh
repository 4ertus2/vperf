#!/usr/bin/env bash
# Build the C++ example workloads used by the integration tests.
# Skips compilation when the binary is already newer than its sources.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p bin

CXXFLAGS="-O3 -march=x86-64-v3 -std=c++17 -Wall"

fresh() {  # fresh BIN SRC... : true when bin newer than every source
    [ -x "$1" ] || return 1
    local bin="$1"; shift
    local src
    for src in "$@"; do
        [ "$bin" -nt "$src" ] || return 1
    done
}

build() {  # build BIN SRC [EXTRA_FLAGS...]
    local bin="$1"; shift
    local src="$1"; shift
    if fresh "bin/$bin" "$src"; then
        return 0
    fi
    g++ $CXXFLAGS "$@" -o "bin/$bin" "$src"
}

build membound   membound.cpp
build sleeper    sleeper.cpp

build simd_levels_scalar simd_levels.cpp \
    -fno-tree-vectorize -fno-tree-slp-vectorize -DUSE_SCALAR
build simd_levels_sse    simd_levels.cpp -msse4.2 -DUSE_SSE
build simd_levels_avx    simd_levels.cpp -mavx2 -mfma -DUSE_AVX
if g++ $CXXFLAGS -mavx512f -mavx512dq \
       -DUSE_AVX512 -fsyntax-only simd_levels.cpp 2>/dev/null; then
    build simd_levels_avx512 simd_levels.cpp \
        -mavx512f -mavx512dq -DUSE_AVX512
fi

echo "built: $(ls bin | tr '\n' ' ')"
