"""Tests for PipelineExecutor — batch and dry run modes."""

from __future__ import annotations

import json
import textwrap
from unittest.mock import MagicMock, patch

from tram.core.context import RunStatus
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


def _make_pipeline(extra_yaml: str = "") -> object:
    yaml_text = textwrap.dedent(f"""
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
          {extra_yaml}
    """)
    return load_pipeline_from_yaml(yaml_text)


class TestPipelineExecutorDryRun:
    def test_pipeline_config_accepts_record_chunk_size(self):
        config = _make_pipeline("record_chunk_size: 1000")
        assert config.record_chunk_size == 1000

    def test_pipeline_config_post_batch_cleanup_defaults_true(self):
        config = _make_pipeline()
        assert config.post_batch_cleanup is True
        # Serialization round-trip preserves the default.
        assert config.model_dump()["post_batch_cleanup"] is True

    def test_pipeline_config_accepts_post_batch_cleanup(self):
        config = _make_pipeline("post_batch_cleanup: true")
        assert config.post_batch_cleanup is True

    def test_pipeline_config_accepts_post_batch_cleanup_false(self):
        config = _make_pipeline("post_batch_cleanup: false")
        assert config.post_batch_cleanup is False

    def test_dry_run_valid_pipeline(self):
        config = _make_pipeline()
        executor = PipelineExecutor()
        result = executor.dry_run(config)
        assert result["valid"] is True
        assert result["issues"] == []

    def test_dry_run_invalid_source_type(self):
        """Test that dry_run catches PluginNotFoundError for unknown plugin."""
        # We manually patch get_source to simulate an unknown plugin
        from tram.core.exceptions import PluginNotFoundError
        config = _make_pipeline()
        executor = PipelineExecutor()

        with patch("tram.pipeline.executor.get_source", side_effect=PluginNotFoundError("No source 'x'")):
            result = executor.dry_run(config)

        assert result["valid"] is False
        assert any("source" in issue for issue in result["issues"])

    def test_dry_run_rejects_unknown_filename_template_tokens(self):
        config = _make_pipeline()
        config.sinks[0].filename_template = "{epoch_ms}_{unknown_token}.bin"
        executor = PipelineExecutor()

        result = executor.dry_run(config)

        assert result["valid"] is False
        assert any("unknown_token" in issue for issue in result["issues"])


class TestPipelineExecutorBatchRun:
    def test_batch_run_success(self):
        """Mock source and sink to verify batch_run returns RunResult."""
        config = _make_pipeline()
        executor = PipelineExecutor()

        records = [{"id": "1", "val": "hello"}, {"id": "2", "val": "world"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "test.json"}),
        ])

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = json.dumps(records).encode()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_in == 2
        assert result.records_out == 2
        assert result.records_skipped == 0
        assert result.bytes_in == len(json.dumps(records).encode())
        assert result.bytes_out == len(json.dumps(records).encode())
        mock_sink.write.assert_called_once()

    def test_batch_run_empty_source(self):
        """A source with no files should succeed with 0 records."""
        config = _make_pipeline()
        executor = PipelineExecutor()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([])
        mock_sink = MagicMock()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_in == 0
        mock_sink.write.assert_not_called()

    def test_batch_run_continue_on_error(self):
        """on_error=continue should skip bad chunks and continue."""
        config = _make_pipeline()
        executor = PipelineExecutor()

        from tram.core.exceptions import SerializerError

        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (b"bad", {}),
            (b"good", {}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        # First call raises, second succeeds
        mock_ser_in.parse.side_effect = [SerializerError("bad data"), [{"x": 1}]]
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b'[{"x":1}]'

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_skipped > 0

    def test_batch_run_invokes_post_batch_cleanup_by_default(self):
        config = _make_pipeline()
        executor = PipelineExecutor()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([])
        mock_sink = MagicMock()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_post_batch_cleanup") as cleanup,
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        cleanup.assert_called_once_with(config)

    def test_batch_run_skips_post_batch_cleanup_when_disabled(self):
        config = _make_pipeline("post_batch_cleanup: false")
        executor = PipelineExecutor()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([])
        mock_sink = MagicMock()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_post_batch_cleanup") as cleanup,
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        cleanup.assert_not_called()

    def test_batch_run_invokes_post_batch_cleanup_on_success_when_enabled(self):
        config = _make_pipeline("post_batch_cleanup: true")
        executor = PipelineExecutor()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([])
        mock_sink = MagicMock()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_post_batch_cleanup") as cleanup,
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        cleanup.assert_called_once_with(config)

    def test_batch_run_invokes_post_batch_cleanup_on_failure_when_enabled(self):
        config = _make_pipeline("on_error: abort\n          post_batch_cleanup: true")
        executor = PipelineExecutor()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([(b"bad", {})])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = ValueError("boom")

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_post_batch_cleanup") as cleanup,
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.FAILED
        cleanup.assert_called_once_with(config)

    def test_batch_run_closes_sinks_in_finally(self):
        """batch_run must close sinks (and the DLQ sink) after a successful run."""
        config = _make_pipeline()
        executor = PipelineExecutor()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([])
        mock_sink = MagicMock()
        mock_dlq = MagicMock()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_build_dlq_sink", return_value=mock_dlq),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        mock_sink.close.assert_called_once()
        mock_dlq.close.assert_called_once()

    def test_batch_run_closes_sinks_on_failure(self):
        """batch_run must still close sinks when the run fails."""
        config = _make_pipeline("on_error: abort")
        executor = PipelineExecutor()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([(b"bad", {})])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = ValueError("boom")

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.FAILED
        mock_sink.close.assert_called_once()

    def test_batch_run_swallows_sink_close_errors(self):
        """A failing sink close() must not mask the run result."""
        config = _make_pipeline()
        executor = PipelineExecutor()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([])
        mock_sink = MagicMock()
        mock_sink.close.side_effect = RuntimeError("close boom")

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        mock_sink.close.assert_called_once()

    def test_batch_run_retry_closes_sinks_and_source_from_failed_attempt(self):
        """The retry path rebuilds sinks/source per attempt; the failed
        attempt's instances must be closed too, not just the final attempt's
        (the old code leaked e.g. ClickHouse flush timers from failed tries)."""
        from tram.core.exceptions import TramError

        config = _make_pipeline(
            "on_error: retry\n"
            "          retry_count: 1\n"
            "          retry_delay_seconds: 0"
        )
        executor = PipelineExecutor()

        first_source = MagicMock()
        second_source = MagicMock()
        first_sink = MagicMock()
        second_sink = MagicMock()
        first_dlq = MagicMock()
        second_dlq = MagicMock()

        with (
            patch.object(executor, "_build_source", side_effect=[first_source, second_source]),
            patch.object(executor, "_build_sinks", side_effect=[
                [(first_sink, None, [])],
                [(second_sink, None, [])],
            ]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_build_dlq_sink", side_effect=[first_dlq, second_dlq]),
            patch.object(executor, "_run_batch_chunks", side_effect=[TramError("boom"), None]),
            patch("time.sleep"),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        first_sink.close.assert_called_once()     # failed attempt, closed at retry
        second_sink.close.assert_called_once()    # final attempt, closed in finally
        first_dlq.close.assert_called_once()
        second_dlq.close.assert_called_once()
        first_source.close.assert_called_once()   # failed attempt's source connection
        second_source.close.assert_called_once()

    # ── D4: records_out counts records delivered, not sink fanout ────────────

    def test_records_out_counts_delivered_records_with_condition_sink(self):
        """D4: a condition-filtered sink must not inflate records_out — only
        the records it actually wrote count as delivered."""
        from tram.agent.metrics import PipelineStats

        config = _make_pipeline()
        executor = PipelineExecutor()
        stats = PipelineStats(run_id="r1", pipeline_name="test-exec", schedule_type="batch")

        records = [{"id": "1", "val": "keep"}, {"id": "2", "val": "drop"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "test.json"}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = json.dumps(records).encode()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, "val == 'keep'", [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config, stats=stats)

        assert result.records_in == 2
        assert result.records_out == 1  # only the record the sink actually wrote
        assert stats.snapshot()["records_out"] == 1

    def test_records_out_not_multiplied_by_multi_sink_fanout(self):
        """D4: two sinks each writing every record must report records, not
        records x sinks — fanout is counted in bytes_out, not records_out."""
        from tram.agent.metrics import PipelineStats

        config = _make_pipeline()
        executor = PipelineExecutor()
        stats = PipelineStats(run_id="r1", pipeline_name="test-exec", schedule_type="batch")

        records = [{"id": "1", "val": "a"}, {"id": "2", "val": "b"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "test.json"}),
        ])
        mock_sink_a = MagicMock()
        mock_sink_b = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = json.dumps(records).encode()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[
                (mock_sink_a, None, []),
                (mock_sink_b, None, []),
            ]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config, stats=stats)

        assert result.records_in == 2
        assert result.records_out == 2  # not 4 — no fanout
        assert stats.snapshot()["records_out"] == 2
        assert stats.snapshot()["bytes_out"] > 0  # I/O fanout is still counted

    def test_records_out_ignores_failed_sink(self):
        """D4: a sink that fails must not inflate records_out — a record is
        counted once, via the sink that delivered it. (The failed sink still
        bumps records_skipped via record_error — unchanged behavior.)"""
        config = _make_pipeline()
        executor = PipelineExecutor()

        records = [{"id": "1", "val": "a"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "test.json"}),
        ])
        ok_sink = MagicMock()
        failing_sink = MagicMock()
        failing_sink.write.side_effect = Exception("boom")
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = json.dumps(records).encode()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[
                (ok_sink, None, []),
                (failing_sink, None, []),
            ]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.records_in == 1
        assert result.records_out == 1  # delivered once, not doubled by fanout

    def test_records_skipped_when_condition_filters_everything(self):
        """A sink that filters out every record counts the chunk as skipped."""
        config = _make_pipeline()
        executor = PipelineExecutor()

        records = [{"id": "1", "val": "a"}, {"id": "2", "val": "b"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "test.json"}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = json.dumps(records).encode()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, "val == 'zzz'", [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.records_out == 0
        assert result.records_skipped == 2

    # ── Retry parity: stats reset with the rebuilt context ───────────────────

    def test_retry_resets_stats_accumulator(self):
        """Retry parity: on on_error=retry the run context is rebuilt and the
        stats accumulator is reset too — live totals reflect only the final
        attempt, matching the RunResult's numbers."""
        from tram.agent.metrics import PipelineStats
        from tram.core.exceptions import TramError

        config = _make_pipeline(
            "on_error: retry\n"
            "          retry_count: 1\n"
            "          retry_delay_seconds: 0"
        )
        executor = PipelineExecutor()
        stats = PipelineStats(run_id="r1", pipeline_name="test-exec", schedule_type="batch")

        calls = {"n": 0}

        def fake_run_chunks(*args, **kwargs):
            calls["n"] += 1
            ctx = args[7]
            if calls["n"] == 1:
                # Attempt 1 processes records before dying.
                ctx.inc_records_in(5)
                ctx.inc_records_out(5)
                stats.increment(records_in=5, records_out=5)
                raise TramError("boom")
            # Attempt 2 processes a fresh 3 records.
            ctx.inc_records_in(3)
            ctx.inc_records_out(3)
            stats.increment(records_in=3, records_out=3)
            return None

        with (
            patch.object(executor, "_build_source", side_effect=[MagicMock(), MagicMock()]),
            patch.object(executor, "_build_sinks", side_effect=[
                [(MagicMock(), None, [])],
                [(MagicMock(), None, [])],
            ]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_run_batch_chunks", side_effect=fake_run_chunks),
            patch("time.sleep"),
        ):
            result = executor.batch_run(config, stats=stats)

        assert result.status == RunStatus.SUCCESS
        assert result.records_in == 3  # final attempt only
        assert stats.snapshot()["records_in"] == 3  # live == final, no accumulation

    def test_post_batch_cleanup_ignores_missing_trim_support(self):
        config = _make_pipeline()

        with (
            patch("tram.pipeline.executor.gc.collect") as collect,
            patch("tram.pipeline.executor._try_trim_process_heap", return_value=False) as trim,
        ):
            PipelineExecutor._post_batch_cleanup(config)

        collect.assert_called_once_with()
        trim.assert_called_once_with()


class TestTransformChain:
    """Test that transforms are applied in order during execution."""

    def test_transform_chain_applied(self):
        """Verify multiple transforms are called in sequence."""
        config = _make_pipeline()
        executor = PipelineExecutor()

        call_order = []

        class OrderedTransform:
            def __init__(self, label):
                self.label = label

            def apply(self, records):
                call_order.append(self.label)
                return records

        t1, t2, t3 = OrderedTransform("t1"), OrderedTransform("t2"), OrderedTransform("t3")

        mock_source = MagicMock()
        mock_source.read.return_value = iter([(b'[{"x":1}]', {})])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = [{"x": 1}]
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b'[{"x":1}]'

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[t1, t2, t3]),
        ):
            executor.batch_run(config)

        assert call_order == ["t1", "t2", "t3"]

    def test_transforms_receive_runtime_meta_when_supported(self):
        config = _make_pipeline()
        executor = PipelineExecutor()

        class MetaAwareTransform:
            def __init__(self):
                self.metas = []

            def set_runtime_meta(self, meta):
                self.metas.append(dict(meta))

            def apply(self, records):
                return records

        t = MetaAwareTransform()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([(b'[{"x":1}]', {"source_filename": "test.json"})])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = [{"x": 1}]
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b'[{"x":1}]'

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[t]),
        ):
            executor.batch_run(config)

        assert len(t.metas) == 1
        assert t.metas[0]["source_filename"] == "test.json"
        assert t.metas[0]["pipeline_name"] == "test-exec"
        assert "run_id" in t.metas[0]
