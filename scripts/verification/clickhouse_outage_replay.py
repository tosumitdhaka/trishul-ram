"""Guarded outage/replay verification for the isolated v1.8.3 ClickHouse fixture."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import unquote, urlsplit

from clickhouse_driver import Client

import tram.serializers  # noqa: F401
import tram.transforms  # noqa: F401
from tram.connectors.clickhouse.sink import ClickHouseSink
from tram.core.context import RunStatus
from tram.core.exceptions import SinkError
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml

NAME = "tram-v183-clickhouse-test"
endpoint = urlsplit(os.environ["TRAM_TEST_CLICKHOUSE_URL"])
assert endpoint.hostname in {"localhost", "127.0.0.1"}
CFG = {
    "host": "127.0.0.1",
    "port": 19083,
    "database": "trishul",
    "username": unquote(endpoint.username or ""),
    "password": unquote(endpoint.password or ""),
    "connect_timeout": 1,
    "send_receive_timeout": 2,
    "batch_size": 100,
    "batch_timeout_seconds": 0,
}
ROOT = Path("docs/reviews/evidence/v1.8.3/clickhouse-outage")
ROOT.mkdir(parents=True, exist_ok=True)


def event(**entry):
    print(json.dumps(entry), flush=True)


def control(action):
    actual = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.Name}}", NAME],
        text=True,
    ).strip()
    assert actual == "/" + NAME
    subprocess.run(["docker", action, NAME], check=True, capture_output=True)
    event(container=NAME, action=action)


def query(sql):
    client = Client(
        host=CFG["host"],
        port=CFG["port"],
        database=CFG["database"],
        user=CFG["username"],
        password=CFG["password"],
        connect_timeout=1,
        send_receive_timeout=2,
    )
    try:
        return client.execute(sql)
    finally:
        client.disconnect()


def ready():
    deadline = time.monotonic() + 45
    while True:
        try:
            query("SELECT 1")
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


ready()
version = query("SELECT version()")[0][0]
event(service_version=version, container=NAME)
results = []
for repetition in range(1, 4):
    table = f"trishul.outage_v183_{repetition}_{int(time.time())}"
    query(f"CREATE TABLE {table} (id UInt64, val String) ENGINE=MergeTree ORDER BY id")
    sink = ClickHouseSink({**CFG, "table": table})
    stopped = False
    try:
        rows = [{"id": 1, "val": "buffer-retained"}, {"id": 2, "val": "exact-content"}]
        sink.write(json.dumps(rows).encode(), {})
        control("stop")
        stopped = True
        try:
            sink.commit()
        except SinkError:
            pass
        else:
            raise AssertionError("outage commit incorrectly confirmed")
        assert sink._buffer == rows
        control("start")
        stopped = False
        ready()
        assert query(f"SELECT id,val FROM {table} ORDER BY id") == []
        try:
            sink.commit()
        except SinkError:
            pass
        else:
            raise AssertionError("unobserved outage latch incorrectly cleared")
        assert sink.commit().confirmed
        assert sink._buffer == []
        assert query(f"SELECT id,val FROM {table} ORDER BY id") == [
            (1, "buffer-retained"),
            (2, "exact-content"),
        ]
        sink.close()
        event(repetition=repetition, scenario="real-outage-buffer-restart-commit", result="passed")

        replay_rows = [{"id": 10, "val": "source-retained"}, {"id": 11, "val": "replay-content"}]
        with tempfile.TemporaryDirectory(prefix="tram-v183-ch-outage-") as temporary:
            incoming = Path(temporary) / "in"
            processed = Path(temporary) / "processed"
            incoming.mkdir()
            original = incoming / "input.json"
            original.write_text(json.dumps(replay_rows))
            source_bytes = original.read_bytes()
            config = load_pipeline_from_yaml(
                json.dumps(
                    {
                        "pipeline": {
                            "name": "clickhouse-outage-source-replay",
                            "thread_workers": 2,
                            "on_error": "abort",
                            "source": {
                                "type": "local",
                                "path": str(incoming),
                                "move_after_read": str(processed),
                                "skip_processed": True,
                            },
                            "serializer_in": {"type": "json"},
                            "serializer_out": {"type": "json"},
                            "sink": {"type": "clickhouse", **CFG, "table": table},
                        }
                    }
                )
            )
            tracker = MagicMock()
            tracker.is_processed.return_value = False
            executor = PipelineExecutor(file_tracker=tracker)
            failed_sink = ClickHouseSink({**CFG, "table": table})
            control("stop")
            stopped = True
            with patch.object(executor, "_build_sinks", return_value=[(failed_sink, None, [])]):
                failed = executor.batch_run(config)
            assert failed.status == RunStatus.FAILED
            assert failed_sink._buffer == replay_rows
            assert original.read_bytes() == source_bytes
            assert not (processed / original.name).exists()
            tracker.mark_processed.assert_not_called()
            control("start")
            stopped = False
            ready()
            assert query(f"SELECT id,val FROM {table} ORDER BY id") == [
                (1, "buffer-retained"),
                (2, "exact-content"),
            ]
            replay = executor.batch_run(config)
            assert replay.status == RunStatus.SUCCESS
            assert not original.exists()
            assert (processed / original.name).read_bytes() == source_bytes
            tracker.mark_processed.assert_called_once()
            readback = query(f"SELECT id,val FROM {table} ORDER BY id")
            assert readback == [
                (1, "buffer-retained"),
                (2, "exact-content"),
                (10, "source-retained"),
                (11, "replay-content"),
            ]
            record = {
                "repetition": repetition,
                "service_version": version,
                "direct_retained_rows": rows,
                "source_retained_bytes": source_bytes.decode(),
                "failed_status": failed.status.value,
                "replay_status": replay.status.value,
                "tracker_marks": tracker.mark_processed.call_count,
                "confirmed_readback": readback,
                "container": NAME,
                "scenarios": [
                    "real-outage-buffer-restart-commit",
                    "real-outage-threaded-source-replay",
                ],
            }
            results.append(record)
            (ROOT / f"repetition-{repetition}.json").write_text(json.dumps(record, indent=2) + "\n")
            event(
                repetition=repetition,
                scenario="real-outage-threaded-source-replay",
                result="passed",
                rows=readback,
            )
    finally:
        if stopped:
            control("start")
            ready()
        sink.close()
        query(f"DROP TABLE IF EXISTS {table}")
(ROOT / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
event(result="passed", repetitions=len(results), evidence=str(ROOT / "summary.json"))
