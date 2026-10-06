#!/usr/bin/env bash
# check_hygiene.sh — bench-hygiene pre-check for the perf harness.
#
# The #86 lesson: leftover ENABLED pipelines (interval/cron-scheduled or
# manually startable) fire mid-measurement and steal worker CPU, silently
# corrupting bench cells. Before ANY matrix cell or ladder step runs, assert
# that no enabled pipeline exists on the target manager.
#
# Aborts (exit 1) listing every enabled pipeline unless TRAM_PERF_ALLOW_DIRTY=1
# is set. Exit 2 means the check itself failed (API unreachable / response
# unparseable) — also a hard abort, because a bench cannot trust an unknown
# scheduler state.
#
# run_one.sh calls this once per invocation, which covers every matrix cell
# and every ladder step; the ladder driver also calls it once at startup so a
# dirty scheduler fails before any step script is generated.
#
# Env (harness conventions — see README §2):
#   TRAM_API_URL          manager API base URL (default http://127.0.0.1:30001)
#   TRAM_API_KEY          X-API-Key for the manager API (optional)
#   TRAM_PERF_ALLOW_DIRTY =1 bypasses the assert (crash-resume / debugging)
#   PERF_PYTHON           python interpreter (default <repo>/.venv/bin/python)
#
# Usage: check_hygiene.sh
set -euo pipefail

PERF_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$(dirname "$PERF_ROOT")")"
PYTHON="${PERF_PYTHON:-$REPO_ROOT/.venv/bin/python}"
API_URL="${TRAM_API_URL:-http://127.0.0.1:30001}"

if [[ "${TRAM_PERF_ALLOW_DIRTY:-0}" == "1" ]]; then
    echo "check_hygiene: TRAM_PERF_ALLOW_DIRTY=1 — enabled-pipeline assert skipped"
    exit 0
fi

API_KEY_HEADER=()
if [[ -n "${TRAM_API_KEY:-}" ]]; then
    API_KEY_HEADER=(-H "X-API-Key: $TRAM_API_KEY")
fi

api_body="$(curl -sS --max-time 20 "${API_URL}/api/pipelines?limit=200" "${API_KEY_HEADER[@]}" 2>&1)" || rc=$?
if [[ "${rc:-0}" -ne 0 ]]; then
    echo "check_hygiene: FATAL — API unreachable at $API_URL (curl rc=${rc:-0})" >&2
    exit 2
fi

enabled_list="$(printf '%s' "$api_body" | "$PYTHON" -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except Exception as exc:
    print(f"check_hygiene: cannot parse /api/pipelines: {exc}", file=sys.stderr)
    sys.exit(2)
dirty = sorted(r.get("name", "?") for r in rows if isinstance(r, dict) and r.get("enabled"))
print("\n".join(dirty))
')"

if [[ -n "$enabled_list" ]]; then
    echo "check_hygiene: FATAL — enabled pipeline(s) would contaminate the bench:" >&2
    printf '%s\n' "$enabled_list" | sed 's/^/  - /' >&2
    echo "Disable them (e.g. scripts/perf/results/ab-86-2026-10-01/disable_leftovers.py)" >&2
    echo "or set TRAM_PERF_ALLOW_DIRTY=1 to run anyway." >&2
    exit 1
fi
echo "check_hygiene: clean — no enabled pipelines on $API_URL"
exit 0