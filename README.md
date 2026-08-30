# vperf — VTune-style CPU profiling on Linux

`vperf` wraps the Linux `perf` tool to reproduce Intel VTune's most valuable
CPU analyses on amd64 machine (AMD and Intel), with zero Python dependencies:

- **Hotspots** — self/inclusive time per function with call stacks (DWARF),
  per-thread breakdown, flame graphs
- **Hardware counters** — instructions retired, clockticks, IPC/CPI,
  branch mispredict %, L1/L2/LLC miss counts and rates, dTLB (page) misses,
  context switches & migrations/s
- **Pipeline bound analysis** — Backend Bound / Frontend Bound
  (VTune's Top-down Microarchitecture Analysis approximated; uses perf's
  TMA-style metrics where available)
- **Cache-hierarchy classification** — on AMD Zen, data-source fill events
  (`ls_any_fills_from_sys.*`, `l2_cache_req_stat.*`) attribute every L1 miss
  to its source: L2 hit, local L3 hit, or DRAM/MMIO (= true LLC misses)
- **Effective CPU utilization** — average busy cores + utilization timeline
- **Reports** — terminal summary + a single-file interactive `report.html`
  (metric overview, hotspots table, memery access summary, flame graph,
  timelines, call tree, threads)

Artifacts (`stat.csv`, `perf.data`, `script.txt`, `meta.json`) are kept in the
profile directory so reports can be regenerated any time with `vperf report`.

## Setup

Requirements: Linux, `perf` (linux-tools), Python ≥ 3.10.

```bash
# one-time: allow user-space profiling
sudo sysctl kernel.perf_event_paranoid=1      # -1 also enables full kernel sampling
# persistent:
echo 'kernel.perf_event_paranoid=1' | sudo tee /etc/sysctl.d/99-perf.conf
```

### Install with uv (recommended)

```bash
# Create a virtual environment (required on Ubuntu/Debian).
uv venv

# Install vperf in editable mode inside the venv
uv pip install -e .

# Activate the venv so `vperf` is on your PATH
source .venv/bin/activate

# Verify everything is ready (probes access, metrics, attach capability)
vperf doctor
```

After `source .venv/bin/activate`, `vperf` works like any system command.
To leave the venv: `deactivate`. To re-enter later: `source .venv/bin/activate`.

### Install with pip (if you manage your own venv)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install .
vperf doctor
```

## Usage

```bash
vperf run <vperf-args> -- ./yourapp <yourapp-args>
```

```bash
# run with differet options
vperf run -- ./yourapp                            # profile with defaults
vperf run -o baseline -- ./yourapp input.bin      # save to a named directory
vperf run -f 999 -- ./yourapp input.bin           # higher sampling frequency
vperf run --no-record -- ./yourapp input.bin      # counters only (faster, no call stacks)
vperf run --no-memory --no-wait -- ./yourapp      # skip optional passes
vperf run --callgraph fp -- ./yourapp             # frame-pointer unwinding (no debug info needed)

# compare two runs
vperf diff .vperf/baseline .vperf/optimized

# attach to a running process (needs CAP_PERFMON, see doctor)
vperf attach -p 1234 --duration 10

# regenerate summary + report.html from saved artifacts
vperf report .vperf/run_20260824_021912
```

Open `report.html` in any browser — fully offline, no CDN.

### Reading the output like a VTune veteran

| Signal | Interpretation |
|---|---|
| **Headline** | |
| Effective CPU Utilization ≪ cores | serial or I/O-bound; threading opportunity |
| CPU Time ≈ Elapsed × cores | compute-bound; latency-dominated |
| **Pipeline** | |
| IPC ≥ 2 | compute-bound, executing efficiently |
| IPC < 0.5 | stalled; look at bound analysis below |
| Backend Bound high | memory hierarchy limited → check LLC/L1D/dTLB miss rates |
| Frontend Bound high | fetch/decode limited (i-cache, big code footprint) |
| Bad Speculation > 5% | branch mispredicts or machine clears wasting cycles |
| Retiring < 30% | most pipeline slots lost; deep stall or contention |
| **Branches** | |
| Branch Mispredict % > 5 | unpredictable branches dominate |
| **Memory hierarchy** | |
| LLC Miss % > 30% | working set exceeds cache; DRAM-bound |
| L1D Miss Rate > 5% | data-cache thrashing; blocking/tiling opportunity |
| dTLB Miss Rate > 1% | page-table walks hurting latency |
| **HPC / vectorization** | |
| Vectorization Ratio < 50% | scalar or mixed-width code; widen with intrinsics or compiler hints |
| FP Ops/s ≈ theoretical peak | compute-saturated; check memory won't help |
| Backend Bound + high FP Ops/s | memory-bound despite vectorization (common with large arrays) |
| **OS noise** | |
| Context Switches/s > 100 | scheduling pressure; pin threads or increase work per task |
| Page Faults/s high | first-touch allocation or huge-page opportunity |
| **Memory access (IBS, AMD)** | |
| DRAM access % high | true LLC misses; optimize data layout |
| L1 access % ≈ 100% | working set fits in cache |
| Avg latency > 200 cyc | deep memory stalls; prefetching or data restructuring needed |
| **Hotspots** | |
| Hotspots `[kernel]` heavy | syscalls/page faults; consider off-CPU analysis |
| One function > 50% self | clear #1 target; optimize or vectorize that function |
| Inlined functions dominate | compiler flattened the call tree; inspect unrolled loops |

## How it works

1. **Capability probe** — tiny throwaway runs determine supported events,
   `-M` metrics and the best precise cycles event (`cycles:P` → fallbacks).
2. **Counting pass** — `perf stat -x,` with all counters + metrics.
3. **Sampling pass** — `perf record -F 199 -e <precise> --call-graph dwarf`.
4. **Post-processing** — `perf script` dump parsed in pure Python; stacks are
   folded into self/inclusive times; metrics derived; HTML/SVG rendered.

Notes & caveats:
- Two passes means the workload runs twice (VTune does the same for some
  analyses). Use `--no-stat`/`--no-record` for single-pass runs.
- Multiplexing: counters share PMU registers; perf scales counts, but
  ratios across different groups carry some noise.
- DWARF unwinding is done offline; `DEBUGINFOD_URLS` is stripped from perf's
  environment to prevent multi-second network hangs.
- True Intel TMA level-1/2 requires Intel's `slots` PMU. On AMD the backend /
  frontend bound numbers use AMD dispatch-stall equivalents — directionally
  comparable, not identical definitions.
- Attach mode (`-p`) needs `CAP_PERFMON`/`CAP_SYS_PTRACE`
  (`sudo setcap cap_perfmon,cap_sys_ptrace+ep $(which perf)`) on recent kernels.

## Development

```bash
uv venv && uv pip install -e . pytest ruff
source .venv/bin/activate
vperf doctor                           # verify setup
pytest tests/ -q                       # unit + integration (needs perf access)
ruff check vperf/ tests/

# Note: run the suite WITHOUT pytest-xdist/-n. The integration tests assert
# exact PMU counter relationships; concurrent profiling sessions multiplex
# the hardware counters and break those assertions.
```

Examples in `examples/` are C++ workloads with opposite, well-understood
hardware signatures (used by the integration tests):

```bash
make -C examples                      # g++ -O3 -march=x86-64-v3 -> examples/bin/
vperf run -- examples/bin/simd_levels_avx 2000000
                                        # AVX2 dot product, L1-resident arrays
                                        #   -> IPC ~2.9, ~zero cache/TLB misses
vperf run -- examples/bin/membound 268435456 1.5
                                        # dependent-load pointer chase, 256 MiB
                                        #   -> IPC ~0.14, huge L1/L2/LLC misses,
                                        #      dTLB thrash, first-touch page faults
```

`tests/test_integration_cpp.py` asserts these signatures: SIMD IPC > 2,
chase IPC < 0.35 (>5x contrast), L1/L2 misses > 10M with 10x contrast,
LLC miss rate > 45% for the chase, dTLB misses > 5M, and page faults
covering every 4 KiB page of the mapping.

`examples/simd_levels.cpp` implements the *same* weighted dot product at
four widths via `#ifdef` (scalar / SSE / AVX / AVX-512). The tier tests
document real Zen 4 physics: instructions-per-pass halve with each widening,
cycles shrink until AVX then flatten (double-pumped 512-bit ops), so
AVX-512 IPC drops to ~half of AVX's — a reminder that IPC compares
instructions, not work.

Other workloads: `sleeper` (100 ms spin + 200 ms usleep — wait-analysis
signature: ~2/3 of the window asleep).

## Cycle mode: before/after comparisons with ministat

Single runs are noisy. `vperf cycle` repeats a target N times and writes a
TSV matrix (rows = runs, columns = metrics) for statistical comparison:

```bash
# baseline: current build
vperf cycle -n 30 -- ./myapp > base.tsv

# after your optimization
vperf cycle -n 30 -- ./myapp-fixed > fixed.tsv

# compare one metric column (IPC is column 4)
awk -F'\t' 'NR>1{print $4}' base.tsv  > base_ipc.txt
awk -F'\t' 'NR>1{print $4}' fixed.tsv > fix_ipc.txt
ministat base_ipc.txt fix_ipc.txt
```

ministat prints N/min/max/median/avg/stddev per dataset plus a
"Difference at 95.0% confidence" verdict when the delta is real.

Options: `-n` measured runs (default 30), `--warmup` discarded runs
(default 1), `-j` parallel runs (default = half your logical CPUs, pinned
round-robin to distinct physical cores to exclude SMT sibling contention,
`--no-pin` disables), `--metrics ipc,llc_miss_pct,...` selects columns,
`--tsv FILE` instead of stdout. Progress and per-metric summary go to
stderr, so `> file.tsv` stays clean.

The cycle integration test does exactly this across SIMD tiers:
30-run scalar vs AVX-512 datasets must differ significantly in both
elapsed time (AVX-512 faster) and IPC (lower — double-pump).
