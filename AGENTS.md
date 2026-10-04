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
| a metric, threshold, or the meaning of a report number | `AGENTS.md` — "Metric thresholds" (the README has no interpretation table; the report carries the hints itself via `metrics.all_hints`) |
| a CLI flag or its default | `README.md` (Quick start + Options) **and** `AGENTS.md` flag table |
| anything the reader sees in `report.html` | `AGENTS.md` — "The report UI contract" |
| a new artifact in the profile directory | `AGENTS.md` artifact table (the README does not list artifacts) |
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
   keep the `try/finally` that resumes it (`collector.py:1177`) and the
   `PerfProcess.stop()` escalation (SIGINT → SIGTERM → SIGKILL) that flushes a
   partial `perf.data`. That escalation's grace is
   `_COLLECTOR_STOP_GRACE = 60 s`, not something short: perf's flush is
   proportional to `perf.data`, and a record escalated to SIGTERM mid-flush is a
   record judged *failed*, which takes its samples with it — a 100 MB recording
   discarded because the stop was impatient. Perf that truly hangs still gets
   escalated; it just gets to finish first. The same file is *deliberately*
   deleted afterwards (step 9) — that is disk space, not a discarded recording,
   and it happens only once every dump has been read out of it.
8. **The samplers stop after the collectors, not with the monitor** (step 6,
   `_finish_collector` then `_FreqSampler.stop()`). Their curves are placed
   against the *sample* times, and perf's first sample is not the moment it was
   launched: opening the events takes seconds on a host with a virtualised PMU or
   with hundreds of threads to attach to, and a sampler that stopped when the
   monitor returned leaves a curve covering a stretch no sample falls in — the
   RSS and Frequency charts then draw empty over data that was collected all
   along. The readings past the last sample are outside the window the charts
   draw and cost nothing.
7. **`SIGINT` ends an attached profile, and it produces a report.** In `attach`
   mode a `KeyboardInterrupt` while the collectors run is caught and the
   finalize path continues (`collector.py:1190`), so an interrupted profile still
   writes `meta.json` and `report.html`; `run` mode keeps unwinding. Two things
   make that work and both are load-bearing: `cmd_attach` requests `SIGINT`
   itself, because a driver launches it as a background job and bash hands a
   background job of a non-interactive script an *ignored* `SIGINT` (POSIX asks
   for it, a shell `trap` cannot undo it, and CPython installs no handler over an
   inherited `SIG_IGN`) — and `wait`ing for the client is the driver's job, not
   vperf's. A driver that sends `SIGINT` gets `elapsed_wall` = the time observed,
   plus a warning saying the rest of `--duration` was not collected.
9. **The raw perf recordings are retired unless `--keep-perf-data`.** Nothing
   reads them after `_retire_raw` — `load_profile` opens only the text and JSON
   artifacts — so they are transient by default and the flag is the whole opt-in.
   Two things make it safe: the call sits at the earliest point past the last read
   (the `_memory_report` sort-key ladder's final attempt), and it takes the names
   *this run recorded* rather than listing the directory, because `-o` reuses an
   existing outdir and a `perf.data` left by an earlier run into the same
   directory must not be deleted by this one. Pinned by
   `test_nothing_reads_the_recording_after_it_has_been_retired`, which asserts on
   the reads rather than on the source's ordering. The removal is **not** routed
   through the `warnings` accumulator: it is the default, so every run would
   carry a `Warnings:` header and the header would stop meaning anything.

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
| `vperf/collector.py` | 1848 | the orchestrator: capability probes, the perf passes, artifact writing |
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

`build_parser()` is at `cli.py:407`; `main(argv=None)` at `cli.py:503`.
Subcommands: **run, attach, report, cycle, diff, doctor**.

### Shared by `run` and `attach` (`cli.py:415`)

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
| `--keep-perf-data` | flag / off | keep the raw perf recordings after the dumps instead of deleting them |
| `--startup-grace` | float s / 0.15 | settle window before freezing the target (ignored by `attach`) |

### Per subcommand

- **run** — `cmd` positional, `nargs=REMAINDER`, `metavar="-- CMD"`
  (`cli.py:464`). Requires `-- CMD` (returns 2 otherwise, `cli.py:157`).
  **Propagates the target's exit code**: `128 + abs(code)` for signals
  (`cli.py:183`).
- **attach** — `-p/--pid` (required), `--duration` (default 10.0). `SIGSTOP`s the
  pid, profiles, `SIGCONT`s — but the stop is **best effort**: a pid we may not
  signal (another user's process, e.g. a systemd `clickhouse-server`) is profiled
  from the moment the collectors are up, with a warning, because a process that is
  already running has no startup for the pause to protect. Always returns 0.
  Skips `probe_attach()` on darwin. `cmd_attach` distinguishes `ESRCH` (no such
  process → return 2) from `EPERM` (it exists, we simply may not signal it →
  carry on): signal permission is not profile permission, and perf holds
  `CAP_PERFMON` through file capabilities. `probe_attach` must pass **no
  trailing workload** alongside `-p` — perf attaches *or* launches, never both,
  and asked to do both it prints its usage and exits non-zero, which reads
  exactly like "attach is unavailable" on a host where attaching works.
  `cmd_attach` also asks for `SIGINT` explicitly (`cli.py:204`), because that is
  how an attached profile is meant to end — see the SIGINT invariant below.
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
| `perf.data` | `perf record -q -d -W -o` (+ co-joined memory and `sched:` events) | during measurement — **retired** after the post-target dumps unless `--keep-perf-data` |
| `recording.started` | `_write_recording_marker`, the instant before the collectors are monitored — a driver's cue to start the workload | during measurement |
| `stat_threads.csv` | `perf stat -x, --per-thread` | during measurement |
| `stat.csv` | `perf stat -x,` — legacy non-combined path only | during measurement |
| `perf_ibs.data` / `perf_mem.data` | standalone memory record, only when the co-joined record failed | fallback only — **retired** like `perf.data` |
| `perf_wait.data` | legacy non-combined path's scheduler-tracepoint recording — read only by the `perf script` that becomes `wait.txt` | fallback only — **retired** like `perf.data` |
| `script.txt` | `perf script -i perf.data [--no-inline]`, then compacted in place (`_compact_script_file` / inside `_wait_artifact`) | post-target |
| `wait.txt` | `_wait_artifact` splits the `sched:*` lines **out of** `script.txt` and rewrites it without them, compacting as it goes; deleted if empty | after `script.txt` |
| `mem_report.txt` | `perf mem report -i perf.data --stdio --field-separator=\t --show-total-period [--sort …] [--time-quantum Nms]`, then compacted in place (`_compact_mem_report`) | post-target, concurrent with `perf script` |
| `freq.json`, `rss.json` | `_FreqSampler` / `_RssSampler` threads | after samplers stop |
| `meta.json` | `_write_meta` — **last** of the collection phase | end |
| `report.html` | `build_html` via `cli._finish` / `cmd_report` | report |

### Collection order (`_collect_combined`, `collector.py:970`)

1. Record `started`; `probe_wait()` decides whether `sched:` events go into the
   record.
2. `run`: `Popen(target_cmd, start_new_session=True)` → `_settle_target(...,
   startup_grace)` → `SIGSTOP` the process group. `attach`: `SIGSTOP` the pid
   (tolerating state `T`/`t`); a refused stop leaves `target_paused` false and the
   collectors open on the pid anyway, so only a freeze we took is ever continued.
3. `perf stat --per-thread -p <pid>` and `perf record -p <pid>` are launched
   **independently** (no `--`, so neither waits for the target to exit), each
   given `_COLLECTOR_SETTLE_GRACE = 0.1 s` to fail fast. Retry ladder on failure
   (`collector.py:1095`): drop `-I` intervals → drop co-joined memory → downgrade
   DWARF to fp.
4. `_FreqSampler(interval=0.01)` and `_RssSampler(pid, interval=0.01)` start.
   Their `t0` is `_sample_clock()` and goes into `meta.json` as `freq_t0` /
   `rss_t0` — the origins the report places those two curves by.
   **Not `time.monotonic()`, and not enough on its own.** perf's timestamps come
   from the kernel's clock, which userspace may not be reading: a *time
   namespace* shifts `CLOCK_MONOTONIC` by its offset (26,161 s on the host this
   was found on), and beyond that perf's clock runs ahead of `CLOCK_BOOTTIME` by a
   drift proportional to uptime — 0.38 s at 2.3 h of uptime, 3.9 s at 11.8 h,
   about 0.9 ms per minute, so **+1.6 s a day and no fixed allowance keeps up**.
   So the collector also stamps the recording's two ends on the samplers' clock
   (`record_launch_t0` when `perf record` is up, `record_exit_t0` once it has
   finished writing — stamped before the stop, the samples' span would look
   longer than the recording they came from), and
   `report_html._sample_clock_bias` turns those plus the samples into the offset
   to place the curves by.
5. Target `SIGCONT`s; `_monitor_collectors` runs until the deadline, the
   collectors finish, or 2.0 s after the target exits.
6. Samplers stopped (the last RSS reading is real because the target is still
   alive) → target reaped (SIGTERM → 2 s → SIGKILL to the group) → `perf record`
   stopped → `perf stat` stopped.
7. `stat_threads.csv` parsed; the aggregate `StatData` is
   `_aggregate_thread_stats(thread_stats)`, falling back to `stat.csv`.
8. **`perf script` and `perf mem report` are both *started* with `defer=True`
   before either is joined** (`collector.py:1282`) so the phase costs `max()` of
   the two rather than their sum. Pinned by `test_post_target_dumps_overlap`.
   Do not "tidy" this into a sequential block. Each dump is compacted in place
   once its own child has been joined (see *Collection*), so both rewrites fall
   inside this phase rather than after it.
9. **The raw recordings are retired here** — `_retire_raw` (`collector.py:926`),
   the earliest point past the last read of each: both deferred children are
   joined and `_memory_report`'s sort-key ladder is exhausted. Nothing after this
   line opens `perf.data`, so the space is freed before the parse and the render
   rather than after them. `meta.perf_data = {kept, bytes}` records what the
   directory held, `bytes` being `None` when no recording was ever made.
10. `freq.json`, `rss.json`, then `meta.json`; `ProfileData` returns to
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
wait{enabled,reason,detail}, perf_data{kept,bytes}, freq_t0, rss_t0, rss_peak,
record_launch_t0, record_exit_t0, startup_grace, perf_version, elapsed_wall`. The
macOS backend adds `backend: "macos"`, `callgraph: "sample"`, `cpu_vendor: "Apple"`,
`interval_ms: 50`, and no `perf_data` — there is no recording to retire, so read
that key with `.get()` for the same reason as `wait.reason`.

`perf_data` says what the profile directory held of the raw perf recordings and
whether `--keep-perf-data` left them there. `bytes` is their combined size at the
moment they were retired, `None` when no recording was ever made (the record
failed, `--no-record`, `cycle`). It is not an input to anything: `load_profile`
reads only the text and JSON artifacts, so `vperf report` and `vperf diff`
regenerate byte-identically from a directory the recordings are gone from
(verified).

`wait.reason` is why the Threads tab's nine wait columns are empty, and there are
four: `disabled` (`--no-wait`), `unavailable` (the probe failed — `detail` is
`doctor.wait_denial_reason()`, which names the real cause, tracefs being root-only
and `CAP_DAC_READ_SEARCH` the thing that reads it, plus the `setcap` line for the
host's real perf binary), `empty` (the events were recorded and the dump held none)
and `unsupported` (macOS). It is `None` when the pass produced `wait.txt`. **The
report must read it with `.get()` and never index it**: `vperf report` re-renders
profile directories written before the key existed, and those keep the older
wording that is true of every cause.

---

## The report build and its scoping model

`report_html.py` is one module in three layers: `_CSS` (`report_html.py:153`, a
minified custom-property theme), `_JS` (`report_html.py:242`, a **raw**
`r"""..."""` string), and the Python renderers, assembled by `build_html`
(`report_html.py:2576`) into a single f-string.

Data reaches the browser as **bare global assignments in a separate `<script>`
block emitted before `_JS`** (`report_html.py:2821`): `S=`, `FREQ=`, `FREQ_T0=`,
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
  reporting the run as if it were the window. Those two curves are placed on the
  sample timeline by the origins the samplers recorded (`freq_t0` / `rss_t0`),
  which is what puts them at the same x as the samples they share a moment with —
  and that placement is *measured*, not assumed: `_sample_clock_bias` reads the
  last sample against the recording's exit (the one comparison with no setup
  latency in it), refuses the correction when the samples claim a longer span
  than the recording had, and adds the offset to the origin, since perf's stamps
  run ahead. Left uncorrected the two charts draw *empty*, silently, with the
  data sitting in the file. When the curves still do not reach the window the
  chart says so in place, and the two reasons are worded apart: a placed profile
  whose readings genuinely fall short, and one collected before the offset was
  measured, which only a re-collection settles.
- **A sampler curve is placed by whichever way lands more of its readings on the
  window** (`_place_curve`), and drawn only where it was measured. Two rules, both
  from what a wrong placement looks like. The measured offset is preferred,
  because it is arithmetic — but on a host where perf delivers its samples long
  after the recording was launched, the gap it measures is mostly *timing*:
  measured here, a 2.87 s recording whose samples are stamped 3.94 s after its
  launch, so subtracting the 2.80 s estimate slid the window past the end of the
  series and a 311 → 1830 MiB rise drew as one flat line, the settled tail being
  all that was left inside. So the measurement is judged by its effect, and when
  it loses, the physical relation stands: the samplers start with the collectors,
  so the first reading belongs at the first sample. And `rssBuckets` fills
  *interior* gaps only — ahead of the first reading and behind the last there is
  nothing to hold, and holding a value there is how one real reading becomes a
  flat line across the whole timeline. Those buckets stay blank, labelled
  "sampler not running".
- HTML escaping: `esc()` in Python, `escHtml()` in JS. Numeric sort keys go in
  `data-v=` attributes so `sortTable` can compare numerically.

Tabs are `overview, hotspots, mem, flame, tree, threads`, switched by
`showTab(btn, id)`; header chart modes `util`/`mem`/`freq` via `setChartMode`;
the Memory tab has its own count/latency switch.

---

## The report UI contract

What the HTML report does, stated as behaviour rather than as a tutorial — the
README deliberately does not teach the UI, so this is the only place it is
written down. Changing any of it changes what the reader sees.

### Flame graph

- Click a frame to zoom into its branch: the subtree is re-laid out to the full
  width, the call path leading to it greyed underneath, the focused frame
  outlined in yellow, and tooltip percentages made relative to the focused
  frame. Click the focused frame again to go up one level; any greyed ancestor
  band is clickable to jump straight to it.
- `Reset zoom` in the bar under the graph returns to the full graph. Switching
  thread in the header selector, or moving the time selection, also resets it.
- The graph is exactly as tall as the rows it draws and **never renders a row of
  hairlines over the flame**: it ends at the last row with a frame at least 2px
  wide, or at 48 rows (`MAX_FLAME_DEPTH`), whichever comes first. The rows cut
  off fold into the frame they hang off, keeping the width they gave it, and its
  tooltip says how many rows it stands for. Zooming into a shallow branch raises
  the bottom edge with it rather than leaving empty space above.
- It scales to the panel width; a frame too narrow for a label gets one as soon
  as it is zoomed into.
- It follows the thread selector *and* the time selection, at no file-size cost —
  the samples ship once and are folded again in the browser.

### Time selection

- Two movable borders on the header chart. **Drag inside the plot** to select,
  **drag the selection** to move it, **double-click** or press `Reset Selection`
  to clear. A range is picked on the chart, never typed. The label beside the
  button and the scope line under the chart name the range in seconds, its share
  of the run, the sample count and the cycles behind it.
- The curve always shows the **whole run** with the out-of-selection parts
  dimmed, so a selection keeps its context and the borders line up with the axis.
  All three chart modes (utilization / RSS / frequency) share that axis and that
  selection; the RSS and frequency curves are placed on the sample timeline by the
  clock their samplers share with perf. Every time shown anywhere in the report
  is seconds into the run — perf's raw `CLOCK_MONOTONIC` timestamps stay inside.
- The utilization y axis is **busy cores in the current scope, capped at what
  that scope could possibly use**: all threads at the machine's logical CPU
  count, a name group at its own thread count, **a single thread at one core**.
  Its average is that scope's own CPU time over the charted window (per-thread
  `task-clock` `perf stat`, summed over a group) — so a thread that used half a
  core reads 0.5, and the plot never puts a thread at 16.
  The *shape* between those points is an estimate and the code says so: perf
  attributes a sample the cycles its core ran since that core's previous sample,
  which is whatever else ran in between, so sampled buckets are smoothed before
  they are scaled. A curve resting on the ceiling means "all of them, saturated".
- What follows the selection: **Hotspots** (self, inclusive, estimated CPU
  time), the **Flame Graph**, the **Call Tree**, the **Memory** tab (all five
  panels plus the accesses-over-time chart, which shades the selection), and the
  whole Threads tab — per-thread **cycles** and the scheduler's **on-CPU, Sleep,
  Blocked/IO, Runnable, Off-CPU** columns and counts, plus the "where the time
  went" bar. The wait half is a per-thread off-CPU timeline on the same clock the
  borders are drawn on, folded in the browser from the shipped slices.
- While dragging, the chart and the counters follow the borders; the flame graph,
  call tree and memory panels rebuild once the drag settles — this is what keeps
  dragging smooth on a 100k+ sample profile.
- Memory samples are only known to the `--time-quantum` slice they fell in
  (default ~100 slices, clamped 25 ms–1 s), so a window cutting a slice in half
  counts half of it. A profile collected before that existed — or on a perf that
  rejected the `time` sort key — keeps a whole-run Memory tab and says so.
- **An attached profile is a process, and the report says which process.** A
  profile taken with `vperf attach` covers every thread of that pid: attaching to
  a `clickhouse-server` means the Overview counters and the utilization curve
  include the ~164 `ThreadPool` workers and the `MergeMutate`/`Fetch`/`Bg*`
  background threads alongside the query's, and the query is read by grouping or
  scoping to its threads. Nothing in the report normalizes that away — the
  utilization ceiling is the pid's thread count, not the query's — so the scope
  line and the thread grouping are the tools for it.

### Memory usage over time

- The **Memory RSS** chart sits in the header between Utilization and Frequency,
  sampled every 10 ms from `/proc/<pid>/statm` — the same number `top` prints. It
  is a measured value, not an estimate: bucketing only loses spikes shorter than
  a bucket, so the run's peak is drawn as its own dashed line and labelled with
  the same number the terminal summary quotes.
- It is the **whole process and does not follow the thread selector.** A process
  is one address space: `/proc/<pid>/task/<tid>/statm` and the per-thread
  `RssAnon`/`RssFile` in `.../status` both report the process total, so procfs
  has no per-thread footprint to plot anywhere (measured on a 4-thread process
  with 300 MiB allocated on one thread: 312.4 MiB reported by all four). For
  per-thread memory *behaviour*, the Memory tab follows the scope.
- `--no-rss` removes the curve and the peak row, and a profile collected before
  RSS sampling existed has neither and says so in place of the chart.

### Grouping threads by name

- A checkbox next to the thread selector replaces the thread list with one entry
  per thread name — the entry that was `ThreadPool ×54` becomes one line.
- The group covers *every* thread of that name the profile knows, not just the
  hottest 20 the ungrouped list shows. Ordering matches the ungrouped list:
  most of the run's sampled cycles first (share in the label), name breaking
  ties, groups that sampled nothing last.
- Hotspots, the utilization chart and the flame graph merge the members' samples;
  the utilization curve is the pool's total busy cores, so a 16-thread pipeline
  reads as up to 16.
- Overview counters are **summed before anything is derived** from them, so the
  group IPC is `Σinstructions / Σcycles` — not an average that would weigh a
  thread which sampled 10 cycles like one that ran the whole window.
- The Memory tab adds up the members' IBS/PEBS samples and the scope line says
  how many of them had any (`QueryPipelineEx ×16 threads, memory from 12 of 16`).
- A group whose threads have no per-thread counters — the usual case, since
  `perf stat --per-thread` only reports threads alive when counting attaches —
  falls back to what the sampler knows: thread count, cycle share of the run, and
  the CPU time that share works out to.
- Costs nothing extra in the file: only the whole-run graph is pre-rendered, every
  other scope is folded in the browser from the same samples.
- The "where the time went" bar adds the members' waits, while the per-thread
  table below keeps listing every thread. The Frequency chart stays run-level.
- `perf mem report` labels every thread of a process with the *process* name, so
  a thread groups under the name the sampler saw; only threads the sampler never
  caught fall back to the coarser memory-report name.

### Wait / off-CPU columns

- The Threads tab merges the per-thread CPU and wait tables: each row carries
  sampled cycles next to on/off-CPU seconds, joined on tid, and the wait columns
  read `n/a` when scheduler tracepoints were not collected. **The note over the
  table names which absence that is**, because only one of the four is the
  reader's to fix: `--no-wait` is a choice the profile records, a denied probe
  carries the `setcap` line that would fix it, an empty dump is neither, and
  macOS has no such tracepoints to record. A profile written before
  `meta.wait.reason` existed keeps the older wording, which is true of all four —
  read it with `.get()` and never index it, or `vperf report` breaks on every
  directory this version did not write.
- **On-CPU + Off-CPU is exactly the thread's observed window**, and the "where
  the time went" bar splits the same way. `prev_state` maps to columns as:

  | Column | `prev_state` | Means |
  |---|---|---|
  | Sleep | `S` | interruptible — futexes, condition variables, sleeping syscalls |
  | Blocked/IO | `D` | every uninterruptible wait: disk I/O **and** page-fault waits from a cold cache |
  | Runnable | `R` | run-queue wait after a preemption |
  | *stopped* / *unknown* | — | traced but not attributable to a wait class |

- A thread still switched out when the recording ends is left **uncharged**, not
  guessed at: the tiny first delta was earned before the window opened.
- The counts (Preempted, Sleeps, Blocks) are read off the shipped intervals, so
  they count the switches that cost measurable time — a switch-out whose wait the
  window never showed owns no interval and is not counted. That is also what makes
  the server's count and the browser's the same number.
- A wait **straddling a selection edge** is charged by the share inside it: a
  count can come out fractional (rounded for display), a halved slice contributes
  half its seconds. The delay-band histogram does not move at all — it counts the
  waits that *started*, so it stays whole-run.
- Every column heading carries a `?` that says what that column measures, hover
  or keyboard focusable, so the definitions sit on the columns they define rather
  than in prose above the table.

### Metric thresholds

`metrics.all_hints` and the report's footnotes hang off these. Changing one is a
behaviour change to both docs.

| Signal | Threshold | Reading |
|---|---|---|
| Effective CPU Utilization ≪ cores | — | serial or I/O-bound; a threading opportunity |
| IPC | ≥ 2 / < 0.5 | compute-bound and efficient / stalled, look at the bound split |
| Backend Bound | high | memory-hierarchy limited → check the LLC / L1D / dTLB rates |
| Frontend Bound | high | fetch/decode limited (i-cache, large code footprint) |
| Bad Speculation | > 5% | branch mispredicts or machine clears wasting cycles |
| Retiring | < 30% | most pipeline slots lost; deep stall or contention |
| Branch mispredict | > 5% | unpredictable branches dominate |
| LLC miss rate | > 30% | working set exceeds cache — DRAM-bound on AMD, but L1 misses that reached L3 on Intel, so cross-check the Memory tab there |
| L1D miss rate | > 5% | data-cache thrashing; blocking/tiling opportunity |
| dTLB miss rate | > 1% | page-table walks hurting latency |
| Vectorization ratio | < 50% | scalar or mixed-width code |
| Avg memory-access latency | > 200 cyc | deep memory stalls; prefetch or restructure |
| Blocked/IO ≫ On-CPU | — | the data is not in cache: waiting on the disk, not computing — the warm-vs-cold difference shows up here and nowhere else |
| Runnable high, On-CPU low | — | oversubscribed; fewer threads or more work each |
| Sleep high, On-CPU low | — | idle-waiting (futex / condvar), the pipeline is starving its own workers |

---

## Invariants per subsystem

### Collection

- **The two text dumps are compacted in place, and may only lose what the
  parser already discarded.** Both are perf's raw stdout, and on a wide target
  they are the two largest things in the directory, so each is rewritten once
  the dump that produced it is done:
  - **`script.txt`** — `_compact_script_file`, or sharing `_wait_artifact`'s
    existing pass when a wait pass ran (there is no reason to walk a 75 MB file
    twice). perf prints a frame it could not symbolize as
    `<addr> [unknown] ([unknown])`, and `parsers._frame` already reduces that
    pair to the single tag `[kernel]` (an address in the kernel's space) or
    `[unresolved]`, deciding on the `ff` prefix alone and **throwing the address
    away** — so the rewrite writes the tag the parser would have produced, which
    makes it provably parse-identical rather than merely equivalent-looking.
    With `kernel.kptr_restrict=1` (here: uid 1000, so `/proc/kallsyms` reads
    back all zeros and *no* kernel address resolves) that is **20% of
    `script.txt`** across five ClickBench/duckdb profiles, 249 MB → 199 MB; a
    workload that stays in userspace barely moves (1.7% on a 1.9 s `membound`).
    It costs ~400 ms per 75 MB, against the tens of seconds `perf script` took
    to write the file.
  - **`mem_report.txt`** — `_compact_mem_report`. We already pass
    `--field-separator=\t`, but perf still right-pads every cell to the column
    width, and that width is set by the *longest symbol in the report*: a
    ClickHouse template instantiation runs to several hundred characters, so
    **87% of the file was spaces** (104 MB of padding around an 11 MB report —
    the largest artifact in the directory, ahead of `script.txt`). `_split_cells`
    splits on the tab and strips each cell, so the padding was being thrown away
    on read and the rewrite only avoids writing it. 235 MB → 29 MB across the
    same five profiles, ~145 ms per 104 MB; the win scales with how long the
    target's symbol names are (51% on `membound`, whose symbols are short).
  - Both are **plain text and still tab-separated**, so a profile directory
    stays greppable. Gzip wins far harder (98%, both files) and is lossless, but
    it turns two documented, hand-inspectable artifacts into binary blobs and
    would reach `load_profile`'s paths and the wait carve-out — deliberately not
    taken.
  - Every helper is a **fixed point** (`vperf report` re-reads a directory this
    version did not write, and `-o` reuses an outdir) and preserves each line's
    own terminator. Assert the *parse* of the written file, never the size: a
    version that stripped the trailing `\n` off with the last cell merged the
    column header into the first row and took the whole memory report to zero
    samples, while a string-level check of the same transform passed.
  - **`_UNRESOLVED_FRAME_RE` requires 4+ hex digits on purpose.** A null frame
    prints a bare `0`, which `_FRAME_RE`'s `addr` group also refuses — so `sym`
    swallows the run and the report shows a frame named `0 [unknown]`.
    Rewriting it would change what the reader sees, so it is left alone.
- **`load_profile`'s return value is a fixed-shape 8-tuple**:
  `(meta, stat, script_path, mem_report_path, wait_path, freq_timeline,
  thread_stats, rss_timeline)`. Index 6 is the thread map slot *even when it is
  `None`*. Consumers index it positionally — do not return `None` in place of a
  trailing slot or reorder it.
- **`perf` exits 0 with an empty report when it rejects a `--sort` key.**
  `_memory_report` therefore has a 5-attempt ladder over `_MEMORY_SORT` variants
  and treats "rc 0 + empty output" as a **failure**, not as "no samples"
  (`_perf_error_summary`, `collector.py:322`). `perf report` has no `tgid` key —
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
- **Known latent bug:** `_FreqSampler` is defined **twice** — `collector.py:30`
  (dead code, `interval_ms=` kwarg) and `collector.py:535` (the live one,
  `interval=` seconds). The legacy `collect()` path still calls it with the old
  kwarg (`collector.py:1497`), so that path would raise. Tests monkeypatch
  `collector._FreqSampler` with `_FakeSampler`, so it is not exercised. Collapse
  the two definitions if you touch either.

### The samplers and the PMU

`doctor`'s **`sample spread`** row is the one that says whether a profile on this
host is a time series at all: `probe_sampling_spread` records for a few seconds
and looks at the span between the first and last sample. A host whose PMU is not
really there — a virtualised one — opens the event, delivers a single burst
seconds after launch, and goes quiet; measured here, a 28 s recording produced 15
samples inside 5 ms. Every hotspot, flame graph and timeline from such a host is
one instant, so this is a WARN and not a FAIL, but it changes how any number from
that host should be read. The row also reports the clock offset it measured on
the way through, since that is what decides whether the two header curves can be
placed at all. When the sampler curves come out empty for the same reason, the
report says so in place of the chart rather than drawing an empty box.

### Vendors

Three independent mechanisms, all keyed off `doctor.cpu_vendor()`:

1. **Event gating** — `_probe_capabilities` (`collector.py:429`) probes
   `GENERIC_EVENTS + AMD_ONLY_EVENTS` only on `AuthenticAMD`, so Intel never
   emits `<not counted>` noise. Memoized in `_PROBE_CACHE` (cleared by tests).
2. **Memory backend gating** — `_memory_plan` (`collector.py:217`) skips the IBS
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
(`cli.py:30`), `cli.cmd_attach` (`cli.py:197`), `cli.cmd_cycle` (`cli.py:298`),
`cli.cmd_doctor` (`cli.py:393`), plus `collector.collect` (`collector.py:1419`)
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
  properties. **A JS string literal containing an apostrophe must escape it or use
  double quotes** (`"the sampler's"` or `'the sampler\'s'`): one unescaped `'` ends
  the literal early and makes the whole `<script>` block unparseable, which takes
  the report with it — no charts, no tabs, no selection, with every byte of its
  data still in the file. The string contracts in `tests/test_report_html.py`
  cannot see it (a substring is present in a file that does not parse), so
  `_js_lex_errors` walks `_JS` and every generated script block for unterminated
  literals, and a `node --check` test covers it properly wherever node exists.
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

`bench/clickbench_profiles.sh --mode server` profiles the same queries against a
MergeTree `hits` in a `clickhouse-server`, one `vperf attach` per query, and the
parts of it worth knowing before changing it:

- **The window is the query, and the driver ends it.** A query's runtime is not
  knowable in advance (Q00 ~0.1 s, Q35 ~90 s), so the driver launches
  `vperf attach --duration <ceiling>` in the background, runs the query with
  `clickhouse-client --time`, and `SIGINT`s vperf the moment the client returns.
  Do not replace this with a per-query duration.
- **Readiness comes from the freeze, or failing that from `perf.data`.** vperf
  freezes the pid it can signal while its collectors open, so the driver watches
  `/proc/<pid>/stat` for `T` and then for its release. A pid it may *not* signal
  — the systemd server — cannot be frozen and vperf does not try, so there is no
  handshake to watch; the driver then waits for `recording.started`, the file vperf
  writes the instant before it monitors the collectors (`wait_for_recording`).
  That wait is not optional: vperf spends ~1 s probing PMU capabilities before it
  opens anything, and `perf.data` exists ~100 ms before samples start arriving, so
  a sub-second query's SIGINT lands in one of those gaps and ends the run with
  nothing collected. `kill -0` is how the driver decides which path to take
  — **not** as a liveness test, since a systemd server is another user's process
  and `kill -0` on it returns EPERM (use `/proc`, as `pid_alive()` does). The
  reasoning about `perf.data` existing mid-run is unaffected by the recording
  being retired at the end of it (step 9 of the collection order): retirement is
  post-target, and no driver here watches for the file.
- **The pid `--private-server` profiles is the port's owner, never `$!`.**
  `clickhouse server` **forks**: the pid the shell hands back is a supervisor
  whose main thread is named `ClickHouseWatch`, running 7 threads and *zero*
  `ThreadPool` workers, while the server that answers queries is its child with
  318 of them. Profiling the supervisor profiles an idle process, and it fails
  quietly: measured on a 4 s window covering a 1.3 s query, 27 samples against
  the supervisor (every one `AsyncLogger`) against 733 across `ThreadPool`,
  `MergeMutate` and `TCPHandler` against the server. The profile that results
  reads as a broken profiler rather than as a wrong pid — 0.03 s of CPU, no
  `QueryPipelineEx`, a 223 KB report — so `start_private_server` resolves the pid
  by asking **which process listens on `$PRIVATE_PORT`**
  (`port_owner_pid`: the LISTEN socket's inode from `/proc/net/tcp{,6}`, matched
  against `/proc/<pid>/fd` for `$!` and its `children`), and **refuses to
  profile** if it cannot tell. `stop_private_server` still signals the
  supervisor, which owns the child's lifecycle. This is the same hazard
  `discover_server_pid` already dodges for the systemd server (a pid file, and
  `clickhouse-watchdog` excluded); the private path had no equivalent.
  **Do not "simplify" this to `pgrep -P "$PRIVATE_PID"`** — by port is the only
  statement that is true by construction, and the port is what the query uses.
- **`perf record -p <pid>` does not follow threads created after it opens** (no
  `--inherit`), and for ClickHouse that is usually harmless: the pools exist from
  server start, and the per-query `QueryPipelineEx` threads are *reused* from a
  pool after the first query, so a sweep's second query onwards captures them.
  Only the very first query of a freshly started server runs on pipeline threads
  that did not exist at attach. Measured, adding `-i` is **not** the fix — a
  second query captured 667 `QueryPipelineEx` samples with or without it — so
  leave it off unless a measurement says otherwise.
- **Sampling rates are per mode.** `FREQ`/`MEM_PERIOD` default to 499 Hz /
  1000003 for a local engine and 99 Hz / 4000003 for a server: cost is per thread,
  and 499 Hz × 359 threads × 16 KiB DWARF stacks is ~140 MB for a one-second
  window. Env vars still win.
- **The schema comes from the checkout, not from the parquet variant.**
  `clickhouse/create.sql` (MergeTree, `PRIMARY KEY` only, no `PARTITION BY`/`ORDER
  BY`, `fsync_after_insert=1`) and `clickhouse/queries.sql`, which is byte-equal
  to the parquet variant's. No OPTIMIZE is done, which is why
  `--optimize`/`--optimize-final` are opt-in.
- **The load streams the parquet in; the server never reads it where it lies.**
  ClickHouse confines `file()` to `user_files_path`
  (`/var/lib/clickhouse/user_files/`, inside a `700 clickhouse:clickhouse`
  `/var/lib`), so the in-place read ClickBench uses — `INSERT INTO hits SELECT *
  FROM file('<parquet>') --max-insert-threads $(nproc)/4`, reached through a
  root-owned symlink — fails here with `Code: 291 DATABASE_ACCESS_DENIED, "File
  ... is not inside /var/lib/clickhouse/user_files"`. Streaming the bytes
  (`INSERT INTO hits FORMAT Parquet`, server-side parsing with
  `input_format_parallel_parsing`) needs no privileges at all. Do not "fix" this
  by reintroducing the group/`user_files` setup: nothing needs it any more.
- **`check_columns` guards the insert.** The Parquet reader matches columns *by
  name* and `input_format_parquet_allow_missing_columns` is 1, so a dataset
  missing a column would insert cleanly and then answer every query with that
  column's default. The check therefore runs *client-side* —
  `clickhouse-local --query "DESCRIBE SELECT * FROM file('<parquet>')"`, which
  needs no server — comparing against `create.sql`'s column list (between the `(`
  after `TABLE` and the `)`, skipping the trailing `PRIMARY KEY`), and it runs
  before a byte is sent.
- **Auth is the client's, not the driver's.** Host, port, user and password come
  from `~/.clickhouse-client/config.xml`; the only credentials the driver ever
  passes are the empty password and `--port` of `--private-server`. **Every
  `clickhouse-client` invocation has to carry `${CONN_ARGS[@]}`, the query
  included.** `ch_plain` and `run_logged` always went through `conn_args`; the
  query itself did not, so a `--private-server` run sent the query to the
  client's default port 9000 — measured: the query ran against the shared server
  while vperf profiled the private one, so the report described a process that
  never saw the query, and with nothing on 9000 it failed `Code: 210` and the
  profile covered an idle server. A private server whose port is ignored is not
  the server being profiled.
- **Loading is opt-in (`LOAD="skip"` by default) and the private data dir is
  reused, never wiped.** Loading `hits` is 100M rows and ~200 s; measured here,
  an `rm -rf "$PRIVATE_DIR"` on start made two consecutive 9 GiB loads of 208 s
  and 198 s for the same rows, because a fresh server has no `hits` and the load
  is the only thing that makes one. `OUT_ROOT/_private-clickhouse` therefore
  outlives the run and the second sweep reuses the table — `LOAD="skip"` now
  means *reuse, and error naming `--load` if there is nothing to reuse*, so the
  folder reuse and the default are one change and neither works alone. Keep
  `--load` as the `DROP`+`CREATE`+`INSERT` it is: it is also how a changed schema
  is picked up. Two costs to know: the failed-load path must stop the private
  server (`load_table || exit 1` used to orphan it holding its port, and skip-by-
  default makes that the *ordinary* first run), and a server killed the moment
  the sweep ends never merges its parts — 30 GB of `store/` for a 9.1 GiB table,
  reclaimed with `rm -rf` on the dir or by `--optimize`.

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