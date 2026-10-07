sleep 5
/home/dhaka/trishul/trishul-ram/.venv/bin/python /tmp/opencode/v160-rerun/perf/generators/loadgen_webhook.py --url http://127.0.0.1:30002/webhooks/ingest --rate 100 --concurrency 50 --duration 180 --payload-file /tmp/opencode/perf-a2/corpus_100k.jsonl --summary /tmp/lg-sat-s1-M-step1-100rps-1.json &
wait
