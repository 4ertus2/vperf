#!/usr/bin/env bash
# Profile llama.cpp inference with vperf and generate a VTune-style report.
#
# Runs llama-cli with a configurable prompt and profiles
# the model loading + token generation. The report shows where CPU time goes
# during inference: matrix multiplications, memory access patterns, thread
# utilization.
#
# Usage:
#   ./llama_cpp_profile.sh
#
# Output: .vperf/llama_<timestamp>/report.html

set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
if command -v readlink >/dev/null 2>&1; then
    SCRIPT_PATH="$(readlink -f "$SCRIPT_PATH")"
fi
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

LLAMA_CLI="${LLAMA_CLI:-$HOME/src/ik_llama.cpp/_build/bin/llama-cli}"
MODEL="${MODEL:-$HOME/llm/Qwen3.6-35B-A3B-UD-IQ4_NL.gguf}"
THREADS="${THREADS:-8}"
OUT_ROOT="${OUT_ROOT:-$REPO_ROOT/.vperf}"
VPERF="${VPERF:-}"
TOKENS="${TOKENS:-100}"
PROMPT="${PROMPT:-Who killed Laura Palmer?}"

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            cat <<EOF
Usage: $(basename "$0")

Profile llama.cpp inference with vperf.

Options:
  -t, --threads N  number of threads for llama-cli    (default: 8)
  -n, --tokens N   tokens to generate                 (default: 100)
  -p, --prompt STR prompt for llama-cli               (default: "Who killed Laura Palmer?")

Environment:
  LLAMA_CLI       llama.cpp cli                    (default: ~/src/ik_llama.cpp/_build/bin/llama-cli)
  MODEL           path to .gguf model file         (default: ~/llm/Qwen3.6-35B-A3B-UD-IQ4_NL.gguf)
  THREADS         llama-cli thread count           (default: 8)
  TOKENS          tokens to generate               (default: 100)
  PROMPT          prompt for llama-cli             (default: "Who killed Laura Palmer?")
  OUT_ROOT        profile output root              (default: <repo>/.vperf)
  VPERF           vperf command to use             (default: repo venv, else python3 -m vperf)
EOF
            exit 0
            ;;
        -t|--threads) shift; THREADS="$1" ;;
        -n|--tokens) shift; TOKENS="$1" ;;
        -p|--prompt) shift; PROMPT="$1" ;;
        *) echo "ERROR: unknown option $1" >&2; exit 1 ;;
    esac
    shift
done

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

# Validate prerequisites.
[ -x "$LLAMA_CLI" ] || {
    echo "ERROR: llama-cli not found at $LLAMA_CLI" >&2
    exit 1
}
[ -f "$MODEL" ] || {
    echo "ERROR: model not found at $MODEL" >&2
    exit 1
}

MODEL_NAME="$(basename "$MODEL" .gguf)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DIR="$OUT_ROOT/llama_${MODEL_NAME}_${TIMESTAMP}"

mkdir -p "$DIR"

echo "llama.cpp profile: $DIR"
echo "  model:     $MODEL"
echo "  threads:   $THREADS"
echo "  tokens:    $TOKENS"
echo "  prompt:    $PROMPT"
echo "  llama-cli: $LLAMA_CLI"
echo "  vperf:     $VPERF"
echo

set +e
(
    cd "$OUT_ROOT"
    export DIR
    $VPERF run -o "$DIR" -- \
        bash -c 'exec >"$DIR/llama.txt" 2>&1; exec "$@"' _ \
            "$LLAMA_CLI" \
            -m "$MODEL" \
            -p "$PROMPT" \
            -n "$TOKENS" \
            -t "$THREADS" \
            --no-mmap \
            --no-display-prompt
) >"$DIR/vperf.log" 2>&1
rc=$?
set -e

echo
echo "vperf exit=$rc"
echo "report: $DIR/report.html"
echo "log:    $DIR/vperf.log"
echo "llama:  $DIR/llama.txt"

# Print the vperf summary if available.
if [ -f "$DIR/vperf.log" ]; then
    echo
    echo "--- vperf summary ---"
    grep -A 30 'vperf summary' "$DIR/vperf.log" | head -35 || true
fi

exit "$rc"
