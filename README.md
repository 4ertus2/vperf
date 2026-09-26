# vperf — VTune-style CPU profiling on Linux

`vperf` wraps the Linux `perf` tool to reproduce Intel VTune's most valuable
CPU analyses on amd64 machine (AMD and Intel), with zero Python dependencies:

- **Hotspots** — self/inclusive time per function with frame-pointer call
  stacks by default, optional DWARF, per-thread breakdown, flame graphs
- **Hardware counters** — instructions retired, clockticks, IPC/CPI,
  branch mispredict %, L1/L2/LLC miss counts and rates, dTLB (page) misses,
  context switches & migrations/s
- **Pipeline bound analysis** — Backend Bound / Frontend Bound
  (VTune's Top-down Microarchitecture Analysis approximated; uses perf's
  TMA-style metrics where available, otherwise `stalled-cycles-*` ratios)
- **Cache-hierarchy classification** — on AMD Zen, data-source fill events
  (`ls_any_fills_from_sys.*`, `l2_cache_req_stat.*`) attribute every L1 miss
  to its source: local L3 hit or DRAM/MMIO (= true LLC misses). On Intel the
  generic `LLC-loads` / `LLC-load-misses` pair is used instead; that pair
  measures L1 misses that reached L3, not DRAM traffic, and the report labels
  which definition produced each number
- **Memory-access profiling** — AMD IBS or Intel PEBS, whichever the host
  exposes (see `vperf doctor`)
- **Effective CPU utilization** — average busy cores + utilization timeline
- **Reports** — terminal summary + a single-file interactive `report.html`
  (metric overview, hotspots table, memory access summary, click-to-zoom flame
  graph, timelines, call tree, threads). The HTML thread selector scopes CPU
  views, the Overview metrics, and the IBS/PEBS Memory tab to the selected
  thread. The Threads tab merges the per-thread CPU and wait tables: each row
  carries sampled cycles next to on/off-CPU seconds, joined on tid, and the
  wait columns read `n/a` when scheduler tracepoints were not collected.

Artifacts (`stat.csv` or `stat_threads.csv`, `perf.data`, `script.txt`,
`meta.json`) are kept in the profile directory so reports can be regenerated
any time with `vperf report`. `meta.json` records the CPU vendor, so a profile
collected on AMD and re-reported on Intel (or the reverse) keeps the vendor
calibrated constants it was collected with.

## CPU vendor support

| Area | AMD (Zen) | Intel |
|---|---|---|
| Memory-access sampling | `ibs_op` (sampling *period*, default 100003) | `mem-loads`/`mem-stores` PEBS (load-latency threshold, default `--ldlat 30`) |
| LLC classification | `ls_any_fills_from_sys.*` — DRAM/MMIO fills per L3 lookup | `LLC-loads`/`LLC-load-misses` — L1 misses that reached L3 |
| L1 / L2 miss counts | `ls_any_fills_from_sys.all`, `l2_cache_req_stat.ic_dc_miss_in_l2` | not exposed; `L1-dcache-load-misses` drives the L1D miss *rate* only |
| FP / vectorization | `fp_ret_sse_avx_ops.*`, `fp_ops_retired_by_width.*` | not exposed; Overview omits the FP rows |
| Branch-mispredict penalty (Bad Speculation model) | 13 cyc | 15 cyc |

Only `AMD_ONLY_EVENTS` are vendor-gated: on Intel and on unrecognised vendors
they are never requested, so `perf stat` does not emit `<not counted>` noise.
Everything else is collected and reported identically on both vendors.

`vperf run` / `vperf attach` expose `--mem-period` to set the AMD IBS sampling
period (default 100003 cycles). Memory samples scale linearly with the run
length divided by the period, so raise it for long-running targets: at
100003 a 60 s multi-threaded target produces millions of IBS samples, which
also makes `perf script` / `perf mem report` proportionally slower.

`--no-inline` drops DWARF inline expansion from both `perf script` and
`perf mem report`. Self time then lands on the enclosing (non-inlined)
function instead of the innermost inlined callee. The trade is worth it on
huge C++ targets: a ClickHouse debug build (4.9 GB, 1.5 M symbols) spends
~80 s per `perf` invocation expanding inlines versus ~2 s without, and that
cost is paid twice per profile.


## Setup

Requirements: Linux, `perf` (linux-tools), Python ≥ 3.10.

```bash
# one-time: allow user-space profiling
sudo sysctl kernel.perf_event_paranoid=1      # -1 also enables full kernel sampling
# persistent:
echo 'kernel.perf_event_paranoid=1' | sudo tee /etc/sysctl.d/99-perf.conf
```

### Enable Wait / off-CPU analysis

The Wait report uses scheduler tracepoints, not only PMU counters. On systems
where tracefs event files remain root-only, `kernel.perf_event_paranoid=0` and
a tracefs remount may not be enough. Enable tracepoint access for the user who
runs `vperf`:

`CAP_DAC_READ_SEARCH` lets `perf` read root-owned tracefs event metadata when
remounting the tracefs mount does not change the individual file permissions.

```bash
sudo sysctl -w kernel.perf_event_paranoid=0
sudo mount -o remount,mode=755 /sys/kernel/tracing/
```

Now grant the capabilities — **to the perf binary that actually executes**:

```bash
sudo setcap cap_perfmon,cap_sys_ptrace,cap_dac_read_search=ep "$(command -v perf)"
```

On Debian and Ubuntu that is not enough. `/usr/bin/perf` is a shell wrapper that
`exec`s a versioned ELF, and **the kernel ignores file capabilities on a
script** — only the interpreter is executed, and `bash` has no capabilities. So
`setcap` on `/usr/bin/perf` succeeds, `getcap` reports the caps, and `perf` still
runs with an empty capability set. Point `setcap` at the ELF instead:

```bash
sudo setcap cap_perfmon,cap_sys_ptrace,cap_dac_read_search=ep \
  "/usr/lib/linux-tools/$(uname -r)/perf"
```

`vperf doctor` detects this case and prints the exact command for your host, so
the shortest path is to run it and copy the `setcap` line it reports:

```bash
vperf doctor
```

Verify access before running a profile:

```bash
perf stat -e sched:sched_switch -- true
vperf doctor
```

The `sysctl` and tracefs changes are temporary. For a persistent Wait setting,
use `kernel.perf_event_paranoid=0` in `/etc/sysctl.d/99-perf.conf` and reapply
the tracefs mount after reboot. The `setcap` is permanent until the `perf`
package is upgraded, which replaces the binary and drops the xattr.

A profile collected before access was enabled has no `wait.txt`; rerun the
profiling command to generate a new Wait report.

### Run without installing (no venv)

`vperf` has **zero runtime dependencies** — everything it imports is in the
Python standard library, so a virtualenv buys you nothing but an isolated
place to put pytest and ruff. To run straight from a checkout with the system
`python3`:

```bash
cd /path/to/vperf

python3 -m vperf doctor
python3 -m vperf run -- ./yourapp
python3 -m vperf report .vperf/run_20260824_021912
```

`python3 -m vperf` runs from the repository root. From anywhere else, put the
checkout on the import path:

```bash
PYTHONPATH=/path/to/vperf python3 -m vperf run -- ./yourapp
```

Or make a one-line wrapper, so you can type `vperf` without a venv:

```bash
printf '#!/bin/sh\nexec python3 -m vperf "$@"\n' > ~/.local/bin/vperf
chmod +x ~/.local/bin/vperf
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.bashrc to persist
```

The examples in this README use the bare `vperf` command. Everything below
works identically as `python3 -m vperf`; only the venv-activated console
script provides the short name.

### Install with uv (recommended, for development)

```bash
# Create a virtual environment (required on Ubuntu/Debian for pip installs).
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
vperf run --callgraph dwarf -- ./yourapp         # higher-quality stacks when DWARF is available
vperf run --mem-period 1000003 -- ./longjob      # thin the IBS memory samples (AMD)
vperf run --no-inline -- ./hugebinary            # skip DWARF inline expansion

# compare two runs
vperf diff .vperf/baseline .vperf/optimized

# attach to a running process (needs CAP_PERFMON, see doctor)
vperf attach -p 1234 --duration 10

# regenerate summary + report.html from saved artifacts
vperf report .vperf/run_20260824_021912
```

Open `report.html` in any browser — fully offline, no CDN.

### Flame graph

The Flame Graph tab behaves like the SVG `flamegraph.pl` output:

- **Click a frame** to zoom into that branch — its subtree is re-laid out to the
  full width, the call path leading to it is greyed underneath, and the focused
  frame is outlined in yellow. Percentages in tooltips become relative to the
  focused frame.
- **Click the focused frame again** to go back up one level, or click any greyed
  ancestor band to jump straight to it.
- **Reset Zoom** (top-right of the graph) or **Reset zoom** (panel header) returns
  to the full graph. Switching thread in the header selector also resets the zoom.
- The graph scales to the panel width, and frames too narrow to show a label get
  one as soon as they are zoomed into.

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
| LLC Miss % > 30% | working set exceeds cache. On AMD this is DRAM-bound; on Intel it means L1 misses that reached L3, so cross-check the Memory tab before calling it DRAM |
| L1D Miss Rate > 5% | data-cache thrashing; blocking/tiling opportunity |
| dTLB Miss Rate > 1% | page-table walks hurting latency |
| **HPC / vectorization** (AMD only — the rows are absent on Intel) | |
| Vectorization Ratio < 50% | scalar or mixed-width code; widen with intrinsics or compiler hints |
| FP Ops/s ≈ theoretical peak | compute-saturated; check memory won't help |
| Backend Bound + high FP Ops/s | memory-bound despite vectorization (common with large arrays) |
| **OS noise** | |
| Context Switches/s > 100 | scheduling pressure; pin threads or increase work per task |
| Page Faults/s high | first-touch allocation or huge-page opportunity |
| **Memory access (IBS / PEBS)** | |
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
2. **Counting/sampling pass** — when both are enabled, one synchronized
   `perf stat --per-thread` + `perf record` session collects hardware counters
   and CPU samples from the same target lifetime. The default callgraph mode
   is frame pointers, so debug info is not required for stack capture. CPU
   cycles and AMD IBS or Intel PEBS remain in the same recording; the existing
   Memory data is post-processed from that `perf.data` rather than collected again.
3. **Fallbacks** — the normal CLI keeps CPU sampling when co-joined memory
   sampling is unavailable; memory analysis is then omitted.
4. **Post-processing** — `perf script` and `perf mem report` are two
   independent reads of `perf.data`, so they run at the same time (by then the
   target is gone and the counters are stopped, so nothing is being measured)
   and the phase costs the slower of the two rather than their sum. Both keep
   writing their dump to the profile directory, which is what `vperf report`
   replays. `perf stat`, `perf script`, and `perf mem report` dumps are then
   parsed in pure Python; memory events are kept out of CPU hotspots, full
   stacks are folded for hotspot analysis, and a user-only stack view is built
   for the Flame Graph and Call Tree. Metrics are derived and HTML/SVG
   rendered.

Notes & caveats:
- The normal combined run uses one workload lifetime for per-thread counters,
  CPU samples, and co-joined memory samples. The HTML thread selector scopes
  CPU views, Overview cards, and the Memory tab; the terminal report remains
  whole-run scoped.
- `perf stat --per-thread` reports independent rows for threads present when
  collection attaches. TIDs created later may be unavailable rather than
  estimated, and legacy profiles without `stat_threads.csv` remain
  aggregate-only for Overview.
- The Memory section reuses the existing per-TID IBS/PEBS report; it is not
  recollected when the Overview hardware counters are enabled.
- The Flame Graph and Call Tree show user-space frames only. Kernel frames are
  replaced by a synthetic `[kernel boundary]` leaf while preserving their
  original sample weight; unclassifiable frames are omitted. Hotspots and
  metrics retain the full sample data.
- Multiplexing: counters share PMU registers; perf scales counts, but ratios
  across different groups carry some noise.
- Frame-pointer unwinding is the default and does not require DWARF debug info,
  but the target must preserve frame pointers. Missing frame pointers can
  produce short, unresolved, or incorrect stacks and may reduce hotspot and
  call-tree quality. Use `--callgraph dwarf` for optimized binaries with good
  DWARF/CFI unwind data.
- Stacks are capped at 128 frames from the caller end, which is perf's own
  call-graph depth. A broken frame-pointer chain otherwise keeps resolving into
  stale stack memory and can emit thousands of frames per sample, all of them
  past the real outermost frame: the leaf side that self-time attribution needs
  is kept, the rest is dropped. Symbol names still require a symbol table; fully
  stripped binaries can only provide address-based samples.
- DWARF unwinding is done offline; `DEBUGINFOD_URLS` is stripped from perf's
  environment to prevent multi-second network hangs.
- True Intel TMA level-1/2 needs Intel's `slots` PMU and perf's `tma_*`
  metrics, which `vperf` does not request. On **both** vendors the
  Backend/Frontend Bound numbers therefore come from perf's `backend_bound` /
  `frontend_cycles_idle` metrics when they resolve, and otherwise from
  `stalled-cycles-backend` / `stalled-cycles-frontend` divided by cycles —
  i.e. a stall *ratio*, not a slot fraction. Bad Speculation and Retiring are
  a model, not measurements; the assumed recovery penalty is printed next to
  the result.
- Attach mode (`-p`) needs `CAP_PERFMON`/`CAP_SYS_PTRACE`
  (`vperf doctor` prints the exact `setcap` command; on Debian/Ubuntu it must
  target the versioned ELF, not the `/usr/bin/perf` wrapper) on recent kernels.

## Development

pytest and ruff are the only things a virtualenv is actually for; `vperf`
itself runs uninstalled.

```bash
uv venv && uv pip install -e . pytest ruff
source .venv/bin/activate
vperf doctor                           # verify setup
pytest tests/ -q                       # unit + integration (needs perf access)
ruff check vperf/ tests/

# Without a venv, the profiler itself still works — only the tooling needs one:
python3 -m vperf doctor
PYTHONPATH=$PWD python3 -m pytest tests/ -q

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
LLC miss rate > 45% for the chase (relaxed to 30% on Intel, whose LLC-load
counters report a lower rate than AMD fill events), dTLB misses > 5M, and page
faults covering every 4 KiB page of the mapping. The L1/L2-count and
AVX-512 tier assertions self-skip on CPUs that lack the AMD events or the
AVX-512 tier.

`examples/simd_levels.cpp` implements the *same* weighted dot product at
four widths via `#ifdef` (scalar / SSE / AVX / AVX-512). The tier tests
document real Zen 4 physics: instructions-per-pass halve with each widening,
cycles shrink until AVX then flatten (double-pumped 512-bit ops), so
AVX-512 IPC drops to ~half of AVX's — a reminder that IPC compares
instructions, not work.

Other workloads: `sleeper` (100 ms spin + 200 ms usleep — wait-analysis
signature: ~2/3 of the window asleep).

## Profiling a whole ClickBench sweep

`bench/clickbench_profiles.sh` profiles every ClickBench query with
`clickhouse-local` and keeps one profile directory per query:

```bash
bench/clickbench_profiles.sh --dry-run          # show the commands first
bench/clickbench_profiles.sh --from 18 --to 22  # a subset
bench/clickbench_profiles.sh --resume           # skip queries already profiled
```

The schema and the queries come from the ClickBench checkout
(`$CLICKBENCH_DIR/clickhouse-parquet/`), and the run is strictly sequential —
concurrent profiling sessions multiplex the hardware counters. Queries are timed
once and repeated in-process only while they are too short to sample, with the
repetition count recorded in the run's TSV index. Each profile keeps the exact
statement list in `queries.sql` next to the usual artifacts, so
`vperf report <dir>` can regenerate the HTML at any time.

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
