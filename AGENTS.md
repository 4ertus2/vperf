# AGENTS.md — working on `vperf`

Engineering guide. **`README.md` is the user-facing document** — read it first.

| Change | Update |
|---|---|
| a metric, threshold, or the meaning of a report number | "Metric thresholds" here |
| a CLI flag or its default | `README.md` **and** the flag table here |
| anything the reader sees in `report.html` | "The report UI contract" here |
| a new artifact in the profile directory | the artifact table here |
| internals, tests, conventions | `AGENTS.md` only |

---

## Non-negotiables

1. **Zero runtime dependencies.** Everything imported is stdlib; the venv exists only for
   `pytest` and `ruff`.
2. **Never run the suite with `pytest -n` / `pytest-xdist`.** Integration tests assert exact
   PMU counter relationships and concurrent sessions multiplex the hardware counters; same
   reason `bench/clickbench_profiles.sh --engine both` runs sequentially.
3. **Every `json.dumps(...)` in `report_html.build_html` must be followed by
   `.replace("</", "<\\/")`** — the payload is bare globals in a `<script>` block, and a
   `</script>` in a symbol name would close the tag.
4. **Never reformat the fixture strings in `tests/test_parsers.py` /
   `tests/test_memory_ibs.py`.** They are byte-exact `perf script` / `perf mem report`
   output, tabs included; the ruff per-file ignores (`E101`, `W29`, `E501`) exist for that.
5. **Comments and docstrings carry measurements** ("clickhouse-local needs ~90 ms to build
   its pool"). Preserve them; add the same for a new limit.
6. **The target is always `SIGCONT`'d.** Keep the `try/finally` that resumes it
   (`collector.py:1184`) and the `PerfProcess.stop()` escalation (SIGINT → SIGTERM →
   SIGKILL) that flushes a partial `perf.data`. Grace is `_COLLECTOR_STOP_GRACE = 60 s`: a
   record escalated mid-flush is judged *failed* and loses its samples. The file is deleted
   later (step 9), after every dump has been read.
7. **`SIGINT` ends an attached profile and still reports.** `attach` catches
   `KeyboardInterrupt` and continues to finalize (`collector.py:1195`); `run` keeps
   unwinding. `cmd_attach` requests `SIGINT` itself (`cli.py:197`) because a background job
   of a non-interactive script inherits an *ignored* `SIGINT` (POSIX; a `trap` cannot undo
   it). Waiting for the client is the driver's job; it gets `elapsed_wall` plus a warning.
8. **The samplers stop after the collectors, not with the monitor** (`_finish_collector`
   then `_FreqSampler.stop()`). Their curves are placed against *sample* times and perf's
   first sample lags launch, so stopping with the monitor leaves the RSS/Frequency charts
   empty over collected data.
9. **Raw recordings are retired unless `--keep-perf-data`.** Nothing reads them after
   `_retire_raw` (`collector.py:931`); `load_profile` opens only text and JSON. The call
   sits past the last read and takes the names *this run* recorded, because `-o` reuses an
   outdir. Pinned by `test_nothing_reads_the_recording_after_it_has_been_retired`. Not
   routed through `warnings` — being the default, every run would carry a `Warnings:` header.

---

## Commands

```bash
uv venv && uv pip install -e . pytest ruff && source .venv/bin/activate
pytest tests/ -q                 # unit + integration; needs real perf access
ruff check vperf/ tests/         # the only lint gate
python3 -m vperf doctor          # the profiler itself needs no venv
```

- `ruff`: `line-length = 140`, `select = ["E", "F", "W"]`. **No isort, formatter or mypy
  config** (`.mypy_cache/` is not a gate). `bench/` and `examples/` are not linted.
- Green suite needs Linux + `perf` + `kernel.perf_event_paranoid <= 1` (or `CAP_PERFMON`);
  wait tests also need `paranoid <= 0` or `CAP_PERFMON`/`CAP_DAC_READ_SEARCH`. On
  Debian/Ubuntu `setcap` must target `/usr/lib/linux-tools/$(uname -r)/perf`, not the
  `/usr/bin/perf` wrapper (the kernel ignores file caps on scripts).

---

## Module map

All under `vperf/`: `cli` (argparse, dispatch, shared analyze→report), `collector` (probes,
perf passes, artifacts), `perf` (wrapper), `doctor` (capability probes), `parsers` (stat CSV +
`perf script`), `stacks` (hotspots, folds, call tree), `metrics` (`MetricsReport`, ~40 optional
floats), `memory` (`perf mem report`), `wait` (off-CPU), `timeline`/`flamegraph` (SVG),
`report_html` (single-file dashboard), `report_terminal`, `cycle` (TSV), `diff`,
`backends/macos` (`sample` + `ps`).

Entry points: `vperf = "vperf.cli:main"`, `python3 -m vperf`. Key APIs:
`collector.collect(...) -> ProfileData`, `collector.load_profile(outdir,
include_threads=False)`, `report_html.build_html(...) -> str`. `cli._analyze` /
`cli._finish` are shared by `run`, `attach` and `report`, so a regenerated report is
byte-identical.

---

## CLI surface

`build_parser()` at `cli.py:407`; `main()` at `cli.py:503`. Subcommands: **run, attach,
report, cycle, diff, doctor**. Shared by `run` and `attach`:

| Flag | Default | Effect |
|---|---|---|
| `-o, --outdir` | `.vperf/<mode>_<stamp>` | profile directory |
| `-f, --freq` | 199 | CPU sampling Hz |
| `-I, --interval` | off | `perf stat` interval ms; conflicts with `--per-thread` |
| `--no-stat` / `--no-wait` / `--no-rss` / `--no-inline` | — | skip counting / sched tracepoints / RSS / DWARF inline |
| `--callgraph` | `fp` (`dwarf`\|`fp`\|`none`) | unwinder |
| `--mem-period` | 100003 | AMD IBS period (cycles) |
| `--mem-time-quantum` | ~100 slices, 25 ms–1 s | Memory-tab slice width |
| `--keep-perf-data` | off | keep raw recordings after the dumps |
| `--startup-grace` | 0.15 s | ceiling on the pre-freeze wait; freeze once `/proc/<pid>/task` has held still for 20–50 ms (10% of the ceiling, floor 20 ms); 0 attaches at once (ignored by `attach`) |

- **run** — `cmd` REMAINDER, requires `-- CMD` (else 2, `cli.py:157`). Propagates the exit
  code: `128 + abs(code)` for signals (`cli.py:183`).
- **attach** — `-p/--pid`, `--duration` (10.0). `SIGSTOP`s/profiles/`SIGCONT`s, best effort:
  a pid we may not signal (systemd `clickhouse-server`) is profiled with a warning. Always 0.
  No `probe_attach()` on darwin. `ESRCH` → 2; `EPERM` → carry on. `probe_attach` passes **no
  trailing workload** with `-p`: perf attaches *or* launches, and doing both prints usage.
- **report** — `dir`; `load_profile(..., include_threads=True)`, re-renders terminal +
  `report.html`. Returns 0.
- **cycle** — `-n` (30), `--warmup` (1), `--sleep` (0.2), `--tsv FILE`, `-j` (`ncpus // 2`),
  `--no-pin`, `--metrics CSV`, `cmd`. No darwin. Pins to physical cores via `taskset`;
  **TSV to stdout, summary to stderr**. `KeyboardInterrupt` → `SystemExit(130)`.
- **diff** — `base compared`; prints `render_diff`.
- **doctor** — `macos_doctor()` on darwin else `run_doctor()`; `0 if rep.ok else 2`.

---

## Data flow

### Profile directory artifacts

| File | Written by | Phase |
|---|---|---|
| `perf.data` | `perf record -q -d -W -o` (+ co-joined memory and `sched:`) | measurement — **retired** unless `--keep-perf-data` |
| `recording.started` | `_write_recording_marker` — a driver's cue to start the workload | measurement |
| `stat_threads.csv` / `stat.csv` | `perf stat -x, --per-thread` / `perf stat -x,` (legacy path) | measurement |
| `perf_ibs.data` / `perf_mem.data` / `perf_wait.data` | standalone fallbacks (memory; legacy sched) | fallback — **retired** |
| `script.txt` | `perf script -i perf.data [--no-inline]`, compacted in place | post-target |
| `wait.txt` | `_wait_artifact` splits `sched:*` out of `script.txt`, deletes if empty | after `script.txt` |
| `mem_report.txt` | `perf mem report … --field-separator=\t --show-total-period [--sort …] [--time-quantum Nms]`, compacted | post-target, concurrent with `perf script` |
| `freq.json`, `rss.json` | `_FreqSampler` / `_RssSampler` | after samplers stop |
| `meta.json` | `_write_meta` — **last** of collection | end |
| `report.html` | `build_html` via `cli._finish` / `cmd_report` | report |

### Collection order (`_collect_combined`, `collector.py:975`)

1. Record `started`; `probe_wait()` decides whether `sched:` events go in.
2. `run`: `Popen(..., start_new_session=True)` → `_settle_target(..., startup_grace)` waits
   until the thread count holds still (20–50 ms) or the ceiling, then `SIGSTOP` the group.
   `attach`: `SIGSTOP` the pid (tolerating `T`/`t`); a refused stop
   leaves `target_paused` false, so only a freeze we took is continued.
3. `perf stat --per-thread -p` and `perf record -p` launch **independently** (no `--`), each
   with `_COLLECTOR_SETTLE_GRACE = 0.1 s`. Retry ladder (`collector.py:1098`): drop `-I` →
   drop co-joined memory → DWARF → fp.
4. Samplers start (10 ms); `t0` is `_sample_clock()` → `freq_t0`/`rss_t0`. **Not
   `time.monotonic()`:** perf's clock is the kernel's, which userspace may not read (a *time
   namespace* shifted `CLOCK_MONOTONIC` by 26,161 s), and perf runs ahead of
   `CLOCK_BOOTTIME` by ~0.9 ms/min. The collector also stamps the recording's ends on the
   samplers' clock (`record_launch_t0`; `record_exit_t0` after writing), and
   `_sample_clock_bias` turns those plus the samples into the offset.
5. Target `SIGCONT`s; `_monitor_collectors` runs until the deadline, the collectors finish,
   or 2.0 s after the target exits.
6. Samplers stopped (the last RSS reading is real: the target is alive) → target reaped
   (SIGTERM → 2 s → SIGKILL to the group) → `perf record` stopped → `perf stat` stopped.
7. `stat_threads.csv` parsed; aggregate via `_aggregate_thread_stats(...)`, falling back to
   `stat.csv`.
8. **`perf script` and `perf mem report` both start with `defer=True` before either is
   joined** (`collector.py:1288`), so the phase costs `max()`. Pinned by
   `test_post_target_dumps_overlap` — do not make it sequential. Each dump is compacted once
   its child is joined.
9. **Raw recordings retired** — `_retire_raw` (`collector.py:931`); `meta.perf_data = {kept,
   bytes}` (`bytes` `None` if nothing was recorded).
10. `freq.json`, `rss.json`, `meta.json`; `ProfileData` returns to `cli._finish`.

### Report pipeline (`cli._analyze` → `cli._finish`)

`parse_perf_script(stream, skip_events=memory_events)` (streamed) → drop `sched:*` →
`cap_stacks` → `build_profile(keep_sample_chains=True)` →
`scale_hotspot_times(task-clock/1000)` → `compute_metrics` → utilization-timeline fallback
when perf gave no stat intervals → `_load_mem_profile` / `_load_wait_profile` →
`render_terminal` → `build_html` → `report.html`.

`meta.json` carries `version, mode, target{…}, host, cpu_vendor, kernel, ncpus, freq,
interval_ms, events[], metrics[], precise_event, callgraph, inline, thread_stats{…},
memory{…}, wait{enabled,reason,detail}, perf_data{kept,bytes}, freq_t0, rss_t0, rss_peak,
record_launch_t0, record_exit_t0, startup_grace, perf_version, elapsed_wall`. macOS adds
`backend`, `callgraph: "sample"`, `cpu_vendor: "Apple"`, `interval_ms: 50`, and no
`perf_data` — read that with `.get()`. `perf_data` is not an input: `load_profile` reads
only text and JSON, so `report`/`diff` regenerate byte-identically without the recordings.

`wait.reason` explains empty wait columns: `disabled` (`--no-wait`), `unavailable` (probe
failed — `detail` is `doctor.wait_denial_reason()`: tracefs / `CAP_DAC_READ_SEARCH` plus the
host's `setcap` line), `empty` (recorded, dump held none), `unsupported` (macOS). `None`
when `wait.txt` exists. **Read it with `.get()`, never index** — `report` re-renders
directories older than the key.

---

## The report build and its scoping model

`report_html.py` is three layers: `_CSS` (`report_html.py:153`), `_JS` (`report_html.py:242`,
a **raw** `r"""..."""` string) and the Python renderers, assembled by `build_html`
(`report_html.py:2576`) into one f-string. Data arrives as **bare globals in a `<script>`
block emitted before `_JS`** (`report_html.py:2821`): `S`,
`FREQ`/`FREQ_T0`, `RSS`/`RSS_T0`/`RSS_PEAK`, `MEM_*`, `MEMORY_HTML`/`OVERVIEW_HTML`,
`THREAD_*`/`GROUP_OPTS`, `WAIT`, `T0`, `TSPAN`, `NCPU`, `TOTAL_CYCLES`, `CPU_TIME`,
`MAX_FLAME_DEPTH`; then `<script>init();</script>`.

**Samples ship once; everything else is folded in the browser.** `S = _sample_payload(...)` →
`[rows, syms, dsos, roots]`, rows `[tid, time, period, stack[], leaf_dso_idx, root_idx,
user_frames[]|null]`, interned via `_Interner`; only the whole-run all-threads flame SVG,
first hotspots table and first call tree are pre-rendered. `WAIT` is per-thread, **integer µs
from `T0`**, `null` when scheduler records do not overlap the sample window.

**Cannot follow the selection:** Overview metrics (`perf stat --per-thread` counts once; perf
refuses `-I` with it), the frequency chart, the RSS curve. Curves are placed by
`freq_t0`/`rss_t0`, *measured*: `_sample_clock_bias` compares the last sample with the
recording's exit, refuses the correction when samples claim a longer span, and adds the offset
— uncorrected the charts draw *empty*. `_place_curve` keeps whichever placement lands more
readings on the window; when the offset loses, the first reading belongs at the first sample.
`rssBuckets` fills *interior* gaps only. Escaping: `esc()` / `escHtml()`; numeric sort keys in
`data-v=`. Tabs via `showTab(btn, id)`, chart modes via `setChartMode`.

---

## The report UI contract

Behaviour, not tutorial — the README does not teach the UI.

- **Flame graph.** Click to zoom (subtree fills the width, path greys underneath); click again
  to go up. Height is the rows drawn, **never a row of hairlines**: it ends at the last row
  with a frame ≥ 2px wide, or 48 rows (`MAX_FLAME_DEPTH`); cut rows fold into their parent.
- **Time selection.** **Drag inside the plot** to select, **drag the selection** to move,
  **double-click** / `Reset Selection` to clear. The curve shows the **whole run** with the
  rest dimmed; all three modes share the axis. Utilization's y axis is **busy cores in scope,
  capped at what the scope could use**: all threads at the logical CPU count, a group at its
  thread count, **a single thread at one core** — so half a core reads 0.5.
- Follows the selection: **Hotspots**, **Flame Graph**, **Call Tree**, the **Memory** tab, the
  whole Threads tab. Chart and counters follow while dragging; heavy panels rebuild when the
  drag settles. Memory samples are known only to their `--time-quantum` slice, so a window
  cutting one in half counts half.
- **An attached profile is a process.** `clickhouse-server` includes ~164 `ThreadPool` workers
  and `MergeMutate`/`Fetch`/`Bg*` alongside the query's; read the query by grouping or scoping.
  The utilization ceiling is the pid's thread count.
- **Memory RSS.** Sampled every 10 ms from `/proc/<pid>/statm`, drawn between Utilization and
  Frequency; the peak is its own dashed line. **Whole-process, does not follow the thread
  selector** — a process is one address space (per-thread `statm` and `RssAnon`/`RssFile`
  report the total: 312.4 MiB from all four threads of a 4-thread process with 300 MiB on one).
  The Memory tab follows the scope. `--no-rss` removes curve and peak row.
- **Grouping by name.** One entry per thread name (`ThreadPool ×54` → one line) covering *every*
  thread of that name. Hotspots, utilization and the flame graph merge members' samples (a
  16-thread pipeline reads as up to 16 cores). Overview counters are **summed before anything
  is derived**, so group IPC is `Σinstructions / Σcycles`; the Memory tab sums members' IBS/PEBS
  samples and the scope line says how many had any. Without per-thread counters it falls back to
  thread count, cycle share and the CPU time implied.
- **Wait columns.** CPU and wait tables merge on tid; wait columns read `n/a` without
  tracepoints, and **the note names which absence**: `--no-wait`, a denied probe (carries the
  `setcap` line), an empty dump, macOS. **On-CPU + Off-CPU is exactly the observed window**:

  | Column | `prev_state` | Means |
  |---|---|---|
  | Sleep | `S` | interruptible — futexes, condvars, sleeping syscalls |
  | Blocked/IO | `D` | uninterruptible: disk I/O **and** cold-cache page-fault waits |
  | Runnable | `R` | run-queue wait after preemption |
  | *stopped* / *unknown* | — | traced, not attributable |

  A thread still switched out at the end is **uncharged**, not guessed at. Counts come off
  the shipped intervals, so server and browser agree. A wait **straddling a selection edge**
  is charged by the share inside it; the delay-band histogram stays whole-run.

### Metric thresholds

`metrics.all_hints` and the report's footnotes hang off these; changing one is a behaviour
change to both docs.

| Signal | Threshold | Reading |
|---|---|---|
| Effective CPU Utilization ≪ cores | — | serial or I/O-bound; threading opportunity |
| IPC | ≥ 2 / < 0.5 | compute-bound and efficient / stalled |
| Backend Bound | high | memory-hierarchy limited → LLC / L1D / dTLB rates |
| Frontend Bound | high | fetch/decode limited (i-cache, code footprint) |
| Bad Speculation | > 5% | mispredicts or machine clears |
| Retiring | < 30% | most slots lost; deep stall or contention |
| Branch mispredict | > 5% | unpredictable branches dominate |
| LLC miss rate | > 30% | exceeds cache — DRAM-bound on AMD; L1 misses reaching L3 on Intel (cross-check the Memory tab) |
| L1D miss rate | > 5% | data-cache thrashing; blocking/tiling |
| dTLB miss rate | > 1% | page-table walks |
| Vectorization ratio | < 50% | scalar or mixed-width code |
| Avg memory-access latency | > 200 cyc | deep stalls; prefetch or restructure |
| Blocked/IO ≫ On-CPU | — | data not in cache: disk wait, not compute |
| Runnable high, On-CPU low | — | oversubscribed; fewer threads or more work each |
| Sleep high, On-CPU low | — | idle-waiting (futex/condvar); the pipeline starves its workers |

---

## Invariants per subsystem

### Collection

- **Both text dumps are compacted in place, and may only lose what the parser already
  discarded.** `script.txt` (`_compact_script_file`, or `_wait_artifact`'s pass) rewrites
  perf's unsymbolized `<addr> [unknown] ([unknown])` to the same `[kernel]`/`[unresolved]`
  tag `parsers._frame` derives from the `ff` prefix — provably parse-identical, **20%** saved
  with `kernel.kptr_restrict=1` (249 → 199 MB; 1.7% on `membound`), ~400 ms per 75 MB.
  `mem_report.txt` (`_compact_mem_report`) strips perf's right-padding to the longest symbol:
  **87% of the file was spaces** (104 MB around 11 MB), 235 → 29 MB, ~145 ms per 104 MB. Both
  stay plain tab-separated text (greppable); gzip would break `load_profile`. Every helper is
  a **fixed point** and preserves line terminators — assert the *parse*, never the size.
  `_UNRESOLVED_FRAME_RE` requires 4+ hex digits on purpose: a null frame's bare `0` is left
  alone (`0 [unknown]`).
- **`load_profile` returns a fixed-shape 8-tuple**: `(meta, stat, script_path,
  mem_report_path, wait_path, freq_timeline, thread_stats, rss_timeline)`. Index 6 is the
  thread map slot *even when `None`*; consumers index positionally.
- **`perf` exits 0 with an empty report when it rejects a `--sort` key.** `_memory_report`
  has a 5-attempt ladder over `_MEMORY_SORT` and treats "rc 0 + empty" as **failure**
  (`_perf_error_summary`, `collector.py:322`). `perf report` has no `tgid` key — `pid`
  already means "command and tid".
- **`--startup-grace` is a correctness knob.** `perf stat --per-thread` reports only threads
  alive at attach (measured on `clickhouse-local`: 1 at 0 ms, 5 at 23 ms, 25 at 37 ms, 37 at
  61 ms, 38 at 106 ms). Threads created after the freeze are never counted as rows — do not
  estimate counters for them. The wait is adaptive: `_settle_target` polls
  `/proc/<pid>/task` and freezes once the count has held still for 20–50 ms (10% of the
  ceiling, floor 20 ms), so a short run is not lost to a fixed sleep. A target finishing
  inside the window is *reported rather than profiled*, so keep the ceiling below the
  shortest run of interest.
- **Deep stacks are iterative, never recursive**: `flamegraph.render_flame_svg`,
  `report_html._tree_html`, `stacks._build_call_tree`, `macos._collect_leaves`; `_Chain`
  (`macos.py:227`) uses `__slots__`.
- `cap_stacks` (128 frames, `stacks.py:73`) is applied **twice** — `cli._analyze` and inside
  `build_profile` — so a direct caller cannot build an unbounded tree.
- **The flame layout exists twice and must agree**: Python (`flamegraph._color`,
  `_rows_below`, `_frame_text`, `render_flame_svg`) and its JS twin (`flameSvg`,
  `flameRowsBelow`, `flameColor`, `flameLabelText`). `MAX_FLAME_DEPTH` is injected from
  Python; `test_report_html.py` asserts on the JS as a string contract.
- **Known latent bug:** `_FreqSampler` is defined **twice** — `collector.py:30` (dead,
  `interval_ms=`) and `collector.py:535` (live, `interval=` seconds). The legacy `collect()`
  path calls the old kwarg (`:1502`) and would raise; tests monkeypatch it. Collapse both if
  you touch either.

### The samplers and the PMU

`doctor`'s **`sample spread`** row says whether a profile on this host is a time series:
`probe_sampling_spread` looks at the span between first and last sample. A virtualised PMU
opens the event, delivers one burst seconds after launch and goes quiet — a 28 s recording
produced 15 samples inside 5 ms. Every hotspot, flame graph and timeline from such a host is
one instant, so it is a WARN, not a FAIL. The row also reports the clock offset, which
decides whether the header curves can be placed.

### Vendors

Three mechanisms, keyed off `doctor.cpu_vendor()`:

1. **Event gating** — `_probe_capabilities` (`collector.py:429`) probes `GENERIC_EVENTS +
   AMD_ONLY_EVENTS` only on `AuthenticAMD`, so Intel never emits `<not counted>` noise.
   Memoized in `_PROBE_CACHE` (tests clear it).
2. **Memory backend gating** — `_memory_plan` (`collector.py:217`) skips IBS on known Intel;
   Intel uses `probe_intel_mem()` + `_intel_memory_events()`, scraping `perf mem record -e
   list` / `perf list --details` for `mem-loads`/`mem-stores` and injecting `ldlat=`
   (`INTEL_LDLAT`, 30). Unknown vendor probes both.
3. **Interpretation gating** — `compute_metrics` prefers AMD `ls_any_fills_from_sys.*`, falls
   back to `LLC-loads`/`LLC-load-misses`, and records which in `MetricsReport.llc_source`;
   `cache_hierarchy_rows` labels rows accordingly. An Intel LLC reading counts L1 misses
   reaching L3, **not DRAM traffic** — never present it as a fill count. FP/vectorization
   rows are absent without the AMD events.

**Vendor pinning**: `meta.json` stores `cpu_vendor`, threaded through `compute_metrics(...,
vendor=)`, `compute_thread_metrics(...)`, `branch_mispredict_penalty(vendor)` and
`diff._analyze_dir`, so an AMD profile re-reported on Intel keeps its constants. `None` =
"this host"; unknown vendor gets `DEFAULT_BRANCH_PENALTY` 15.0 (AMD 13.0, Intel 15.0).

### Platforms

`sys.platform == "darwin"` is checked in exactly four places: `cli._ensure_access`
(`cli.py:31`), `cmd_attach` (`cli.py:216`), `cmd_cycle` (`cli.py:298`), `cmd_doctor`
(`cli.py:393`), plus `collector.collect` (`collector.py:1446`) delegating to `collect_macos`.
Heavy imports are deferred *inside* the branch so Linux never pays — keep it so.

### Wait / off-CPU analysis (`wait.py`)

The module docstring (`wait.py:1`) is the design document; this is what not to break.

- **The identity:** a thread is on-CPU only between two of its own `sched:sched_stat_runtime`
  points; the delta is its share of the gap, the rest was off-CPU. `sched:sched_switch` gives
  the switch-out instant and `prev_state`; `sched_process_exit` closes the window.
- **One stream, not paired switches:** only the target's tree is recorded (no `-a`), and a
  thread's switch-*in* is visible only when the CPU's holder was in the tree. The accounting
  stream cannot go missing; paired switches can.
- **`sched_stat_wait` / `_sleep` / `_blocked` / `_iowait` are deliberately not collected:**
  gated on scheduler delay accounting and unbuilt on some kernels — on 7.0.0-34-generic all
  four are listed and never fire (0 system-wide while `sched_switch` counts ~12k/s), which is
  how Sleep and Blocked/IO used to be permanently zero. `prev_state` cannot go missing.
- **Both renderings must parse:** positional `comm:pid [prio] STATE ==> comm:pid [prio]` and
  the older `prev_pid=`/`prev_state=` form (`_switch_line`, `wait.py:356`).
- **Slice model:** one flat row per thread — accounting span, the CPU its first point earned,
  and every off-CPU slice as `(start, length, state)` in integer µs on the sample clock;
  on-CPU is what remains, so a selection answers *exactly*, not by resampling. Cost: 22k
  slices / 269 KB / +2.6% for a 1.2 s ClickBench profile; folding is one linear pass inside
  the 140 ms drag debounce.
- **Preserve:** counts come off the slices, so a switch-out the window never showed owns no
  slice and is not counted; a wait straddling a selection edge is charged by its inside share.

### Errors

No exception hierarchy for user-facing conditions — only `PerfError` (`perf.py:14`) and
`MacosBackendError` (`backends/macos.py:34`). The channel is a **`warnings: list[str]`
accumulator** threaded through the collector and rendered by `cli._finish`; unrecoverable
failures print to stderr and `SystemExit(2)` (or `return 2`). A probe hitting `OSError`
returns `None` (soft skip). `except Exception` appears only around thread starts and in test
helpers. stdout carries data; stderr carries diagnostics, progress, summaries.

---

## Conventions

- `from __future__ import annotations` everywhere except `__init__.py`, `__main__.py`,
  `backends/__init__.py`.
- Modern generics and PEP 604 unions throughout (`dict[str, float]`, `str | None`), including
  dataclass fields. Some parameters stay bare (`report_html.esc`, `_fmt`, `_fmt_count`,
  `timeline.utilization_from_samples`), with quoted forward refs (`m: "MetricsReport"`).
- **Types are honest about `None`**: optional metrics are `float | None`, render as `"n/a"`,
  never coerced to `0`.
- Docstrings: one-line imperative summary, blank line, prose. **No `Args:`/`Returns:`.**
  Parameters in RST italics (`*vendor*`).
- Naming: lowercase modules without underscores; `snake_case` functions; `_`-private;
  `UPPER_SNAKE` constants; `PascalCase` dataclass nouns. Compat aliases are plain assignments
  with a comment (`ThreadStat = ThreadStatData`, …). `__all__` exists in `doctor`, `memory`,
  `parsers`, `cycle`, `wait`, `diff`, `backends.macos` — not in `cli`, `collector`, `stacks`,
  `metrics`, `flamegraph`, `perf`, `timeline`, `report_*`.
- HTML/CSS/JS: lowerCamelCase classes and globals, `--kebab-case` custom properties. **A JS
  literal containing an apostrophe must escape it or use double quotes**: one unescaped `'`
  makes the whole `<script>` unparseable — no charts, no tabs, no selection, every byte of
  data still in the file. String contracts cannot see it, so `_js_lex_errors` walks `_JS` and
  every generated block, and a `node --check` test covers it where node exists.
- Compat aliases and duplicated helpers are deliberate; remove one and its aliases in the
  same commit.

---

## Testing

No `conftest.py`, no markers, no shared fixtures. 15 files, ~300 test functions: plain
functions plus grouping classes. `test_parsers.py` (byte-exact stat/script/stacks/flamegraph),
`test_collector.py` (passes, `meta.json`, artifacts — monkeypatched fakes),
`test_report_html.py` (HTML + **string contracts on `_CSS`/`_JS`**), `test_wait_analysis.py`
(sched ledger + live integration), `test_memory*.py` (IBS + PEBS), `test_vendor_metrics.py`,
`test_hpc.py` / `test_bad_spec.py`, `test_integration_cpp.py` (hardware signatures), and
`test_cycle.py` / `test_diff.py` / `test_perf.py` / `test_doctor_caps.py` /
`test_macos_sample.py`.

Three styles: **byte-exact fixtures** (`WHOLE_RUN`, `PER_THREAD`, `INTERVALS`, `SCRIPT`,
`IBS_SCRIPT`, `MEM_REPORT`, `PEBS_REPORT`, `MEMORY_REPORT`, `SAMPLE_TWO_THREADS`, `_FP_CSV` —
do not reformat); **monkeypatched fakes** (`_FakeDeferred`, `_FakeCollector`, `_FakeTarget`,
`_FakeSampler`, `_FakeRssSampler`, `_TimelineDeferred`, `fake_run_perf`/`fake_start_perf`,
and `stub_perf` setting `perf_mod.PERF = "python3"`; the live `_FreqSampler` is never
exercised — see the latent bug); **integration** against `examples/bin/*` from `make -C
examples` (`g++ -O3 -march=x86-64-v3 -fno-omit-frame-pointer`).

`test_report_html.py` asserts **substrings of `_CSS`/`_JS`**, sometimes with occurrence
counts and sometimes that a body does *not* contain something. Renaming a JS function means
editing those tests in the same commit.

Skipping via `pytest.mark.skipif`: the module gate in every perf-touching file (`not
shutil.which("perf") or not probe_stat(["-e", "task-clock"])[0]`), class gates (`not
probe_ibs()`, `not probe_intel_mem()`, `not probe_wait()`), and inline `pytest.skip(...)` for
single assertions.

`test_integration_cpp.py` asserts the documented signatures: SIMD IPC > 2, chase IPC < 0.35
(>5× contrast), L1/L2 misses > 10M with 10× contrast, LLC miss rate > 45% for the chase (30%
on Intel), dTLB misses > 5M, page faults covering every 4 KiB page.

---

## Benchmarks and examples

```bash
make -C examples                                    # -> examples/bin/
vperf run -- examples/bin/simd_levels_avx 2000000   # IPC ~2.9, L1-resident
vperf run -- examples/bin/membound 268435456 1.5    # IPC ~0.14, miss storm
bench/clickbench_profiles.sh --engine both          # sequential; never parallel
bench/clear-caches.py                               # fadvise DONTNEED + mincore verify
LABEL=ik_llama bench/llama_cpp_profile.sh -t 8 -n 100
```

`clear-caches.py` needs no root (`POSIX_FADV_DONTNEED` + `mincore`), so a run is *known* cold.
Startup grace is per engine: `clickhouse-local` ~90 ms for its pool against a 140 ms cheapest
query; duckdb 16 threads within 8 ms against 80 ms.

`clickbench_profiles.sh --mode server` runs one `vperf attach` per query. Non-optional traps:
**the driver ends the window** (launch attach with a ceiling, `SIGINT` it when the client
returns); **readiness comes from the freeze, else `recording.started`** (a sub-second query's
`SIGINT` otherwise lands in a gap); **`--private-server` profiles the port's owner, never
`$!`** (`clickhouse server` forks a `ClickHouseWatch` supervisor with 7 threads against the
child's 318). Every `clickhouse-client` call must carry `${CONN_ARGS[@]}`, the query included.
`LOAD="skip"` reuses `OUT_ROOT/_private-clickhouse`; never wipe it (a load is 100M rows /
~200 s).

---

## Git

- One feature/fix branch per PR, merged with a merge commit.
- Commit subjects: lowercase, Conventional-Commit-ish, scoped, with a PR number, stating
  **what the reader now sees**: `fix(html): the Threads tab leads with its graphics (#41)`.
- Never commit `.vperf/` or `examples/bin/` (gitignored). No secrets — `vperf` never handles
  credentials.
