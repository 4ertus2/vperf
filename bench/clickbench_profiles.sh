#!/usr/bin/env bash
# Profile every ClickBench query with vperf and keep one HTML report per query.
#
# Each query gets its own profile directory under .vperf/, named with the
# ClickBench query number and the launch timestamp, e.g.
#   .vperf/q00_20260926_120501/report.html
# A per-run log (clickbench_<runid>.log) holds the driver progress and a TSV
# index (clickbench_<runid>.tsv) the headline metrics.
#
# Queries are run strictly one at a time: concurrent profiling sessions
# multiplex the hardware counters and distort the measurements.
#
# The schema and the queries come from the ClickBench checkout itself
# ($CLICKBENCH_DIR/clickhouse-parquet/{create.sql,queries.sql}), so any
# checkout works - nothing is read from an agent-specific skill folder. The
# DDL refers to 'hits.parquet' relative to the cwd, so the queries run with
# DATA_DIR as their working directory.
#
# Against the full 100M-row hits.parquet a query runs for seconds, which is
# long enough to sample. A query that is still too short is repeated
# in-process, at most KMAX times, so the report describes the query and not
# just the engine's startup; reps are recorded in the index and the exact
# statement list is kept next to each profile as queries.sql.
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
TARGET_S="${TARGET_S:-2.0}"
KMAX="${KMAX:-25}"
PROBE_REPS="${PROBE_REPS:-3}"
VPERF="${VPERF:-}"
INLINE=0
RESUME=0
FROM=0
TO=0
TO_SET=0
DRY_RUN=0
RETIME=0

usage() {
    cat <<EOF
Usage: clickbench_profiles.sh [options]

Profiles each ClickBench query with vperf, one profile directory per query.

Options:
  --from N     first query index (0-based, default: all)
  --to N       last query index, inclusive (default: all)
  --dry-run    print the commands instead of running them
  --retime     re-measure query timings instead of reusing the cache
  --inline     keep DWARF inline expansion (40x slower post-processing on a
                ClickHouse debug build; default: --no-inline)
  --resume     skip queries that already have a report.html under \$OUT_ROOT
  -h, --help   this help

Environment:
  DATA_DIR        dir containing hits.parquet         (default: \$HOME/data)
  CLICKBENCH_DIR  ClickBench checkout root; the schema and the queries are
                  read from its clickhouse-parquet/ directory
                                                        (default: \$HOME/src/ClickBench)
  OUT_ROOT        profile output root                 (default: <repo>/.vperf)
  FREQ            vperf sampling frequency in Hz       (default: 499)
  MEM_PERIOD      vperf --mem-period, IBS cycles        (default: 1000003)
  TARGET_S        seconds of query work per profile    (default: 2.0)
  KMAX            cap on repetitions per query         (default: 25)
  VPERF           vperf command to use                (default: repo venv,
                                                           else python3 -m vperf)
EOF
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --from) shift; FROM="$1" ;;
        --to) shift; TO="$1"; TO_SET=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --retime) RETIME=1 ;;
  --inline) INLINE=1 ;;
  --resume) RESUME=1 ;;
        -h|--help) usage ;;
        *) echo "ERROR: unknown option $1" >&2; usage ;;
    esac
    shift
done

[ -f "$DATA_DIR/hits.parquet" ] || { echo "ERROR: $DATA_DIR/hits.parquet not found" >&2; exit 1; }
[ -d "$BENCH_DIR/clickhouse-parquet" ] || {
    echo "ERROR: $BENCH_DIR/clickhouse-parquet not found." >&2
    echo "       Set CLICKBENCH_DIR to a ClickBench checkout." >&2
    exit 1
}
[ -f "$BENCH_DIR/clickhouse-parquet/queries.sql" ] || {
    echo "ERROR: $BENCH_DIR/clickhouse-parquet/queries.sql not found" >&2; exit 1; }
[ -f "$BENCH_DIR/clickhouse-parquet/create.sql" ] || {
    echo "ERROR: $BENCH_DIR/clickhouse-parquet/create.sql not found" >&2; exit 1; }
command -v clickhouse-local >/dev/null 2>&1 || { echo "ERROR: clickhouse-local not in PATH" >&2; exit 1; }

# ClickBench also publishes the table split into 100 parquet parts
# (hits_0.parquet ... hits_99.parquet, ~120 MB each).  Pointing DATA_DIR at one
# of those still runs, but every query then finishes in milliseconds and the
# profiles describe the engine's startup rather than the query - so warn.
DATA_BYTES=$(stat -c%s "$DATA_DIR/hits.parquet" 2>/dev/null || echo 0)
if [ "$DATA_BYTES" -lt 1000000000 ]; then
    echo "WARNING: $DATA_DIR/hits.parquet is only $DATA_BYTES bytes." >&2
    echo "WARNING: that looks like one of the 100 ClickBench partitions, not the" >&2
    echo "WARNING: full 100M-row hits.parquet (~14.8 GB) the queries are written" >&2
    echo "WARNING: for.  Profiles will be short and startup-dominated." >&2
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

# Read the whole query file up front: leaving the queries file on stdin would
# make clickhouse-local try to parse it as an input table.
DDL="$(cat "$BENCH_DIR/clickhouse-parquet/create.sql")"
mapfile -t QUERIES < "$BENCH_DIR/clickhouse-parquet/queries.sql"
N=${#QUERIES[@]}
[ "$TO_SET" = 1 ] || TO=$((N - 1))   # --to 0 is a valid range, not "unset"

RUN_ID="$(date +%Y%m%d_%H%M%S)"
LOG="$OUT_ROOT/clickbench_${RUN_ID}.log"
TSV="$OUT_ROOT/clickbench_${RUN_ID}.tsv"
TIMES="$OUT_ROOT/clickbench_query_times.tsv"
mkdir -p "$OUT_ROOT"
DONE_DIRS=()

if [ "$DRY_RUN" = 0 ]; then
    printf '#\telapsed_s\tcpu_s\tipc\tsamples\tibs_samples\treps\texit\tdir\n' >"$TSV"
    : >"$LOG"
fi

# Progress and the final table go to the terminal and the run log.
say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG" >&2; }

repeat_sql() {  # <sql> <reps> -> the DDL followed by <reps> statements, one per line
    local sql="$1" k="$2" r
    printf '%s\n' "$DDL"
    for ((r = 0; r < k; r++)); do printf '%s\n' "$sql"; done
}

# Wall time of one clickhouse-local process running <sql> k times, including
# process startup; two points give the marginal per-execution cost.
# A single execve argument is capped at 128 KiB (MAX_ARG_STRLEN), so a query
# repeated a few thousand times does not fit in --query; a file does.
SQL_FILE=""
write_sql() {  # <sql> <reps> -> path of a file holding the DDL + reps statements
    local out
    out="$(mktemp "${TMPDIR:-/tmp}/vperf-cb-XXXXXX.sql")"
    repeat_sql "$1" "$2" >"$out"
    printf '%s' "$out"
}

time_sql() {  # <sql> <k> -> seconds
    local f t0 t1
    f="$(write_sql "$1" "$2")"
    t0=$(date +%s.%N)
    (cd "$DATA_DIR" && clickhouse-local --format=Null --queries-file "$f") \
        </dev/null >/dev/null 2>&1 || true
    t1=$(date +%s.%N)
    rm -f "$f"
    awk -v a="$t0" -v b="$t1" 'BEGIN{printf "%.4f", b-a}'
}

reps_for() {  # <marginal seconds> -> repetition count
    awk -v t="$TARGET_S" -v m="$1" -v kmax="$KMAX" -v one="$2" 'BEGIN{
        m = (m > 0.0005 ? m : 0.0005)
        k = int(t / m + 0.999)
        if (k < one) k = one
        if (k > kmax) k = kmax
        print k
    }'
}

# --- per-query calibration -------------------------------------------------
# The cache holds one row per query index, so a partial run calibrates only the
# range it is about to profile and later runs reuse the rest.
declare -a MARGINAL REPS CACHED_IDX
for i in "${!QUERIES[@]}"; do
    MARGINAL[i]=0
    REPS[i]=1
    CACHED_IDX[i]=0
done
# The cached reps are only valid for the dataset and the knobs they were
# measured with, so the cache carries a fingerprint of both.
FINGERPRINT="bytes=$DATA_BYTES target_s=$TARGET_S kmax=$KMAX probe=$PROBE_REPS"
CACHE_VALID=0
if [ "$RETIME" = 0 ] && [ -f "$TIMES" ] && [ "$DRY_RUN" = 0 ] && \
   [ "$(sed -n '2p' "$TIMES")" = "# $FINGERPRINT" ]; then
    CACHE_VALID=1
    while IFS=$'\t' read -r idx m k; do
        [[ "$idx" =~ ^[0-9]+$ ]] || continue
        MARGINAL[idx]="$m"
        REPS[idx]="$k"
        CACHED_IDX[idx]=1
    done <"$TIMES"
    say "reusing query timings from $TIMES"
elif [ -f "$TIMES" ] && [ "$DRY_RUN" = 0 ] && [ "$RETIME" = 0 ]; then
    # the cached reps belong to a different dataset or a different setting;
    # keeping any of them would size this run's repetitions wrongly
    say "cached timings in $TIMES were measured on a different dataset/setting - ignoring them"
fi

calibrate() {  # <query index>
    local i="$1" q t1 tn m k label
    q="${QUERIES[$i]}"
    label="$(printf 'Q%02d' "$i")"
    t1="$(time_sql "$q" 1)"
    if awk -v a="$t1" -v t="$TARGET_S" 'BEGIN{exit !(a >= t)}'; then
        # one execution already fills the window: no need to probe further, and
        # repeating a slow query would only make the sweep longer
        m="$t1"
        k=1
        say "  $label 1x=${t1}s -> already >= ${TARGET_S}s, reps=1"
    else
        tn="$(time_sql "$q" "$PROBE_REPS")"
        m="$(awk -v a="$t1" -v b="$tn" -v n="$PROBE_REPS" \
            'BEGIN{d=(b-a)/(n-1); if (d < 0.0005) d = 0.0005; printf "%.6f", d}')"
        k="$(reps_for "$m" 1)"
        say "  $label 1x=${t1}s ${PROBE_REPS}x=${tn}s -> ${m}s/iter, reps=$k"
    fi
    MARGINAL[i]="$m"
    REPS[i]="$k"
    printf '%s\t%s\t%s\n' "$i" "$m" "$k" >>"$TIMES.tmp"
}

if [ "$DRY_RUN" = 0 ]; then
    # rows for indices in the range that are about to be (re)calibrated
    STALE=""
    for ((i = FROM; i <= TO; i++)); do
        if [ "$RETIME" = 1 ] || [ "${CACHED_IDX[$i]}" = 0 ]; then
            STALE="$STALE $i"
        fi
    done
    if [ "$CACHE_VALID" = 1 ] && [ -f "$TIMES" ]; then
        awk -F'\t' -v stale="$STALE " '
            BEGIN{n=split(stale,a," "); for(i=1;i<=n;i++) drop[a[i]]=1}
            !($1 in drop)' "$TIMES" | grep -v '^# ' >"$TIMES.tmp"
    else
        : >"$TIMES.tmp"
    fi
    PENDING=()
    for ((i = FROM; i <= TO; i++)); do
        if [ "$RETIME" = 1 ] || [ "${CACHED_IDX[$i]}" = 0 ]; then
            PENDING+=("$i")
        fi
    done
    if [ ${#PENDING[@]} -eq 0 ]; then
        say "calibration cached for Q$(printf '%02d' "$FROM")..Q$(printf '%02d' "$TO")"
    else
        say "calibrating ${#PENDING[@]} quer$([ ${#PENDING[@]} -eq 1 ] && echo y || echo ies) (one timed run each, $PROBE_REPS only when short)"
        for i in "${PENDING[@]}"; do calibrate "$i"; done
    fi
    { printf '#\tmarginal_s\treps\n'
      printf '# %s\n' "$FINGERPRINT"
      grep -E '^[0-9]' "$TIMES.tmp" | sort -n -k1,1
    } >"$TIMES.new"
    mv "$TIMES.new" "$TIMES"
fi

# --- profile sweep ---------------------------------------------------------
INLINE_FLAG="--no-inline"
if [ "$INLINE" = 1 ]; then
    INLINE_FLAG=""
fi
say "queries=$((TO - FROM + 1))/$N freq=${FREQ}Hz mem_period=$MEM_PERIOD target=${TARGET_S}s ${INLINE_FLAG:-inlined}"
say "data=$DATA_DIR/hits.parquet ($(du -h "$DATA_DIR/hits.parquet" | cut -f1))"
say "vperf=$VPERF"
say "index=$TSV"

for ((i = FROM; i <= TO; i++)); do
    q="${QUERIES[$i]}"
    label="$(printf 'Q%02d' "$i")"
    k="${REPS[$i]:-1}"
    ts="$(date +%Y%m%d_%H%M%S)"
    dir="$OUT_ROOT/$(printf 'q%02d_%s' "$i" "$ts")"

    if [ "$RESUME" = 1 ]; then
        for done_dir in "$OUT_ROOT/$(printf 'q%02d' "$i")"_*; do
            if [ -f "$done_dir/report.html" ]; then
                say "$label skip (already profiled as ${done_dir##*/})"
                printf '%s\t-\t-\t-\t-\t-\t%s\t-\t%s\n' \
                    "$label" "$k" "${done_dir##*/}" >>"$TSV"
                continue 2
            fi
        done
    fi

    if [ "$DRY_RUN" = 1 ]; then
        echo "== $label (reps=$k) -> $dir"
        echo "   (cd $DATA_DIR && $VPERF run -f $FREQ --mem-period $MEM_PERIOD $INLINE_FLAG -o $dir -- clickhouse-local --time --format=Null --queries-file <DDL + $k statements>) </dev/null"
        continue
    fi

    mkdir -p "$dir"
    say "$label start (reps=$k) -> ${dir##*/}"
    sql="$(write_sql "$q" "$k")"
    t0=$(date +%s)
    # stdin must be /dev/null (see the sql-bench caveat) and the cwd must be
    # DATA_DIR for the DDL's relative hits.parquet path, hence the subshell.
    set +e
    (
        cd "$DATA_DIR"
        $VPERF run -f "$FREQ" --mem-period "$MEM_PERIOD" $INLINE_FLAG -o "$dir" -- \
            clickhouse-local --time --format=Null --queries-file "$sql"
    ) </dev/null >"$dir/vperf.log" 2>&1
    rc=$?
    set -e
    cp "$sql" "$dir/queries.sql"
    rm -f "$sql"
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
    # Headline numbers come from the terminal summary vperf already printed.
    read -r cpu ipc samples ibs < <(python3 -c '
import sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
def val(label):
    for line in text.splitlines():
        if line.startswith(label) and "|" in line:
            v = line.split("|", 1)[1].split()
            return v[0] if v and v[0] not in ("n/a", "-") else "-"
    return "-"
print(val("CPU Time"), val("IPC / CPI"), val("Samples Collected"),
      val("IBS Samples Collected"))' "$dir/vperf.log" 2>/dev/null) || true
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$label" "${elapsed:--}" "${cpu:--}" "${ipc:--}" "${samples:--}" \
        "${ibs:--}" "$k" "$rc" "${dir##*/}" >>"$TSV"
    DONE_DIRS+=("$dir")
done

if [ "$DRY_RUN" = 1 ]; then
    exit 0
fi

reports=0
degraded=0
for d in "${DONE_DIRS[@]}"; do
    [ -f "$d/report.html" ] && reports=$((reports + 1))
    if grep -qE 'startup grace|perf stat failed|retrying' "$d/vperf.log" 2>/dev/null; then
        degraded=$((degraded + 1))
        say "  degraded: ${d##*/} ($(grep -oE 'Target exited during collector startup grace.*' "$d/vperf.log" | head -1))"
    fi
done

say "summary (label elapsed_s cpu_s ipc samples ibs reps exit dir):"
{ column -t -s $'\t' "$TSV" || cat "$TSV"; } | tee -a "$LOG" >&2
say "report.html present: $reports/${#DONE_DIRS[@]} (degraded: $degraded)"
say "index: $TSV"
say "log: $LOG"
