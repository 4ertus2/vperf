// Runs all four SIMD tiers (scalar, SSE, AVX, AVX-512) in separate threads.
// Each thread gets its own copy of the working set so there is no sharing.
//
// Build: make simd_levels_all  (needs -pthread -mavx512f -mavx512dq)
// Usage: simd_levels_all <passes-per-thread>

#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <thread>
#include <vector>

#if defined(__linux__)
#include <pthread.h>
static void set_thread_name(const char* name) {
    pthread_setname_np(pthread_self(), name);
}
#else
static void set_thread_name(const char*) {}
#endif

#if defined(__AVX512F__)
#include <immintrin.h>
#endif

namespace {

constexpr size_t kN = 2048;

void init_arrays(double* x, double* w) {
    uint64_t rng = 0x243F6A8885A308D3ULL;
    auto next = [&rng]() {
        rng ^= rng << 13;
        rng ^= rng >> 7;
        rng ^= rng << 17;
        return rng;
    };
    for (size_t i = 0; i < kN; ++i) {
        x[i] = (double)(next() >> 11) / (double)(1ULL << 52);
        w[i] = (double)(next() >> 11) / (double)(1ULL << 52);
    }
}

// ---- scalar kernel --------------------------------------------------------

double kernel_scalar(const double* x, const double* w) {
    double a0 = 0, a1 = 0, a2 = 0, a3 = 0;
    for (size_t i = 0; i < kN; i += 4) {
        a0 += x[i + 0] * w[i + 0];
        a1 += x[i + 1] * w[i + 1];
        a2 += x[i + 2] * w[i + 2];
        a3 += x[i + 3] * w[i + 3];
    }
    return (a0 + a1) + (a2 + a3);
}

// ---- SSE kernel -----------------------------------------------------------

#if defined(__SSE4_2__) || defined(__SSE2__)
#include <xmmintrin.h>

double kernel_sse(const double* x, const double* w) {
    __m128d a0 = _mm_setzero_pd();
    __m128d a1 = _mm_setzero_pd();
    __m128d a2 = _mm_setzero_pd();
    __m128d a3 = _mm_setzero_pd();
    for (size_t i = 0; i < kN; i += 8) {
        a0 = _mm_add_pd(a0, _mm_mul_pd(_mm_load_pd(&x[i + 0]), _mm_load_pd(&w[i + 0])));
        a1 = _mm_add_pd(a1, _mm_mul_pd(_mm_load_pd(&x[i + 2]), _mm_load_pd(&w[i + 2])));
        a2 = _mm_add_pd(a2, _mm_mul_pd(_mm_load_pd(&x[i + 4]), _mm_load_pd(&w[i + 4])));
        a3 = _mm_add_pd(a3, _mm_mul_pd(_mm_load_pd(&x[i + 6]), _mm_load_pd(&w[i + 6])));
    }
    a0 = _mm_add_pd(a0, a1);
    a2 = _mm_add_pd(a2, a3);
    a0 = _mm_add_pd(a0, a2);
    __m128d sh = _mm_unpackhi_pd(a0, a0);
    return _mm_cvtsd_f64(_mm_add_sd(a0, sh));
}
#endif

// ---- AVX kernel -----------------------------------------------------------

#if defined(__AVX2__)
#include <immintrin.h>

double kernel_avx(const double* x, const double* w) {
    __m256d a0 = _mm256_setzero_pd();
    __m256d a1 = _mm256_setzero_pd();
    __m256d a2 = _mm256_setzero_pd();
    __m256d a3 = _mm256_setzero_pd();
    for (size_t i = 0; i < kN; i += 16) {
        a0 = _mm256_fmadd_pd(_mm256_load_pd(&x[i + 0]), _mm256_load_pd(&w[i + 0]), a0);
        a1 = _mm256_fmadd_pd(_mm256_load_pd(&x[i + 4]), _mm256_load_pd(&w[i + 4]), a1);
        a2 = _mm256_fmadd_pd(_mm256_load_pd(&x[i + 8]), _mm256_load_pd(&w[i + 8]), a2);
        a3 = _mm256_fmadd_pd(_mm256_load_pd(&x[i + 12]), _mm256_load_pd(&w[i + 12]), a3);
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
#endif

// ---- AVX-512 kernel -------------------------------------------------------

#if defined(__AVX512F__)
#include <immintrin.h>

double kernel_avx512(const double* x, const double* w) {
    __m512d a0 = _mm512_setzero_pd();
    __m512d a1 = _mm512_setzero_pd();
    __m512d a2 = _mm512_setzero_pd();
    __m512d a3 = _mm512_setzero_pd();
    for (size_t i = 0; i < kN; i += 32) {
        a0 = _mm512_fmadd_pd(_mm512_load_pd(&x[i + 0]), _mm512_load_pd(&w[i + 0]), a0);
        a1 = _mm512_fmadd_pd(_mm512_load_pd(&x[i + 8]), _mm512_load_pd(&w[i + 8]), a1);
        a2 = _mm512_fmadd_pd(_mm512_load_pd(&x[i + 16]), _mm512_load_pd(&w[i + 16]), a2);
        a3 = _mm512_fmadd_pd(_mm512_load_pd(&x[i + 24]), _mm512_load_pd(&w[i + 24]), a3);
    }
    a0 = _mm512_add_pd(a0, a1);
    a2 = _mm512_add_pd(a2, a3);
    a0 = _mm512_add_pd(a0, a2);
    return _mm512_reduce_add_pd(a0);
}
#endif

// ---- thread entry points --------------------------------------------------

struct ThreadResult {
    const char* name;
    double sink;
    double elapsed;
};

void run_tier(ThreadResult* res, const char* name,
              double (*kernel)(const double*, const double*),
              uint64_t passes) {
    set_thread_name(name);
    alignas(64) double x[kN], w[kN];
    init_arrays(x, w);

    timespec ts0, ts1;
    clock_gettime(CLOCK_MONOTONIC, &ts0);

    double sink = 0.0;
    for (uint64_t p = 0; p < passes; ++p) {
        sink += kernel(x, w);
    }

    clock_gettime(CLOCK_MONOTONIC, &ts1);
    res->name = name;
    res->sink = sink;
    res->elapsed = (double)(ts1.tv_sec - ts0.tv_sec) +
                   (double)(ts1.tv_nsec - ts0.tv_nsec) * 1e-9;
}

}  // namespace

int main(int argc, char** argv) {
    uint64_t passes = argc > 1 ? strtoull(argv[1], nullptr, 10) : 5000000;

    std::vector<ThreadResult> results(4);
    std::vector<std::thread> threads;

    threads.emplace_back(run_tier, &results[0], "scalar", kernel_scalar, passes);

#if defined(__SSE2__) || defined(__SSE4_2__)
    threads.emplace_back(run_tier, &results[1], "sse", kernel_sse, passes);
#endif

#if defined(__AVX2__)
    threads.emplace_back(run_tier, &results[2], "avx", kernel_avx, passes);
#endif

#if defined(__AVX512F__)
    if (__builtin_cpu_supports("avx512f")) {
        threads.emplace_back(run_tier, &results[3], "avx512", kernel_avx512, passes);
    }
#endif

    for (auto& t : threads) {
        t.join();
    }

    double total_elapsed = 0;
    for (auto& r : results) {
        if (r.name) {
            printf("%s: iters=%llu sink=%.6f elapsed=%.3f\n",
                   r.name, (unsigned long long)passes, r.sink, r.elapsed);
            if (r.elapsed > total_elapsed) total_elapsed = r.elapsed;
        }
    }
    printf("total_elapsed=%.3f\n", total_elapsed);
    return 0;
}
