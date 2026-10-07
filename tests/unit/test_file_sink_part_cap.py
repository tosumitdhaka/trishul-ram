"""Issue #77 — local-sink part-index cap must fail loud, never skip silently.

Regression tests: writes past ``max_index`` on the single/stream path raise a
SinkError that the run context records (run not clean-success), every dropped
record is counted, and append-mode rollover behavior is unchanged.
"""
from __future__ import annotations

import json
import logging

import pytest

from tram.connectors.file_sink_common import LocalRollingBackend, RollingWriter
from tram.connectors.local.sink import LocalSink
from tram.core.context import PipelineRunContext
from tram.core.exceptions import SinkError
from tram.pipeline.executor import PipelineExecutor
from tram.serializers.json_serializer import JsonSerializer


def _single_meta() -> dict:
    return {
        "pipeline_name": "test",
        "serializer_type": "json",
        "serializer_config": {"type": "json"},
        "output_record_count": 1,
    }


def _local_sink(tmp_path, *, max_index: int) -> LocalSink:
    return LocalSink({
        "path": str(tmp_path),
        "filename_template": "out_{part}.json",
        "file_mode": "single",
        "max_index": max_index,
    })


def _process_as_stream(records: list[dict], sink: LocalSink) -> PipelineRunContext:
    """Drive the executor exactly like the stream path: one record per chunk."""
    executor = PipelineExecutor()
    serializer_out = JsonSerializer({})
    ctx = PipelineRunContext(pipeline_name="test")
    for record in records:
        executor._process_records(
            [record],
            {"pipeline_name": "test"},
            [],
            serializer_out,
            [(sink, None, [])],
            ctx,
            "continue",
        )
    return ctx


def test_default_max_index_is_99999() -> None:
    from tram.models.pipeline import LocalSinkConfig

    cfg = LocalSinkConfig(type="local", path="/tmp/out")
    assert cfg.max_index == 99999


def test_single_mode_past_cap_raises_sink_error(tmp_path) -> None:
    sink = _local_sink(tmp_path, max_index=3)
    for _ in range(3):
        sink.write(b'{"x": 1}', _single_meta())

    # One file part per write up to the cap; parts 1..3 rendered with the
    # zero-padding width derived from max_index.
    assert sorted(p.name for p in tmp_path.glob("out_*.json")) == [
        "out_1.json",
        "out_2.json",
        "out_3.json",
    ]

    with pytest.raises(SinkError, match="max_index=3"):
        sink.write(b'{"x": 1}', _single_meta())
    # The dropped record is counted, not silently forgotten.
    assert sink._writer.dropped_past_cap_total == 1
    assert len(list(tmp_path.glob("out_*.json"))) == 3  # nothing written past the cap


def test_stream_path_past_cap_surfaces_run_error_and_counts_records(tmp_path) -> None:
    """The stream path (per-record writes) must never report clean success:
    records_out pins at the cap, the run carries cap errors, and the dropped
    records are counted (true skipped = in − out, issue #84's definition)."""
    sink = _local_sink(tmp_path, max_index=3)
    ctx = _process_as_stream([{"seq": i} for i in range(6)], sink)

    assert ctx.records_in == 6
    assert ctx.records_out == 3  # pinned at the cap — past-cap records never written
    assert ctx.records_in - ctx.records_out == 3  # dropped count
    assert ctx.errors  # run is not clean-success
    assert any("max_index=3" in error for error in ctx.errors)
    assert sink._writer.dropped_past_cap_total == 3
    assert len(list(tmp_path.glob("out_*.json"))) == 3


def test_append_mode_rollover_unchanged_until_cap(tmp_path) -> None:
    """Append-mode rollover keeps working up to the cap (behavior unchanged);
    past the cap it fails loud exactly like single mode."""
    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "events_{part}.ndjson",
        "file_mode": "append",
        "max_records": 1,
        "max_index": 3,
    })
    meta = {
        "pipeline_name": "test",
        "serializer_type": "ndjson",
        "serializer_config": {"type": "ndjson"},
        "output_record_count": 1,
    }
    for seq in range(1, 4):
        sink.write(json.dumps({"seq": seq}).encode(), dict(meta))

    assert sorted(p.name for p in tmp_path.glob("events_*.ndjson")) == [
        "events_1.ndjson",
        "events_2.ndjson",
        "events_3.ndjson",
    ]
    assert (tmp_path / "events_1.ndjson").read_text() == '{"seq": 1}\n'
    assert (tmp_path / "events_3.ndjson").read_text() == '{"seq": 3}\n'

    with pytest.raises(SinkError, match="max_index=3"):
        sink.write(json.dumps({"seq": 4}).encode(), dict(meta))
    assert sink._writer.dropped_past_cap_total == 1


def test_writer_drop_counter_tracks_each_past_cap_record_and_message_carries_count(
    tmp_path,
) -> None:
    writer = RollingWriter(
        filename_template="out_{part}.json",
        file_mode="single",
        max_records=None,
        max_time=None,
        max_bytes=None,
        max_index=2,
        sink_name="Test",
        logger=logging.getLogger("test-part-cap"),
    )
    backend = LocalRollingBackend(tmp_path, overwrite=True)
    meta = {
        "serializer_type": "json",
        "serializer_config": {"type": "json"},
        "output_record_count": 1,
    }
    writer.write(b"1", meta, backend=backend, handle=None)
    writer.write(b"2", meta, backend=backend, handle=None)

    with pytest.raises(SinkError) as excinfo:
        writer.write(b"3", meta, backend=backend, handle=None)
    assert "max_index=2" in str(excinfo.value)
    assert "1 record(s) past the cap skipped" in str(excinfo.value)

    with pytest.raises(SinkError) as excinfo2:
        writer.write(b"4", meta, backend=backend, handle=None)
    assert "2 record(s) past the cap skipped" in str(excinfo2.value)

    assert writer.dropped_past_cap_total == 2
    assert writer.max_index == 2