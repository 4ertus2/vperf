# vperf — VTune-style CPU profiling on Linux

`vperf` wraps the Linux `perf` tool to reproduce Intel VTune's most valuable
CPU analyses on any machine (AMD and Intel), with zero Python dependencies:

- **Hotspots** — self/inclusive time per function with call stacks (DWARF),
  per-thread breakdown, flame graphs
- **Hardware counters** — instructions retired, clockticks, IPC/CPI,
  branch mispredict %, L1/L2/LLC miss counts and rates, dTLB (page) misses,
  context switches & migrations/s
- **Pipeline bound analysis** — Backend Bound / Frontend Bound
  (VTune's Top-down Microarchitecture Analysis approximated; uses perf's
  TMA-style metrics where available, e.g. AMD Zen `de_no_dispatch_per_slot`)
- **Cache-hierarchy classification** — on AMD Zen, data-source fill events
  (`ls_any_fills_from_sys.*`, `l2_cache_req_stat.*`) attribute every L1 miss
  to its source: L2 hit, local L3 hit, or DRAM/MMIO (= true LLC misses)
- **Effective CPU utilization** — average busy cores + utilization timeline
- **Reports** — rich terminal summary + a single-file interactive
  `report.html` (metric cards, sortable hotspots table, flame graph,
  timelines, call tree)

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

Check readiness (probes access, precise sampling, supported metrics, attach):

```bash
vperf doctor
```

Install:

```bash
pip install .            # or: uv pip install .
```

## Usage

```bash
# profile a command (two passes: counting + sampling)
vperf run -- ./yourapp --arg1 val

# useful flags
vperf run -f 999 -o myprofile -- ./yourapp     # 999 Hz, custom output dir
vperf run --callgraph fp -- ./yourapp          # frame-pointer unwinding
vperf run --no-record -- ./yourapp             # counters only (fast)

# attach to a running process (needs CAP_PERFMON/CAP_SYS_PTRACE, see doctor)
vperf attach -p 1234 --duration 10

# regenerate terminal summary + report.html from artifacts
vperf report .vperf/run_20260824_021912
```

Open `report.html` in any browser — fully offline, no CDN.

### Reading the output like a VTune veteran

| Signal | Interpretation |
|---|---|
| Effective CPU Utilization ≪ cores | serial or I/O-bound; threading opportunity |
| IPC ≥ 2 | compute-bound, executing efficiently |
| IPC < 0.5 | stalled; look at bound analysis below |
| Backend Bound high | memory hierarchy limited → check LLC/L1D/dTLB miss rates |
| Frontend Bound high | fetch/decode limited (i-cache, big code footprint) |
| Branch Mispredict % > 5 | unpredictable branches dominate |
| Hotspots `[kernel]` heavy | syscalls/page faults; consider off-CPU analysis |

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
.venv/bin/python -m pytest tests/ -q   # unit + integration (needs perf access)
ruff check src/
```

Examples in `examples/` are C++ workloads with opposite, well-understood
hardware signatures (used by the integration tests):

```bash
examples/build.sh                       # g++ -O3 -march=x86-64-v3 -> examples/bin/
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
