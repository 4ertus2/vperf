# vperf — VTune-style CPU profiling on Linux and macOS

`vperf` wraps Linux `perf` (and macOS's `sample`) and answers the questions a
profile is for: **where did the time go, why did the cores stall, which cache
levels did the memory traffic hit, and what were the threads doing when they
weren't on a CPU.** One command, one self-contained HTML report. AMD and Intel,
Python ≥ 3.10, **zero runtime dependencies**.

## Quick start

```bash
# 1. allow user-space profiling (one-time, needs sudo)
sudo sysctl kernel.perf_event_paranoid=1

# 2. check what this machine supports
vperf doctor

# 3. profile a program
vperf run -- ./yourapp input.bin

# 4. open the report
xdg-open .vperf/run_*/report.html
```

`vperf run` prints a summary to the terminal and writes `report.html` into a
fresh `.vperf/run_<timestamp>/` directory. The report is a single HTML file that
works offline in any browser — no CDN, no server, nothing fetched from the
network.

No install step either — with a checkout, the system Python is enough:

```bash
python3 -m vperf run -- ./yourapp input.bin
```

For development, `uv venv && uv pip install -e .` gives you the `vperf` console
script. See [Development](#development).

## What the report shows

- **Overview** — CPU time, effective utilization, IPC/CPI, branch mispredicts,
  cache and TLB miss rates, and a Backend/Frontend Bound breakdown.
- **Hotspots** — self and inclusive time per function.
- **Flame graph** — click a frame to zoom into that branch.
- **Call tree** — the same stacks, collapsible.
- **Memory access** — AMD IBS or Intel PEBS samples by cache level (L1 / L3 /
  DRAM) with latency bands, the top symbols, and a timeline over the run.
- **Wait / off-CPU** — per thread, On-CPU seconds next to Sleep, Blocked/IO and
  Runnable seconds, with a "where the time went" bar.
- **Timelines** — CPU utilization, resident memory (with the run's peak) and
  frequency, across the whole run. The memory and frequency curves are placed on
  the sample timeline by a clock origin each sampler records, and that clock is
  not one userspace is guaranteed to be reading: inside a Linux time namespace
  `CLOCK_MONOTONIC` is shifted by the namespace's offset, and perf's own clock can
  run ahead of `CLOCK_BOOTTIME` by a drift that grows with uptime — measured here
  as 0.38 s at 2.3 h of uptime and 3.9 s at 11.8 h, about +1.6 s a day. So vperf
  measures that offset per profile, from where the recording started and ended
  against where its samples fall, and uses it to place the curves — unless the
  measurement demonstrably puts the readings outside the measured window, in which
  case the curve is anchored to the first sample instead, because on such a host
  much of the gap is the delay before perf's *first* sample rather than a clock
  difference. Either way the curve is drawn only where it was measured: a stretch
  with no reading is left blank and labelled, never continued, since a held value
  is a measurement nobody took. Left uncorrected both curves draw *empty* while
  their data sits in the file, which is invisible rather than wrong-looking.
- **Scoping** — a thread selector (with an optional group-by-name mode) and a
  time selection on the chart narrow every view that has the data for it.

Hardware a machine doesn't expose reads `n/a` rather than being guessed at.
`vperf doctor` prints exactly what your machine supports.

## CPU vendor support

| Area | AMD (Zen) | Intel |
|---|---|---|
| Memory-access sampling | `ibs_op` (sampling *period*, default 100003) | `mem-loads`/`mem-stores` PEBS (load-latency threshold, default `--ldlat 30`) |
| LLC classification | `ls_any_fills_from_sys.*` — DRAM/MMIO fills per L3 lookup | `LLC-loads`/`LLC-load-misses` — L1 misses that reached L3 |
| L1 / L2 miss counts | `ls_any_fills_from_sys.all`, `l2_cache_req_stat.ic_dc_miss_in_l2` | not exposed; `L1-dcache-load-misses` drives the L1D miss *rate* only |
| FP / vectorization | `fp_ret_sse_avx_ops.*`, `fp_ops_retired_by_width.*` | not exposed; Overview omits the FP rows |
| Branch-mispredict penalty (Bad Speculation model) | 13 cyc | 15 cyc |

Only the AMD-only events are vendor-gated, so Intel never sees `<not counted>`
noise. Everything else is collected and reported identically on both.

## Options worth knowing

| Flag | Use it when |
|---|---|
| `-o, --outdir NAME` | you want a named profile directory — `vperf diff` compares two |
| `-f, --freq HZ` (199) | the default sample rate misses short phases |
| `--callgraph dwarf` | the target has good DWARF; `fp` (the default) needs no debug info but requires the target to preserve frame pointers |
| `--startup-grace SEC` (0.15) | the target spawns its thread pool during startup — per-thread counters only see the threads alive when counting attaches |
| `--mem-period N` (100003) | a long multi-threaded run produces millions of AMD IBS samples; raise the period to thin them |
| `--no-inline` | a huge C++ binary spends most of the run expanding DWARF inlines (measured: a 4.9 GB ClickHouse debug build, ~80 s per `perf` call with inlines, ~2 s without) |
| `--no-stat` / `--no-wait` / `--no-rss` | skip a pass you don't need — each one removes its panel from the report |
| `--mem-time-quantum MS` | finer or coarser Memory-tab slices (default ~100 over the run, clamped 25 ms–1 s) |

`--startup-grace` is worth calibrating against your target: measured on
`clickhouse-local`, the thread pool goes 1 thread at 0 ms, 3 at 11 ms, 25 at
42 ms and 43 at 93 ms. Counters only start once the target resumes, so a longer
grace costs no measurement accuracy — but a target that exits inside the window
is reported rather than profiled, so keep the value below the shortest run you
care about.

## Other commands

```bash
vperf attach -p 1234 --duration 10          # profile a running process (needs CAP_PERFMON)
vperf report .vperf/run_20260824_021912      # regenerate report.html from a saved profile
vperf diff .vperf/before .vperf/after        # compare two profiles
```

A single profile is noisy, so `vperf cycle` repeats a target N times and writes a
TSV matrix of metrics — one row per run — which `ministat` (from the BSD
toolkit) turns into a verdict at 95% confidence:

```bash
vperf cycle -n 30 -- ./myapp        > base.tsv
vperf cycle -n 30 -- ./myapp-fixed  > fixed.tsv
awk -F'\t' 'NR>1{print $4}' base.tsv  > base_ipc.txt   # column 4 is IPC
awk -F'\t' 'NR>1{print $4}' fixed.tsv > fixed_ipc.txt
ministat base_ipc.txt fixed_ipc.txt
```

Options: `-n` measured runs (default 30), `--warmup` discarded runs (default 1),
`-j` parallel runs (default = half your logical CPUs, pinned round-robin to
distinct physical cores so SMT siblings do not contend), `--metrics CSV` to pick
columns, `--tsv FILE` instead of stdout. Progress and the per-metric summary go to
stderr, so `> file.tsv` stays clean.

## macOS

macOS has no `perf` and none of the PMU / tracepoint surface the counting passes
need, so `vperf` there runs a **sample-based** backend: hotspots, flame graph,
call tree, RSS and a utilization timeline, all in the same report; every
counter-driven metric reads `n/a`. Requires Xcode Command Line Tools (for
`/usr/bin/sample`). `vperf doctor` reports what is available. The counter flags
(`--callgraph`, `--no-inline`, `--mem-period`, `--freq`) are ignored there, and
`vperf cycle` is unavailable.

## Caveats

- The Flame Graph and Call Tree show **user-space frames only**; kernel frames
  become a synthetic `[kernel boundary]` leaf that keeps their sample weight.
  Hotspots and metrics keep the full data.
- Frame pointers are the default unwinder and need no debug info, but the target
  must preserve them. Missing frame pointers degrade stacks — use
  `--callgraph dwarf` for optimized binaries with good unwind data. Fully
  stripped binaries give address-level samples only.
- Counters share PMU registers, so ratios *across* different multiplexed groups
  carry some noise.
- Backend/Frontend Bound are **stall ratios, not TMA slot fractions**: perf's
  `tma_*` metrics need Intel's `slots` PMU, which `vperf` does not request. Bad
  Speculation and Retiring are a model; the assumed recovery penalty is printed
  next to the result.
- Per-thread counters cover only the threads alive when counting attaches (see
  `--startup-grace`). Threads created afterwards are unavailable rather than
  estimated.

## How it works

1. A capability probe runs tiny throwaway profiles to find which events, `-M`
   metrics and precise cycles event this machine supports.
2. The target is frozen, then **one** synchronized `perf stat --per-thread` +
   `perf record` session collects counters, CPU samples and memory-access
   samples from the same workload lifetime, with the scheduler tracepoints
   co-joined into the same recording.
3. After the target exits, `perf script` and `perf mem report` read `perf.data`
   in parallel, then the dumps are parsed in pure Python.
4. Metrics are derived and `report.html` is rendered as a single file.

[AGENTS.md](AGENTS.md) documents the pipeline, the artifacts and the internal
contracts.

## Development

`pytest` and `ruff` are the only things a virtualenv is for; `vperf` itself runs
uninstalled.

```bash
uv venv && uv pip install -e . pytest ruff
source .venv/bin/activate

vperf doctor                          # verify this host can be profiled
pytest tests/ -q                      # unit + integration (needs perf access)
ruff check vperf/ tests/

# Note: run the suite WITHOUT pytest-xdist/-n. The integration tests assert
# exact PMU counter relationships; concurrent profiling sessions multiplex
# the hardware counters and break those assertions.
```

`examples/` holds C++ workloads with opposite, well-understood hardware
signatures, used by the integration tests:

```bash
make -C examples
vperf run -- examples/bin/simd_levels_avx 2000000   # AVX2, L1-resident  -> IPC ~2.9
vperf run -- examples/bin/membound 268435456 1.5    # 256 MiB chase      -> IPC ~0.14
```

[AGENTS.md](AGENTS.md) is the contributor guide — module map, data flow, the
report UI contract, internal invariants and conventions. Read it before
changing anything under `vperf/`.

## Profiling a ClickBench sweep

`bench/clickbench_profiles.sh` profiles every ClickBench query against
`clickhouse-local` and duckdb, one profile directory per query and engine, and
keeps the statement it ran in `queries.sql` next to the report:

```bash
bench/clear-caches.py                      # evict the dataset, report residency (no root)
bench/clickbench_profiles.sh --engine both --from 13 --to 13
```

Each query runs exactly once — nothing repeated, retimed or retried — and the
engines run sequentially, because concurrent profiling sessions multiplex the
hardware counters. `bench/clear-caches.py` makes a cold run *known* rather than
assumed (`posix_fadvise(POSIX_FADV_DONTNEED)`, verified with `mincore`), which
is how the I/O half of a query becomes visible: the same Q13 on the same machine
spent 1.3 s in Blocked/IO cold against 0.3 s warm, 12.3 s of CPU against 22.7 s.

### Against a running clickhouse-server

`--mode server` runs the same 43 queries against a MergeTree `hits` table in a
`clickhouse-server` rather than `clickhouse-local` reading the parquet, and
profiles each one by attaching vperf to the server:

```bash
bench/clickbench_profiles.sh --mode server --load             # load, then profile all
bench/clickbench_profiles.sh --mode server --from 18 --to 18  # one query, table reused
bench/clickbench_profiles.sh --mode server --optimize-final   # merge every part first
bench/clickbench_profiles.sh --mode server --private-server    # own server, own data dir
```

Loading takes no privileges at all. ClickHouse confines the `file()` function to
its `user_files_path` (`/var/lib/clickhouse/user_files/`, inside a `700`
clickhouse-owned `/var/lib`), so a server cannot read a parquet that sits
anywhere else — ClickBench's own load works around that with a root-owned
symlink. The driver streams the file's bytes in instead
(`INSERT INTO hits FORMAT Parquet`, parsed by the server with parallel parsing),
so nothing is installed, nothing is added to a group, and no `sudo` is involved.
The dataset's columns are checked against the schema before the first byte is
sent, because the Parquet reader matches columns *by name* and would otherwise
fill a missing one with defaults.

Credentials come from `~/.clickhouse-client/config.xml` (`<host>`, `<port>`,
`<user>`, `<password>`) — the driver has no auth flags of its own, so the
server's credentials stay in one place you control. `--load` drops and refills,
`--skip-load` uses whatever is there, and the default loads only when `hits` is
missing. Loading 100M rows into a table with `fsync_after_insert = 1` takes
minutes, and the log carries the table's size as it fills. `--optimize` /
`--optimize-final` are opt-in because ClickBench does not optimize, and because
the schema has no `PARTITION BY`: a FINAL merges every part of the whole table.

Two things to know about the reports:

- **A profile is the whole server process**, not the query. A running
  ClickHouse holds hundreds of background threads (164 `ThreadPool`, 16
  `MergeMutate`, 16 `Fetch` and the `Bg*` pools on a stock 16-core box), so the
  Overview counters and the utilization curve cover all of them — group or scope
  to the query's threads in the Threads tab to read the query. `--private-server`
  starts a dedicated server instead, which keeps those threads out of the
  profile.
- **Each profile covers exactly the query.** The runtime is not knowable in
  advance (Q00 is ~0.1 s, Q35 ~90 s), so a fixed window would either truncate the
  query or pad it with idle server; vperf ends the profile the moment the client
  returns. `--max-duration` (300 s) is only the ceiling.

Three things differ from a single-process target, all because sampling cost is
per *thread* and a server has hundreds of them:

- **Lower default rates.** `FREQ` defaults to 99 Hz here instead of 499, and
  `MEM_PERIOD` to 4000003 instead of 1000003: a `clickhouse-server` runs 359
  threads on a stock 16-core box (164 `ThreadPool`, 16 `MergeMutate`, 16
  `Fetch`, the `Bg*` pools), and IBS samples every thread that retires cycles,
  including background merges. Override with `FREQ=499 MEM_PERIOD=1000003`.
- **No scheduler tracepoints** (`--wait` opts back in): a server switches
  hundreds of background threads and their off-CPU time is not what a query
  report is about.
- **No freeze.** vperf cannot `SIGSTOP` a server it does not own — a systemd one
  belongs to `clickhouse` — and does not need to: an already-running process has
  no startup for the pause to protect. The profile starts when the collectors
  open. Profiling still needs `perf` with `CAP_PERFMON`, which its file
  capabilities usually carry; `vperf doctor` checks it and prints the `setcap`
  line for your host if not.

Expect a few seconds of post-processing per query on top of the query itself:
`perf.data` for a one-second window over the whole server is ~100 MB, and both
the flush and the `perf script` pass read all of it.