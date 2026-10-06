#!/usr/bin/env bash
# restage.sh <mw|single> — provision pods with bench fixtures (schemas, lookup,
# source batches, in-pod binary-format conversion via TRAM's own serializers).
# v1.6.0 images bake the serializer extras (#79) — no pip staging needed.
set -euo pipefail
NS=trishul-ram
B=/tmp/opencode/perf-b
if [[ "$1" == "mw" ]]; then PODS="trishul-ram-worker-0 trishul-ram-worker-1 trishul-ram-worker-2"; else PODS="trishul-ram-0"; fi

for p in $PODS; do
  echo "== $p"
  kubectl exec -n $NS $p -- sh -c 'mkdir -p /data/schemas /data/perf/in /data/perf/out /data/perf/src'
  kubectl cp "$B/schemas/cdr.avsc" "$NS/$p:/data/schemas/cdr.avsc"
  kubectl cp "$B/schemas/cdr.proto" "$NS/$p:/data/schemas/cdr.proto"
  kubectl cp "$B/lookup.csv" "$NS/$p:/data/perf/lookup.csv"
  for d in csv xml pmxml flat json nested-json; do
    kubectl exec -n $NS $p -- mkdir -p "/data/perf/src/$d"
  done
  for f in "$B"/batches/csv/*.csv; do kubectl cp "$f" "$NS/$p:/data/perf/src/csv/$(basename "$f")"; done
  for f in "$B"/batches/xml/*.xml; do kubectl cp "$f" "$NS/$p:/data/perf/src/xml/$(basename "$f")"; done
  for f in "$B"/batches/pmxml/*.xml; do kubectl cp "$f" "$NS/$p:/data/perf/src/pmxml/$(basename "$f")"; done
  for f in "$B"/batches/flat/*.jsonl; do kubectl cp "$f" "$NS/$p:/data/perf/src/flat/$(basename "$f")"; done
  for f in "$B"/batches/json/*.jsonl; do kubectl cp "$f" "$NS/$p:/data/perf/src/json/$(basename "$f")"; done
  for f in "$B"/batches/nested-json/*.jsonl; do kubectl cp "$f" "$NS/$p:/data/perf/src/nested-json/$(basename "$f")"; done
  kubectl cp "$B/podgen.py" "$NS/$p:/tmp/podgen.py"
  kubectl exec -n $NS $p -- python /tmp/podgen.py
  kubectl exec -n $NS $p -- sh -c 'ls /data/perf/formats/*/ | head -3; echo formats=$(ls /data/perf/formats | wc -l)'
done
echo "restage complete ($1)"
