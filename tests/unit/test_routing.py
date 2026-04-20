"""Tests for conditional multi-sink routing (v0.5.0)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tram.connectors.local.sink import LocalSink
from tram.core.context import PipelineRunContext
from tram.pipeline.executor import PipelineExecutor, _filter_by_condition
from tram.serializers.ndjson_serializer import NdjsonSerializer

# ── _filter_by_condition ───────────────────────────────────────────────────


def test_filter_by_condition_all_pass():
    records = [{"x": 5}, {"x": 10}]
    result = _filter_by_condition(records, "x > 3")
    assert result == records


def test_filter_by_condition_partial():
    records = [{"x": 1}, {"x": 5}, {"x": 10}]
    result = _filter_by_condition(records, "x >= 5")
    assert result == [{"x": 5}, {"x": 10}]


def test_filter_by_condition_none_pass():
    records = [{"x": 1}, {"x": 2}]
    result = _filter_by_condition(records, "x > 100")
    assert result == []


def test_filter_by_condition_string_field():
    records = [{"status": "ok"}, {"status": "error"}]
    result = _filter_by_condition(records, 'status == "ok"')
    assert result == [{"status": "ok"}]


# ── Multi-sink routing in executor ────────────────────────────────────────


def _make_sink_mock():
    m = MagicMock()
    m.write = MagicMock()
    return m


def test_process_chunk_no_condition_writes_all():
    executor = PipelineExecutor()

    sink1 = _make_sink_mock()
    sink2 = _make_sink_mock()

    serializer_in = MagicMock()
    serializer_in.parse.return_value = [{"x": 1}, {"x": 2}]
    serializer_out = MagicMock()
    serializer_out.serialize.return_value = b"data"

    sinks = [(sink1, None, []), (sink2, None, [])]
    ctx = PipelineRunContext(pipeline_name="test")

    executor._process_chunk(
        b"raw", {}, serializer_in, [], serializer_out, sinks, ctx, "continue"
    )

    sink1.write.assert_called_once()
    sink2.write.assert_called_once()
    assert ctx.records_out == 2


def test_process_chunk_condition_filters_sink():
    executor = PipelineExecutor()

    sink_high = _make_sink_mock()
    sink_low = _make_sink_mock()

    serializer_in = MagicMock()
    serializer_in.parse.return_value = [{"level": "high"}, {"level": "low"}]
    serializer_out = MagicMock()
    serializer_out.serialize.return_value = b"data"

    sinks = [(sink_high, 'level == "high"', []), (sink_low, 'level == "low"', [])]
    ctx = PipelineRunContext(pipeline_name="test")

    executor._process_chunk(
        b"raw", {}, serializer_in, [], serializer_out, sinks, ctx, "continue"
    )

    # Each sink gets called once with its filtered data
    sink_high.write.assert_called_once()
    sink_low.write.assert_called_once()

    # Verify the serialiser was called with filtered subsets
    calls = serializer_out.serialize.call_args_list
    args_0 = calls[0][0][0]
    args_1 = calls[1][0][0]
    assert any(r["level"] == "high" for r in args_0)
    assert any(r["level"] == "low" for r in args_1)


def test_process_chunk_condition_no_match_skips_sink():
    executor = PipelineExecutor()

    sink_special = _make_sink_mock()
    sink_catch_all = _make_sink_mock()

    serializer_in = MagicMock()
    serializer_in.parse.return_value = [{"x": 1}]
    serializer_out = MagicMock()
    serializer_out.serialize.return_value = b"data"

    sinks = [(sink_special, "x > 999", []), (sink_catch_all, None, [])]
    ctx = PipelineRunContext(pipeline_name="test")

    executor._process_chunk(
        b"raw", {}, serializer_in, [], serializer_out, sinks, ctx, "continue"
    )

    sink_special.write.assert_not_called()  # condition failed
    sink_catch_all.write.assert_called_once()  # catch-all gets it


def test_process_chunk_condition_routes_to_separate_local_files(tmp_path):
    executor = PipelineExecutor()

    sink_msc = LocalSink({
        "path": str(tmp_path),
        "filename_template": "MSC_{part}.ndjson",
        "file_mode": "append",
    })
    sink_smsc = LocalSink({
        "path": str(tmp_path),
        "filename_template": "SMSC_{part}.ndjson",
        "file_mode": "append",
    })

    serializer_in = MagicMock()
    serializer_in.parse.return_value = [
        {"nf_name": "MSC", "value": 1},
        {"nf_name": "SMSC", "value": 2},
    ]
    serializer_out = NdjsonSerializer({"type": "ndjson"})
    ctx = PipelineRunContext(pipeline_name="route-test")

    executor._process_chunk(
        b"raw",
        {"pipeline_name": "route-test"},
        serializer_in,
        [],
        serializer_out,
        [(sink_msc, 'nf_name == "MSC"', []), (sink_smsc, 'nf_name == "SMSC"', [])],
        ctx,
        "continue",
    )

    assert (tmp_path / "MSC_00001.ndjson").read_text() == '{"nf_name": "MSC", "value": 1}\n'
    assert (tmp_path / "SMSC_00001.ndjson").read_text() == '{"nf_name": "SMSC", "value": 2}\n'


def test_process_chunk_partitions_single_sink_by_field_template(tmp_path):
    executor = PipelineExecutor()

    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "{field.nf_name}_{part}.ndjson",
        "file_mode": "append",
    })

    serializer_in = MagicMock()
    serializer_in.parse.return_value = [
        {"nf_name": "MSC", "value": 1},
        {"nf_name": "SMSC", "value": 2},
        {"nf_name": "MSC", "value": 3},
    ]
    serializer_out = NdjsonSerializer({"type": "ndjson"})
    ctx = PipelineRunContext(pipeline_name="partition-test")

    executor._process_chunk(
        b"raw",
        {"pipeline_name": "partition-test"},
        serializer_in,
        [],
        serializer_out,
        [(sink, None, [])],
        ctx,
        "continue",
    )

    assert (tmp_path / "MSC_00001.ndjson").read_text() == (
        '{"nf_name": "MSC", "value": 1}\n{"nf_name": "MSC", "value": 3}\n'
    )
    assert (tmp_path / "SMSC_00001.ndjson").read_text() == '{"nf_name": "SMSC", "value": 2}\n'


def test_process_chunk_partition_missing_field_uses_unknown(tmp_path):
    executor = PipelineExecutor()

    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "{field.nf_name}_{part}.ndjson",
        "file_mode": "append",
    })

    serializer_in = MagicMock()
    serializer_in.parse.return_value = [{"value": 1}]
    serializer_out = NdjsonSerializer({"type": "ndjson"})
    ctx = PipelineRunContext(pipeline_name="partition-test")

    executor._process_chunk(
        b"raw",
        {"pipeline_name": "partition-test"},
        serializer_in,
        [],
        serializer_out,
        [(sink, None, [])],
        ctx,
        "continue",
    )

    assert (tmp_path / "unknown_00001.ndjson").read_text() == '{"value": 1}\n'


def test_process_chunk_partition_rolls_per_field_value(tmp_path):
    executor = PipelineExecutor()

    sink = LocalSink({
        "path": str(tmp_path),
        "filename_template": "{field.nf_name}_{part}.ndjson",
        "file_mode": "append",
        "max_records": 2,
    })

    serializer_in = MagicMock()
    serializer_in.parse.return_value = [
        {"nf_name": "MSC", "value": 1},
        {"nf_name": "MSC", "value": 2},
        {"nf_name": "SMSC", "value": 10},
    ]
    serializer_out = NdjsonSerializer({"type": "ndjson"})
    ctx = PipelineRunContext(pipeline_name="partition-roll")

    executor._process_chunk(
        b"raw-1",
        {"pipeline_name": "partition-roll"},
        serializer_in,
        [],
        serializer_out,
        [(sink, None, [])],
        ctx,
        "continue",
    )

    serializer_in.parse.return_value = [
        {"nf_name": "MSC", "value": 3},
        {"nf_name": "SMSC", "value": 11},
    ]
    executor._process_chunk(
        b"raw-2",
        {"pipeline_name": "partition-roll"},
        serializer_in,
        [],
        serializer_out,
        [(sink, None, [])],
        ctx,
        "continue",
    )

    assert (tmp_path / "MSC_00001.ndjson").read_text() == (
        '{"nf_name": "MSC", "value": 1}\n{"nf_name": "MSC", "value": 2}\n'
    )
    assert (tmp_path / "MSC_00002.ndjson").read_text() == '{"nf_name": "MSC", "value": 3}\n'
    assert (tmp_path / "SMSC_00001.ndjson").read_text() == (
        '{"nf_name": "SMSC", "value": 10}\n{"nf_name": "SMSC", "value": 11}\n'
    )


def test_pipelineconfig_sinks_list_valid():
    """PipelineConfig accepts sinks list."""
    from tram.models.pipeline import PipelineConfig

    cfg = PipelineConfig.model_validate({
        "name": "test",
        "source": {"type": "local", "path": "/tmp"},
        "serializer_in": {"type": "json"},
        "serializer_out": {"type": "json"},
        "sinks": [{"type": "local", "path": "/tmp/out"}],
    })
    assert len(cfg.sinks) == 1


def test_pipelineconfig_legacy_sink_wrapped():
    """Legacy `sink:` is auto-wrapped into `sinks` list."""
    from tram.models.pipeline import PipelineConfig

    cfg = PipelineConfig.model_validate({
        "name": "test",
        "source": {"type": "local", "path": "/tmp"},
        "serializer_in": {"type": "json"},
        "serializer_out": {"type": "json"},
        "sink": {"type": "local", "path": "/tmp/out"},
    })
    assert len(cfg.sinks) == 1
    assert cfg.sinks[0].type == "local"


def test_pipelineconfig_no_sink_raises():
    from pydantic import ValidationError

    from tram.models.pipeline import PipelineConfig

    with pytest.raises(ValidationError):
        PipelineConfig.model_validate({
            "name": "test",
            "source": {"type": "local", "path": "/tmp"},
            "serializer_in": {"type": "json"},
            "serializer_out": {"type": "json"},
        })
