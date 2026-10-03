# AGENTS.md — working on `vperf`

Guidance for humans and AI agents changing this repository.

**`README.md` is the user-facing document** (features, setup, CLI flags, the
report UI, how to read the numbers). This file is the *engineering* document:
module map, data flow, internal contracts, conventions and the traps that will
bite you. Read `README.md` first if you have not — it explains what the tool
measures and why the numbers mean what they say.

When a change alters observable behaviour, **both files may need updating**:

| Change | Update |
|---|---|
| a metric, threshold, or the meaning of a report number | `README.md` (vendor table, VTune interpretation table, relevant section) **and** `AGENTS.md` if it touches a contract below |
| a CLI flag or its default | `README.md` (Usage + Flags) **and** `AGENTS.md` flag table |
| a new artifact in the profile directory | `README.md` (artifact paragraph) **and** `AGENTS.md` (artifact table) |
| internals, tests, conventions | `AGENTS.md` only |

---

## Non-negotiables

1. **Zero runtime dependencies.** Everything `vperf` imports is stdlib. A venv
   exists only for `pytest` and `ruff`. Do not add a runtime import that a
   `pip install` of `vperf` alone would not satisfy.
2. **Never run the suite with `pytest -n` / `pytest-xdist`.** The integration
   tests assert exact PMU counter relationships; concurrent profiling sessions
   multiplex the hardware counters and break those assertions. The same reason
   makes `bench/clickbench_profiles.sh --engine both` run its engines
   sequentially. Do not add `pytest-xdist` to the venv, do not parallelize the
   profiling tests.
3. **Every `json.dumps(...)` in `report_html.build_html` must be followed by
   `.replace("</", "<\\/")`.** The payload is emitted into a `<script>` block as
   bare globals; without the guard a `</script>` in a symbol name closes the tag.
4. **Never reformat or "fix" the fixture string constants in
   `tests/test_parsers.py` / `tests/test_memory_ibs.py`.** They reproduce real
   `perf script` / `perf mem report` output byte-for-byte, tabs included. The
   ruff per-file ignores (`E101`, `W29`, `E501`) exist for exactly that reason.
5. **Comments and docstrings in this repo carry measurements and hardware
   facts.** They are load-bearing documentation ("clickhouse-local needs ~90 ms to
   build its pool", "measured ~40M dTLB misses for membound vs ~5k for simd").
   Preserve them when editing around a line; add the same kind of comment when
   you add a limit.
6. **The target is always `SIGCONT`'d.** Any change to the collection path must
   keep the `try/finally` that resumes it (`collector.py:884`) and the
   `PerfProcess.stop()` escalation (SIGINT → SIGTERM → SIGKILL) that flushes a
   partial `perf.data`.

---

## Commands

```bash
# one-time env (uv is the documented path)
uv venv && uv pip install -e . pytest ruff
source .venv/bin/activate

pytest tests/ -q                 # unit + integration; needs real perf access
ruff check vperf/ tests/         # the only lint gate

# the profiler itself needs no venv at all
python3 -m vperf doctor
PYTHONPATH=$PWD python3 -m pytest tests/ -q
```

- `ruff` is configured in `pyproject.toml`: `line-length = 140`,
  `select = ["E", "F", "W"]`. **No isort, no formatter, no mypy config.** Style
  is hand-maintained; there is a `.mypy_cache/` but mypy is not installed and is
  not a gate — do not treat it as one. `bench/` and `examples/` are not linted.
- A green suite needs Linux + `perf` on PATH + `kernel.perf_event_paranoid <= 1`
  (or `CAP_PERFMON`). The wait tests additionally need `paranoid <= 0` or
  `CAP_PERFMON`/`CAP_DAC_READ_SEARCH`; on Debian/Ubuntu `setcap` must target
  `/usr/lib/linux-tools/$(uname -r)/perf`, not the `/usr/bin/perf` shell wrapper
  (the kernel ignores file capabilities on scripts). `vperf doctor` prints the
  exact command for the host.

---

## Module map

| File | Lines | Responsibility |
|---|---|---|
| `vperf/cli.py` | 460 | argparse surface, subcommand dispatch, the shared analyze→report path |
| `vperf/collector.py` | 1461 | the orchestrator: capability probes, the perf passes, artifact writing |
| `vperf/perf.py` | 215 | thin, well-behaved wrapper around the `perf` binary |
| `vperf/doctor.py` | 429 | capability probes + environment report (the source of truth for what this host supports) |
| `vperf/parsers.py` | 504 | `perf stat -x,` CSV and `perf script` → dataclasses |
| `vperf/stacks.py` | 303 | samples → hotspots, folded stacks, call tree |
| `vperf/metrics.py` | 391 | the derived-metric model (`MetricsReport`, ~40 optional floats) |
| `vperf/memory.py` | 354 | `perf mem report` parser (IBS + PEBS) |
| `vperf/wait.py` | 533 | scheduler-tracepoint off-CPU analysis |
| `vperf/timeline.py` | 300 | server-side SVG charts (util / threads / frequency) |
| `vperf/flamegraph.py` | 207 | folded stacks → interactive server-side SVG flame graph |
| `vperf/report_html.py` | 2624 | the single-file offline dashboard (CSS + JS embedded) |
| `vperf/report_terminal.py` | 267 | terminal summary tables |
| `vperf/cycle.py` | 125 | cycle-mode TSV matrix + statistical summary |
| `vperf/diff.py` | 154 | two-profile comparison |
| `vperf/backends/macos.py` | 478 | macOS `sample` + `ps` backend |

Entry points: console script `vperf = "vperf.cli:main"`, and
`python3 -m vperf` (`vperf/__main__.py`).

Notable APIs:

- `collector.collect(target_cmd, pid, outdir, ...) -> ProfileData` and
  `collector.load_profile(outdir, include_threads=False)`.
- `cli._analyze(...)` / `cli._finish(...)` are shared by `run`, `attach` and
  `report`, so a report regenerated later is byte-identical to the original.
- `report_html.build_html(meta, samples, m, prof, mem, wp, freq_timeline,
  rss_timeline) -> str`.

---

## CLI surface

`build_parser()` is at `cli.py:364`; `main(argv=None)` at `cli.py:452`.
Subcommands: **run, attach, report, cycle, diff, doctor**.

### Shared by `run` and `attach` (`cli.py:372`)

| Flag | Type / default | Effect |
|---|---|---|
| `-o, --outdir` | str / `.vperf/<mode>_<YYYYmmdd_HHMMSS>` | profile directory |
| `-f, --freq` | int / 199 | CPU sampling Hz |
| `-I, --interval` | int ms / off | `perf stat` interval; conflicts with `--per-thread` |
| `--no-stat` | flag | skip the counting pass |
| `--callgraph` | `dwarf`\|`fp`\|`none` / `fp` | unwinder |
| `--no-wait` | flag | skip scheduler tracepoints |
| `--no-rss` | flag | skip the RSS sampler |
| `--mem-period` | int / 100003 | AMD IBS sampling period (cycles) |
| `--mem-time-quantum` | ms / ~100 slices, clamped 25 ms–1 s | Memory-tab slice width |
| `--no-inline` | flag | drop DWARF inline expansion |
| `--startup-grace` | float s / 0.15 | settle window before freezing the target (ignored by `attach`) |

### Per subcommand

- **run** — `cmd` positional, `nargs=REMAINDER`, `metavar="-- CMD"`
  (`cli.py:413`). Requires `-- CMD` (returns 2 otherwise, `cli.py:154`).
  **Propagates the target's exit code**: `128 + abs(code)` for signals
  (`cli.py:179`).
- **attach** — `-p/--pid` (required), `--duration` (default 10.0).
  `SIGSTOP`s the pid, profiles, `SIGCONT`s. Always returns 0. Skips
  `probe_attach()` on darwin.
- **report** — positional `dir`. Loads artifacts with
  `load_profile(..., include_threads=True)` and re-renders terminal +
  `report.html`. Returns 0 (it has no target to report).
- **cycle** — `-n/--runs` (30), `--warmup` (1), `--sleep` (0.2), `--tsv FILE`,
  `-j/--jobs` (default `ncpus // 2`), `--no-pin`, `--metrics CSV`,
  `cmd REMAINDER`. Refuses on darwin. Pins runs round-robin to distinct physical
  cores via `taskset`; **the TSV goes to stdout, the summary to stderr**, so
  `> file.tsv` stays clean. `KeyboardInterrupt` → `SystemExit(130)`.
- **diff** — positionals `base compared`. Prints `render_diff`.
- **doctor** — no arguments. Dispatches to `macos_doctor()` on darwin, else
  `run_doctor()`. Returns `0 if rep.ok else 2`.

---

## Data flow

### Profile directory artifacts

| File | Written by | Phase |
|---|---|---|
| `perf.data` | `perf record -q -d -W -o` (+ co-joined memory and `sched:` events) | during measurement |
| `stat_threads.csv` | `perf stat -x, --per-thread` | during measurement |
| `stat.csv` | `perf stat -x,` — legacy non-combined path only | during measurement |
| `perf_ibs.data` / `perf_mem.data` | standalone memory record, only when the co-joined record failed | fallback only |
| `script.txt` | `perf script -i perf.data [--no-inline]` | post-target |
| `wait.txt` | `_wait_artifact` splits the `sched:*` lines **out of** `script.txt` and rewrites it without them; deleted if empty | after `script.txt` |
| `mem_report.txt` | `perf mem report -i perf.data --stdio --field-separator=\t --show-total-period [--sort …] [--time-quantum Nms]` | post-target, concurrent with `perf script` |
| `freq.json`, `rss.json` | `_FreqSampler` / `_RssSampler` threads | after samplers stop |
| `meta.json` | `_write_meta` — **last** of the collection phase | end |
| `report.html` | `build_html` via `cli._finish` / `cmd_report` | report |

### Collection order (`_collect_combined`, `collector.py:735`)

1. Record `started`; `probe_wait()` decides whether `sched:` events go into the
   record.
2. `run`: `Popen(target_cmd, start_new_session=True)` → `_settle_target(...,
   startup_grace)` → `SIGSTOP` the process group. `attach`: `SIGSTOP` the pid
   (tolerating state `T`/`t`).
3. `perf stat --per-thread -p <pid>` and `perf record -p <pid>` are launched
   **independently** (no `--`, so neither waits for the target to exit), each
   given `_COLLECTOR_SETTLE_GRACE = 0.1 s` to fail fast. Retry ladder on failure
   (`collector.py:822`): drop `-I` intervals → drop co-joined memory → downgrade
   DWARF to fp.
4. `_FreqSampler(interval=0.01)` and `_RssSampler(pid, interval=0.01)` start.
   Their `t0` is `time.monotonic()` and is the clock that lines `freq.json`,
   `rss.json` and the perf sample timestamps up — it must be written to
   `meta.json` as `freq_t0` / `rss_t0`.
5. Target `SIGCONT`s; `_monitor_collectors` runs until the deadline, the
   collectors finish, or 2.0 s after the target exits.
6. Samplers stopped (the last RSS reading is real because the target is still
   alive) → target reaped (SIGTERM → 2 s → SIGKILL to the group) → `perf record`
   stopped → `perf stat` stopped.
7. `stat_threads.csv` parsed; the aggregate `StatData` is
   `_aggregate_thread_stats(thread_stats)`, falling back to `stat.csv`.
8. **`perf script` and `perf mem report` are both *started* with `defer=True`
   before either is joined** (`collector.py:952`) so the phase costs `max()` of
   the two rather than their sum. Pinned by `test_post_target_dumps_overlap`.
   Do not "tidy" this into a sequential block.
9. `freq.json`, `rss.json`, then `meta.json`; `ProfileData` returns to
   `cli._finish`.

### Report pipeline (`cli._analyze` → `cli._finish`)

`parse_perf_script(stream, skip_events=memory_events)` (streamed — the dump can
be hundreds of MB) → drop `sched:*` samples → `cap_stacks` →
`build_profile(keep_sample_chains=True)` → `scale_hotspot_times(task-clock/1000)`
→ `compute_metrics` → utilization-timeline fallback when perf gave no stat
intervals → `_load_mem_profile` / `_load_wait_profile` → `render_terminal` to
stdout → `build_html` → `report.html`.

`meta.json` keys: `version, mode, target{cmd,pid,duration,exit_code}, started,
host, cpu_vendor, kernel, ncpus, freq, interval_ms, events[], metrics[],
precise_event, callgraph, inline, thread_stats{enabled,cojoined,file},
memory{enabled,backend,period,ldlat,events,data_file,cojoined,time_quantum_ms},
wait{enabled}, freq_t0, rss_t0, rss_peak, startup_grace, perf_version,
elapsed_wall`. The macOS backend adds `backend: "macos"`, `callgraph: "sample"`,
`cpu_vendor: "Apple"`, `interval_ms: 50`.

---

## The report build and its scoping model

`report_html.py` is one module in three layers: `_CSS` (`report_html.py:42`, a
minified custom-property theme), `_JS` (`report_html.py:131`, a **raw**
`r"""..."""` string), and the Python renderers, assembled by `build_html`
(`report_html.py:2402`) into a single f-string.

Data reaches the browser as **bare global assignments in a separate `<script>`
block emitted before `_JS`** (`report_html.py:2608`): `S=`, `FREQ=`, `FREQ_T0=`,
`RSS=`, `RSS_T0=`, `RSS_PEAK=`, `MEM_BACKEND=`, `MEM_ROWS=`, `MEM_SYM=`,
`MEM_CHART_TITLES=`, `MEM_TLB=`, `MEM_SLICES=`, `MEM_Q=`, `MEM_TRUNC=`,
`MEMORY_HTML=`, `OVERVIEW_HTML=`, `THREAD_GROUPS=`, `THREAD_OPTS=`,
`GROUP_OPTS=`, `THREAD_CPU=`, `WAIT=`, `T0=`, `TSPAN=`, `NCPU=`,
`TOTAL_CYCLES=`, `CPU_TIME=`, `MAX_FLAME_DEPTH=`, `MEM_LEVELS=`, `MEM_BANDS=`.
A final `<script>init();</script>` boots it.

The central performance invariant: **samples ship once and everything else is
folded in the browser.**

- `S = _sample_payload(samples, prof)` → `[rows, syms, dsos, roots]`, each row
  `[tid, time, period, stack[], leaf_dso_idx, root_idx, user_frames[]|null]`,
  interned through `_Interner`. Per-thread and per-group copies were dropped
  because they took one ClickBench report from 6.8 MB to 22.6 MB.
- Only the whole-run all-threads flame SVG, the first hotspots table and the
  first call tree are pre-rendered server-side (first paint / no-JS fallback).
- `MEMORY_HTML` / `OVERVIEW_HTML` are `dict[str, html]` keyed `"all"`, a bare tid,
  or `"gN"`; the browser picks the key matching the current `scopeKey`.
  `THREAD_GROUPS` maps `gN -> tids`; a name owned by exactly one thread reuses
  that tid's key.
- `WAIT` is a flat per-thread timeline in **integer microseconds from `T0`**, and
  is `null` when the scheduler records do not overlap the sample window (a wait
  pass collected separately describes a *different run*). Null keeps the columns
  whole-run and says so in place.
- **What cannot follow the time selection, by design:** the Overview metrics
  (`perf stat --per-thread` counts once over the whole run; perf refuses `-I`
  together with `--per-thread`), the frequency chart, and the RSS curve (a
  process is one address space — procfs has no per-thread footprint). The HTML
  reveals a `.whole-run-note` via `body.sel-active` rather than silently
  reporting the run as if it were the window.
- HTML escaping: `esc()` in Python, `escHtml()` in JS. Numeric sort keys go in
  `data-v=` attributes so `sortTable` can compare numerically.

Tabs are `overview, hotspots, mem, flame, tree, threads`, switched by
`showTab(btn, id)`; header chart modes `util`/`mem`/`freq` via `setChartMode`;
the Memory tab has its own count/latency switch.

---

## Invariants per subsystem

### Collection

- **`load_profile`'s return value is a fixed-shape 8-tuple**:
  `(meta, stat, script_path, mem_report_path, wait_path, freq_timeline,
  thread_stats, rss_timeline)`. Index 6 is the thread map slot *even when it is
  `None`*. Consumers index it positionally — do not return `None` in place of a
  trailing slot or reorder it.
- **`perf` exits 0 with an empty report when it rejects a `--sort` key.**
  `_memory_report` therefore has a 5-attempt ladder over `_MEMORY_SORT` variants
  and treats "rc 0 + empty output" as a **failure**, not as "no samples"
  (`_perf_error_summary`, `collector.py:300`). `perf report` has no `tgid` key —
  `pid` already means "command and tid"; do not add one to the sort lists.
- **`--startup-grace` is a correctness knob, not a speed knob.**
  `perf stat --per-thread` only reports threads alive at the instant it attaches.
  Measured on `clickhouse-local`: 1 thread at 0 ms, 3 at 11 ms, 25 at 42 ms, 43
  at 93 ms. Threads created after the freeze are never counted — do not
  estimate them. A target that finishes inside the grace window is *reported
  rather than profiled*, so the value must stay below the shortest run of
  interest.
- **Deep stacks must be handled iteratively, never recursively.**
  `flamegraph.render_flame_svg`, `report_html._tree_html`,
  `stacks._build_call_tree` and `macos._collect_leaves` all use an explicit
  stack; `_Chain` (`macos.py:227`) uses `__slots__` for O(1) leaf emission. A
  broken frame-pointer chain records thousands of frames per sample.
- `cap_stacks` (128 frames, `stacks.py:73`) is applied **twice** — once in
  `cli._analyze`, once inside `build_profile`. That is deliberate, so a direct
  caller cannot build an unbounded tree. Keep both.
- **The flame-graph layout exists twice and the two copies must agree**: the
  Python side (`flamegraph._color`, `_rows_below`, `_frame_text`,
  `render_flame_svg`) and the JS twin in `_JS` (`flameSvg`, `flameRowsBelow`,
  `flameColor`, `flameLabelText`). `MAX_FLAME_DEPTH` is injected into JS from
  Python. Change one and you must change the other; `tests/test_report_html.py`
  asserts on the JS as a string contract.
- **Known latent bug:** `_FreqSampler` is defined **twice** — `collector.py:29`
  (dead code, `interval_ms=` kwarg) and `collector.py:514` (the live one,
  `interval=` seconds). The legacy `collect()` path still calls it with the old
  kwarg (`collector.py:1149`), so that path would raise. Tests monkeypatch
  `collector._FreqSampler` with `_FakeSampler`, so it is not exercised. Collapse
  the two definitions if you touch either.

### Vendors

Three independent mechanisms, all keyed off `doctor.cpu_vendor()`:

1. **Event gating** — `_probe_capabilities` (`collector.py:407`) probes
   `GENERIC_EVENTS + AMD_ONLY_EVENTS` only on `AuthenticAMD`, so Intel never
   emits `<not counted>` noise. Memoized in `_PROBE_CACHE` (cleared by tests).
2. **Memory backend gating** — `_memory_plan` (`collector.py:195`) skips the IBS
   probe entirely on a known-Intel host; Intel falls through to
   `probe_intel_mem()` + `_intel_memory_events()`, which scrapes
   `perf mem record -e list` / `perf list --details` for `mem-loads`/`mem-stores`
   and injects `ldlat=` (`INTEL_LDLAT`, default 30). Unknown vendor probes both.
3. **Interpretation gating** — `compute_metrics` prefers AMD
   `ls_any_fills_from_sys.*` and falls back to `LLC-loads`/`LLC-load-misses`,
   recording which in `MetricsReport.llc_source` (`LLC_SOURCE_AMD` vs
   `LLC_SOURCE_GENERIC`); `cache_hierarchy_rows` then labels each row with the
   definition that produced it. An Intel LLC reading counts L1 misses that
   reached L3, **not DRAM traffic**, and must never be presented as a fill count.
   FP/vectorization rows are simply absent when the AMD events are missing.

**Vendor pinning for reproducibility**: `meta.json` stores `cpu_vendor`, which is
threaded through `compute_metrics(..., vendor=)`, `compute_thread_metrics(...,
vendor=)`, `branch_mispredict_penalty(vendor)` and `diff._analyze_dir`. So a
profile collected on AMD re-reported on Intel keeps the constants it was
collected with. `branch_mispredict_penalty(None)` means "this host"; an unknown
vendor gets `DEFAULT_BRANCH_PENALTY` 15.0 (AMD 13.0, Intel 15.0).

### Platforms

`sys.platform == "darwin"` is checked in exactly four places: `cli._ensure_access`
(`cli.py:30`), `cli.cmd_attach` (`cli.py:191`), `cli.cmd_cycle` (`cli.py:255`),
`cli.cmd_doctor` (`cli.py:350`), plus `collector.collect` (`collector.py:1094`)
which delegates to `collect_macos`. Heavy imports are deferred *inside* the
branch so Linux never pays for them — keep it that way.

### Wait / off-CPU analysis (`wait.py`)

The module docstring (`wait.py:1`) is the design document; this is the short
version of what an agent must not break.

- **The identity:** a thread is only on-CPU between two of its own
  `sched:sched_stat_runtime` accounting points. The delta is its on-CPU share of
  the gap; everything else in the gap was off-CPU. `sched:sched_switch` supplies
  the switch-out instant and `prev_state`; `sched_process_exit` closes the
  window. On-CPU + Off-CPU is therefore exactly the thread's observed window.
- **Why the split rests on one stream, not on paired switch events:** only the
  target's own process tree is recorded (no `-a`, so the totals are still
  right), but a thread's switch-*in* is only visible when the task that held the
  CPU was also in the tree. The accounting stream cannot go missing; paired
  switches can.
- **Why `sched_stat_wait` / `_sleep` / `_blocked` / `_iowait` are deliberately
  not collected:** they are gated on the scheduler's delay accounting and are not
  built on some kernels — on 7.0.0-34-generic `perf list` names all four and none
  ever fires (0 events system-wide while `sched_switch` counts ~12k/s), which is
  exactly how the Sleep and Blocked/IO columns used to come out permanently zero.
  `prev_state` is the state that cannot go missing.
- **`prev_state` vocabulary → report column:** `S` → Sleep (interruptible),
  `D` → Blocked/IO (uninterruptible — disk I/O *and* page-fault waits),
  `R` → Runnable (run-queue after preemption), plus `stopped` / `unknown`.
- **Both renderings of the event must be parsed**: the positional
  `comm:pid [prio] STATE ==> comm:pid [prio]` that `perf script` prints, and the
  `prev_pid=`/`prev_state=` field form older perf produced (`_switch_line`,
  `wait.py:356`).
- **A thread still switched out at the end is left uncharged, not guessed at.**
  A row's On-CPU + Off-CPU covers its observed window; the tiny first delta was
  earned before the window opened.
- **The slice model.** Each thread ships as one flat row — where its accounting
  points start and end, the CPU its first point earned before them, and every
  off-CPU slice as `(start, length, state)` — in integer microseconds on the
  sample clock. On-CPU is what is left of the covered span once the slices have
  taken their share. That is why a time selection answers these columns
  *exactly* rather than by resampling, and why the whole window folds back onto
  the server-rendered numbers. Cost: 22k slices / 269 KB / +2.6% of the report
  for a 1.2 s ClickBench profile; folding is one linear pass inside the 140 ms
  drag debounce.
- **Two consequences to preserve:** the counts (Preempted, Sleeps, Blocks) are
  read off the slices, so a switch-out whose wait the window never showed owns
  no slice and is not counted — which is also what makes the server's count and
  the browser's the same number; and a wait that **straddles a selection edge** is
  charged by the share inside it, so counts can come out fractional (rounded for
  display) and a halved slice contributes half its seconds.

### Errors

There is no exception hierarchy for user-facing conditions — only `PerfError`
(`perf.py:14`) and `MacosBackendError` (`backends/macos.py:34`). The primary
error channel is a **`warnings: list[str]` accumulator** threaded through the
collector and rendered by `cli._finish`. Recoverable failures append to it;
unrecoverable ones `print(..., file=sys.stderr)` and `raise SystemExit(2)`, or
`return 2` from a `cmd_*`. A probe that hits `OSError` returns `None` (soft
skip), never raises. `except Exception` appears only around thread starts and in
test helpers. stdout carries data; stderr carries diagnostics, progress and
summaries.

---

## Conventions

- `from __future__ import annotations` at the top of every module except
  `__init__.py`, `__main__.py`, `backends/__init__.py`.
- Modern builtin generics and PEP 604 unions throughout
  (`dict[str, float]`, `list[ScriptSample]`, `str | None`), including in
  dataclass fields. Return types annotated on most public functions; some
  deliberately leave parameters bare (`report_html.esc`, `_fmt`, `_fmt_count`,
  `timeline.utilization_from_samples`) and use quoted forward refs
  (`m: "MetricsReport"`).
- **Types are honest about `None`.** Optional metrics are `float | None` and
  render as `"n/a"`; they are never coerced to `0`.
- Docstrings: one-line imperative summary, blank line, then prose. **No
  `Args:`/`Returns:` sections anywhere.** Parameter references use RST italics
  (`*vendor*`, `*min_cpu_s*`). The "why" is the content — several are 10–25
  lines of design rationale.
- Naming: modules lowercase without underscores; `snake_case` functions;
  private `_`-prefixed; constants `UPPER_SNAKE` (private `_UPPER_SNAKE`);
  dataclasses are `PascalCase` nouns. Backwards-compat aliases are plain
  assignments with a comment (`ThreadStat = ThreadStatData`,
  `compute_metrics_for_thread = compute_thread_metrics`,
  `parse_per_thread_stat = parse_per_thread_stat_csv`).
  `__all__` exists in `doctor`, `memory`, `parsers`, `cycle`, `wait`, `diff` and
  `backends.macos` — not in `cli`, `collector`, `stacks`, `metrics`,
  `flamegraph`, `perf`, `timeline`, `report_*`.
- HTML/CSS/JS: lowerCamelCase classes and JS globals, `--kebab-case` CSS custom
  properties.
- Backwards-compat aliases and duplicated helpers are deliberate; when you
  remove one, remove its aliases in the same commit.

---

## Testing

No `conftest.py`, no custom markers, no shared fixtures. 15 files, ~247 test
functions: plain functions plus a handful of grouping classes
(`TestSimdTiers`, `TestIpcSignatures`, `TestCacheMissDetection`,
`TestPageMissDetection`, `TestBoundAnalysis`, `TestCollectionPlumbing`,
`TestParseUnit`, `TestFold`, `TestIbsIntegration`, `TestIntelPebsIntegration`,
`TestWaitIntegration`, `TestCycleIntegration`, `TestAmdFillEvents`,
`TestIntelLlcLoads`, `TestBackendLabel`, `TestVendorDetection`,
`TestVectorizationCharacterization`).

| File | Covers |
|---|---|
| `test_parsers.py` | stat CSV, perf script, stacks, flamegraph — byte-exact fixtures |
| `test_collector.py` | collection passes, `meta.json`, artifacts — monkeypatched fakes |
| `test_report_html.py` | HTML structure + **string contracts on `_CSS`/`_JS`** |
| `test_wait_analysis.py` | sched ledger + folds, plus a live wait integration |
| `test_memory.py` / `test_memory_ibs.py` | `perf mem report` parsing; IBS + PEBS unit and integration |
| `test_vendor_metrics.py` | AMD fill events vs Intel LLC loads |
| `test_hpc.py`, `test_bad_spec.py` | AMD FP width / vectorization, Bad Speculation model |
| `test_integration_cpp.py` | hardware signatures of the `examples/` binaries |
| `test_cycle.py`, `test_diff.py`, `test_perf.py`, `test_doctor_caps.py`, `test_macos_sample.py` | respective subsystems |

Three testing styles, all in use:

1. **Byte-exact fixtures** — module-level string constants reproducing real perf
   output (`WHOLE_RUN`, `PER_THREAD`, `INTERVALS`, `SCRIPT`, `IBS_SCRIPT`,
   `MEM_REPORT`, `PEBS_REPORT`, `MEMORY_REPORT`, `SAMPLE_TWO_THREADS`,
   `_FP_CSV`). Do not reformat them.
2. **Monkeypatched fakes** for collector plumbing: `_FakeDeferred`,
   `_FakeCollector`, `_FakeTarget`, `_FakeSampler`, `_FakeRssSampler`,
   `_TimelineDeferred`, `fake_run_perf`/`fake_start_perf`, and a `stub_perf`
   fixture that sets `perf_mod.PERF = "python3"` so a Python script stands in for
   perf. The live `_FreqSampler` is never exercised this way — see the latent
   bug above.
3. **Integration** against `examples/bin/*`, built by `make -C examples`
   (`g++ -O3 -march=x86-64-v3 -fno-omit-frame-pointer`).

`tests/test_report_html.py` asserts **substrings of `_CSS` and `_JS`**, sometimes
with occurrence counts (`_JS.count("timeToX(") >= 4`) and sometimes that a
function body does *not* contain something
(`_JS.split("function " + site)[1].split("\nfunction ")[0]`). Renaming a JS
function means editing these tests in the same commit.

Skipping, all via `pytest.mark.skipif`:

- module gate in every perf-touching file:
  `pytestmark = pytest.mark.skipif(not shutil.which("perf") or not probe_stat(["-e", "task-clock"])[0], reason="perf access unavailable")`
- class gates: `not probe_ibs()`, `not probe_intel_mem()`, `not probe_wait()`
- inline `pytest.skip(...)` for a single assertion ("AMD fp width events
  unavailable", "g++ not available", "AVX-512 tier not built")

`test_integration_cpp.py` asserts the documented signatures: SIMD IPC > 2,
chase IPC < 0.35 (>5× contrast), L1/L2 misses > 10M with 10× contrast, LLC miss
rate > 45% for the chase (relaxed to 30% on Intel, whose LLC-load counters
report a lower rate than AMD fill events), dTLB misses > 5M, and page faults
covering every 4 KiB page of the mapping.

---

## Benchmarks and examples

```bash
make -C examples                                    # -> examples/bin/
vperf run -- examples/bin/simd_levels_avx 2000000   # IPC ~2.9, L1-resident
vperf run -- examples/bin/membound 268435456 1.5    # IPC ~0.14, miss storm

bench/clickbench_profiles.sh --engine both          # sequential; never parallel
bench/clear-caches.py                               # fadvise DONTNEED + mincore verify
```

`bench/clear-caches.py` needs no root: `posix_fadvise(POSIX_FADV_DONTNEED)` on a
clean file discards its pages, where `/proc/sys/vm/drop_caches` is `0600`
root-only and a setuid bit on a script is ignored by the kernel. It measures
residency with `mincore` afterwards, so a run is *known* to be cold rather than
assumed to be.

Startup grace is per engine in the sweep driver: `clickhouse-local` needs ~90 ms
to build its pool while its cheapest query takes 140 ms; duckdb has its 16
threads up within 8 ms while its cheapest query takes 80 ms.

---

## Git

- One feature/fix branch per PR, merged with a merge commit.
- Commit subjects are lowercase, Conventional-Commit-ish, carry a scope and a PR
  number, and state **what the reader now sees** rather than the mechanism:
  `fix(html): the Threads tab leads with its graphics (#41)`,
  `feat: add macOS backend (sample-based profiling)`,
  `perf: stream the perf script dump and skip the samples we discard (#28)`.
- Never commit profiles (`.vperf/`) or build output (`examples/bin/`); both are
  gitignored.
- No secrets. `vperf` never handles credentials — it profiles local processes.