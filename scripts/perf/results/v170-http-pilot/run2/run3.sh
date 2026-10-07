#!/usr/bin/env bash
# v1.7.0 Pilot A, accel side (baseline already captured in run2/base with the
# a4ac731 pin). Adoption-aware: measures time-to-adoption under flag=1
# (fallback: fresh re-registration), then runs the loadgen ladder.
set -uo pipefail
cd /home/dhaka/trishul/trishul-ram
API=http://127.0.0.1:30001
LG=".venv/bin/python scripts/perf/generators/loadgen_webhook.py"
OUT=/tmp/opencode/http-pilot/run2
URL=http://127.0.0.1:30002/webhooks/ingest
PAY=/tmp/opencode/perf-a2/corpus_100k.jsonl

canary_code() { curl -s -o /dev/null -w "%{http_code}" --max-time 8 -X POST "$URL" \
    -H 'Content-Type: application/json' -d '[{"c":1}]' || true; }

# 0) wait for the background flag=0 adoption poll to conclude first
while kill -0 1986061 2>/dev/null; do sleep 15; done
echo "== flag0 adoption result: $(tail -1 /tmp/opencode/http-pilot/adoption-flag0.log)"

# 1) flip to accelerated runtime
helm upgrade trishul-ram ./helm -n trishul-ram --reuse-values --set env.TRAM_HTTP_ACCELERATED=1 >/dev/null 2>&1
kubectl rollout status statefulset/trishul-ram-worker -n trishul-ram --timeout=420s >/dev/null 2>&1
echo "== flag=1 rollout done $(date +%H:%M:%S)"
kubectl logs -n trishul-ram trishul-ram-worker-0 2>/dev/null | grep "HTTP runtime" | tail -1

# 2) adoption measurement: poll up to 600s
t0=$(date +%s); adopted=""
for i in $(seq 1 30); do
    code=$(canary_code)
    [ "$code" = "202" ] && { adopted=$(( $(date +%s) - t0 )); break; }
    sleep 20
done
if [ -n "$adopted" ]; then
    echo "ADOPTED flag=1 after ${adopted}s of polling"
else
    echo "NO ADOPTION flag=1 after 600s — re-registering (runbook fallback)"
    curl -s -o /dev/null -X DELETE "$API/api/pipelines/s1-webhook-local" || true
    sleep 2
    curl -s -o /dev/null -w "re-register: %{http_code}\n" -X POST "$API/api/pipelines" \
        -H "Content-Type: text/yaml" --data-binary @scripts/perf/templates/s1_webhook_local.yaml
    for i in $(seq 1 12); do code=$(canary_code); [ "$code" = "202" ] && break; sleep 5; done
    echo "post-register canary: $code"
fi

# 3) accel loadgen ladder (same shape as the base side)
for rate in 500 1000 1500 2000 2500; do
    echo "=== [accel] rate=$rate conc=50 dur=40"
    $LG --url "$URL" --rate "$rate" --concurrency 50 --duration 40 \
        --payload-file "$PAY" --summary "$OUT/accel-r$rate-c50.json" 2>&1 | tail -2
    sleep 5
done
echo "=== [accel] rate=2000 conc=200 dur=40"
$LG --url "$URL" --rate 2000 --concurrency 200 --duration 40 \
    --payload-file "$PAY" --summary "$OUT/accel-r2000-c200.json" 2>&1 | tail -2
sleep 5

# 4) restore default runtime
helm upgrade trishul-ram ./helm -n trishul-ram --reuse-values --set env.TRAM_HTTP_ACCELERATED=0 >/dev/null 2>&1
kubectl rollout status statefulset/trishul-ram-worker -n trishul-ram --timeout=420s >/dev/null 2>&1
echo "== restored flag=0 $(date +%H:%M:%S)"
curl -s -o /dev/null -X POST "$API/api/pipelines/s1-webhook-local/stop" || true
echo "DONE"
