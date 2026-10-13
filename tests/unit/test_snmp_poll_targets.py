"""Bounded per-device polling, failures and stop ownership."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from tram.connectors.snmp.source import SNMPPollSource
from tram.core.exceptions import SourceError
from tram.models.pipeline import SnmpPollSourceConfig


def _config(**overrides):
    return {"targets": [f"192.0.2.{i}" for i in range(1, 11)],
            "oids": ["1.3.6.1.2.1.1.3.0"], "resolve_oids": False,
            "max_concurrency": 3, **overrides}


@pytest.mark.parametrize("overrides", [
    {"host": "localhost"}, {"targets": ["x", "x"]}, {"targets": [""]},
    {"max_concurrency": 0}, {"max_concurrency": 11}, {"targets": [str(i) for i in range(11)]},
    {"target_timeout": 0}, {"timeout": 0}, {"retries": -1},
])
def test_config_rejects_invalid_target_admission(overrides):
    with pytest.raises(ValidationError):
        SnmpPollSourceConfig(type="snmp_poll", **_config(**overrides))


def test_ten_targets_bounded_and_attributed(monkeypatch):
    active = peak = 0

    async def get(source, hlapi, typed=False):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return {"device": source.host}
        finally:
            active -= 1

    monkeypatch.setattr(SNMPPollSource, "_do_get", get)
    source = SNMPPollSource(_config())
    outputs = list(source.read())
    assert peak == 3
    assert active == 0
    assert len(outputs) == 10
    assert {meta["source_host"] for _, meta in outputs} == set(source.targets)
    for payload, meta in outputs:
        record = json.loads(payload)
        assert record["device"] == record["_source_host"] == meta["source_host"]
        assert len(meta["poll_results"]) == 10
        assert all(r["status"] == "success" for r in meta["poll_results"])


def test_timeout_isolated_healthy_records_before_overall_failure(monkeypatch):
    async def get(source, hlapi, typed=False):
        if source.host.endswith(".2"):
            await asyncio.sleep(10)
        return {"value": "42"}

    monkeypatch.setattr(SNMPPollSource, "_do_get", get)
    source = SNMPPollSource(_config(targets=["192.0.2.1", "192.0.2.2"], target_timeout=0.02))
    stream = source.read()
    payload, meta = next(stream)
    assert json.loads(payload)["_source_host"] == "192.0.2.1"
    assert {r["target"]: r["status"] for r in meta["poll_results"]} == {
        "192.0.2.1": "success", "192.0.2.2": "timeout"}
    with pytest.raises(SourceError, match="192.0.2.2=timeout"):
        next(stream)


def test_stop_awaits_active_cleanup_and_restart_uses_fresh_instance(monkeypatch):
    started = threading.Event()
    cleaned = threading.Event()

    async def get(source, hlapi, typed=False):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.sleep(0.01)
            cleaned.set()

    monkeypatch.setattr(SNMPPollSource, "_do_get", get)
    source = SNMPPollSource(_config(max_concurrency=1))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: list(source.read()))
        assert started.wait(2)
        source.stop()
        with pytest.raises(SourceError, match="cancelled"):
            future.result(timeout=2)
    assert cleaned.is_set()
    assert len(source.poll_results) == 10
    assert all(r["status"] == "cancelled" for r in source.poll_results)

    async def healthy(source, hlapi, typed=False):
        return {"value": "42"}

    monkeypatch.setattr(SNMPPollSource, "_do_get", healthy)
    assert len(list(SNMPPollSource(_config()).read())) == 10


def test_single_host_payload_preserved(monkeypatch):
    async def healthy(source, hlapi, typed=False):
        return {"value": "42"}

    monkeypatch.setattr(SNMPPollSource, "_do_get", healthy)
    source = SNMPPollSource({"host": "192.0.2.1", "oids": ["1.3.6"], "resolve_oids": False})
    payload, meta = next(source.read())
    assert "_source_host" not in json.loads(payload)
    assert meta["source_host"] == "192.0.2.1"


@pytest.mark.parametrize("thread_workers", [1, 2])
def test_executor_publishes_healthy_local_output_before_failed_cycle(tmp_path, monkeypatch, thread_workers):
    from tram.core.context import RunStatus
    from tram.pipeline.executor import PipelineExecutor
    from tram.pipeline.loader import load_pipeline_from_yaml

    async def poll(source, hlapi, typed=False):
        if source.host.endswith(".2"):
            raise TimeoutError
        return {"device": source.host, "value": "42"}

    monkeypatch.setattr(SNMPPollSource, "_do_get", poll)
    config = load_pipeline_from_yaml(f"""
    pipeline:
      name: target-cycle
      thread_workers: {thread_workers}
      on_error: abort
      source:
        type: snmp_poll
        targets: [192.0.2.1, 192.0.2.2, 192.0.2.3]
        oids: [1.3.6.1.2.1.1.3.0]
        resolve_oids: false
      serializer_in:
        type: json
      serializer_out:
        type: ndjson
      sinks:
        - type: local
          path: {tmp_path}
          filename_template: "{{field._source_host}}.jsonl"
          file_mode: append
    """)
    result = PipelineExecutor().batch_run(config)
    assert result.status in {RunStatus.FAILED, RunStatus.PARTIAL}
    assert "192.0.2.2=timeout" in (result.error or "")
    records = [json.loads(line) for path in tmp_path.glob("*.jsonl")
               for line in path.read_text().splitlines()]
    assert {r["_source_host"] for r in records} == {"192.0.2.1", "192.0.2.3"}
    assert all(r["device"] == r["_source_host"] and r["value"] == "42" for r in records)


def test_render_failure_isolated_from_healthy_device(monkeypatch):
    async def poll(source, hlapi, typed=False):
        # A scalar named identically on each host; typed capture for classification.
        return {"1.3.6.1.2.1.1.3.0": ("42", "TimeTicks")}

    original = SNMPPollSource._render_poll

    def render(source, raw, polled_at):
        if source.host.endswith(".2"):
            raise SourceError("private decode detail")
        yield from original(source, raw, polled_at)

    monkeypatch.setattr(SNMPPollSource, "_do_get", poll)
    monkeypatch.setattr(SNMPPollSource, "_render_poll", render)
    source = SNMPPollSource(_config(targets=["192.0.2.1", "192.0.2.2"], classify=True, index_depth=1))
    stream = source.read()
    payload, meta = next(stream)
    assert json.loads(payload)["_source_host"] == "192.0.2.1"
    statuses = {r["target"]: r for r in meta["poll_results"]}
    assert statuses["192.0.2.1"]["bindings"] == statuses["192.0.2.1"]["records"] == 1
    assert statuses["192.0.2.2"]["status"] == "render_failed"
    with pytest.raises(SourceError, match="192.0.2.2=render_failed") as exc:
        next(stream)
    assert "private decode detail" not in str(exc.value)


def test_device_counter_state_remains_separate():
    from tram.transforms.counter_delta import CounterDeltaTransform

    transform = CounterDeltaTransform({"fields": ["count"], "first_sample": "drop", "key_fields": []})
    for host, count in [("192.0.2.1", 100), ("192.0.2.2", 1000)]:
        transform.set_runtime_meta({"source_host": host})
        assert transform.apply([{"count": count, "_polled_at": "2026-10-13T00:00:00Z"}]) == []
    for host, count, expected in [("192.0.2.1", 110, 10), ("192.0.2.2", 1050, 50)]:
        transform.set_runtime_meta({"source_host": host})
        output = transform.apply([{"count": count, "_polled_at": "2026-10-13T00:05:00Z"}])
        assert output[0]["count_delta"] == expected
    assert len(transform.get_state()) == 2


@pytest.mark.parametrize("control", ["stop", "deadline"])
def test_executor_stop_cancels_poll_and_reports_aborted(tmp_path, monkeypatch, control):
    from tram.core.context import RunStatus
    from tram.pipeline.executor import PipelineExecutor
    from tram.pipeline.loader import load_pipeline_from_yaml

    entered = threading.Event()
    cleaned = threading.Event()

    async def waiting(source, hlapi, typed=False):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            cleaned.set()

    monkeypatch.setattr(SNMPPollSource, "_do_get", waiting)
    config = load_pipeline_from_yaml(json.dumps({"pipeline": {
        "name": "stop-target-cycle", "source": {"type": "snmp_poll", **_config()},
        "serializer_in": {"type": "json"}, "serializer_out": {"type": "ndjson"},
        "sink": {"type": "local", "path": str(tmp_path)},
    }}))
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        controls = {"stop_event": stop} if control == "stop" else {"deadline": time.monotonic() + 0.2}
        future = pool.submit(PipelineExecutor().batch_run, config, **controls)
        assert entered.wait(2)
        if control == "stop":
            stop.set()
        result = future.result(timeout=2)
    assert result.status == RunStatus.ABORTED
    assert cleaned.is_set()
    assert list(tmp_path.iterdir()) == []


def test_scheduled_pilot_prevents_overlapping_runs(tmp_path):
    from unittest.mock import MagicMock

    from tests.unit.test_pipeline_controller import _started_controller
    from tram.pipeline.loader import load_pipeline_from_yaml

    config = load_pipeline_from_yaml(json.dumps({"pipeline": {
        "name": "scheduled-target-cycle", "enabled": False,
        "schedule": {"type": "interval", "interval_seconds": 300},
        "source": {"type": "snmp_poll", **_config()},
        "serializer_in": {"type": "json"},
        "sink": {"type": "local", "path": str(tmp_path)},
    }}))
    controller = _started_controller()
    try:
        controller.manager.register(config, yaml_text="")
        controller._add_interval_job(config)
        assert controller._scheduler.get_job("batch-scheduled-target-cycle").max_instances == 1
        controller.manager.set_status(config.name, "running")
        controller.executor = MagicMock()
        controller._run_batch(config.name)
        controller.executor.batch_run.assert_not_called()
    finally:
        controller.stop()
