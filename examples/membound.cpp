// Memory-hierarchy-bound workload: randomized pointer chasing over a large
// buffer. Every hop is a dependent load to a random location, so execution
// is serialized on cache-miss latency.
//
// Expected signature: low IPC, massive L1/L2/LLC misses, heavy dTLB misses
// (random access across many 4 KiB pages), high backend bound.
//
// Build: g++ -O3 -march=x86-64-v3 -std=c++17 -o membound membound.cpp

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>

int main(int argc, char** argv) {
    size_t bytes = argc > 1 ? (size_t)atoll(argv[1]) : (size_t)256 << 20;
    double target_seconds = argc > 2 ? atof(argv[2]) : 2.0;

    size_t n = bytes / sizeof(uint64_t);
    if (n < 16) {
        fprintf(stderr, "usage: membound [bytes>=128] [seconds]\n");
        return 1;
    }
    uint64_t* arr = (uint64_t*)malloc(n * sizeof(uint64_t));
    if (!arr) {
        fprintf(stderr, "allocation of %zu bytes failed\n", bytes);
        return 1;
    }

    // Fisher-Yates permutation with xorshift64: the chase visits every node
    // exactly once per traversal in reproducible random order.
    for (size_t i = 0; i < n; ++i) arr[i] = i;
    uint64_t rng = 0x9E3779B97F4A7C15ULL;
    auto next = [&rng]() {
        rng ^= rng << 13;
        rng ^= rng >> 7;
        rng ^= rng << 17;
        return rng;
    };
    for (size_t i = n - 1; i > 0; --i) {
        size_t j = (size_t)(next() % (i + 1));
        uint64_t t = arr[i];
        arr[i] = arr[j];
        arr[j] = t;
    }

    timespec ts0, ts1;
    clock_gettime(CLOCK_MONOTONIC, &ts0);

    const uint64_t kBatch = 1ULL << 20;  // hops between clock checks
    uint64_t hops = 0;
    uint64_t p = 0;
    for (;;) {
        for (uint64_t k = 0; k < kBatch; ++k) {
            p = arr[p];  // dependent load: nothing else can proceed
        }
        hops += kBatch;

        clock_gettime(CLOCK_MONOTONIC, &ts1);
        double el = (double)(ts1.tv_sec - ts0.tv_sec) +
                    (double)(ts1.tv_nsec - ts0.tv_nsec) * 1e-9;
        if (el >= target_seconds) break;
    }

    printf("membound hops=%llu sink=%llu\n",
           (unsigned long long)hops, (unsigned long long)p);
    free(arr);
    return 0;
}
