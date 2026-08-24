// SIMD compute-bound workload: AVX2 packed-double reductions over a
// cache-resident working set. Expected signature: very high IPC,
// near-zero cache/TLB miss rates.
//
// Build: g++ -O3 -march=x86-64-v3 -std=c++17 -o simd simd.cpp

#include <immintrin.h>

#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <ctime>

namespace {

inline double hsum4(__m256d a, __m256d b, __m256d c, __m256d d) {
    __m256d ab = _mm256_add_pd(a, b);
    __m256d cd = _mm256_add_pd(c, d);
    __m256d s = _mm256_add_pd(ab, cd);
    __m128d lo = _mm256_castpd256_pd128(s);
    __m128d hi = _mm256_extractf128_pd(s, 1);
    __m128d sum = _mm_add_pd(lo, hi);
    sum = _mm_add_sd(sum, _mm_unpackhi_pd(sum, sum));
    return _mm_cvtsd_f64(sum);
}

}  // namespace

int main(int argc, char** argv) {
    if (!__builtin_cpu_supports("avx2")) {
        fprintf(stderr, "AVX2 not supported on this CPU\n");
        return 1;
    }

    // 16 KiB of doubles: fits entirely in L1D (32 KiB), so the kernel is
    // purely execution-throughput-bound with no memory-hierarchy noise.
    constexpr size_t kN = 2048;
    alignas(64) static double x[kN];

    uint64_t seed = 12345;
    for (size_t i = 0; i < kN; ++i) {
        seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
        x[i] = (double)((seed >> 11) & ((1ULL << 40) - 1)) / (double)(1ULL << 40);
    }

    double target_seconds = argc > 1 ? atof(argv[1]) : 2.0;

    timespec ts0, ts1;
    clock_gettime(CLOCK_MONOTONIC, &ts0);

    uint64_t iters = 0;
    double sink = 0.0;
    for (;;) {
        // 8 independent accumulators break the FP add latency chain so the
        // kernel is throughput-bound (loads/FADDs), not latency-bound.
        __m256d a0 = _mm256_setzero_pd();
        __m256d a1 = _mm256_setzero_pd();
        __m256d a2 = _mm256_setzero_pd();
        __m256d a3 = _mm256_setzero_pd();
        __m256d a4 = _mm256_setzero_pd();
        __m256d a5 = _mm256_setzero_pd();
        __m256d a6 = _mm256_setzero_pd();
        __m256d a7 = _mm256_setzero_pd();
        for (size_t i = 0; i < kN; i += 64) {  // 64 doubles per iter
            a0 = _mm256_add_pd(a0, _mm256_load_pd(&x[i + 0]));
            a1 = _mm256_add_pd(a1, _mm256_load_pd(&x[i + 8]));
            a2 = _mm256_add_pd(a2, _mm256_load_pd(&x[i + 16]));
            a3 = _mm256_add_pd(a3, _mm256_load_pd(&x[i + 24]));
            a4 = _mm256_add_pd(a4, _mm256_load_pd(&x[i + 32]));
            a5 = _mm256_add_pd(a5, _mm256_load_pd(&x[i + 40]));
            a6 = _mm256_add_pd(a6, _mm256_load_pd(&x[i + 48]));
            a7 = _mm256_add_pd(a7, _mm256_load_pd(&x[i + 56]));
        }
        sink += hsum4(_mm256_add_pd(_mm256_add_pd(a0, a1), _mm256_add_pd(a2, a3)),
                      _mm256_add_pd(_mm256_add_pd(a4, a5), _mm256_add_pd(a6, a7)),
                      _mm256_setzero_pd(), _mm256_setzero_pd());
        ++iters;

        if ((iters & 0x3F) == 0) {  // check the clock every 64 iterations
            clock_gettime(CLOCK_MONOTONIC, &ts1);
            double el = (double)(ts1.tv_sec - ts0.tv_sec) +
                        (double)(ts1.tv_nsec - ts0.tv_nsec) * 1e-9;
            if (el >= target_seconds) break;
        }
    }

    printf("simd iters=%llu sink=%.3f\n",
           (unsigned long long)iters, sink);
    return 0;
}
