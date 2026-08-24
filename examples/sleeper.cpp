// Deterministic latency-bound workload for wait-analysis tests:
// alternates 100 ms of busy computation with 200 ms of usleep.
// Expected signature under sched:sched_stat_* tracing:
//   sleep share ~2/3 of window, on-CPU share ~1/3, blocked ~0.
//
// Build: g++ -O3 -march=x86-64-v3 -std=c++17 -o sleeper sleeper.cpp

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <unistd.h>

static double spin_ms(int ms) {
    timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    double sink = 0.0;
    for (;;) {
        for (int i = 0; i < 200000; ++i) {
            sink += i * 0.5;
        }
        clock_gettime(CLOCK_MONOTONIC, &t1);
        double el = (double)(t1.tv_sec - t0.tv_sec) +
                    (double)(t1.tv_nsec - t0.tv_nsec) * 1e-9;
        if (el >= ms / 1000.0) break;
    }
    return sink;
}

int main(int argc, char** argv) {
    double target_seconds = argc > 1 ? atof(argv[1]) : 1.8;

    timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    double sink = 0.0;
    int rounds = 0;
    for (;;) {
        sink += spin_ms(100);
        usleep(200000);   // voluntary sleep -> sched_stat_sleep
        ++rounds;

        clock_gettime(CLOCK_MONOTONIC, &t1);
        double el = (double)(t1.tv_sec - t0.tv_sec) +
                    (double)(t1.tv_nsec - t0.tv_nsec) * 1e-9;
        if (el >= target_seconds) break;
    }

    printf("sleeper rounds=%d sink=%.3f\n", rounds, sink);
    return 0;
}
