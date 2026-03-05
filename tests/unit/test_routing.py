"""Tests for conditional multi-sink routing (v0.5.0)."""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from tram.core.context import PipelineRunContext
from tram.pipeline.executor import PipelineExecutor, _filter_by_condition


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
