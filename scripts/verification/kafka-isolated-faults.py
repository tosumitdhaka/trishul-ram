"""Fault only tram-v183-kafka-test; never the deployed kind installation.

Run with TRAM_TEST_ISOLATED_BROKER_FAULTS=1 using the project Python.
Writes three restart/replay results to /tmp/tram-v183-kafka-faults.json.
"""
import json
import os
import subprocess
import time
import uuid

from kafka import KafkaConsumer, KafkaProducer, TopicPartition
from kafka.admin import KafkaAdminClient, NewTopic

from tram.connectors.kafka.sink import KafkaSink
from tram.connectors.kafka.source import KafkaSource
from tram.core.exceptions import SinkError
from tram.interfaces.base_source import AckDisposition

if os.environ.get("TRAM_TEST_ISOLATED_BROKER_FAULTS") != "1":
    raise SystemExit("Set TRAM_TEST_ISOLATED_BROKER_FAULTS=1 for the disposable Kafka fixture")


def admin() -> KafkaAdminClient:
    return KafkaAdminClient(
        bootstrap_servers=["localhost:19092"], api_version=(2, 5), request_timeout_ms=3000
    )


def wait_ready() -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            client = admin()
            client.list_topics()
            client.close()
            return
        except Exception:
            time.sleep(0.5)
    raise AssertionError("isolated Kafka fixture did not recover")


results = []
for repetition in range(3):
    topic = "tram-disconnect-" + uuid.uuid4().hex[:12]
    client = admin()
    client.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    client.close()
    group = "tram-disconnect-" + uuid.uuid4().hex[:12]
    producer = KafkaProducer(
        bootstrap_servers=["localhost:19092"], api_version=(2, 5), acks="all",
        request_timeout_ms=1500, max_block_ms=1500, delivery_timeout_ms=2000, retries=0,
    )
    for identity in (0, 1):
        producer.send(topic, value=json.dumps({"id": identity}).encode()).get(timeout=10)
    source_config = {
        "brokers": ["localhost:19092"], "topic": topic, "group_id": group,
        "auto_offset_reset": "earliest", "_ack_managed": True,
    }
    source = KafkaSource(source_config)
    reader = source.read()
    initial = [next(reader), next(reader)]
    sink = KafkaSink({"brokers": ["localhost:19092"], "topic": topic})
    sink._producer = producer
    stopped = False
    try:
        subprocess.run(["docker", "stop", "-t", "0", "tram-v183-kafka-test"], check=True,
                       capture_output=True)
        stopped = True
        source.ack(initial[1][1], AckDisposition.DELIVERED)
        assert source._pending_commits == {}, "undecided offset 0 was bypassed"
        try:
            sink.write(b"outage-publication", {})
        except SinkError as error:
            publication_error = str(error)
        else:
            raise AssertionError("publication during broker outage returned success")
    finally:
        if stopped:
            subprocess.run(["docker", "start", "tram-v183-kafka-test"], check=True,
                           capture_output=True)
        wait_ready()
        reader.close()
        producer.close(timeout=5)

    restarted = KafkaSource(source_config)
    reader = restarted.read()
    try:
        replay = [next(reader), next(reader)]
        assert [body for body, _ in replay] == [body for body, _ in initial]
        for _, meta in replay:
            restarted.ack(meta, AckDisposition.DELIVERED)
    finally:
        reader.close()
    inspector = KafkaConsumer(
        bootstrap_servers=["localhost:19092"], group_id=group, enable_auto_commit=False
    )
    try:
        committed = inspector.committed(TopicPartition(topic, 0))
        assert committed == 2
    finally:
        inspector.close()
    result = {
        "repetition": repetition + 1, "topic": topic,
        "fixture": "tram-v183-kafka-test", "broker": "apache/kafka:3.9.1",
        "offered_ids": [0, 1], "initial_decided_ids": [1], "initial_pending": {},
        "outage_publication": "SinkError", "error": publication_error,
        "replayed_ids": [json.loads(body)["id"] for body, _ in replay],
        "final_committed_resume_offset": committed,
        "duplicate_window": "decided id 1 is replayed when id 0 remains undecided",
    }
    results.append(result)
    print(json.dumps(result), flush=True)
    client = admin()
    client.delete_topics([topic])
    client.close()

with open("/tmp/tram-v183-kafka-faults.json", "w") as output:
    json.dump(results, output, indent=2)
