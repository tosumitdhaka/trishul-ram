sleep 5
/home/dhaka/trishul/trishul-ram/.venv/bin/python /tmp/opencode/v160-rerun/perf/generators/loadgen_webhook.py --url http://127.0.0.1:30001/webhooks/ingest --rate 200 --concurrency 50 --duration 180 --payload-file /tmp/opencode/perf-a2/corpus_100k.jsonl --summary /tmp/lg-sat-s1-single-M-step2-200rps-1.json &
wait
