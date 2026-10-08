"""Tests for PipelineExecutor — batch, stream, and dry run modes."""

from __future__ import annotations

import json
import textwrap
import threading
from unittest.mock import MagicMock, patch

import pytest

from tram.core.context import RunStatus
from tram.interfaces.base_source import AckDisposition
from tram.pipeline.executor import PipelineExecutor, _filter_by_condition
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

    def test_pipeline_config_rejects_zero_rate_limit_rps(self):
        """GH #48 §2.4: rate_limit_rps=0 would divide by zero in the token
        bucket — rejected at validation, never a runtime ZeroDivisionError."""
        from tram.core.exceptions import ConfigError

        with pytest.raises(ConfigError, match="rate_limit_rps"):
            _make_pipeline("rate_limit_rps: 0")

    def test_pipeline_config_rejects_negative_rate_limit_rps(self):
        from tram.core.exceptions import ConfigError

        with pytest.raises(ConfigError, match="rate_limit_rps"):
            _make_pipeline("rate_limit_rps: -1")

    def test_pipeline_config_accepts_positive_rate_limit_rps(self):
        config = _make_pipeline("rate_limit_rps: 5")
        assert config.rate_limit_rps == 5

    def test_pipeline_config_rejects_inject_meta_with_thread_workers(self):
        """GH #48 §2.5: inject_meta reads runtime metadata written onto the
        shared transform instance — thread_workers > 1 races it, so it is
        gated like the stateful transforms."""
        from tram.core.exceptions import ConfigError

        with pytest.raises(ConfigError, match="thread_workers"):
            _make_pipeline(
                "thread_workers: 2\n"
                "          transforms:\n"
                "            - type: inject_meta\n"
                "              fields:\n"
                "                source_filename: src_file"
            )

    def test_pipeline_config_accepts_inject_meta_with_single_thread(self):
        config = _make_pipeline(
            "transforms:\n"
            "          - type: inject_meta\n"
            "            fields:\n"
            "              source_filename: src_file"
        )
        assert [t.type for t in config.transforms] == ["inject_meta"]

    def test_pipeline_config_rejects_sink_level_inject_meta_with_thread_workers(self):
        """v1.4.7 review: sink-level inject_meta races thread_workers > 1 the
        same way the top-level one does — per-sink transform instances are
        built once per run (executor _build_sinks) and shared across the
        chunk threads, so the runtime-meta write/read pair races."""
        from tram.core.exceptions import ConfigError

        with pytest.raises(ConfigError, match="thread_workers"):
            _make_pipeline(
                "thread_workers: 2\n"
                "          sinks:\n"
                "            - type: local\n"
                "              path: /tmp/out\n"
                "              transforms:\n"
                "                - type: inject_meta\n"
                "                  fields:\n"
                "                    source_filename: src_file"
            )

    def test_pipeline_config_accepts_sink_level_inject_meta_with_parallel_sinks(self):
        """parallel_sinks without thread_workers > 1 stays allowed: each
        sink's transform instances are only touched by that sink's own
        fan-out thread, so no instance is shared across threads."""
        config = _make_pipeline(
            "parallel_sinks: true\n"
            "          sinks:\n"
            "            - type: local\n"
            "              path: /tmp/out\n"
            "              transforms:\n"
            "                - type: inject_meta\n"
            "                  fields:\n"
            "                    source_filename: src_file"
        )
        assert config.parallel_sinks is True
        assert config.sinks[0].transforms[0].type == "inject_meta"

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

    def test_dry_run_closes_built_sinks_and_source_on_success(self):
        """dry_run builds real sink instances (e.g. ClickHouse) — they must be
        closed so a per-request executor does not leak flush timers/connections."""
        config = _make_pipeline(
            "dlq:\n"
            "            type: local\n"
            "            path: /data/dlq\n"
        )
        executor = PipelineExecutor()
        mock_source = MagicMock()
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
            result = executor.dry_run(config)

        assert result["valid"] is True
        mock_sink.close.assert_called_once()
        mock_dlq.close.assert_called_once()
        mock_source.close.assert_called_once()

    def test_dry_run_closes_built_sinks_when_source_validation_fails(self):
        """On a validation-failure path, previously built instances are still
        closed — a broken source must not leak the built sinks."""
        config = _make_pipeline()
        executor = PipelineExecutor()
        mock_sink = MagicMock()

        with (
            patch.object(executor, "_build_source", side_effect=RuntimeError("bad source")),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.dry_run(config)

        assert result["valid"] is False
        assert any("source" in issue for issue in result["issues"])
        mock_sink.close.assert_called_once()

    def test_dry_run_closes_built_source_when_sink_validation_fails(self):
        """A failing sink constructor must not leak the already-built source."""
        config = _make_pipeline()
        executor = PipelineExecutor()
        mock_source = MagicMock()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", side_effect=RuntimeError("bad sink")),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.dry_run(config)

        assert result["valid"] is False
        assert any("sinks" in issue for issue in result["issues"])
        mock_source.close.assert_called_once()


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
        """on_error=continue should skip bad chunks and continue — and the run
        must NOT report clean success for the failed records: it finishes
        PARTIAL with the loss accounted (V18-01 §7 / plan C)."""
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

        assert result.status == RunStatus.PARTIAL
        assert result.records_skipped > 0
        assert result.records_failed == 1  # the unparseable chunk is lost

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

    def test_retry_rebuild_preserves_run_id(self):
        """The retry rebuild must keep the ORIGINAL run_id so the final
        RunResult (and the worker run-complete callback) carry the run_id the
        trigger returned — otherwise the client's run_id 404s and the manager's
        duplicate-callback dedupe misses (GH #47)."""
        from tram.core.exceptions import TramError

        config = _make_pipeline(
            "on_error: retry\n"
            "          retry_count: 1\n"
            "          retry_delay_seconds: 0"
        )
        executor = PipelineExecutor()

        with (
            patch.object(executor, "_build_source", side_effect=[MagicMock(), MagicMock()]),
            patch.object(executor, "_build_sinks", side_effect=[
                [(MagicMock(), None, [])],
                [(MagicMock(), None, [])],
            ]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_run_batch_chunks", side_effect=[TramError("boom"), None]),
            patch("time.sleep"),
        ):
            result = executor.batch_run(config, run_id="orig-run-1")

        assert result.status == RunStatus.SUCCESS
        assert result.run_id == "orig-run-1"

    def test_retry_exhausted_preserves_run_id(self):
        """Even when every retry attempt fails, the final FAILED RunResult
        carries the original run_id (GH #47)."""
        from tram.core.exceptions import TramError

        config = _make_pipeline(
            "on_error: retry\n"
            "          retry_count: 1\n"
            "          retry_delay_seconds: 0"
        )
        executor = PipelineExecutor()

        with (
            patch.object(executor, "_build_source", side_effect=[MagicMock(), MagicMock()]),
            patch.object(executor, "_build_sinks", side_effect=[
                [(MagicMock(), None, [])],
                [(MagicMock(), None, [])],
            ]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_run_batch_chunks", side_effect=[TramError("boom"), TramError("boom2")]),
            patch("time.sleep"),
        ):
            result = executor.batch_run(config, run_id="orig-run-2")

        assert result.status == RunStatus.FAILED
        assert result.run_id == "orig-run-2"

    def test_batch_run_abort_fails_on_transform_error(self):
        """GH #48 §2.9: on_error=abort with a failing global transform must
        fail the run with a FAILED result — not silently DLQ and continue."""
        config = _make_pipeline("on_error: abort")

        class BoomTransform:
            def apply(self, records):
                raise ValueError("transform boom")

        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps([{"id": "1"}]).encode(), {"source_filename": "t.json"}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = [{"id": "1"}]
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[BoomTransform()]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.FAILED
        assert "transform boom" in (result.error or "")
        mock_sink.write.assert_not_called()

    def test_batch_run_abort_fails_on_sink_transform_error(self):
        """v1.4.7 review (C1): on_error=abort with a failing SINK-level
        transform must fail the run like the global transform path — not
        silently DLQ and continue."""
        config = _make_pipeline("on_error: abort")

        class BoomTransform:
            def apply(self, records):
                raise ValueError("sink transform boom")

        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps([{"id": "1"}]).encode(), {"source_filename": "t.json"}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = [{"id": "1"}]
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(
                executor, "_build_sinks",
                return_value=[(mock_sink, None, [BoomTransform()])],
            ),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.FAILED
        assert "sink transform boom" in (result.error or "")
        mock_sink.write.assert_not_called()

    def test_parallel_sink_fanout_converts_raw_exception_to_tram_error(self):
        """GH #48 §2.16: a non-TramError escaping _write_one_sink (here: a
        serializer failure outside the per-partition retry loop) must be
        folded into TramError so on_error=retry handles it through the
        taxonomy (chunk skipped, error recorded) instead of the raw exception
        exploding the whole run."""
        config = _make_pipeline(
            "parallel_sinks: true\n"
            "          on_error: retry\n"
            "          retry_count: 1\n"
            "          retry_delay_seconds: 0"
        )
        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps([{"id": "1"}]).encode(), {"source_filename": "t.json"}),
        ])
        mock_sink_a = MagicMock()
        mock_sink_b = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = [{"id": "1"}]
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.side_effect = ValueError("serializer exploded")

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
            result = executor.batch_run(config, run_id="r-fan")

        assert result.status == RunStatus.SUCCESS
        assert result.run_id == "r-fan"
        assert any("serializer exploded" in e for e in result.errors)
        mock_sink_a.write.assert_not_called()
        mock_sink_b.write.assert_not_called()

    def test_post_batch_cleanup_ignores_missing_trim_support(self):
        config = _make_pipeline()

        with (
            patch("tram.pipeline.executor.gc.collect") as collect,
            patch("tram.pipeline.executor._try_trim_process_heap", return_value=False) as trim,
        ):
            PipelineExecutor._post_batch_cleanup(config)

        collect.assert_called_once_with()
        trim.assert_called_once_with()

    def test_sink_finalize_failure_degrades_run_not_fails(self):
        """B11 (GH #55): a sink finalize (staged-file rename) failure after all
        chunks were written must not flip the run to FAILED — the run stays
        SUCCESS with the finalize error recorded."""
        config = _make_pipeline()
        executor = PipelineExecutor()

        records = [{"id": "1", "val": "hello"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "file.json"}),
        ])

        mock_sink = MagicMock()
        # The rename fails AFTER the write succeeded.
        mock_sink.finalize_source.side_effect = RuntimeError("rename failed: EACCES")
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

        assert result.status == RunStatus.SUCCESS, (
            "a finalize failure must not fail a run whose data was already written"
        )
        assert result.records_out == 1
        assert any("Sink finalize failed" in e for e in result.errors)
        mock_sink.write.assert_called_once()


class TestBatchDeliveryIntegrity:
    """V18-02 / plan C — batch-path delivery integrity in the executor:

    commit barrier + latched_error gate before success, error-policy
    disposition (abort/retry/continue/dlq), source ack gating (decided units
    only) alongside the legacy finalize() call, and partial outcomes.
    """

    def _run(self, config, mock_source, mock_sink, mock_ser_in, mock_ser_out,
             dlq_sink=None, run_id=None):
        executor = PipelineExecutor()
        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_build_dlq_sink", return_value=dlq_sink),
        ):
            return executor.batch_run(config, run_id=run_id)

    def _one_chunk_run(self, config, sink, *, records=None, source_meta=None,
                       dlq_sink=None):
        records = records if records is not None else [{"id": "1"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), source_meta or {"source_filename": "f.json"}),
        ])
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = json.dumps(records).encode()
        return self._run(config, mock_source, sink, mock_ser_in, mock_ser_out,
                         dlq_sink=dlq_sink), mock_source

    # ── (a) commit or latched failure → no clean success ───────────────────

    def test_commit_failure_abort_never_clean_success(self):
        """A sink commit() failure under abort fails the run — the delivery
        barrier must not be swallowed."""
        config = _make_pipeline("on_error: abort")
        mock_sink = MagicMock()
        mock_sink.commit.side_effect = RuntimeError("commit boom")

        result, mock_source = self._one_chunk_run(config, mock_sink)

        assert result.status == RunStatus.FAILED
        assert "commit boom" in (result.error or "")
        # The unit is undecided: never acked, never finalized as success —
        # the input stays for replay.
        mock_source.ack.assert_not_called()
        assert mock_source.finalize.call_count == 0

    def test_commit_failure_continue_partial(self):
        """Under continue a commit failure records the failure and finishes
        PARTIAL — never clean success, and the unit stays undecided."""
        config = _make_pipeline()  # on_error defaults to continue
        mock_sink = MagicMock()
        mock_sink.commit.side_effect = RuntimeError("commit boom")

        result, mock_source = self._one_chunk_run(config, mock_sink)

        assert result.status == RunStatus.PARTIAL
        assert any("commit boom" in e for e in result.errors)
        mock_source.ack.assert_not_called()

    def test_latched_error_never_clean_success(self):
        """A buffered sink's latched_error() (background flush failure) must
        fail the barrier — no clean success."""
        config = _make_pipeline()
        mock_sink = MagicMock()
        mock_sink.latched_error.return_value = RuntimeError("latched flush failed")

        result, mock_source = self._one_chunk_run(config, mock_sink)

        assert result.status == RunStatus.PARTIAL
        assert any("latched" in e for e in result.errors)
        mock_source.ack.assert_not_called()

    # ── (b) abort retains partial writes and signals the supervisor ────────

    def test_abort_retains_partial_writes_and_fails(self):
        """on_error=abort: records already written stay (no rollback), the run
        reports FAILED to the supervisor, and the in-flight unit is never
        acked and not finalized as success."""
        config = _make_pipeline("on_error: abort")
        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps([{"id": "1"}]).encode(), {"source_filename": "a.json"}),
            (json.dumps([{"id": "2"}]).encode(), {"source_filename": "a.json"}),
        ])
        mock_sink = MagicMock()
        mock_sink.write.side_effect = [None, OSError("disk full")]
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.FAILED
        assert "disk full" in (result.error or "")
        # Partial writes are retained: the first chunk's write succeeded and
        # is not rolled back; the second chunk's write failed and stopped the
        # run (one attempt per chunk, no retry under abort).
        assert mock_sink.write.call_count == 2
        # Abort never acks; the failed unit is finalized with success=False so
        # the input stays for replay.
        mock_source.ack.assert_not_called()
        assert mock_source.finalize.call_args.kwargs["success"] is False

    # ── (c) continue → partial outcome with loss accounting ────────────────

    def test_continue_partial_outcome_with_loss_accounting(self):
        """on_error=continue with a failing sink: PARTIAL terminal outcome,
        per-run loss counters populated, and the failed unit acked with the
        DROPPED disposition (explicit continue policy)."""
        config = _make_pipeline()  # continue
        mock_sink = MagicMock()
        mock_sink.write.side_effect = OSError("sink down")

        result, mock_source = self._one_chunk_run(
            config, mock_sink, records=[{"id": "1"}, {"id": "2"}]
        )

        assert result.status == RunStatus.PARTIAL
        assert result.records_failed == 2
        assert result.records_out == 0
        assert result.dlq_failed == 0
        mock_source.ack.assert_called_once()
        assert mock_source.ack.call_args[0][1] == AckDisposition.DROPPED

    # ── (d) dlq: failed DLQ never acks; success acks DLQ disposition ───────

    def test_dlq_failure_never_acks_and_fails_run(self):
        """A failed DLQ (sink write AND spool both fail) is a failed
        disposition: the run stops, and the unit is never acknowledged."""
        config = _make_pipeline(
            "on_error: dlq\n"
            "          dlq:\n"
            "            type: local\n"
            "            path: /tmp/dlq"
        )
        mock_sink = MagicMock()
        mock_sink.write.side_effect = OSError("sink down")
        mock_dlq = MagicMock()
        mock_dlq.write.side_effect = OSError("dlq down")

        with patch("tram.pipeline.executor._spool_dlq_envelope", return_value=None):
            result, mock_source = self._one_chunk_run(
                config, mock_sink, dlq_sink=mock_dlq
            )

        assert result.status == RunStatus.FAILED
        assert result.dlq_failed == 1
        assert mock_dlq.write.call_count == 1  # attempted, failed
        mock_source.ack.assert_not_called()

    def test_dlq_success_acks_dlq_disposition(self):
        """A durably DLQ'd unit is decided: ack(DLQ) fires and the run (all
        obligations satisfied) reports SUCCESS."""
        config = _make_pipeline(
            "on_error: dlq\n"
            "          dlq:\n"
            "            type: local\n"
            "            path: /tmp/dlq"
        )
        mock_sink = MagicMock()
        mock_sink.write.side_effect = OSError("sink down")
        mock_dlq = MagicMock()

        result, mock_source = self._one_chunk_run(
            config, mock_sink, dlq_sink=mock_dlq
        )

        assert result.status == RunStatus.SUCCESS
        assert result.dlq_succeeded == 1
        mock_dlq.write.assert_called_once()
        mock_source.ack.assert_called_once()
        assert mock_source.ack.call_args[0][1] == AckDisposition.DLQ

    # ── (e) ack only on decided units, with the right disposition ──────────

    def test_delivered_unit_acked_delivered_and_finalized(self):
        """A fully delivered unit is acked DELIVERED and the legacy
        finalize(success=True) still runs (transitional contract: both calls
        happen, finalize on its existing schedule)."""
        config = _make_pipeline()
        mock_sink = MagicMock()

        result, mock_source = self._one_chunk_run(
            config, mock_sink, records=[{"id": "1"}, {"id": "2"}]
        )

        assert result.status == RunStatus.SUCCESS
        assert result.records_out == 2
        mock_source.ack.assert_called_once()
        assert mock_source.ack.call_args[0][1] == AckDisposition.DELIVERED
        # Legacy compat: finalize() still invoked with success=True.
        mock_source.finalize.assert_called_once()
        assert mock_source.finalize.call_args.kwargs["success"] is True

    # ── (g) filtered units count as success ────────────────────────────────

    def test_filtered_unit_acked_filtered_and_success(self):
        """A unit whose records are condition-routed out of every sink is
        successful intentional non-delivery: ack(FILTERED), run SUCCESS."""
        config = _make_pipeline()
        executor = PipelineExecutor()
        records = [{"id": "1", "val": "a"}, {"id": "2", "val": "b"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "f.json"}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, "val == 'zzz'", [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS  # filtering stays success
        assert result.records_skipped == 2
        mock_sink.write.assert_not_called()
        mock_source.ack.assert_called_once()
        assert mock_source.ack.call_args[0][1] == AckDisposition.FILTERED

    # ── (f) finalize still invoked (legacy compat) ─────────────────────────

    def test_finalize_invoked_on_undecided_units_with_success_false(self):
        """Under continue with a commit failure the unit stays undecided (no
        ack) and finalize(success=False) keeps the input for replay."""
        config = _make_pipeline()
        mock_sink = MagicMock()
        mock_sink.commit.side_effect = RuntimeError("commit boom")

        result, mock_source = self._one_chunk_run(config, mock_sink)

        assert result.status == RunStatus.PARTIAL
        mock_source.ack.assert_not_called()
        mock_source.finalize.assert_called_once()
        assert mock_source.finalize.call_args.kwargs["success"] is False

    # ── (5) batch_size boundary: incomplete file never marked done ─────────

    def test_batch_size_boundary_unit_never_acked_nor_finalized(self):
        """Preserve: an incomplete file at a batch_size boundary is never
        marked done — and under the transitional ack gate its unit is never
        acknowledged either (the next run reprocesses it)."""
        config = _make_pipeline("batch_size: 1")
        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps([{"id": "1"}]).encode(), {"source_filename": "a.json", "source_path": "/in/a.json"}),
            (json.dumps([{"id": "2"}]).encode(), {"source_filename": "b.json", "source_path": "/in/b.json"}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_in == 1
        # a.json is the boundary file: fully written but the run stopped at
        # the cap — never marked done (finalize skipped) and never acked.
        assert mock_source.finalize.call_count == 0
        mock_source.ack.assert_not_called()

    def test_threaded_path_acks_decided_units_per_file(self):
        """thread_workers > 1: every fully processed file is a decided unit —
        ack(DELIVERED) fires per file after its chunks drain, and the legacy
        finalize(success=True) still runs (transitional contract)."""
        config = _make_pipeline("thread_workers: 2")
        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps([{"id": "1"}]).encode(), {"source_filename": "a.json", "source_path": "/in/a.json"}),
            (json.dumps([{"id": "2"}]).encode(), {"source_filename": "b.json", "source_path": "/in/b.json"}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_out == 2
        # One ack per file, in read order, both DELIVERED.
        assert mock_source.ack.call_count == 2
        dispositions = [c.args[1] for c in mock_source.ack.call_args_list]
        assert dispositions == [AckDisposition.DELIVERED, AckDisposition.DELIVERED]
        # Legacy compat: finalize still runs for every decided file.
        assert mock_source.finalize.call_count == 2
        for call in mock_source.finalize.call_args_list:
            assert call.kwargs["success"] is True


class TestPipelineExecutorStreamRun:
    """Stream lifecycle: sinks/source must close on every exit path and the
    stop-watcher must not leak on the crash path (GH #46)."""

    def _patched(self, executor, mock_source, mock_sink, mock_dlq=None):
        """Context manager that starts/stops the build patches around a
        stream_run call (mirrors the batch tests' parenthesized form)."""
        from contextlib import contextmanager

        @contextmanager
        def _manager():
            patches = (
                patch.object(executor, "_build_source", return_value=mock_source),
                patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
                patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
                patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
                patch.object(executor, "_build_transforms", return_value=[]),
                patch.object(executor, "_build_dlq_sink", return_value=mock_dlq),
            )
            for p in patches:
                p.start()
            try:
                yield
            finally:
                for p in reversed(patches):
                    p.stop()

        return _manager()

    @staticmethod
    def _rescheduling_timer_sink():
        """ClickHouse-style sink: a self-rescheduling timer stopped only by
        close(). The timer is daemon so a failed assertion cannot keep the
        pytest interpreter alive."""

        class ReschedulingTimerSink:
            def __init__(self):
                self._alive = True
                self._timer: threading.Timer | None = None
                self._reschedule()
                self.closed = False

            def _reschedule(self) -> None:
                if not self._alive:
                    return
                timer = threading.Timer(3600.0, self._reschedule)
                timer.daemon = True
                self._timer = timer
                timer.start()

            def write(self, records, meta):
                pass

            def close(self) -> None:
                self._alive = False
                if self._timer is not None:
                    self._timer.cancel()
                self.closed = True

        return ReschedulingTimerSink()

    def test_stream_run_stop_closes_sinks_and_source(self):
        """A graceful stream stop must close sinks (and the DLQ sink) like the
        batch finally does — the old code leaked e.g. ClickHouse flush timers."""
        config = _make_pipeline()
        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([])
        mock_sink = MagicMock()
        mock_dlq = MagicMock()

        with self._patched(executor, mock_source, mock_sink, mock_dlq):
            executor.stream_run(config, threading.Event())

        mock_sink.close.assert_called_once()
        mock_dlq.close.assert_called_once()
        mock_source.close.assert_called_once()

    def test_stream_run_crash_closes_sinks(self):
        """A crashing stream (source raises) must still close sinks in the
        finally — the exception path is the leak-prone one."""
        config = _make_pipeline()
        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.side_effect = RuntimeError("source exploded")
        mock_sink = MagicMock()

        watchers_before = [
            t for t in threading.enumerate() if t.name == "tram-stop-watcher"
        ]
        with self._patched(executor, mock_source, mock_sink):
            with pytest.raises(RuntimeError, match="source exploded"):
                executor.stream_run(config, threading.Event())

        mock_sink.close.assert_called_once()
        # The stop-watcher must exit on the crash path too (stop_event never
        # fires) — no leaked watcher thread per crash cycle.
        watchers_after = [
            t for t in threading.enumerate() if t.name == "tram-stop-watcher"
        ]
        assert len(watchers_after) == len(watchers_before)

    def test_stream_run_stop_leaves_no_live_sink_timer(self):
        """A sink with a self-rescheduling timer (ClickHouse-style) must have
        its timer stopped by the executor's sink close on a graceful stop."""
        sink = self._rescheduling_timer_sink()
        config = _make_pipeline()
        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.return_value = iter([])

        with self._patched(executor, mock_source, sink):
            executor.stream_run(config, threading.Event())

        assert sink.closed is True
        assert sink._timer is not None
        assert not sink._timer.is_alive()

    def test_stream_run_crash_leaves_no_live_sink_timer(self):
        """The crash path must also stop the sink's self-rescheduling timer."""
        sink = self._rescheduling_timer_sink()
        config = _make_pipeline()
        executor = PipelineExecutor()
        mock_source = MagicMock()
        mock_source.read.side_effect = RuntimeError("source exploded")

        with self._patched(executor, mock_source, sink):
            with pytest.raises(RuntimeError, match="source exploded"):
                executor.stream_run(config, threading.Event())

        assert sink.closed is True
        assert sink._timer is not None
        assert not sink._timer.is_alive()


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


class TestSinkConditionCompileOnce:
    """Compile-once sink conditions (perf follow-up 2026-10-07 §2).

    Sink routing must keep the filter_rows pattern: one parse per condition
    string (cached on the executor instance), thread-local evaluators,
    per-record names binding, and unchanged TramError routing errors.
    """

    def test_condition_eval_error_raises_tram_error(self):
        """Error parity: a condition that fails at evaluation raises TramError
        with the 'Condition eval error:' wording, as before the change."""
        from tram.core.exceptions import TramError

        executor = PipelineExecutor()
        with pytest.raises(TramError, match="Condition eval error: 'missing_field > 5'"):
            _filter_by_condition(
                [{"id": "1"}], "missing_field > 5", _cache=executor._condition_cache
            )

    def test_syntactically_invalid_condition_raises_tram_error(self):
        """A condition that fails to PARSE surfaces with the same observable
        behavior as an eval-time failure: TramError with the 'Condition eval
        error:' wording. (Before the change the parse happened per record
        inside eval; now it happens once and is wrapped identically.)"""
        from tram.core.exceptions import TramError

        executor = PipelineExecutor()
        with pytest.raises(TramError, match="Condition eval error: 'x =='"):
            _filter_by_condition([{"x": 1}], "x ==", _cache=executor._condition_cache)
        # A parse failure must never be cached.
        assert executor._condition_cache == {}

    def test_empty_records_short_circuit_without_parsing(self):
        """Empty-input parity: the per-record-loop implementation never parsed
        the condition for an empty batch, so a syntactically bad condition
        returned [] silently — the compile-once path must do the same."""
        executor = PipelineExecutor()
        result = _filter_by_condition([], "x ==", _cache=executor._condition_cache)
        assert result == []
        # Nothing parsed, nothing cached.
        assert executor._condition_cache == {}

    def test_batch_run_abort_invalid_condition_fails_with_condition_eval_error(self):
        """A syntactically invalid sink condition under on_error=abort fails
        the run with the same 'Condition eval error:' message — the call-site
        change (instance cache) does not alter the abort path."""
        config = _make_pipeline("on_error: abort")
        executor = PipelineExecutor()

        records = [{"id": "1", "val": "a"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "test.json"}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, "val ==", [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.FAILED
        assert "Condition eval error:" in (result.error or "")
        mock_sink.write.assert_not_called()

    def test_condition_parsed_once_per_executor_instance(self):
        """Compile-once: the condition string is parsed a single time and
        cached on the executor instance, not per record or per call."""
        executor = PipelineExecutor()
        records = [{"x": i} for i in range(10)]
        for _ in range(3):
            result = _filter_by_condition(records, "x >= 5", _cache=executor._condition_cache)
            assert [r["x"] for r in result] == [5, 6, 7, 8, 9]
        assert list(executor._condition_cache) == ["x >= 5"]
        # Different conditions are cached independently, and a second executor
        # instance does not share the first's cache.
        result = _filter_by_condition(records, "x < 3", _cache=executor._condition_cache)
        assert [r["x"] for r in result] == [0, 1, 2]
        assert set(executor._condition_cache) == {"x >= 5", "x < 3"}
        assert PipelineExecutor()._condition_cache == {}

    def test_condition_names_isolated_across_threads(self):
        """Names isolation: two threads routing different records through the
        SAME executor and condition concurrently produce per-record results
        with no cross-bleed (thread-local evaluators, per-record names)."""
        executor = PipelineExecutor()
        condition = "keep == 1"
        a_records = [{"id": f"a{i}", "keep": 1} for i in range(25)]
        b_records = [{"id": f"b{i}", "keep": 0} for i in range(25)]
        a_ids = [r["id"] for r in a_records]
        barrier = threading.Barrier(2)
        failures = []
        results = {}

        def route(tag, records, expected_ids):
            try:
                for _ in range(40):
                    barrier.wait(timeout=10)
                    filtered = _filter_by_condition(
                        records, condition, _cache=executor._condition_cache
                    )
                    got = [r["id"] for r in filtered]
                    if got != expected_ids:
                        failures.append(f"{tag}: expected {expected_ids}, got {got}")
                        return
                results[tag] = "ok"
            except Exception as exc:  # barrier timeout or unexpected error
                failures.append(f"{tag}: {exc!r}")

        t1 = threading.Thread(target=route, args=("a", a_records, a_ids))
        t2 = threading.Thread(target=route, args=("b", b_records, []))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert failures == []
        assert results == {"a": "ok", "b": "ok"}
        assert list(executor._condition_cache) == [condition]

    def test_multi_sink_different_conditions_route_mixed_records(self):
        """Routing behavior is unchanged for mixed records across multiple
        sinks with different conditions: each sink receives exactly the
        records its condition selects (batch-run path with the instance
        cache)."""
        config = _make_pipeline()
        executor = PipelineExecutor()

        records = [
            {"id": "1", "val": "a"},
            {"id": "2", "val": "b"},
            {"id": "3", "val": "a"},
        ]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "test.json"}),
        ])
        sink_a = MagicMock()
        sink_b = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        serialized_calls = []

        def fake_serialize(records_in):
            serialized_calls.append(records_in)
            return json.dumps(records_in).encode()

        mock_ser_out = MagicMock()
        mock_ser_out.serialize.side_effect = fake_serialize

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[
                (sink_a, "val == 'a'", []),
                (sink_b, "val == 'b'", []),
            ]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_in == 3
        # records_out is the largest single-sink write (D4 conservative lower
        # bound when sink conditions are disjoint).
        assert result.records_out == 2
        # Sinks are processed in declaration order: a then b.
        assert [r["id"] for r in serialized_calls[0]] == ["1", "3"]
        assert [r["id"] for r in serialized_calls[1]] == ["2"]
        assert list(executor._condition_cache) == ["val == 'a'", "val == 'b'"]
