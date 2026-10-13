"""Actual source/executor progress with a broker-enforced one-delivery window."""

import json
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.local.sink import LocalSink
from tram.core.context import RunStatus
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


@pytest.mark.parametrize("thread_workers", [1, 2])
@pytest.mark.parametrize("prefetch", [1, 3])
@pytest.mark.parametrize("filtered", [False, True])
def test_amqp_prefetch_one_stream_advances_after_confirmed_output(
    tmp_path, thread_workers, prefetch, filtered,
):
    config = load_pipeline_from_yaml(json.dumps({"pipeline": {
        "name": "amqp-prefetch-one", "thread_workers": thread_workers,
        "schedule": {"type": "stream"},
        "source": {"type": "amqp", "queue": "events", "prefetch_count": prefetch},
        "transforms": [{"type": "filter", "condition": "False"}] if filtered else [],
        "serializer_in": {"type": "json"}, "serializer_out": {"type": "ndjson"},
        "stream_flush_records": 500, "stream_flush_interval_s": 0.05,
        "sink": {
            "type": "local", "path": str(tmp_path),
            "filename_template": "output.jsonl", "file_mode": "append",
        },
    }}))
    pika = MagicMock()
    connection = pika.BlockingConnection.return_value
    channel = connection.channel.return_value
    callbacks = queue.Queue()
    consumer_stop = threading.Event()
    completed = threading.Event()
    stop = threading.Event()
    owner_thread = []
    acknowledged = []
    initial_deliveries = threading.Event()
    write_calls = []

    def deliver(tag):
        callback = channel.basic_consume.call_args.kwargs["on_message_callback"]
        callback(channel, MagicMock(delivery_tag=tag, routing_key="events", redelivered=False),
                 MagicMock(message_id=str(tag)), json.dumps([{"id": tag}]).encode())

    def consume():
        owner_thread.append(threading.get_ident())
        for tag in range(1, prefetch + 1):
            deliver(tag)
        initial_deliveries.set()
        while not consumer_stop.is_set():
            try:
                callbacks.get(timeout=0.05)()
            except queue.Empty:
                pass

    def acknowledge(delivery_tag):
        assert threading.get_ident() == owner_thread[0]
        if not filtered:
            records = [json.loads(line) for line in
                       (tmp_path / "output.jsonl").read_text().splitlines()]
            assert {"id": delivery_tag} in records
        acknowledged.append(delivery_tag)
        if delivery_tag + prefetch <= 3:
            deliver(delivery_tag + prefetch)
        if len(acknowledged) == 3:
            completed.set()
            stop.set()

    connection.add_callback_threadsafe.side_effect = callbacks.put
    channel.start_consuming.side_effect = consume
    channel.stop_consuming.side_effect = consumer_stop.set
    channel.basic_ack.side_effect = acknowledge
    executor = PipelineExecutor()
    source = executor._build_source(config)
    original_get = source._msg_queue.get
    original_write = LocalSink.write

    def get_after_initial_deliveries(*args, **kwargs):
        assert initial_deliveries.wait(2)
        return original_get(*args, **kwargs)

    def counted_write(sink, data, meta):
        write_calls.append(data)
        return original_write(sink, data, meta)

    with (
        patch.dict(sys.modules, {"pika": pika}),
        patch.object(executor, "_build_source", return_value=source),
        patch.object(source._msg_queue, "get", side_effect=get_after_initial_deliveries),
        patch.object(LocalSink, "write", new=counted_write),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(executor.stream_run, config, stop)
        try:
            assert completed.wait(3), "prefetch window never released after confirmed output"
            result = future.result(timeout=3)
        finally:
            stop.set()
            future.result(timeout=3)

    assert result.status == RunStatus.SUCCESS
    assert acknowledged == [1, 2, 3]
    if filtered:
        assert not (tmp_path / "output.jsonl").exists()
        assert write_calls == []
    else:
        records = [json.loads(line) for line in
                   (tmp_path / "output.jsonl").read_text().splitlines()]
        assert sorted(records, key=lambda record: record["id"]) == [
            {"id": tag} for tag in (1, 2, 3)
        ]
        assert len(write_calls) == (3 if prefetch == 1 else 1)
