sleep 5
/home/dhaka/trishul/trishul-ram/.venv/bin/python /home/dhaka/trishul/trishul-ram/scripts/perf/generators/loadgen_webhook.py --url http://127.0.0.1:30001/webhooks/ingest --rate 100 --concurrency 400 --duration 180 --payload-file /tmp/opencode/reladder-single-clean/corpus_100k.jsonl --summary /tmp/opencode/reladder-single-clean/lg-sat-s1-single-M-step1-100rps-1.json &
wait
