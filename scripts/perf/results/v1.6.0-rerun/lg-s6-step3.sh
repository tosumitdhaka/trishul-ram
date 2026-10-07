sleep 5
/home/dhaka/trishul/trishul-ram/.venv/bin/python /tmp/opencode/v160-rerun/perf/generators/kafka_loadgen.py --brokers 172.19.0.2:30094 --topic perf-cdr --rate 1000 --duration 45 --payload-file /tmp/opencode/perf-b/batches/flat/batch_000001.jsonl &
/home/dhaka/trishul/trishul-ram/.venv/bin/python /tmp/opencode/v160-rerun/perf/generators/kafka_loadgen.py --brokers 172.19.0.2:30094 --topic perf-cdr --rate 1000 --duration 45 --payload-file /tmp/opencode/perf-b/batches/flat/batch_000001.jsonl &
wait
