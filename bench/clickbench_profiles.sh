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
FREQ="${FREQ:-499}"
# AMD IBS period in cycles.  The Memory tab is only as good as the number of
# classified accesses, and vperf caps recorded stacks at 128 frames, so the
# perf dumps stay cheap: at 1e6 a 30 s-CPU query yields ~60k IBS samples for a
# few seconds of extra post-processing.  Raise it for a very long target.
MEM_PERIOD="${MEM_PERIOD:-1000003}"
VPERF="${VPERF:-}"
ENGINE="clickhouse"
INLINE=0
RESUME=0
FROM=0
TO=0
TO_SET=0
DRY_RUN=0

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

usage() {
    cat <<EOF
Usage: clickbench_profiles.sh [options]

Profiles each ClickBench query once with vperf, one profile directory per
query and engine.

Options:
  --engine E    clickhouse (default), duckdb, or both - run in that order
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
  FREQ            vperf sampling frequency in Hz       (default: 499)
  MEM_PERIOD      vperf --mem-period, IBS cycles       (default: 1000003)
  STARTUP_GRACE   seconds to let the target settle before the collectors
                  attach, for every engine             (default: per engine,
                                                       0.10 s)
  VPERF           vperf command to use                (default: repo venv,
                                                           else python3 -m vperf)
EOF
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --engine) shift; ENGINE="$1" ;;
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

case "$ENGINE" in
    both) ENGINES="clickhouse duckdb" ;;
    clickhouse|duckdb) ENGINES="$ENGINE" ;;
    *) echo "ERROR: --engine must be clickhouse, duckdb or both (got '$ENGINE')" >&2; exit 1 ;;
esac

[ -f "$DATA_DIR/hits.parquet" ] || { echo "ERROR: $DATA_DIR/hits.parquet not found" >&2; exit 1; }
[ -d "$BENCH_DIR" ] || {
    echo "ERROR: $BENCH_DIR not found.  Set CLICKBENCH_DIR to a ClickBench checkout." >&2
    exit 1
}

# ClickBench also publishes the table split into 100 parquet parts
# (hits_0.parquet ... hits_99.parquet, ~120 MB each).  A profile taken against
# one of those is a profile of a different table, so say so rather than let it
# pass as a ClickBench result.
DATA_BYTES=$(stat -c%s "$DATA_DIR/hits.parquet" 2>/dev/null || echo 0)
if [ "$DATA_BYTES" -lt 1000000000 ]; then
    echo "WARNING: $DATA_DIR/hits.parquet is only $DATA_BYTES bytes." >&2
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
        dir="$OUT_ROOT/$(printf 'q%02d_%s_%s' "$i" "$engine" "$ts")"

        # scoped to this engine: a flat directory also holds the other
        # engine's profiles, and those do not count as this query being done
        if [ "$RESUME" = 1 ]; then
            for done_dir in "$OUT_ROOT/$(printf 'q%02d_%s_' "$i" "$engine")"*; do
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

    reports=0
    degraded=0
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

    say "summary [$engine] (label elapsed_s cpu_s ipc samples ibs exit dir):"
    { column -t -s $'\t' "$tsv" || cat "$tsv"; } | tee -a "$LOG" >&2
    say "report.html present [$engine]: $reports/${#DONE_DIRS[@]} (without samples: $degraded)"
    say "index: $tsv"
    say "log: $LOG"
}

for e in $ENGINES; do
    sweep "$e"
done
