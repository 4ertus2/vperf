# vperf — VTune-style CPU profiling on Linux and macOS

`vperf` wraps the Linux `perf` tool (and macOS's `sample` command) to reproduce
Intel VTune's most valuable CPU analyses on amd64 machines (AMD and Intel), with
zero Python dependencies:

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
- **Memory usage over time** — the target's resident set, sampled from `/proc`
  like `top` reads it, with the run's peak in the terminal summary
- **Wait / off-CPU analysis** — on-CPU vs Sleep / Blocked-IO / Runnable seconds
  per thread, read from the scheduler's own tracepoints
- **Reports** — terminal summary + a single-file interactive `report.html`
  (metric overview, hotspots table, memory access summary, click-to-zoom flame
  graph, timelines, call tree, threads)

The report's thread selector scopes the CPU views, the Overview metrics and the
IBS/PEBS Memory tab to the selected thread, and the **movable borders on the CPU
utilization chart scope every tab the profile has the samples for** — Hotspots,
Flame Graph, Call Tree, Memory and the per-thread cycles — to a time range. Every
column heading on the Threads tab carries a `?` that says what that column
measures. The sections below walk through each of these; if you are profiling
something now, start with [Usage](#usage).

Artifacts (`stat.csv` or `stat_threads.csv`, `perf.data`, `script.txt`,
`mem_report.txt`, `freq.json`, `rss.json`, `meta.json`) are kept in the profile
directory so reports can be regenerated any time with `vperf report`. `meta.json`
records the CPU vendor, so a profile collected on AMD and re-reported on Intel (or
the reverse) keeps the vendor calibrated constants it was collected with.

> **Working on vperf?** See [AGENTS.md](AGENTS.md) — the contributor guide:
> architecture, module map, data flow, internal contracts and conventions.

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

## Setup

Requirements: Linux, `perf` (linux-tools), Python ≥ 3.10.

```bash
# one-time: allow user-space profiling
sudo sysctl kernel.perf_event_paranoid=1      # -1 also enables full kernel sampling
# persistent:
echo 'kernel.perf_event_paranoid=1' | sudo tee /etc/sysctl.d/99-perf.conf
```

### macOS

vperf also runs on **macOS (Apple Silicon and Intel)** with a reduced feature
set. macOS has no `perf` command and none of the PMU / tracepoint surface vperf
relies on for its counting passes, so the macOS backend (`vperf/backends/
macos.py`) collects what macOS does expose and drives the *same* reports:

| Area | macOS backend | Availability |
|---|---|---|
| Hotspots, flame graph, call tree, per-thread breakdown | `sample <pid>` call-graph dumps, converted to the perf-script form the report pipeline reads | ✅ |
| Memory usage over time (RSS) + peak | `ps -o rss=` polled on a background thread | ✅ |
| CPU utilization timeline | `ps -o time=` deltas → the same busy-cores curve | ✅ |
| Hardware counters, IPC/cache/pipeline metrics | — | ❌ reads `n/a` |
| Memory-access (IBS/PEBS) | — | ❌ skipped |
| Wait / off-CPU (sched tracepoints) | — | ❌ skipped |
| CPU frequency | — | ❌ no frequency chart |
| `vperf cycle` | — | ❌ needs hardware counters |

Requirements: macOS with Xcode Command Line Tools (for `/usr/bin/sample`),
Python ≥ 3.10.

```bash
vperf doctor                      # reports the macOS backend + what is missing
vperf run -o baseline -- ./yourapp
vperf attach -p 1234 --duration 10
```

A macOS profile is a normal vperf profile directory: `meta.json`, `script.txt`
and `rss.json` are written, so `vperf report` regenerates the HTML report and
`vperf diff` compares two macOS runs. The Overview shows `n/a` for everything
that needs a hardware counter, exactly as a Linux profile would when the
counter is missing.

The macOS hotspot data comes from `sample`, whose per-thread call-graph dump is
already symbolicated — so `--callgraph`, `--no-inline`, `--mem-period` and the
frequency knobs are ignored there (they have no meaning without perf).

### Enable Wait / off-CPU analysis

The Wait report needs two scheduler tracepoints (`sched:sched_stat_runtime` and
`sched:sched_switch`), co-joined into the same recording as the CPU samples.
`perf` has to be able to read root-owned tracefs event metadata, so on systems
where those event files stay root-only `kernel.perf_event_paranoid=0` and a
tracefs remount may not be enough — `CAP_DAC_READ_SEARCH` is what lets `perf` read
them when remounting the tracefs does not change the individual file
permissions. Grant the capabilities **to the perf binary that actually
executes**:

```bash
sudo sysctl -w kernel.perf_event_paranoid=0
sudo mount -o remount,mode=755 /sys/kernel/tracing/
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
profiling command to generate a new Wait report. See
[Wait / off-CPU analysis](#wait--off-cpu-analysis) for what the columns mean.

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
vperf run --mem-time-quantum 50 -- ./longjob    # 50ms memory timeline slices
vperf run --no-rss -- ./yourapp                  # skip the memory-usage timeline
vperf run --no-inline -- ./hugebinary            # skip DWARF inline expansion
vperf run --startup-grace 0.3 -- ./slowstartup  # let the thread pool come up first

# compare two runs
vperf diff .vperf/baseline .vperf/optimized

# attach to a running process (needs CAP_PERFMON, see doctor)
vperf attach -p 1234 --duration 10

# regenerate summary + report.html from saved artifacts
vperf report .vperf/run_20260824_021912
```

Open `report.html` in any browser — fully offline, no CDN.

Other subcommands: `vperf cycle` for a statistically sound before/after matrix
(see [Cycle mode](#cycle-mode-beforeafter-comparisons-with-ministat)) and
`vperf doctor` to check what this host supports.

### Flags and tuning knobs

`run` and `attach` share these. Defaults are in brackets.

`-f/--freq` (199 Hz) sets the CPU sampling rate. `--callgraph`
(`fp`\|`dwarf`\|`none`, default `fp`) picks the unwinder; `fp` needs no debug
info but the target must preserve frame pointers, `dwarf` is slower but more
accurate on optimized binaries with good unwind data. `--no-stat` skips the
counting pass entirely, `--no-wait` skips the scheduler tracepoints and
`--no-rss` skips the memory-usage timeline.

`--mem-time-quantum` sets the time slice of the HTML Memory tab (default: about
100 slices over the run, clamped to 25 ms–1 s). Finer slices make the memory
timeline finer and `mem_report.txt` larger; the whole-run numbers are the same
either way.

`--mem-period` sets the AMD IBS sampling period (default 100003 cycles). Memory
samples scale linearly with the run length divided by the period, so raise it for
long-running targets: at 100003 a 60 s multi-threaded target produces millions of
IBS samples, which also makes `perf script` / `perf mem report` proportionally
slower.

`--no-inline` drops DWARF inline expansion from both `perf script` and
`perf mem report`. Self time then lands on the enclosing (non-inlined)
function instead of the innermost inlined callee. The trade is worth it on
huge C++ targets: a ClickHouse debug build (4.9 GB, 1.5 M symbols) spends
~80 s per `perf` invocation expanding inlines versus ~2 s without, and that
cost is paid twice per profile.

`--startup-grace` (default 0.15 s) is how long the target is left to settle
before the counting pass freezes it. `perf stat --per-thread` reports counters
only for the threads alive at the moment it attaches, so a runtime that spawns
its thread pool during startup needs that window to cover the pool — otherwise
the per-thread Overview and the per-thread Memory rows come back nearly empty
while the sampled threads are all there. Measured on `clickhouse-local` against
a 14 GB ClickBench file, the pool goes 1 thread at 0 ms, 3 at 11 ms, 25 at 42 ms
and 43 at 93 ms, so 0.15 s covers it. Raise it for a slower startup, lower it
(0 attaches at once) to shave wall time and accept the loss. Counters only start
after the target resumes, so a longer grace costs no measurement accuracy — and
a target that finishes inside the window is reported rather than profiled, so
keep the value below the shortest run you care about. Threads created after the
freeze are still never counted, whatever the value.

`-o/--outdir` names the profile directory (default `.vperf/<mode>_<timestamp>`).
`-I/--interval` prints per-interval `perf stat` counters instead of one
whole-run total; it is off by default and cannot be combined with the per-thread
counters the report relies on.

### Flame graph

The Flame Graph tab behaves like the SVG `flamegraph.pl` output:

- **Click a frame** to zoom into that branch — its subtree is re-laid out to the
  full width, the call path leading to it is greyed underneath, and the focused
  frame is outlined in yellow. Percentages in tooltips become relative to the
  focused frame.
- **Click the focused frame again** to go back up one level, or click any greyed
  ancestor band to jump straight to it.
- **Reset zoom** in the bar under the graph returns to the full graph. Switching
  thread in the header selector, or moving the time selection, also resets it.
- The graph scales to the panel width, and frames too narrow to show a label get
  one as soon as they are zoomed into.
- The graph is exactly as tall as the rows it draws, and never shows a row of
  hairlines over the flame: it ends at the last row with a frame at least 2px
  wide and at 48 rows, whichever comes first. A target with broken frame
  pointers records one stack thousands of frames deep, and the cut rows fold
  into the frame they hang off — which keeps the width they gave it and says in
  its tooltip how many rows it stands for. Zooming into a shallow branch brings
  the bottom edge up with it instead of leaving empty space above.

The graph follows the thread selector *and* the time selection: the report ships
each sample once (as indices into a symbol table) and folds it again in the
browser, so a scoped or time-sliced flame graph costs nothing in file size — a
ClickBench report went from 22.6 MB to 6.8 MB once the per-thread and per-group
copies were dropped.

### Time selection

The chart at the top of the report has two movable borders, and
they scope every tab the profile holds the data for:

- **Drag inside the plot** to select a range, **drag the selection** to move it,
  **double-click** or press **Reset Selection** to clear it. A range is picked
  on the chart, never typed. The label beside the button and the scope line
  under the chart say what is selected: the range in seconds into the run, the
  share of the run, the sample count and the cycles behind it.
- The curve always shows the **whole run** with the parts outside the selection
  dimmed, so a selection keeps its context and the borders line up with the axis.
  All three chart modes (CPU Utilization, Memory RSS, Frequency) share that
  axis and the same selection, and the frequency and memory curves are placed
  on the sample timeline by the clock their samplers share with perf. Every time the report shows is seconds
  into the run — perf's raw `CLOCK_MONOTONIC` timestamps stay inside it.
- The utilization y axis is **busy cores in the current scope, capped at what
  that scope could possibly use**: every thread at the machine's logical CPU
  count, a name group at its own thread count, and **a single thread at one
  core**. Its average is that scope's own CPU time over the window the chart
  shows — the per-thread `task-clock` `perf stat` already counted, summed over
  a group. So a thread that used half a core reads as 0.5, and the plot never
  puts a thread at 16. The *shape* between those points is an estimate: perf
  hands a sample the cycles its core ran since that core's last sample, which
  is whatever else ran in between, so the sampled buckets are smoothed before
  they are scaled. Read the curve as "how busy, and where", not as a counter,
  and remember a curve resting on the ceiling means "all of them, saturated".
- The Memory tab's own timeline plots, by cache source on the same axis and with
  the same shaded selection as the chart above it, either **accesses per time
  slice** or the **stall cycles they cost** — the selector in the panel's top
  right corner switches between the two, and both follow the thread scope.
  Latency there is a sum of each source's access latency, not an average: the
  same weight the tab's "Average Access Latency" divides by the access count.
- What follows the selection: **Hotspots** (self, inclusive and estimated CPU
  time, over the selection), the **Flame Graph**, the **Call Tree**, the
  **Memory** tab (all five panels, plus a memory-accesses-over-time chart with
  the selection shaded), and the whole Threads tab — the per-thread **cycles**
  and the scheduler's **on-CPU, Sleep, Blocked/IO, Runnable, Off-CPU** columns
  and counts, plus the "where the time went" bar over them. The wait half is
  not a counter: it is a per-thread off-CPU timeline on the same clock the
  borders are drawn on, so a range on the chart is a range on it, and it is
  folded in the browser from the slices the report ships (see
  [Wait / off-CPU analysis](#wait--off-cpu-analysis)). Two things in that tab
  still do not move: the **Overview** metrics, and the delay-band histogram,
  which counts the waits that *started* — a wait is not cut in half by a
  selection the way a second is.
- What cannot: the **Overview** metrics. `perf stat --per-thread` counts once
  over the whole profile (perf refuses `-I` together with `--per-thread`), so
  that panel says *whole run* while a selection is active instead of quietly
  reporting the run as if it were the window.
- While you drag, the chart and the counters follow the borders; the flame
  graph, the call tree and the memory panels are rebuilt once the drag settles,
  which keeps dragging smooth on a profile with 100k+ samples.
- Memory samples are only known to the `--time-quantum` slice they fell in
  (default: about 100 slices over the run, 25 ms–1 s, `--mem-time-quantum` to
  override), so a window that cuts a slice in half counts half of it. A profile
  captured before this existed — or on a perf that rejected the `time` sort key —
  keeps its whole-run Memory tab and says so.

### Memory usage over time

The **Memory RSS** chart in the header (between CPU Utilization and Frequency)
plots the target's resident memory across the run, sampled every 10 ms from
`/proc/<pid>/statm` — the same number `top` prints — and the run's peak is also
printed in the terminal summary. The curve is a measured value, not an estimate:
the only thing bucketing loses is a spike shorter than a bucket, so the peak is
drawn as its own dashed line at the top of the plot, labelled with the same
number the terminal quotes.

It is the **whole process, and it does not follow the thread selector**. A
process is one address space, so every thread of it reads the same resident
size — `/proc/<pid>/task/<tid>/statm` and the per-thread `RssAnon`/`RssFile` in
`.../status` both report the process total, which is why there is no per-thread
footprint to plot anywhere in procfs (measured on a 4-thread process with 300 MiB
allocated on one thread: 312.4 MiB reported by all four). For per-thread memory
*behaviour* use the Memory tab, which follows the selector: its timeline counts
the selected thread's or group's accesses and stall cycles per time slice.

`--no-rss` skips the sampling, and then the report has no memory curve and no
peak row. A profile collected before this existed has neither and says so in
place of the chart.

### Grouping threads by name

Tick **Group threads by name** next to the thread selector and the dropdown
lists thread names instead of threads — the entry that was `ThreadPool ×54`
before is one line, not 54:

- the group covers *every* thread of that name the profile knows, not just the
  hottest 20 the ungrouped list shows
- the list is ordered like the ungrouped one: the group holding most of the
  run's sampled cycles first (its share is in the label), the name breaking
  ties, and groups that sampled nothing last
- Hotspots, the utilization chart and the flame graph merge the members'
  samples; the utilization curve is the pool's total cores busy, so a 16-thread
  pipeline reads as up to 16 busy cores
- Overview counters are summed before anything is derived from them, so the
  group IPC is `Σinstructions / Σcycles` rather than an average that would weigh
  a thread which sampled 10 cycles like one that ran the whole window
- the Memory tab adds up the members' IBS/PEBS samples, and the scope line says
  how many of them had any (`QueryPipelineEx ×16 threads, memory from 12 of 16`)
- a group whose threads have no per-thread counters — the usual case, since
  `perf stat --per-thread` only reports the threads alive when counting
  attaches — falls back to what the sampler knows: thread count, cycle share of
  the run, and the CPU time that share works out to
- the flame graph of a group costs nothing extra in the report: only the
  whole-run graph is drawn into the file, and every other scope — one thread or
  a whole name group — is folded again in the browser from the same samples
- the Threads tab's "where the time went" bar follows the scope — a group adds
  its members' waits, the way it merges their samples — while the per-thread
  table below it keeps listing every thread, because that is what a per-thread
  table is. The Frequency chart stays run-level, as it was before grouping
  existed

`perf mem report` labels every thread of a process with the *process* name, so a
thread is grouped under the name the sampler saw for it; only threads the
sampler never caught fall back to the coarser memory-report name.

### Wait / off-CPU analysis

The Threads tab merges the per-thread CPU and wait tables into one: each row
carries sampled cycles next to on/off-CPU seconds, joined on tid, and the wait
columns read `n/a` when scheduler tracepoints were not collected. Its
on/off-CPU half comes straight from the scheduler, not from PMU counters — the
**On-CPU** half is the CPU time it charged a thread, and every second between two
of that thread's accounting points that the charge does not explain is
**Off-CPU**, split by the state the thread was switched out in. The report puts
each definition on the column it defines, as a `?` on the heading — hover it, or
tab to it — so a table can be read without this section.

It collects two scheduler tracepoints, co-joined into the same recording as the
CPU samples:

| Event | What it contributes |
|---|---|
| `sched:sched_stat_runtime` | the CPU time a thread earned since its previous accounting point — reported when it is switched out, and on kernels with `CONFIG_SCHED_INFO` once per scheduler tick per CPU. Per thread the deltas sum to exactly that thread's `perf stat` `task-clock` |
| `sched:sched_switch` | the switch-out instant and `prev_state`, the scheduler's own spelling of the state the task was switched out in |

A thread can only be on-CPU between two of its own events, so the delta is
exactly its on-CPU share of the gap between them and the rest of the gap was
off-CPU; the state of the switch-out that opened the gap says what kind of
wait it was. `sched:sched_process_exit` closes a thread's window when it
leaves. Only the target's own process tree is recorded, so no `-a` is needed
for the totals — but a thread's switch-*in* is only visible when the task that
held the CPU was also in the tree, which is why the split rests on the single
accounting stream rather than on paired switch events.

**On-CPU + Off-CPU is therefore exactly the thread's observed window**, and that
is what the "where the time went" bar splits. `prev_state` maps onto the
columns like this:

| Report column | `prev_state` | Means |
|---|---|---|
| **Sleep** | `S` | interruptible wait — futexes, condition variables, sleeping syscalls |
| **Blocked/IO** | `D` | every uninterruptible wait: disk I/O *and* the page-fault waits a cold page cache causes |
| **Runnable** | `R` | run-queue wait after a preemption |
| *stopped* / *unknown* | — | traced but not attributable to a wait class |

The scheduler's delay-accounting tracepoints (`sched_stat_wait`,
`sched_stat_sleep`, `sched_stat_blocked`, `sched_stat_iowait`) would report the
delay directly and would be the simpler source, but their call sites are gated
on the scheduler's delay accounting and are simply not built on some kernels —
that is how the Sleep and Blocked/IO columns used to come out permanently zero.
`prev_state` is the state that cannot go missing, so it is the only source the
report reads, and it is read from both renderings of the event that older and
newer `perf` produce. The report puts these definitions on the columns
themselves, one `?` per heading on the Threads tab, so a table can be read
without this section.

Two more things the columns mean, so the numbers are not over-read:

- **Blocked/IO is the `D` state**, which is every uninterruptible wait: disk
  I/O *and* the page-fault waits a cold page cache causes. A query that scans
  a file for the first time shows up here in both ways.
- A thread that is still switched out when the recording ends is left
  uncharged rather than guessed at, so a row's On-CPU + Off-CPU covers its
  observed window (the tiny first delta was earned before the window opened).

**The whole table follows the time selection exactly**, because the report ships
each thread's off-CPU intervals as a timeline on the sample clock and folds them
in the browser rather than resampling counters. Two consequences worth stating:

- The **counts** (Preempted, Sleeps, Blocks) are read off the intervals, so they
  count the switches that cost measurable time — a switch-out whose wait the
  window never showed owns no interval and is not counted. That is also what
  makes the server's count and the browser's the same number.
- A wait that **straddles the edge** of a selection is charged by the share of
  it inside, so a count can come out fractional (rounded for display) and an
  interval cut in half contributes half its seconds.

A profile whose scheduler records cannot be lined up with its samples (a wait
pass collected separately, so the records describe another run of the target)
keeps these columns whole-run and says so in place. See
[Enable Wait / off-CPU analysis](#enable-wait--off-cpu-analysis) for the access
setup this needs.

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
| **Wait / off-CPU (Threads tab)** | |
| Blocked/IO ≫ On-CPU | the data is not in cache: the thread is waiting on the disk, not computing — the difference between a warm and a cold run shows up here and nowhere else |
| Runnable high, On-CPU low | oversubscribed — more runnable threads than cores; use fewer threads or more work each |
| Sleep high, On-CPU low | idle-waiting, not slow: a futex or condition variable, so the pipeline is starving its own workers |
| **Hotspots** | |
| Hotspots `[kernel]` heavy | syscalls/page faults; consider off-CPU analysis |
| One function > 50% self | clear #1 target; optimize or vectorize that function |
| Inlined functions dominate | compiler flattened the call tree; inspect unrolled loops |

## How it works

1. **Capability probe** — tiny throwaway runs determine supported events,
   `-M` metrics and the best precise cycles event (`cycles:P` → fallbacks).
2. **Counting/sampling pass** — when both are enabled, the target is left to
   settle for `--startup-grace` seconds and then one synchronized
   `perf stat --per-thread` + `perf record` session collects hardware counters
   and CPU samples from the same target lifetime, both attached to the frozen
   target. The default callgraph mode is frame pointers, so debug info is not
   required for stack capture. CPU cycles and AMD IBS or Intel PEBS remain in the
   same recording; the existing Memory data is post-processed from that
   `perf.data` rather than collected again.
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
   for the Flame Graph and Call Tree. `perf mem report` is asked for its
   `--sort time` view, so the memory rows also carry the time slice their
   samples fell in and the HTML Memory tab can answer a time selection. Metrics
   are derived and HTML/SVG rendered.

Notes & caveats:
- The normal combined run uses one workload lifetime for per-thread counters,
  CPU samples, and co-joined memory samples. The HTML thread selector scopes
  CPU views, Overview cards, and the Memory tab, and the chart's time selection
  scopes every sample-derived view; the terminal report remains whole-run
  scoped.
- `perf stat --per-thread` reports independent rows for threads present when
  collection attaches, which is why the target is settled for `--startup-grace`
  seconds first (default 0.15 s). TIDs created after that are unavailable
  rather than estimated, and legacy profiles without `stat_threads.csv` remain
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

The architecture, module map, internal contracts and coding conventions live in
[AGENTS.md](AGENTS.md) — read it before changing anything under `vperf/`.

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

`bench/clickbench_profiles.sh` profiles every ClickBench query with vperf and
keeps one profile directory per query and engine:

```bash
bench/clickbench_profiles.sh --engine duckdb          # one engine
bench/clickbench_profiles.sh --engine both            # clickhouse, then duckdb
bench/clickbench_profiles.sh --dry-run                # print the real argv first
bench/clickbench_profiles.sh --from 18 --to 22        # a subset
bench/clickbench_profiles.sh --resume                 # skip what this engine has
```

```
.vperf/q00_clickhouse_20260926_120501/report.html
.vperf/q00_duckdb_20260926_120501/report.html
.vperf/clickbench_<engine>_<runid>.{log,tsv}          # progress + headline metrics
```

Each engine gets the schema and the query text from its own directory in the
ClickBench checkout (`$CLICKBENCH_DIR/<engine>-parquet/`) — the two sets differ
where the engines disagree on a function name — and is invoked the way its CLI
wants it: `clickhouse-local --time --format=Null --query=<schema> <query>` and
`duckdb -no-stdin -c .timer on -c <schema> -c <query>`. `--engine both` runs
them in sequence, never side by side, since concurrent profiling sessions
multiplex the hardware counters.

Every query is executed exactly once: nothing is repeated, retimed or retried,
so a report describes the query as ClickBench defines it — where its CPU time
goes and what its threads were doing over the query's timeline. Each profile
keeps the exact statement it ran in `queries.sql` next to the usual artifacts,
so `vperf report <dir>` can regenerate the HTML at any time. A query that
finishes before the collectors can attach still gets a report, just without
samples, and the run log names it — which is why the startup grace is per
engine: `clickhouse-local` needs ~90 ms to build its thread pool while its
cheapest query takes 140 ms, and duckdb has its 16 threads up within 8 ms while
its cheapest query takes 80 ms.

`bench/clear-caches.py` evicts the dataset before a run and needs no root:
`posix_fadvise(POSIX_FADV_DONTNEED)` on a clean file discards that file's
pages, where `/proc/sys/vm/drop_caches` is `0600` root-only and a setuid bit on
a script is ignored by the kernel anyway. It takes a path or a directory
(`bench/clear-caches.py` alone does `$DATA_DIR`, else `~/data`) and measures
every file with `mincore` afterwards, so the run is *known* to be cold rather
than assumed to be — which matters, because a 14 GB file this box reads in
about a second does not stay in the page cache on its own.

```bash
bench/clear-caches.py                      # evict $DATA_DIR, report residency
bench/clickbench_profiles.sh --from 13 --to 13
```

That is how the I/O half of a query becomes visible. The same Q13 on the same
machine with the same flags, the only difference being whether the 14 GB
`hits.parquet` was resident: **1.3 s of Blocked/IO against 0.3 s**, all of it
on the `ParquetDecoder` and `ParquetPrefetch` threads, and 10.5 cores busy
against 12.7. CPU time moves with it — 12.3 s against 22.7 s — because the
threads that would be decoding are blocked in `D` instead, so the utilization
curve drops and the wait columns say exactly why.

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