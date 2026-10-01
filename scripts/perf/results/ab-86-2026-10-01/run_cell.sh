#!/usr/bin/env bash
# run_cell.sh — one A/B cell of the TRAM #86 controlled re-run.
# Usage: run_cell.sh <mw|single> <M|H> <s2csv|s2pmxml|s3|s3p1000> <rep>
#
# Wraps the (copied) harness run_one.sh, adds:
#   * cgroup cpu.stat delta across the executing pod(s) (real CPU during run)
#   * kubectl top sampling every 2s DURING the run (original sampled after)
#   * raw run-history JSON for the pipeline (run-complete payloads)
#   * per-pod logs (since run start) for bookkeeping cadence evidence
#   * mock REST /collect stats + request log slice for s3
set -uo pipefail

TOPO="$1"; PROF="$2"; SCEN="$3"; REP="$4"
ROOT=/tmp/opencode/v160-86
HARNESS=$ROOT/perf
REPO=/home/dhaka/trishul/trishul-ram
VENV=$REPO/.venv/bin/python
RUN_ID="ab-${TOPO}-${SCEN}-${PROF}-rep${REP}"
RUN_DIR=$ROOT/results/$RUN_ID
mkdir -p "$RUN_DIR"

export TRAM_API_URL=http://127.0.0.1:30001
export TRAM_NAMESPACE=trishul-ram
export PERF_PYTHON=$VENV

case "$SCEN" in
  s2csv)   TEMPLATE=$HARNESS/templates/s2_local_local_csv.yaml;    PIPE=s2-local-local-csv ;;
  s2pmxml) TEMPLATE=$HARNESS/templates/s2_local_local_pmxml.yaml; PIPE=s2-local-local-pmxml ;;
  s3)      TEMPLATE=$HARNESS/templates/s3_rest_rest.yaml;          PIPE=s3-rest-rest ;;
  s3p1000) TEMPLATE=$ROOT/perf-templates/s3_rest_rest_p1000.yaml; PIPE=s3-rest-rest-p1000 ;;
  *) echo "unknown scenario $SCEN"; exit 2 ;;
esac

T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
T0EPOCH=$(date +%s.%N)
echo "$T0" > "$RUN_DIR/t0.txt"

# pods whose CPU + logs we capture
if [[ "$TOPO" == "mw" ]]; then
  PODS="trishul-ram-manager-0 trishul-ram-worker-0 trishul-ram-worker-1 trishul-ram-worker-2"
else
  PODS="trishul-ram-0"
fi

# reset mock sink counters (s3/s3p1000)
if [[ "$SCEN" == s3 || "$SCEN" == s3p1000 ]]; then
  curl -s "http://127.0.0.1:18080/collect/stats?reset=1" > "$RUN_DIR/mock-pre.json"
  : > /tmp/opencode/v160-86/logs/mock-req-current.log
fi

# cgroup cpu.stat before
: > "$RUN_DIR/cpu_stat_before.txt"
for p in $PODS; do
  echo "== $p ==" >> "$RUN_DIR/cpu_stat_before.txt"
  kubectl exec -n trishul-ram "$p" -c tram -- cat /sys/fs/cgroup/cpu.stat 2>/dev/null \
    >> "$RUN_DIR/cpu_stat_before.txt" || echo "(unavailable)" >> "$RUN_DIR/cpu_stat_before.txt"
done

# kubectl top sampler running DURING the run (2s)
( while :; do
    ts=$(date -u +%H:%M:%S.%3N)
    kubectl top pods -n trishul-ram --no-headers 2>/dev/null | sed "s/^/$ts /"
    sleep 2
  done ) > "$RUN_DIR/top-during.csv" 2>/dev/null &
SAMPLER_PID=$!

# run the harness
cd "$HARNESS"
TS_RUN_START=$(date +%s)
./run_one.sh "$TEMPLATE" "$RUN_ID" --warmup 10 --duration 120 > "$RUN_DIR/run_one.log" 2>&1
RC=$?
TS_RUN_END=$(date +%s)
kill "$SAMPLER_PID" 2>/dev/null

# cgroup cpu.stat after
: > "$RUN_DIR/cpu_stat_after.txt"
for p in $PODS; do
  echo "== $p ==" >> "$RUN_DIR/cpu_stat_after.txt"
  kubectl exec -n trishul-ram "$p" -- cat /sys/fs/cgroup/cpu.stat 2>/dev/null \
    >> "$RUN_DIR/cpu_stat_after.txt" || \
  kubectl exec -n trishul-ram "$p" -c tram -- cat /sys/fs/cgroup/cpu.stat 2>/dev/null \
    >> "$RUN_DIR/cpu_stat_after.txt" || echo "(unavailable)" >> "$RUN_DIR/cpu_stat_after.txt"
done

# raw run-history rows (run-complete payloads persisted by the manager)
curl -s "$TRAM_API_URL/api/runs?pipeline=$PIPE&limit=100" > "$RUN_DIR/run-rows.json"

# mock sink stats + request log slice
if [[ "$SCEN" == s3 || "$SCEN" == s3p1000 ]]; then
  curl -s "http://127.0.0.1:18080/collect/stats" > "$RUN_DIR/mock-post.json"
  grep 'REQ /records' /tmp/opencode/v160-86/logs/mock-rest.log \
    | awk -v t0="$T0EPOCH" -F't=' '$2+0 >= t0-1' > "$RUN_DIR/mock-requests.log" || true
fi

# per-pod logs since run start (bookkeeping cadence evidence)
mkdir -p "$RUN_DIR/podlogs"
for p in $PODS; do
  kubectl logs -n trishul-ram "$p" --since-time="$T0" --timestamps > "$RUN_DIR/podlogs/$p.log" 2>/dev/null || \
    echo "(no logs)" > "$RUN_DIR/podlogs/$p.log"
done

# harness collector artifacts (written under the /tmp harness copy)
if [[ -d "$HARNESS/results/$RUN_ID" ]]; then
  cp -r "$HARNESS/results/$RUN_ID/." "$RUN_DIR/" 2>/dev/null
  rm -rf "$HARNESS/results/$RUN_ID"
fi

echo "run_cell[$RUN_ID]: rc=$RC wall=$((TS_RUN_END - TS_RUN_START))s t0=$T0"
exit $RC
