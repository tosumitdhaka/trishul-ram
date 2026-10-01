"""Issue #84 — records_skipped double-count + misleading no-sink-wrote error.

Pins the corrected counter semantics:
* stream-shaped chunks (one record per chunk) report skipped = in - out, not 2x;
* the batch over-count (one extra per failed chunk) is gone;
* empty/no-op chunks do not emit the "no sink wrote" error;
* a run where every sink genuinely fails still emits it.
"""

from __future__ import annotations

import json
import textwrap
from unittest.mock import MagicMock, patch

from tram.core.context import PipelineRunContext
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


def _make_pipeline() -> object:
    yaml_text = textwrap.dedent("""
        pipeline:
          name: test-exec
          source:
            type: sftp
            host: localhost
            username: user
            password: pass
            remote_path: /data
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: sftp
            host: localhost
            username: user
            password: pass
            remote_path: /out
    """)
    return load_pipeline_from_yaml(yaml_text)


def _process_records(
    ctx: PipelineRunContext,
    executor: PipelineExecutor,
    records: list[dict],
    sinks: list,
    stats=None,
) -> None:
    """Drive one chunk through the shared sink path (used by stream and batch)."""
    ser_out = MagicMock()
    ser_out.serialize.return_value = b"{}"
    executor._process_records(
        records, {"source_filename": "f.json"}, [], ser_out, sinks, ctx,
        "continue", stats=stats,
    )


class TestStreamPathSkipCounting:
    def test_stream_skips_reported_as_in_minus_out(self):
        """Stream-shaped chunks (1 record each) with a failing sink must report
        skipped = records_in - records_out (3), not 2x that (6)."""
        from tram.agent.metrics import PipelineStats

        ctx = PipelineRunContext(pipeline_name="t")
        executor = PipelineExecutor()
        stats = PipelineStats(run_id="r1", pipeline_name="t", schedule_type="stream")
        sink = MagicMock()
        sink.write.side_effect = Exception("boom")
        for record in ({"id": 1}, {"id": 2}, {"id": 3}):
            _process_records(ctx, executor, [record], [(sink, None, [])], stats=stats)

        assert ctx.records_in == 3
        assert ctx.records_out == 0
        assert ctx.records_skipped == 3  # not 6
        assert stats.snapshot()["records_skipped"] == 3
        # the sink failure message is still recorded (as a note, not a count)
        assert any("boom" in e for e in ctx.errors)

    def test_batch_chunk_over_count_gone(self):
        """A batch chunk of N records with a failing sink counts N skipped, not
        N+1 (the old per-sink record_error double-count)."""
        ctx = PipelineRunContext(pipeline_name="t")
        executor = PipelineExecutor()
        sink = MagicMock()
        sink.write.side_effect = Exception("boom")
        _process_records(
            ctx, executor,
            [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}],
            [(sink, None, [])],
        )
        assert ctx.records_in == 4
        assert ctx.records_out == 0
        assert ctx.records_skipped == 4


class TestNoSinkWroteError:
    def test_empty_chunk_produces_no_no_sink_wrote_error(self):
        ctx = PipelineRunContext(pipeline_name="t")
        executor = PipelineExecutor()
        sink = MagicMock()
        sink.write.side_effect = Exception("boom")
        _process_records(ctx, executor, [], [(sink, None, [])])
        assert ctx.records_in == 0
        assert ctx.records_out == 0
        assert ctx.records_skipped == 0
        assert ctx.errors == []

    def test_all_sinks_genuinely_fail_still_emits_error(self):
        ctx = PipelineRunContext(pipeline_name="t")
        executor = PipelineExecutor()
        sink = MagicMock()
        sink.write.side_effect = Exception("boom")
        _process_records(ctx, executor, [{"id": 1}, {"id": 2}], [(sink, None, [])])
        assert ctx.records_skipped == 2
        assert any("no sink wrote" in e for e in ctx.errors)

    def test_condition_filtered_chunk_still_emits_error(self):
        """Records present but every sink condition filters them: genuine skip."""
        ctx = PipelineRunContext(pipeline_name="t")
        executor = PipelineExecutor()
        sink = MagicMock()
        _process_records(
            ctx, executor,
            [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}],
            [(sink, "val == 'zzz'", [])],
        )
        assert ctx.records_out == 0
        assert ctx.records_skipped == 2
        assert any("no sink wrote" in e for e in ctx.errors)

    def test_partial_sink_failure_not_counted_when_another_sink_wrote(self):
        """Fan-out: a failing sink must not bump records_skipped when another
        sink delivered the records — nothing was actually lost."""
        ok_sink = MagicMock()
        failing_sink = MagicMock()
        failing_sink.write.side_effect = Exception("boom")
        ctx = PipelineRunContext(pipeline_name="t")
        executor = PipelineExecutor()
        _process_records(
            ctx, executor, [{"id": 1}],
            [(ok_sink, None, []), (failing_sink, None, [])],
        )
        assert ctx.records_in == 1
        assert ctx.records_out == 1
        assert ctx.records_skipped == 0
        assert any("boom" in e for e in ctx.errors)


class TestBatchRunEndToEnd:
    def test_batch_run_failing_sink_counts_in_minus_out(self):
        config = _make_pipeline()
        executor = PipelineExecutor()
        records = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "test.json"}),
        ])
        failing_sink = MagicMock()
        failing_sink.write.side_effect = Exception("boom")
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = json.dumps(records).encode()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(failing_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.records_in == 3
        assert result.records_out == 0
        assert result.records_skipped == 3  # not 6, not 4
        assert any("boom" in e for e in result.errors)

    def test_batch_run_empty_chunk_no_no_sink_wrote_error(self):
        config = _make_pipeline()
        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (b"[]", {"source_filename": "test.json"}),
        ])
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = []
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(MagicMock(), None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.records_in == 0
        assert result.records_out == 0
        assert result.records_skipped == 0
        assert result.errors == []