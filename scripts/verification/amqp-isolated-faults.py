"""Fault only tram-v183-amqp-test; never the deployed kind installation.

Run with TRAM_TEST_ISOLATED_BROKER_FAULTS=1 and TRAM_TEST_AMQP_URL set.
Writes three restart/redelivery results to /tmp/tram-v183-amqp-faults.json.
"""
import json
import os
import subprocess
import time
import uuid

import pika

from tram.connectors.amqp.sink import AmqpSink
from tram.connectors.amqp.source import AmqpSource
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import AckDisposition

if os.environ.get("TRAM_TEST_ISOLATED_BROKER_FAULTS") != "1":
    raise SystemExit("Set TRAM_TEST_ISOLATED_BROKER_FAULTS=1 for the disposable AMQP fixture")
url = os.environ["TRAM_TEST_AMQP_URL"]
if pika.URLParameters(url).host != "localhost" or pika.URLParameters(url).port != 15683:
    raise SystemExit("TRAM_TEST_AMQP_URL must name localhost:15683")
fixture = "tram-v183-amqp-test"
version = subprocess.check_output(
    ["docker", "inspect", "--format", "{{.Config.Image}}", fixture], text=True
).strip()


def connect() -> pika.BlockingConnection:
    return pika.BlockingConnection(pika.URLParameters(url))


def ready() -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            connection = connect()
            connection.close()
            return
        except Exception:
            time.sleep(0.25)
    raise AssertionError("isolated RabbitMQ fixture did not recover")


results = []
for repetition in range(3):
    queue = "tram-broker-loss-" + uuid.uuid4().hex[:12]
    connection = connect()
    server_version = connection._impl.server_properties["version"]
    connection.channel().queue_declare(queue=queue, durable=True)
    connection.close()
    payload = json.dumps({"id": repetition, "fixture": fixture}).encode()
    sink = AmqpSink({"url": url, "routing_key": queue})
    sink.write(payload, {})
    assert sink.commit().confirmed
    source = AmqpSource({"url": url, "queue": queue})
    reader = source.read()
    initial = next(reader)
    assert initial[0] == payload
    stopped = False
    try:
        subprocess.run(["docker", "stop", "-t", "0", fixture], capture_output=True, check=True)
        stopped = True
        try:
            next(reader)
        except SourceError as error:
            source_error = str(error)
        else:
            raise AssertionError("broker loss was not propagated")
    finally:
        reader.close()
        if stopped:
            subprocess.run(["docker", "start", fixture], capture_output=True, check=True)
        ready()

    restarted = AmqpSource({"url": url, "queue": queue})
    reader = restarted.read()
    try:
        replay, meta = next(reader)
        assert replay == payload
        assert meta["amqp_redelivered"]
        restarted.ack(meta, AckDisposition.DELIVERED)
    finally:
        reader.close()
    connection = connect()
    try:
        count = connection.channel().queue_declare(queue=queue, passive=True).method.message_count
        assert count == 0
        connection.channel().queue_delete(queue=queue)
    finally:
        connection.close()
    result = {
        "repetition": repetition + 1, "fixture": fixture, "broker": version, "server_version": server_version,
        "offered_ids": [repetition], "initial_admitted_ids": [repetition],
        "initial_acknowledged_ids": [], "broker_loss": "SourceError", "error": source_error,
        "persistent_body_survived_restart": replay.decode(),
        "redelivered": meta["amqp_redelivered"], "queue_count_after_decided_ack": count,
    }
    results.append(result)
    print(json.dumps(result), flush=True)

with open("/tmp/tram-v183-amqp-faults.json", "w") as output:
    json.dump(results, output, indent=2)
