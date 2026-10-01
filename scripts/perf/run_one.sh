#!/usr/bin/env bash
# run_one.sh — orchestrate one TRAM performance-bench run.
#
#   register pipeline via API → (stream) start loadgen → warmup + steady state
#   → stop pipeline/loadgen → collector → cleanup (delete pipeline unless
#   --keep).
#
# Usage:
#   run_one.sh <template.yaml> <run-id> [--duration S] [--warmup S]
#              [--loadgen "CMD..."] [--keep] [--collect-duration S]
#
# Environment:
#   TRAM_API_URL     manager API base URL (default http://127.0.0.1:30001)
#   TRAM_API_KEY     X-API-Key for the manager API (optional)
#   TRAM_NAMESPACE   pod namespace for the collector (default tram)
#   PERF_PYTHON      python interpreter (default <repo>/.venv/bin/python)
#   PERF_HELM_VALUES path to helm values.yaml for the collector meta (optional)
#
# Stream pipelines (webhook/kafka sources) auto-start on registration and are
# stopped explicitly after the steady-state window. Batch pipelines (manual
# schedule) are triggered via /run and polled to completion. The collector
# runs after the pipeline stops, sampling pod metrics for --collect-duration
# seconds (default 15) and fetching the run-history summary.
set -euo pipefail

PERF_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "$PERF_ROOT")")"
PYTHON="${PERF_PYTHON:-$REPO_ROOT/.venv/bin/python}"
API_URL="${TRAM_API_URL:-http://127.0.0.1:30001}"
NAMESPACE="${TRAM_NAMESPACE:-tram}"
HELM_VALUES="${PERF_HELM_VALUES:-}"

TEMPLATE="${1:-}"
RUN_ID="${2:-}"
if [[ -z "$TEMPLATE" || -z "$RUN_ID" ]]; then
    echo "usage: run_one.sh <template.yaml> <run-id> [--duration S] [--warmup S] [--loadgen 'CMD...'] [--keep] [--collect-duration S]" >&2
    exit 2
fi
shift 2

DURATION=180
WARMUP=60
COLLECT_DURATION=15
LOADGEN_CMD=""
KEEP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --duration) DURATION="$2"; shift 2 ;;
        --warmup) WARMUP="$2"; shift 2 ;;
        --loadgen) LOADGEN_CMD="$2"; shift 2 ;;
        --collect-duration) COLLECT_DURATION="$2"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        *) echo "run_one: unknown option $1" >&2; exit 2 ;;
    esac
done

API_KEY_HEADER=()
if [[ -n "${TRAM_API_KEY:-}" ]]; then
    API_KEY_HEADER=(-H "X-API-Key: $TRAM_API_KEY")
fi

pipeline_name="$("$PYTHON" -c '
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
p = doc.get("pipeline", doc)
print(p["name"])
' "$TEMPLATE")"

schedule_type="$("$PYTHON" -c '
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
p = doc.get("pipeline", doc)
print(p.get("schedule", {}).get("type", "manual"))
' "$TEMPLATE")"

echo "run_one[$RUN_ID]: pipeline=$pipeline_name schedule=$schedule_type template=$TEMPLATE"

# ── register (delete a stale pipeline of the same name first) ─────────────
register() {
    local rc
    curl -sS -o /dev/null -w "%{http_code}" -X POST "$API_URL/api/pipelines" \
        "${API_KEY_HEADER[@]}" -H "Content-Type: text/yaml" --data-binary "@$TEMPLATE"
}
code="$(register || true)"
if [[ "$code" == "409" ]]; then
    echo "run_one[$RUN_ID]: existing pipeline $pipeline_name — deleting and re-registering"
    curl -sS -o /dev/null -X DELETE "$API_URL/api/pipelines/$pipeline_name" "${API_KEY_HEADER[@]}" || true
    code="$(register)"
fi
if [[ "$code" != "201" && "$code" != "200" ]]; then
    echo "run_one[$RUN_ID]: FATAL register returned HTTP $code" >&2
    exit 1
fi

stop_pipeline() {
    curl -sS -o /dev/null -X POST "$API_URL/api/pipelines/$pipeline_name/stop" "${API_KEY_HEADER[@]}" || true
}

LOADGEN_PID=""
cleanup() {
    if [[ -n "$LOADGEN_PID" ]] && kill -0 "$LOADGEN_PID" 2>/dev/null; then
        kill -TERM "$LOADGEN_PID" 2>/dev/null || true
        wait "$LOADGEN_PID" 2>/dev/null || true
    fi
    stop_pipeline
}
trap cleanup EXIT

run_collector() {
    local args=(--run-id "$RUN_ID" --duration "$COLLECT_DURATION" --interval 5
                --namespace "$NAMESPACE" --api-url "$API_URL" --pipeline "$pipeline_name")
    if [[ -n "${TRAM_API_KEY:-}" ]]; then
        args+=(--api-key "$TRAM_API_KEY")
    fi
    if [[ -n "$HELM_VALUES" ]]; then
        args+=(--helm-values "$HELM_VALUES")
    fi
    "$PYTHON" "$PERF_ROOT/collector/collect.py" "${args[@]}"
}

if [[ "$schedule_type" == "stream" ]]; then
    # Stream pipeline: registration auto-starts it.
    echo "run_one[$RUN_ID]: pipeline auto-started on registration (stream)"
    if [[ -n "$LOADGEN_CMD" ]]; then
        echo "run_one[$RUN_ID]: starting loadgen: $LOADGEN_CMD"
        # shellcheck disable=SC2086
        eval "$LOADGEN_CMD" &
        LOADGEN_PID=$!
    fi
    echo "run_one[$RUN_ID]: warmup ${WARMUP}s"
    sleep "$WARMUP"
    echo "run_one[$RUN_ID]: steady state ${DURATION}s"
    sleep "$DURATION"
    if [[ -n "$LOADGEN_PID" ]] && kill -0 "$LOADGEN_PID" 2>/dev/null; then
        kill -TERM "$LOADGEN_PID" 2>/dev/null || true
        wait "$LOADGEN_PID" 2>/dev/null || true
        LOADGEN_PID=""
    fi
    stop_pipeline
    echo "run_one[$RUN_ID]: pipeline stopped"
else
    # Batch pipeline: trigger one run and poll to completion.
    run_resp="$(curl -sS -X POST "$API_URL/api/pipelines/$pipeline_name/run" "${API_KEY_HEADER[@]}" || true)"
    echo "run_one[$RUN_ID]: triggered batch run: $run_resp"
    deadline=$((SECONDS + DURATION + WARMUP + 120))
    while (( SECONDS < deadline )); do
        status="$("$PYTHON" -c '
import json, sys, urllib.request
url, key = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url)
if key:
    req.add_header("X-API-Key", key)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(json.load(resp).get("status", "unknown"))
except Exception as exc:
    print(f"error:{exc}")
' "$API_URL/api/pipelines/$pipeline_name" "${TRAM_API_KEY:-}")"
        echo "run_one[$RUN_ID]: pipeline status=$status"
        case "$status" in
            stopped|disabled) break ;;
            error:*) echo "run_one[$RUN_ID]: status probe failed — continuing" >&2 ;;
        esac
        sleep 5
    done
fi

# ── collect ────────────────────────────────────────────────────────────────
run_collector

# ── cleanup ────────────────────────────────────────────────────────────────
if [[ "$KEEP" == "1" ]]; then
    echo "run_one[$RUN_ID]: keeping pipeline $pipeline_name (stopped)"
else
    curl -sS -o /dev/null -X DELETE "$API_URL/api/pipelines/$pipeline_name" "${API_KEY_HEADER[@]}" || true
    echo "run_one[$RUN_ID]: deleted pipeline $pipeline_name"
fi
trap - EXIT
echo "run_one[$RUN_ID]: DONE — results under $PERF_ROOT/results/$RUN_ID/"