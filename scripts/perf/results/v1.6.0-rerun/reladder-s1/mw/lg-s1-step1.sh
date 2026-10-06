sleep 5
/home/dhaka/trishul/trishul-ram/.venv/bin/python /home/dhaka/trishul/trishul-ram/scripts/perf/generators/loadgen_webhook.py --url http://127.0.0.1:30002/webhooks/ingest --rate 100 --concurrency 400 --duration 180 --payload-file /tmp/opencode/reladder-mw-clean/corpus_100k.jsonl --summary /tmp/opencode/reladder-mw-clean/lg-sat-s1-mw-M-step1-100rps-1.json &
wait
