// Same algorithm implemented at four SIMD tiers via #ifdef:
//
//   weighted dot product   s = sum(x[i] * w[i])
//
// over an L1-resident working set (2 x 16 KiB), with multiple independent
// accumulator chains so every variant is execution-throughput-bound rather
// than FP-latency-bound.
//
// Build matrix (see Makefile):
//   -DUSE_SCALAR  -O3 -fno-tree-vectorize -fno-tree-slp-vectorize
//   -DUSE_SSE     -msse4.2                (128-bit, mul+add, no FMA)
//   -DUSE_AVX     -mavx2 -mfma            (256-bit FMA)
//   -DUSE_AVX512  -mavx512f -mavx512dq    (512-bit FMA)
//
// Usage: simd_levels_<tier> <fixed-pass-count>
// A fixed pass count (not fixed wall time) makes instruction/cycle/IPC
// counts directly comparable across tiers.

#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <ctime>

#if defined(USE_SSE)
#include <xmmintrin.h>
#elif defined(USE_AVX) || defined(USE_AVX512)
#include <immintrin.h>
#endif

#if !(defined(USE_SCALAR) || defined(USE_SSE) || defined(USE_AVX) || defined(USE_AVX512))
#error "define one of USE_SCALAR / USE_SSE / USE_AVX / USE_AVX512"
#endif

namespace {

constexpr size_t kN = 2048;  // 2 x 16 KiB -> both arrays fit in L1D

alignas(64) double g_x[kN];
alignas(64) double g_w[kN];

void init_arrays() {
    uint64_t rng = 0x243F6A8885A308D3ULL;
    auto next = [&rng]() {
        rng ^= rng << 13;
        rng ^= rng >> 7;
        rng ^= rng << 17;
        return rng;
    };
    for (size_t i = 0; i < kN; ++i) {
        g_x[i] = (double)(next() >> 11) / (double)(1ULL << 52);
        g_w[i] = (double)(next() >> 11) / (double)(1ULL << 52);
    }
}

#if defined(USE_SCALAR)

double kernel() {
    double a0 = 0, a1 = 0, a2 = 0, a3 = 0;
    for (size_t i = 0; i < kN; i += 4) {
        a0 += g_x[i + 0] * g_w[i + 0];
        a1 += g_x[i + 1] * g_w[i + 1];
        a2 += g_x[i + 2] * g_w[i + 2];
        a3 += g_x[i + 3] * g_w[i + 3];
    }
    return (a0 + a1) + (a2 + a3);
}

#elif defined(USE_SSE)

// 128-bit: separate mul/add (pre-FMA era semantics), 4 accumulator chains
double kernel() {
    __m128d a0 = _mm_setzero_pd();
    __m128d a1 = _mm_setzero_pd();
    __m128d a2 = _mm_setzero_pd();
    __m128d a3 = _mm_setzero_pd();
    for (size_t i = 0; i < kN; i += 8) {
        a0 = _mm_add_pd(a0, _mm_mul_pd(_mm_load_pd(&g_x[i + 0]), _mm_load_pd(&g_w[i + 0])));
        a1 = _mm_add_pd(a1, _mm_mul_pd(_mm_load_pd(&g_x[i + 2]), _mm_load_pd(&g_w[i + 2])));
        a2 = _mm_add_pd(a2, _mm_mul_pd(_mm_load_pd(&g_x[i + 4]), _mm_load_pd(&g_w[i + 4])));
        a3 = _mm_add_pd(a3, _mm_mul_pd(_mm_load_pd(&g_x[i + 6]), _mm_load_pd(&g_w[i + 6])));
    }
    a0 = _mm_add_pd(a0, a1);
    a2 = _mm_add_pd(a2, a3);
    a0 = _mm_add_pd(a0, a2);
    __m128d sh = _mm_unpackhi_pd(a0, a0);
    return _mm_cvtsd_f64(_mm_add_sd(a0, sh));
}

#elif defined(USE_AVX)

// 256-bit FMA: one fused multiply-add per 4 doubles
double kernel() {
    __m256d a0 = _mm256_setzero_pd();
    __m256d a1 = _mm256_setzero_pd();
    __m256d a2 = _mm256_setzero_pd();
    __m256d a3 = _mm256_setzero_pd();
    for (size_t i = 0; i < kN; i += 16) {
        a0 = _mm256_fmadd_pd(_mm256_load_pd(&g_x[i + 0]), _mm256_load_pd(&g_w[i + 0]), a0);
        a1 = _mm256_fmadd_pd(_mm256_load_pd(&g_x[i + 4]), _mm256_load_pd(&g_w[i + 4]), a1);
        a2 = _mm256_fmadd_pd(_mm256_load_pd(&g_x[i + 8]), _mm256_load_pd(&g_w[i + 8]), a2);
        a3 = _mm256_fmadd_pd(_mm256_load_pd(&g_x[i + 12]), _mm256_load_pd(&g_w[i + 12]), a3);
    }
    a0 = _mm256_add_pd(a0, a1);
    a2 = _mm256_add_pd(a2, a3);
    a0 = _mm256_add_pd(a0, a2);
    __m128d lo = _mm256_castpd256_pd128(a0);
    __m128d hi = _mm256_extractf128_pd(a0, 1);
    __m128d s = _mm_add_pd(lo, hi);
    s = _mm_add_sd(s, _mm_unpackhi_pd(s, s));
    return _mm_cvtsd_f64(s);
}

#elif defined(USE_AVX512)

// 512-bit FMA: one fused multiply-add per 8 doubles
double kernel() {
    __m512d a0 = _mm512_setzero_pd();
    __m512d a1 = _mm512_setzero_pd();
    __m512d a2 = _mm512_setzero_pd();
    __m512d a3 = _mm512_setzero_pd();
    for (size_t i = 0; i < kN; i += 32) {
        a0 = _mm512_fmadd_pd(_mm512_load_pd(&g_x[i + 0]), _mm512_load_pd(&g_w[i + 0]), a0);
        a1 = _mm512_fmadd_pd(_mm512_load_pd(&g_x[i + 8]), _mm512_load_pd(&g_w[i + 8]), a1);
        a2 = _mm512_fmadd_pd(_mm512_load_pd(&g_x[i + 16]), _mm512_load_pd(&g_w[i + 16]), a2);
        a3 = _mm512_fmadd_pd(_mm512_load_pd(&g_x[i + 24]), _mm512_load_pd(&g_w[i + 24]), a3);
    }
    a0 = _mm512_add_pd(a0, a1);
    a2 = _mm512_add_pd(a2, a3);
    a0 = _mm512_add_pd(a0, a2);
    return _mm512_reduce_add_pd(a0);
}

#endif

}  // namespace

int main(int argc, char** argv) {
#if defined(USE_AVX512)
    if (!__builtin_cpu_supports("avx512f")) {
        fprintf(stderr, "NOAVX512: CPU lacks AVX-512\n");
        return 42;
    }
#endif

    init_arrays();

    uint64_t passes = argc > 1 ? strtoull(argv[1], nullptr, 10) : 20000;

    timespec ts0, ts1;
    clock_gettime(CLOCK_MONOTONIC, &ts0);

    double sink = 0.0;
    for (uint64_t p = 0; p < passes; ++p) {
        sink += kernel();
    }

    clock_gettime(CLOCK_MONOTONIC, &ts1);
    double el = (double)(ts1.tv_sec - ts0.tv_sec) +
                (double)(ts1.tv_nsec - ts0.tv_nsec) * 1e-9;

    printf("iters=%llu sink=%.6f elapsed=%.3f\n",
           (unsigned long long)passes, sink, el);
    return 0;
}
