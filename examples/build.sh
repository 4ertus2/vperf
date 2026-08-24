#!/usr/bin/env bash
# Build the C++ example workloads used by the integration tests.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p bin

CXXFLAGS="-O3 -march=x86-64-v3 -std=c++17 -Wall"

g++ $CXXFLAGS -o bin/simd simd.cpp
g++ $CXXFLAGS -o bin/membound membound.cpp
g++ $CXXFLAGS -o bin/sleeper sleeper.cpp

# same algorithm, four SIMD tiers (see simd_levels.cpp header)
g++ $CXXFLAGS -fno-tree-vectorize -fno-tree-slp-vectorize \
    -DUSE_SCALAR -o bin/simd_levels_scalar simd_levels.cpp
g++ $CXXFLAGS -msse4.2 \
    -DUSE_SSE -o bin/simd_levels_sse simd_levels.cpp
g++ $CXXFLAGS -mavx2 -mfma \
    -DUSE_AVX -o bin/simd_levels_avx simd_levels.cpp
if g++ $CXXFLAGS -mavx512f -mavx512dq \
    -DUSE_AVX512 -o bin/simd_levels_avx512 simd_levels.cpp 2>/dev/null; then
    : # assembler/compiler lacks AVX-512 support: tier simply stays absent
fi

echo "built: $(ls bin | tr '\n' ' ')"
