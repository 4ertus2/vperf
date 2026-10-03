#!/usr/bin/env bash
# Profile every ClickBench query with vperf and keep one HTML report per query,
# for one engine or for both.
#
# Each query gets its own profile directory under .vperf/, named with the
# ClickBench query number, the engine and the launch timestamp, e.g.
#   .vperf/q00_clickhouse_20260926_120501/report.html
#   .vperf/q00_duckdb_20260926_120501/report.html
# A per-run log and TSV index (clickbench_<engine>_<runid>.{log,tsv}) hold the
# driver progress and the headline metrics.
#
# --mode server is the other half of ClickBench: instead of clickhouse-local
# reading hits.parquet itself, the queries run against a MergeTree `hits` table
# in a clickhouse-server, loaded once from that same parquet, and each query is
# profiled by attaching vperf to the server pid.  That profile is the *process*,
# not the query - a ClickHouse server holds hundreds of background threads - so
# the query is read out of the report by scoping to its threads, and the report
# says so.  --private-server starts a dedicated server for the run instead of
# touching a shared one, which keeps those background threads out of the profile.
#
# Queries are run strictly one at a time: concurrent profiling sessions
# multiplex the hardware counters and distort the measurements, so --engine both
# runs the engines in sequence rather than side by side.
#
# Every query is executed exactly once. Nothing is repeated, retimed or
# retried, so a report describes the query as ClickBench defines it: where its
# CPU time goes, and what its threads were doing over the query's timeline. A
# query that finishes before vperf can attach still gets a profile - it just has
# no samples in it, and the run log says so.
#
# The schema and the queries come from the ClickBench checkout itself
# ($CLICKBENCH_DIR/<engine>-parquet/{create.sql,queries.sql}), so any checkout
# works - nothing is read from an agent-specific skill folder. The two engines
# get their own schema and their own query text (the duckdb set differs in the
# handful of places where the engines disagree on a function name), and both
# refer to 'hits.parquet' relative to the cwd, so the queries run with DATA_DIR
# as their working directory.
set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
if command -v readlink >/dev/null 2>&1; then
    SCRIPT_PATH="$(readlink -f "$SCRIPT_PATH")"
fi
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DATA_DIR="${DATA_DIR:-$HOME/data}"
BENCH_DIR="${CLICKBENCH_DIR:-$HOME/src/ClickBench}"
OUT_ROOT="${OUT_ROOT:-$REPO_ROOT/.vperf}"
# Left empty when unset, so each mode can pick its own default below: the cost of
# sampling is per *thread*, and a server has hundreds of them where a
# single-process target has tens.
FREQ="${FREQ:-}"
# AMD IBS period in cycles.  The Memory tab is only as good as the number of
# classified accesses, and vperf caps recorded stacks at 128 frames, so the
# perf dumps stay cheap: at 1e6 a 30 s-CPU query yields ~60k IBS samples for a
# few seconds of extra post-processing.  Raise it for a very long target.
MEM_PERIOD="${MEM_PERIOD:-}"
VPERF="${VPERF:-}"
ENGINE="clickhouse"
MODE="local"
PRIVATE=0
LOAD="auto"          # auto | force | skip
OPTIMIZE="none"      # none | merge | final
SERVER_PID=""
MAX_DURATION="${MAX_DURATION:-300}"
ATTACH_DELAY="${ATTACH_DELAY:-0}"
PRIVATE_PORT="${PRIVATE_PORT:-19000}"
WAIT=0
INLINE=0
RESUME=0
FROM=0
TO=0
TO_SET=0
DRY_RUN=0
PARQUET="$DATA_DIR/hits.parquet"

# Per-engine defaults.  The startup grace is the seconds a target may run
# before vperf freezes it and attaches: `perf stat --per-thread` only counts
# the threads alive at attach time, so the grace has to outlast the engine's
# thread-pool build or the Threads tab collapses to a single thread - and it
# has to stay *under* the cheapest query's runtime, or that query finishes
# before anything is collected.  clickhouse-local needs ~90 ms for its pool:
# measured here, vperf's own 0.15 s default leaves 9 of the 43 queries with no
# samples at all, 0.10 s sees the same pool (48-54 threads with counters) and
# collects the short ones, and 0 collects everything but leaves 1 thread.
# STARTUP_GRACE overrides both.
grace_for() {
    case "$1" in
        clickhouse) printf '%s' "${STARTUP_GRACE:-0.10}" ;;
        # duckdb opens its 16-thread pool before it has read anything (16
        # threads 8 ms after launch), so it has nothing to wait for - and its
        # cheapest query is 80 ms, so anything much above 10 ms would lose it.
        duckdb) printf '%s' "${STARTUP_GRACE:-0.02}" ;;
        *) echo "ERROR: unknown engine $1" >&2; exit 1 ;;
    esac
}

binary_for() {
    case "$1" in
        clickhouse) printf 'clickhouse-local' ;;
        duckdb) printf 'duckdb' ;;
    esac
}

# What a profile directory is named after: the engine in local mode, the server
# it ran on in server mode.  .vperf/ is flat and holds all of these side by
# side, so the token is also what keeps --resume from calling one engine's work
# another engine's.
target_token() {
    if [ "$MODE" = local ]; then
        printf '%s' "$1"
    elif [ "$PRIVATE" = 1 ]; then
        printf 'clickhouse-private'
    else
        printf 'clickhouse-server'
    fi
}

# Liveness, asked the way an unprivileged shell can answer it.  `kill -0` is
# not the test: a clickhouse-server started by systemd belongs to the
# clickhouse user, and signalling it from this shell fails with EPERM even
# though the process is right there - so a `kill -0` probe would reject exactly
# the server this mode is for.  /proc answers without permission.
pid_alive() {  # <pid>
    [ -n "${1:-}" ] && [ -d "/proc/$1" ]
}

pid_is_server() {  # <pid>
    tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null | grep -q 'clickhouse-server'
}

# The daemon's pid: the pid file ClickBench's own start/stop scripts use, else
# the first server process.  The watchdog is a different program
# (clickhouse-watchdog) and must not be picked up - it forks the real server.
discover_server_pid() {
    local pidfile=/run/clickhouse-server/clickhouse-server.pid pid
    if [ -r "$pidfile" ]; then
        pid="$(cat "$pidfile" 2>/dev/null || true)"
        if pid_alive "$pid"; then
            printf '%s' "$pid"
            return 0
        fi
    fi
    pgrep -f 'clickhouse-server --config' 2>/dev/null | head -1 || true
}

usage() {
    cat <<EOF
Usage: clickbench_profiles.sh [options]

Profiles each ClickBench query once with vperf, one profile directory per
query and engine.

Options:
  --engine E    clickhouse (default), duckdb, or both - run in that order
  --mode M      local (default) runs clickhouse-local over hits.parquet;
                server profiles a running clickhouse-server instead
  --private-server  with --mode server, start a dedicated clickhouse-server for
                the run (own config + data dir, no sudo) and attach to that
  --load        (re)load hits from DATA_DIR/hits.parquet before the sweep
  --skip-load   never load; use the hits table the server already has
  --optimize    OPTIMIZE TABLE hits after loading (ClickBench does not)
  --optimize-final  ... FINAL, which merges every part into one
  --server-pid N  clickhouse-server to attach to   (default: discovered)
  --max-duration S  hard ceiling for one query's profile; the profile normally
                ends sooner, when the query returns       (default: 300)
  --attach-delay S  skip the attach readiness handshake, wait S instead
  --wait        collect scheduler tracepoints too. Off by default in server
                mode: a server switches hundreds of background threads and their
                off-CPU time is not what a query report is about
  --from N      first query index (0-based, default: all)
  --to N        last query index, inclusive (default: all)
  --dry-run     print the commands instead of running them
  --inline      keep DWARF inline expansion (40x slower post-processing on a
                ClickHouse debug build; default: --no-inline)
  --resume      skip queries this engine already has a report.html for
  -h, --help    this help

Environment:
  DATA_DIR        dir containing hits.parquet         (default: \$HOME/data)
  CLICKBENCH_DIR  ClickBench checkout root; the schema and the queries for each
                  engine are read from its <engine>-parquet/ directory
                                                        (default: \$HOME/src/ClickBench)
  OUT_ROOT        profile output root                 (default: <repo>/.vperf)
  FREQ            vperf sampling frequency in Hz       (default: 499 local,
                                                       99 server)
  MEM_PERIOD      vperf --mem-period, IBS cycles       (default: 1000003 local,
                                                       4000003 server)
  STARTUP_GRACE   seconds to let the target settle before the collectors
                  attach, for every engine             (default: per engine,
                                                       0.10 s)
  PRIVATE_PORT    TCP port for --private-server          (default: 19000)
  LOAD_TICK       seconds between load progress lines (default: 30)
  VPERF           vperf command to use                (default: repo venv,
                                                           else python3 -m vperf)
EOF
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --engine) shift; ENGINE="$1" ;;
        --mode) shift; MODE="$1" ;;
        --private-server) PRIVATE=1 ;;
        --load) LOAD=force ;;
        --skip-load) LOAD=skip ;;
        --optimize) OPTIMIZE=merge ;;
        --optimize-final) OPTIMIZE=final ;;
        --server-pid) shift; SERVER_PID="$1" ;;
        --max-duration) shift; MAX_DURATION="$1" ;;
        --attach-delay) shift; ATTACH_DELAY="$1" ;;
        --wait) WAIT=1 ;;
        --from) shift; FROM="$1" ;;
        --to) shift; TO="$1"; TO_SET=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --inline) INLINE=1 ;;
        --resume) RESUME=1 ;;
        -h|--help) usage ;;
        *) echo "ERROR: unknown option $1" >&2; usage ;;
    esac
    shift
done

case "$MODE" in
    local|server) ;;
    *) echo "ERROR: --mode must be local or server (got '$MODE')" >&2; exit 1 ;;
esac

if [ "$MODE" = local ]; then
    case "$ENGINE" in
        both) ENGINES="clickhouse duckdb" ;;
        clickhouse|duckdb) ENGINES="$ENGINE" ;;
        *) echo "ERROR: --engine must be clickhouse, duckdb or both (got '$ENGINE')" >&2; exit 1 ;;
    esac
    [ -f "$PARQUET" ] || { echo "ERROR: $PARQUET not found" >&2; exit 1; }
else
    # a clickhouse-server is ClickHouse's engine by definition; there is no
    # "duckdb server" for these queries to run against
    case "$ENGINE" in
        clickhouse) ENGINES="clickhouse" ;;
        *) echo "ERROR: --mode server profiles a clickhouse-server, so --engine must be clickhouse (got '$ENGINE')" >&2; exit 1 ;;
    esac
    command -v clickhouse-client >/dev/null 2>&1 || {
        echo "ERROR: clickhouse-client not in PATH (needed for --mode server)" >&2; exit 1; }
    [ "$LOAD" = skip ] || [ -f "$PARQUET" ] || {
        echo "ERROR: $PARQUET not found (needed to load hits; --skip-load to use the table already there)" >&2
        exit 1; }
    [ -f "$BENCH_DIR/clickhouse/create.sql" ] || {
        echo "ERROR: $BENCH_DIR/clickhouse/create.sql not found (the MergeTree schema)" >&2; exit 1; }
    if [ "$PRIVATE" = 0 ] && [ -z "$SERVER_PID" ]; then
        SERVER_PID="$(discover_server_pid)"
        [ -n "$SERVER_PID" ] || {
            echo "ERROR: no clickhouse-server process found. Start one, or pass --server-pid PID." >&2
            exit 1; }
    fi
    if [ -n "$SERVER_PID" ]; then
        pid_alive "$SERVER_PID" || {
            echo "ERROR: no process with PID $SERVER_PID" >&2; exit 1; }
        pid_is_server "$SERVER_PID" || {
            echo "ERROR: PID $SERVER_PID is not a clickhouse-server:" >&2
            tr '\0' ' ' <"/proc/$SERVER_PID/cmdline" 2>/dev/null >&2 || true
            echo >&2
            exit 1; }
        # not `say`: the run log does not exist yet at preflight time
        printf 'server: pid %s, owned by %s\n' \
            "$SERVER_PID" "$(stat -c%U "/proc/$SERVER_PID" 2>/dev/null || echo '?')" >&2
    fi
fi

if [ -z "$FREQ" ]; then
    # 499 Hz is right for clickhouse-local or duckdb, a process with tens of
    # threads.  A clickhouse-server runs 359 of them here (164 ThreadPool, 16
    # MergeMutate, 16 Fetch, the Bg* pools), and 499 Hz x 359 threads x 16 KiB of
    # DWARF stack is ~140 MB for a one-second query - a profile whose post-
    # processing costs more than the query.  99 Hz over the whole process is
    # still ~350 samples per thread-second; raise it with FREQ= when a short
    # query deserves more.
    if [ "$MODE" = server ]; then FREQ=99; else FREQ=499; fi
fi
if [ -z "$MEM_PERIOD" ]; then
    # likewise: IBS samples every thread that retires cycles, which in a server
    # includes the background merges, so the same period lands 10x more often
    if [ "$MODE" = server ]; then MEM_PERIOD=4000003; else MEM_PERIOD=1000003; fi
fi

[ -d "$BENCH_DIR" ] || {
    echo "ERROR: $BENCH_DIR not found.  Set CLICKBENCH_DIR to a ClickBench checkout." >&2
    exit 1
}

# ClickBench also publishes the table split into 100 parquet parts
# (hits_0.parquet ... hits_99.parquet, ~120 MB each).  A profile taken against
# one of those is a profile of a different table, so say so rather than let it
# pass as a ClickBench result.
DATA_BYTES=$(stat -c%s "$PARQUET" 2>/dev/null || echo 0)
if [ "$DATA_BYTES" -gt 0 ] && [ "$DATA_BYTES" -lt 1000000000 ]; then
    echo "WARNING: $PARQUET is only $DATA_BYTES bytes." >&2
    echo "WARNING: that looks like one of the 100 ClickBench partitions, not the" >&2
    echo "WARNING: full 100M-row hits.parquet (~14.8 GB) the queries are written" >&2
    echo "WARNING: for.  The profiles will not be ClickBench profiles." >&2
fi

# Resolve vperf: explicit override, repo venv, then the source checkout.
if [ -z "$VPERF" ]; then
    if [ -x "$REPO_ROOT/.venv/bin/vperf" ]; then
        VPERF="$REPO_ROOT/.venv/bin/vperf"
    elif command -v vperf >/dev/null 2>&1; then
        VPERF="$(command -v vperf)"
    else
        VPERF="env PYTHONPATH=$REPO_ROOT python3 -m vperf"
    fi
fi

INLINE_FLAG="--no-inline"
if [ "$INLINE" = 1 ]; then
    INLINE_FLAG=""
fi

# Progress and the final table go to the terminal and the run log.
say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG" >&2; }

# The engine's own command line for one query, filled into ARGV.  It has to be
# an array and not a printed list: both engines take the schema as a single
# argument and it spans many lines, which word splitting would tear apart.
# clickhouse-local takes the schema and the statement in one --query; duckdb
# takes them as two -c commands, where -no-stdin is what keeps the CLI from
# reading the loop's stdin after the commands are done (and -readonly is out:
# it refuses an in-memory database, which is what a bare invocation opens).  Both need DATA_DIR as
# the cwd for the DDL's relative hits.parquet path, and neither wants anything
# on stdin.
ARGV=()
build_argv() {  # <engine> <schema> <query>
    case "$1" in
        clickhouse) ARGV=(--time --format=Null "--query=$2 $3") ;;
        duckdb) ARGV=(-no-stdin -c ".timer on" -c "$2" -c "$3") ;;
    esac
}

headline() {  # <vperf.log> -> "<cpu> <ipc> <samples> <ibs>"
    python3 -c '
import sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
# only look at vperf own summary: the engine prints its result before it
banner = text.find(" vperf summary  |")
if banner >= 0:
    text = text[banner:]
def val(label):
    for line in text.splitlines():
        if line.startswith(label) and "|" in line:
            v = line.split("|", 1)[1].split()
            return v[0] if v and v[0] not in ("n/a", "-") else "-"
    return "-"
print(val("CPU Time"), val("IPC / CPI"), val("Samples Collected"),
      val("IBS Samples Collected"))' "$1" 2>/dev/null || echo "- - - -"
}

sweep() {  # <engine>
    local engine="$1" src label dir done_dir q rc dur elapsed t0 reports degraded
    local ddl grace binary run_id tsv to n
    local -a queries
    local from="$FROM"
    src="$BENCH_DIR/${engine}-parquet"
    grace="$(grace_for "$engine")"
    binary="$(binary_for "$engine")"

    [ -d "$src" ] || { echo "ERROR: $src not found (needed for $engine)" >&2; return 1; }
    [ -f "$src/queries.sql" ] || { echo "ERROR: $src/queries.sql not found" >&2; return 1; }
    [ -f "$src/create.sql" ] || { echo "ERROR: $src/create.sql not found" >&2; return 1; }
    command -v "$binary" >/dev/null 2>&1 || {
        echo "ERROR: $binary not in PATH (needed for $engine)" >&2; return 1; }

    # Read the whole query file up front: leaving the queries file on stdin
    # would make the engine try to parse it as input.
    ddl="$(cat "$src/create.sql")"
    mapfile -t queries < "$src/queries.sql"
    n=${#queries[@]}
    # --to 0 is a valid range, not "unset"
    if [ "$TO_SET" = 1 ]; then to="$TO"; else to=$((n - 1)); fi

    run_id="$(date +%Y%m%d_%H%M%S)"
    LOG="$OUT_ROOT/clickbench_${engine}_${run_id}.log"
    tsv="$OUT_ROOT/clickbench_${engine}_${run_id}.tsv"
    DONE_DIRS=()
    mkdir -p "$OUT_ROOT"
    if [ "$DRY_RUN" = 0 ]; then
        printf '#\telapsed_s\tcpu_s\tipc\tsamples\tibs_samples\texit\tdir\n' >"$tsv"
        : >"$LOG"
    else
        LOG=/dev/null   # a dry run should not leave a log behind
    fi

    say "engine=$engine queries=$((to - from + 1))/$n freq=${FREQ}Hz mem_period=$MEM_PERIOD grace=${grace}s ${INLINE_FLAG:-inlined}"
    say "schema=$src"
    say "data=$DATA_DIR/hits.parquet ($(du -h "$DATA_DIR/hits.parquet" | cut -f1))"
    say "vperf=$VPERF"
    say "index=$tsv"

    for ((i = from; i <= to; i++)); do
        q="${queries[$i]}"
        label="$(printf 'Q%02d' "$i")"
        local ts
        ts="$(date +%Y%m%d_%H%M%S)"
        dir="$OUT_ROOT/$(printf 'q%02d_%s_%s' "$i" "$(target_token "$engine")" "$ts")"

        # scoped to this engine: a flat directory also holds the other
        # engine's profiles, and those do not count as this query being done
        if [ "$RESUME" = 1 ]; then
            for done_dir in "$OUT_ROOT/$(printf 'q%02d_%s_' "$i" "$(target_token "$engine")")"*; do
                if [ -f "$done_dir/report.html" ]; then
                    say "$label skip (already profiled as ${done_dir##*/})"
                    printf '%s\t-\t-\t-\t-\t-\t-\t%s\n' \
                        "$label" "${done_dir##*/}" >>"$tsv"
                    continue 2
                fi
            done
        fi

        if [ "$DRY_RUN" = 1 ]; then
            # the real argv, with the schema collapsed so the line stays readable
            echo "== $engine $label -> $dir"
            build_argv "$engine" "$ddl" "$q"
            printf '   (cd %s && %s run -f %s --mem-period %s %s --startup-grace %s -o %s -- %s' \
                "$DATA_DIR" "$VPERF" "$FREQ" "$MEM_PERIOD" "$INLINE_FLAG" "$grace" "$dir" "$binary"
            # the schema is quoted so its * and () stay literal in the pattern
            for arg in "${ARGV[@]}"; do
                printf " %q" "${arg//"$ddl"/<schema.sql>}"
            done
            echo ") </dev/null"
            continue
        fi

        mkdir -p "$dir"
        say "$label start -> ${dir##*/}"
        build_argv "$engine" "$ddl" "$q"
        # keep the exact statement next to the profile, so a report can be
        # traced back to the engine and query that produced it
        printf '%s\n%s\n' "$ddl" "$q" >"$dir/queries.sql"
        t0=$(date +%s)
        set +e
        (
            cd "$DATA_DIR"
            $VPERF run -f "$FREQ" --mem-period "$MEM_PERIOD" $INLINE_FLAG \
                --startup-grace "$grace" -o "$dir" -- "$binary" "${ARGV[@]}"
        ) </dev/null >"$dir/vperf.log" 2>&1
        rc=$?
        set -e
        dur=$(( $(date +%s) - t0 ))
        if [ "$rc" -ne 0 ]; then
            printf 'vperf exit=%s\n' "$rc" >>"$dir/vperf.log"
            say "$label FAILED (vperf exit $rc) after ${dur}s"
        else
            say "$label done in ${dur}s"
        fi

        elapsed=""
        if [ -f "$dir/meta.json" ]; then
            elapsed=$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
e = d.get("elapsed_wall")
print(f"{e:.2f}" if isinstance(e, (int, float)) else "")' "$dir/meta.json" 2>/dev/null || true)
        fi
        read -r cpu ipc samples ibs < <(headline "$dir/vperf.log") || true
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$label" "${elapsed:--}" "${cpu:--}" "${ipc:--}" "${samples:--}" \
            "${ibs:--}" "$rc" "${dir##*/}" >>"$tsv"
        DONE_DIRS+=("$dir")
    done

    if [ "$DRY_RUN" = 1 ]; then
        return 0
    fi

    summarize "$(target_token "$engine")" "label elapsed_s cpu_s ipc samples ibs exit dir"
}

# The end-of-sweep table, shared by both modes: what each profile cost, whether
# it has a report, and which ones came out without samples.
summarize() {  # <token> <columns>
    local token="$1" columns="$2" d
    local reports=0 degraded=0
    for d in "${DONE_DIRS[@]}"; do
        [ -f "$d/report.html" ] && reports=$((reports + 1))
        # any of these leaves a report with no samples in it; a target that
        # finished inside the grace and one whose perf record never opened are
        # the same outcome for the reader of the summary
        if grep -qE 'startup grace|perf record failed|perf stat failed|retrying' \
                "$d/vperf.log" 2>/dev/null; then
            degraded=$((degraded + 1))
            say "  no samples: ${d##*/} ($(grep -oE 'Target exited during collector startup grace.*|perf record failed.*' "$d/vperf.log" | head -1))"
        fi
    done

    say "summary [$token] ($columns):"
    { column -t -s $'\t' "$tsv" || cat "$tsv"; } | tee -a "$LOG" >&2
    say "report.html present [$token]: $reports/${#DONE_DIRS[@]} (without samples: $degraded)"
    say "index: $tsv"
    say "log: $LOG"
}

# ---------------------------------------------------------------------------
# server mode: the queries against a MergeTree `hits` in a clickhouse-server
# ---------------------------------------------------------------------------

# clickhouse-client inherits host, port, user and password from
# ~/.clickhouse-client/config.xml - the driver has no auth flags of its own, so
# the server's credentials live in one place the user controls.  A private
# server is the exception that needs spelling out: it has no password and its
# own port, and the client config would otherwise point the connection at the
# shared server instead.
conn_args() {
    CONN_ARGS=()
    if [ "$PRIVATE" = 1 ]; then
        CONN_ARGS=(--port "$PRIVATE_PORT" --password '')
    fi
}

# A statement whose answer the driver needs (row counts, part counts).  No
# --time: these are bookkeeping, and their timings are noise in the log.
ch_plain() {  # <sql>
    conn_args
    clickhouse-client "${CONN_ARGS[@]}" --query "$1"
}

# The load statement ClickBench itself runs, with the server's own output in the
# run log: an INSERT that fails says why in those words, next to the step that
# caused it.
run_logged() {
    # errexit has to come off for the pipeline (pipefail would abort it before
    # PIPESTATUS could be read) - and it has to go back on afterwards, because
    # `set` inside a function is global to the shell.  Leaving it off would make
    # every later failure in the run a shrug instead of an exit.
    local had_errexit=0 rc
    case "$-" in *e*) had_errexit=1 ;; esac
    set +e
    clickhouse-client "${CONN_ARGS[@]}" "$@" 2>&1 | tee -a "$LOG" >&2
    rc="${PIPESTATUS[0]}"
    [ "$had_errexit" = 1 ] && set -e
    return "$rc"
}

# vperf freezes the pid it attaches to while its collectors open on it and
# resumes it once they are up, so that freeze is the readiness signal: no
# guessed sleep, and no idle server time ahead of the query inside the window
# the profile covers.
wait_for_attach() {  # <pid> <vperf-pid> -> 0 ready, 1 gave up
    local pid="$1" vp="$2" state seen=0
    local deadline=$((SECONDS + 15))
    while [ "$SECONDS" -lt "$deadline" ]; do
        kill -0 "$vp" 2>/dev/null || return 1
        # the comm field can contain spaces, so cut at the last ')' before the
        # single-letter state that follows it
        state="$(sed -e 's/^.*) //' -e 's/ .*//' "/proc/$pid/stat" 2>/dev/null || true)"
        case "$state" in
            T|t) seen=1 ;;
            *) if [ "$seen" = 1 ]; then return 0; fi ;;
        esac
        sleep 0.005
    done
    return 1
}

# Readiness: vperf writes recording.started the instant before it starts
# monitoring the collectors, which is exactly when the workload may begin.  It
# cannot be inferred from perf.data: that file exists from the moment perf record
# opens, and a collector-settle window plus the samplers' start still have to pass
# before samples arrive - vperf also spends ~1 s probing PMU capabilities before
# it opens anything at all.  Both gaps are longer than a fast query, and a SIGINT
# that lands in one ends the run with nothing collected.
wait_for_recording() {  # <profile dir> -> 0 ready, 1 gave up
    local dir="$1"
    local deadline=$((SECONDS + 30))
    while [ "$SECONDS" -lt "$deadline" ]; do
        [ -f "$dir/recording.started" ] && return 0
        kill -0 "$2" 2>/dev/null || return 1
        sleep 0.02
    done
    return 1
}

# clickhouse-client --time's own accounting line: "Elapsed: 3.2 sec. Processed
# 100.00 million rows, 1.20 GB".  It is what ClickBench shows, and it measures
# the query itself rather than the profile window around it.
client_headline() {  # <query stderr file> -> "<elapsed_s> <rows>"
    python3 -c '
import re, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
elapsed = re.search(r"Elapsed:\s*([\d.]+)\s*sec", text)
rows = re.search(r"Processed\s+([^\n,]*)rows?", text)
print(elapsed.group(1) if elapsed else "-", (rows.group(1).strip() if rows else "-"))' "$1" 2>/dev/null || echo "- -"
}

# One query id per profiled query, so the server's own logs (query_log, the
# text in system.processes) can be lined up with the profile directory.
query_id() {  # <token> <label>
    printf 'vperf-%s-%s-%s-%s' "$1" "$2" "$(date +%H%M%S)" "$$"
}

start_private_server() {
    PRIVATE_DIR="$OUT_ROOT/_private-clickhouse"
    rm -rf "$PRIVATE_DIR"
    mkdir -p "$PRIVATE_DIR/data" "$PRIVATE_DIR/tmp" "$PRIVATE_DIR/user_files"
    # A server of our own needs no privileges: its own config, its own data dir,
    # and caches capped so it cannot crowd a shared instance out of RAM.  The
    # HTTP and interserver ports are 0 (off) because a server on this host
    # already owns 8123 and 9009.  user_files_path is a scratch dir this run never
    # reads: the load streams the parquet in rather than asking the server to
    # open it, which is what lets the same config work for a private server as
    # for a shared one.
    cat >"$PRIVATE_DIR/config.xml" <<XMLCONFIG
<clickhouse>
    <logger>
        <level>warning</level>
        <log>$PRIVATE_DIR/server.log</log>
        <errorlog>$PRIVATE_DIR/error.log</errorlog>
    </logger>
    <path>$PRIVATE_DIR/data/</path>
    <tmp_path>$PRIVATE_DIR/tmp/</tmp_path>
    <user_files_path>$PRIVATE_DIR/user_files/</user_files_path>
    <listen_host>127.0.0.1</listen_host>
    <tcp_port>$PRIVATE_PORT</tcp_port>
    <http_port>0</http_port>
    <interserver_http_port>0</interserver_http_port>
    <mark_cache_size>1073741824</mark_cache_size>
    <uncompressed_cache_size>0</uncompressed_cache_size>
    <max_server_memory_usage>8000000000</max_server_memory_usage>
    <mlock_executable>false</mlock_executable>
    <users>
        <default>
            <password></password>
            <networks><ip>127.0.0.1</ip></networks>
            <profile>default</profile>
            <quota>default</quota>
        </default>
    </users>
    <profiles><default/></profiles>
    <quotas><default/></quotas>
</clickhouse>
XMLCONFIG
    say "private server: starting on port $PRIVATE_PORT (data $PRIVATE_DIR)"
    clickhouse server --config-file="$PRIVATE_DIR/config.xml" >>"$PRIVATE_DIR/stdout.log" 2>&1 &
    PRIVATE_PID=$!
    local deadline=$((SECONDS + 90))
    while [ "$SECONDS" -lt "$deadline" ]; do
        pid_alive "$PRIVATE_PID" || {
            echo "ERROR: the private server exited during startup (see $PRIVATE_DIR/error.log)" >&2
            return 1; }
        if clickhouse-client --port "$PRIVATE_PORT" --password '' --query "SELECT 1" >/dev/null 2>&1; then
            SERVER_PID="$PRIVATE_PID"
            say "private server: ready as pid $PRIVATE_PID"
            return 0
        fi
        sleep 0.2
    done
    echo "ERROR: the private server did not become ready within 90s" >&2
    return 1
}

stop_private_server() {
    [ -n "${PRIVATE_PID:-}" ] || return 0
    say "private server: stopping pid $PRIVATE_PID"
    kill -TERM "$PRIVATE_PID" 2>/dev/null || return 0
    local deadline=$((SECONDS + 60))
    while [ "$SECONDS" -lt "$deadline" ] && pid_alive "$PRIVATE_PID"; do
        sleep 0.2
    done
    if pid_alive "$PRIVATE_PID"; then
        say "private server: did not stop in 60s, killing it"
        kill -KILL "$PRIVATE_PID" 2>/dev/null || true
    fi
    PRIVATE_PID=""
}

# The dataset is ours to read, so it is checked here rather than on the server:
# no user_files, no root, and a mismatch is caught before a byte is sent rather
# than after 100M rows.  The guard is stricter than it looks necessary: the
# Parquet reader maps columns by *name* and
# input_format_parquet_allow_missing_columns is 1, so a dataset that is missing a
# column inserts cleanly and then answers every query with that column's default.
check_parquet() {  # <create.sql> <describe output file>
    local err="$2.err"
    if [ ! -r "$PARQUET" ]; then
        echo "ERROR: $PARQUET is not readable by $(id -un)" >&2
        return 1
    fi
    if ! clickhouse-local --query "DESCRIBE SELECT * FROM file('$PARQUET')" >"$2" 2>"$err"; then
        {
            echo "ERROR: could not read $PARQUET."
            echo
            echo "The load streams this file to the server, so it only has to be readable"
            echo "here.  clickhouse-local says:"
            sed 's/^/  /' "$err" 2>/dev/null
        } >&2
        rm -f "$err"
        return 1
    fi
    rm -f "$err"
    if ! check_columns "$1" "$2"; then
        echo "ERROR: $PARQUET does not match the schema in $1 (above)" >&2
        return 1
    fi
}

# Insert progress, because the schema asks for fsync_after_insert=1 and the load
# is minutes long: a silent wait is indistinguishable from a hang.
watch_load() {
    while :; do
        sleep "${LOAD_TICK:-30}"
        say "load: $(ch_plain "SELECT count() FROM system.parts WHERE database=currentDatabase() AND table='hits' AND active") parts, $(ch_plain "SELECT formatReadableSize(sum(bytes_on_disk)) FROM system.parts WHERE database=currentDatabase() AND table='hits' AND active") so far"
    done
}

fmt_bytes() {  # <bytes> -> "8.4 GiB"
    awk -v b="${1:-0}" 'BEGIN{split("B KiB MiB GiB TiB", u, " "); i=1
        while (b >= 1024 && i < 5) {b /= 1024; i++}
        printf (i == 1 ? "%d %s\n" : "%.1f %s\n"), b, u[i]}'
}

# Create and fill `hits`: the MergeTree schema from the checkout, then the
# parquet's own bytes streamed in.
#
# ClickBench's clickhouse/load instead asks the server to read the parquet where
# it lies - INSERT ... SELECT * FROM file('<parquet>') - which needs a symlink in
# the server's user_files, because ClickHouse confines file() to user_files_path
# (/var/lib/clickhouse/user_files/, under a 700 clickhouse-owned /var/lib), and
# putting a file there means root. Measured here: Code: 291, DATABASE_ACCESS_DENIED,
# "File ... is not inside /var/lib/clickhouse/user_files". So the bytes go over the
# wire instead and the server parses them (Parquet is an input format, and
# input_format_parallel_parsing is on). Nothing here needs privileges, and the
# dataset is checked before a byte is sent rather than after 100M rows.
load_table() {
    local create="$BENCH_DIR/clickhouse/create.sql" have rows parts ddl_file rc t0
    local describe="$OUT_ROOT/.hits-describe.tsv" free_bytes watcher=0

    if [ "$DRY_RUN" = 1 ]; then
        say "load (dry run): clickhouse-client < $create"
        say "load (dry run): clickhouse-client --query 'INSERT INTO hits FORMAT Parquet' < $PARQUET"
        if [ "$OPTIMIZE" != none ]; then
            say "load (dry run): OPTIMIZE TABLE hits$( [ "$OPTIMIZE" = final ] && printf ' FINAL')"
        fi
        return 0
    fi

    conn_args
    if ! clickhouse-client "${CONN_ARGS[@]}" --query "SELECT 1" >/dev/null; then
        cat >&2 <<NOCONN
ERROR: cannot talk to the clickhouse-server.

The driver has no auth flags of its own: host, port, user and password come from
~/.clickhouse-client/config.xml, so put the server's credentials there
(<host>, <port>, <user>, <password>) and try again.
NOCONN
        return 1
    fi

    have="$(ch_plain "SELECT count() FROM system.tables WHERE database=currentDatabase() AND name='hits'")"
    if [ "$LOAD" = skip ] && [ "$have" = 0 ]; then
        echo "ERROR: --skip-load, but the server has no hits table in $(ch_plain 'SELECT currentDatabase()')" >&2
        return 1
    elif [ "$LOAD" = skip ]; then
        say "load: skipped (--skip-load)"
    elif [ "$have" = 0 ] || [ "$LOAD" = force ]; then
        say "load: creating hits from $create"
        ddl_file="$(mktemp "$OUT_ROOT/.hits-ddl.XXXXXX.sql")"
        { printf 'DROP TABLE IF EXISTS hits SYNC;\n'; cat "$create"; } >"$ddl_file"
        run_logged --multiquery <"$ddl_file"
        rc=$?
        rm -f "$ddl_file"
        if [ "$rc" -ne 0 ]; then
            echo "ERROR: creating the hits table failed (see $LOG)" >&2
            return 1
        fi

        check_parquet "$create" "$describe" || {
            rm -f "$describe"
            return 1
        }
        rm -f "$describe"

        # the smallest local disk's usable space, not the sum: parts have to fit
        # somewhere, and unreserved_space already has keep_free_space taken out
        free_bytes="$(ch_plain "SELECT min(unreserved_space) FROM system.disks WHERE is_remote = 0 AND is_write_once = 0")"
        if [ -n "${free_bytes:-}" ] && [ "$free_bytes" -lt "$DATA_BYTES" ]; then
            say "WARNING: the server's volumes have $(fmt_bytes "$free_bytes") free, the parquet is $(fmt_bytes "$DATA_BYTES")"
            say "WARNING: the load may run the volume out of disk part-way through"
        fi

        say "load: streaming $PARQUET into the table (the server parses it; Parquet is an input format)"
        say "load: 100M rows into a MergeTree with fsync_after_insert=1 takes minutes; sizes below are the table as it fills"
        t0=$(date +%s)
        watch_load &
        watcher=$!
        set +e
        run_logged --time --query "INSERT INTO hits FORMAT Parquet" <"$PARQUET"
        rc=$?
        set -e
        kill "$watcher" 2>/dev/null
        wait "$watcher" 2>/dev/null || true
        if [ "$rc" -ne 0 ]; then
            echo "ERROR: the insert failed (see $LOG)" >&2
            return 1
        fi
        say "load: inserted in $(( $(date +%s) - t0 ))s"
    else
        say "load: reusing the hits table already in the server"
    fi

    rows="$(ch_plain "SELECT count() FROM hits")"
    parts="$(ch_plain "SELECT count() FROM system.parts WHERE database=currentDatabase() AND table='hits' AND active")"
    say "hits: ${rows:-?} rows in ${parts:-?} active parts"
    say "size: $(ch_plain "SELECT formatReadableSize(total_bytes) FROM system.tables WHERE database=currentDatabase() AND name='hits'")"

    if [ "$OPTIMIZE" != none ]; then
        # Not ClickBench's own step, which is why it is opt-in: with no
        # PARTITION BY in the schema, OPTIMIZE FINAL merges every part into a
        # single part of the whole table, which is minutes of background work
        # that changes what a query reads and therefore what a profile shows.
        if [ "$OPTIMIZE" = final ]; then
            say "optimize: OPTIMIZE TABLE hits FINAL (was $parts parts)"
            run_logged --query "OPTIMIZE TABLE hits FINAL"
        else
            say "optimize: OPTIMIZE TABLE hits (was $parts parts)"
            run_logged --query "OPTIMIZE TABLE hits"
        fi
        rc=$?
        if [ "$rc" -ne 0 ]; then
            echo "ERROR: OPTIMIZE failed (see $LOG)" >&2
            return 1
        fi
        say "optimize: done, now $(ch_plain "SELECT count() FROM system.parts WHERE database=currentDatabase() AND table='hits' AND active") active parts"
    fi
}

# create.sql's column list, in order, against the parquet's own.  A dataset
# with the same columns in another order inserts, and then answers every query
# with the wrong values in some of them - so this is checked, not assumed.
check_columns() {  # <create.sql> <DESCRIBE tsv>
    python3 - "$1" "$2" <<'COLUMNS_PY'
import csv
import re
import sys

sql = open(sys.argv[1], encoding="utf-8").read()
# the column list is what sits between the first '(' of the table definition
# and the ')' that closes it; ENGINE, SETTINGS and the trailing PRIMARY KEY
# clause are not columns
body = sql[sql.index("(", sql.upper().index("TABLE")) + 1:]
cols = []
for line in body.splitlines():
    line = line.strip().rstrip(",")
    if line.startswith(")"):
        break
    if re.match(r"(PRIMARY KEY|KEY|INDEX|CONSTRAINT)\b", line):
        continue
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+\S", line)
    if m:
        cols.append(m.group(1))
with open(sys.argv[2], encoding="utf-8") as source:
    par = [row[0] for row in csv.reader(source, delimiter="\t") if row]
if not cols or not par:
    print(f"  could not read the schema (create.sql: {len(cols)} columns, parquet: {len(par)})")
    sys.exit(1)
if cols == par:
    print(f"  {len(par)} columns match {sys.argv[1]}")
    sys.exit(0)
for i, (a, b) in enumerate(zip(cols + [""] * len(par), par + [""] * len(cols))):
    if a != b:
        print(f"  column {i}: table {a or '<none>'} vs parquet {b or '<none>'}")
sys.exit(1)
COLUMNS_PY
}

# One profile per query, taken by attaching to the server for exactly as long
# as the query runs.  The duration is unknowable in advance - Q00 is ~0.1 s and
# Q35 is ~90 s - so a fixed window would either cut the query off or pad it
# with idle, and the profile is ended by SIGINT the moment the client returns.
sweep_server() {
    local token label dir done_dir q rc crc dur t0 vp cpid qid qtime
    local src="$BENCH_DIR/clickhouse" n to i ts ddl wait_flag client_s
    local -a queries
    local from="$FROM"
    token="$(target_token clickhouse)"
    wait_flag="--no-wait"
    [ "$WAIT" = 1 ] && wait_flag=""

    [ -f "$src/queries.sql" ] || { echo "ERROR: $src/queries.sql not found" >&2; return 1; }
    mapfile -t queries < "$src/queries.sql"
    n=${#queries[@]}
    if [ "$TO_SET" = 1 ]; then to="$TO"; else to=$((n - 1)); fi
    ddl="$(cat "$src/create.sql")"

    say "server=$src queries=$((to - from + 1))/$n pid=$SERVER_PID freq=${FREQ}Hz mem_period=$MEM_PERIOD ceiling=${MAX_DURATION}s ${INLINE_FLAG:-inlined} $wait_flag"
    say "vperf=$VPERF"
    say "index=$tsv"
    say "note: a profile here is the whole server process - scope it to the query's threads in the Threads tab"

    for ((i = from; i <= to; i++)); do
        q="${queries[$i]}"
        label="$(printf 'Q%02d' "$i")"
        ts="$(date +%Y%m%d_%H%M%S)"
        dir="$OUT_ROOT/$(printf 'q%02d_%s_%s' "$i" "$token" "$ts")"

        if [ "$RESUME" = 1 ]; then
            for done_dir in "$OUT_ROOT/$(printf 'q%02d_%s_' "$i" "$token")"*; do
                if [ -f "$done_dir/report.html" ]; then
                    say "$label skip (already profiled as ${done_dir##*/})"
                    printf '%s\t-\t-\t-\t-\t-\t-\t-\t-\t%s\n' \
                        "$label" "${done_dir##*/}" >>"$tsv"
                    continue 2
                fi
            done
        fi

        if [ "$DRY_RUN" = 1 ]; then
            echo "== $token $label -> $dir"
            printf '   %s attach -p %s --duration %s -f %s --mem-period %s %s %s -o %s   (ended by SIGINT when the query returns)\n' \
                "$VPERF" "$SERVER_PID" "$MAX_DURATION" "$FREQ" "$MEM_PERIOD" \
                "$INLINE_FLAG" "$wait_flag" "$dir"
            printf '   (cd %s && clickhouse-client --time --query-id %s --query %q) 2> %s/query.stderr\n' \
                "$DATA_DIR" "$(query_id "$token" "$label")" "$q" "$dir"
            continue
        fi

        mkdir -p "$dir"
        say "$label start -> ${dir##*/}"
        printf '%s\n%s\n' "$ddl" "$q" >"$dir/queries.sql"
        qid="$(query_id "$token" "$label")"

        t0=$(date +%s)
        set +e
        $VPERF attach -p "$SERVER_PID" --duration "$MAX_DURATION" -f "$FREQ" \
            --mem-period "$MEM_PERIOD" $INLINE_FLAG $wait_flag \
            --callgraph dwarf -o "$dir" </dev/null >"$dir/vperf.log" 2>&1 &
        vp=$!
        if [ "$ATTACH_DELAY" != 0 ]; then
            sleep "$ATTACH_DELAY"
        elif kill -0 "$SERVER_PID" 2>/dev/null; then
            wait_for_attach "$SERVER_PID" "$vp" \
                || say "$label WARNING: attach readiness unconfirmed; the query may start before the collectors are up"
        else
            # Not ours to signal (a systemd server belongs to the clickhouse
            # user), so vperf cannot freeze it and there is no freeze to watch
            # for. Its own marker is the next best "collectors are live".
            wait_for_recording "$dir" "$vp" \
                || say "$label WARNING: the collectors never reported ready; the query may start before they are up"
        fi
        ( cd "$DATA_DIR" && clickhouse-client --time --query-id "$qid" --query "$q" \
            </dev/null >"$dir/query.out" 2>"$dir/query.stderr" ) &
        cpid=$!
        wait "$cpid"
        crc=$?
        # the query is done: end the profile on the spot rather than let it run
        # out to the ceiling over an idle server
        kill -INT "$vp" 2>/dev/null
        wait "$vp"
        rc=$?
        set -e
        dur=$(( $(date +%s) - t0 ))
        if [ "$crc" -ne 0 ]; then
            say "$label QUERY FAILED (clickhouse-client exit $crc)"
            head -5 "$dir/query.stderr" 2>/dev/null | while IFS= read -r line; do say "  $line"; done
        else
            say "$label done in ${dur}s (post-processing included)"
        fi
        if [ "$rc" -ne 0 ]; then
            printf 'vperf exit=%s\n' "$rc" >>"$dir/vperf.log"
            if [ "$rc" = 130 ]; then
                say "$label vperf was interrupted before it collected anything (see vperf.log)"
            else
                say "$label vperf exit $rc"
            fi
        fi

        qtime=""
        if [ -f "$dir/meta.json" ]; then
            qtime=$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
e = d.get("elapsed_wall")
print(f"{e:.2f}" if isinstance(e, (int, float)) else "")' "$dir/meta.json" 2>/dev/null || true)
        fi
        read -r client_s _ < <(client_headline "$dir/query.stderr") || true
        read -r cpu ipc samples ibs < <(headline "$dir/vperf.log") || true
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$label" "${qtime:--}" "${client_s:--}" "${cpu:--}" "${ipc:--}" \
            "${samples:--}" "${ibs:--}" "$crc" "$rc" "${dir##*/}" >>"$tsv"
        DONE_DIRS+=("$dir")
    done

    [ "$DRY_RUN" = 1 ] && return 0
    summarize "$token" "label window_s query_s cpu_s ipc samples ibs client_exit vperf_exit dir"
}

if [ "$MODE" = server ]; then
    token="$(target_token clickhouse)"
    run_id="$(date +%Y%m%d_%H%M%S)"
    LOG="$OUT_ROOT/clickbench_${token}_${run_id}.log"
    tsv="$OUT_ROOT/clickbench_${token}_${run_id}.tsv"
    DONE_DIRS=()
    mkdir -p "$OUT_ROOT"
    if [ "$DRY_RUN" = 0 ]; then
        printf '#\twindow_s\tquery_s\tcpu_s\tipc\tsamples\tibs_samples\tclient_exit\tvperf_exit\tdir\n' >"$tsv"
        : >"$LOG"
    else
        LOG=/dev/null
        tsv=/dev/null
    fi
    say "mode=server $token queries from $BENCH_DIR/clickhouse"
    if [ "$PRIVATE" = 1 ]; then
        if [ "$DRY_RUN" = 0 ]; then
            start_private_server
        else
            # nothing to attach to yet in a dry run; name the pid the run will have
            SERVER_PID="(the private server started for this run)"
        fi
    fi
    load_table || exit 1
    sweep_server
    if [ "$PRIVATE" = 1 ] && [ "$DRY_RUN" = 0 ]; then
        stop_private_server
    fi
else
    for e in $ENGINES; do
        sweep "$e"
    done
fi
