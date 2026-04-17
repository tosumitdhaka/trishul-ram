"""Tests for v0.7.0 RunResult additions: dlq_count field."""
from __future__ import annotations

from datetime import UTC, datetime

from tram.core.context import PipelineRunContext, RunResult, RunStatus


def test_runresult_has_dlq_count_field():
    now = datetime.now(UTC)
    r = RunResult(
        run_id="x",
        pipeline_name="p",
        status=RunStatus.SUCCESS,
        started_at=now,
        finished_at=now,
        records_in=1,
        records_out=1,
        records_skipped=0,
        dlq_count=3,
    )
    assert r.dlq_count == 3


def test_runresult_dlq_count_default_zero():
    now = datetime.now(UTC)
    r = RunResult(
        run_id="x",
        pipeline_name="p",
        status=RunStatus.SUCCESS,
        started_at=now,
        finished_at=now,
        records_in=1,
        records_out=1,
        records_skipped=0,
    )
    assert r.dlq_count == 0


def test_from_context_carries_dlq_count():
    ctx = PipelineRunContext(pipeline_name="pipe")
    ctx.record_dlq()
    ctx.record_dlq()
    ctx.inc_bytes_in(128)
    ctx.inc_bytes_out(64)
    result = RunResult.from_context(ctx, RunStatus.SUCCESS)
    assert result.dlq_count == 2
    assert result.bytes_in == 128
    assert result.bytes_out == 64


def test_to_dict_includes_dlq_count():
    now = datetime.now(UTC)
    r = RunResult(
        run_id="x",
        pipeline_name="p",
        status=RunStatus.SUCCESS,
        started_at=now,
        finished_at=now,
        records_in=0,
        records_out=0,
        records_skipped=0,
        dlq_count=5,
    )
    d = r.to_dict()
    assert "dlq_count" in d
    assert d["dlq_count"] == 5
