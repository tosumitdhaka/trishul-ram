#!/usr/bin/env bash
# v1.7.0 Pilot A: HTTP runtime A/B (flag off vs on) at identical CPU (500m workers).
# s1_webhook_local stream (webhook -> local), payload corpus_100k, 5 offered-rate steps
# + one high-concurrency shape per side. Loadgen summaries land in $OUT.
set -uo pipefail
cd /home/dhaka/trishul/trishul-ram
API=http://127.0.0.1:30001
LG=".venv/bin/python scripts/perf/generators/loadgen_webhook.py"
TPL=scripts/perf/templates/s1_webhook_local.yaml
OUT=/tmp/opencode/http-pilot
URL=http://127.0.0.1:30002/webhooks/ingest
PAY=/tmp/opencode/perf-a2/corpus_100k.jsonl
mkdir -p "$OUT"

curl -s -o /dev/null -X POST "$API/api/pipelines/s1-webhook-local/stop" || true
curl -s -o /dev/null -X DELETE "$API/api/pipelines/s1-webhook-local" || true
sleep 3
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/api/pipelines" -H "Content-Type: text/yaml" --data-binary @"$TPL")
echo "register s1: HTTP $code"
sleep 10

run_side() {
    local side=$1
    for rate in 500 1000 1500 2000 2500; do
        echo "=== [$side] rate=$rate conc=50 dur=40"
        $LG --url "$URL" --rate "$rate" --concurrency 50 --duration 40 \
            --payload-file "$PAY" --summary "$OUT/$side-r$rate-c50.json" 2>&1 | tail -2
        sleep 5
    done
    echo "=== [$side] rate=2000 conc=200 dur=40"
    $LG --url "$URL" --rate 2000 --concurrency 200 --duration 40 \
        --payload-file "$PAY" --summary "$OUT/$side-r2000-c200.json" 2>&1 | tail -2
    sleep 5
}

run_side base

helm upgrade trishul-ram ./helm -n trishul-ram --reuse-values --set env.TRAM_HTTP_ACCELERATED=1 >/dev/null 2>&1
kubectl rollout status statefulset/trishul-ram-worker -n trishul-ram --timeout=420s
sleep 20
echo "== runtime evidence (worker-0):"
kubectl logs -n trishul-ram trishul-ram-worker-0 2>/dev/null | grep "HTTP runtime" | tail -3
# pipeline re-adoption: wait for the webhook route to answer again
for i in $(seq 1 30); do
    sc=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$URL" -H 'Content-Type: application/json' -d '[]' --max-time 5 || true)
    [[ "$sc" == "202" || "$sc" == "503" ]] && break
    sleep 5
done
echo "ingress back: $sc"

run_side accel

curl -s -o /dev/null -X POST "$API/api/pipelines/s1-webhook-local/stop" || true
echo "DONE"
