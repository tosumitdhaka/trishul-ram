#!/usr/bin/env bash
# stage_fixtures.sh — push batch input files into the pods' /data/perf/in.
# Usage: stage_fixtures.sh <mw|single>
set -euo pipefail
TOPO="$1"
FIX=/tmp/opencode/v160-86/fixtures

if [[ "$TOPO" == "mw" ]]; then
  PODS="trishul-ram-worker-0 trishul-ram-worker-1 trishul-ram-worker-2"
else
  PODS="trishul-ram-0"
fi

for p in $PODS; do
  kubectl exec -n trishul-ram "$p" -c tram -- mkdir -p /data/perf/in /data/perf/out || true
  kubectl cp "$FIX/batches_csv/." "trishul-ram/$p:/data/perf/in/" -c tram
  kubectl cp "$FIX/batches_pmxml/." "trishul-ram/$p:/data/perf/in/" -c tram
  echo "staged -> $p"
done
