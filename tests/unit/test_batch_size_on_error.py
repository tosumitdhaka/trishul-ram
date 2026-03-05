"""Tests for batch_size cap and on_error='dlq' features (v0.9.0)."""

from __future__ import annotations

import json
import textwrap
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from tram.core.context import PipelineRunContext, RunStatus
from tram.models.pipeline import PipelineConfig
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_pipeline(extra_yaml: str = "") -> PipelineConfig:
    yaml_text = textwrap.dedent(f"""
        pipeline:
          name: bs-test
          source:
            type: local
            path: /tmp/in
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: local
            path: /tmp/out
          {extra_yaml}
    """)
    return load_pipeline_from_yaml(yaml_text)


def _make_pipeline_with_dlq(extra_yaml: str = "") -> PipelineConfig:
    yaml_text = textwrap.dedent(f"""
        pipeline:
          name: bs-dlq
          source:
            type: local
            path: /tmp/in
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: local
            path: /tmp/out
          dlq:
            type: local
            path: /tmp/dlq
          {extra_yaml}
    """)
    return load_pipeline_from_yaml(yaml_text)


# ── batch_size model tests ────────────────────────────────────────────────


class TestBatchSizeModel:
    def test_batch_size_accepts_positive_int(self):
        cfg = PipelineConfig.model_validate({
            "name": "test",
            "source": {"type": "local", "path": "/tmp"},
            "serializer_in": {"type": "json"},
            "serializer_out": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "batch_size": 100,
        })
        assert cfg.batch_size == 100

    def test_batch_size_accepts_none(self):
        cfg = PipelineConfig.model_validate({
            "name": "test",
            "source": {"type": "local", "path": "/tmp"},
            "serializer_in": {"type": "json"},
            "serializer_out": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "batch_size": None,
        })
        assert cfg.batch_size is None

    def test_batch_size_defaults_to_none(self):
        cfg = PipelineConfig.model_validate({
            "name": "test",
            "source": {"type": "local", "path": "/tmp"},
            "serializer_in": {"type": "json"},
            "serializer_out": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
        })
        assert cfg.batch_size is None


# ── batch_size executor tests ─────────────────────────────────────────────


class TestBatchSizeExecution:
    def test_batch_size_limits_records_processed(self):
        """With batch_size=2 and source yielding 5 single-record chunks,
        only 2 records should be processed."""
        config = _make_pipeline("batch_size: 2")
        executor = PipelineExecutor()

        # 5 chunks, each with 1 record
        chunks = [
            (json.dumps([{"idx": i}]).encode(), {"f": f"{i}.json"})
            for i in range(5)
        ]
        mock_source = MagicMock()
        mock_source.read.return_value = iter(chunks)

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        # Each parse call returns exactly 1 record
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_build_dlq_sink", return_value=None),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        # Should have stopped at or before batch_size
        assert result.records_in <= 2
        assert mock_sink.write.call_count <= 2

    def test_batch_size_none_processes_all(self):
        """With batch_size=None, all 5 chunks should be processed."""
        config = _make_pipeline()
        assert config.batch_size is None

        executor = PipelineExecutor()

        chunks = [
            (json.dumps([{"idx": i}]).encode(), {"f": f"{i}.json"})
            for i in range(5)
        ]
        mock_source = MagicMock()
        mock_source.read.return_value = iter(chunks)

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
            patch.object(executor, "_build_dlq_sink", return_value=None),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_in == 5
        assert mock_sink.write.call_count == 5


# ── on_error='dlq' model validation ──────────────────────────────────────


class TestOnErrorDlqModel:
    def test_on_error_dlq_with_dlq_configured_validates_ok(self):
        cfg = PipelineConfig.model_validate({
            "name": "test",
            "source": {"type": "local", "path": "/tmp"},
            "serializer_in": {"type": "json"},
            "serializer_out": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "on_error": "dlq",
            "dlq": {"type": "local", "path": "/tmp/dlq"},
        })
        assert cfg.on_error == "dlq"
        assert cfg.dlq is not None

    def test_on_error_dlq_without_dlq_raises_validation_error(self):
        with pytest.raises(ValidationError, match="on_error='dlq'"):
            PipelineConfig.model_validate({
                "name": "test",
                "source": {"type": "local", "path": "/tmp"},
                "serializer_in": {"type": "json"},
                "serializer_out": {"type": "json"},
                "sinks": [{"type": "local", "path": "/tmp/out"}],
                "on_error": "dlq",
                # no dlq configured
            })

    def test_on_error_continue_without_dlq_is_valid(self):
        cfg = PipelineConfig.model_validate({
            "name": "test",
            "source": {"type": "local", "path": "/tmp"},
            "serializer_in": {"type": "json"},
            "serializer_out": {"type": "json"},
            "sinks": [{"type": "local", "path": "/tmp/out"}],
            "on_error": "continue",
        })
        assert cfg.on_error == "continue"


# ── on_error='dlq' executor behaviour ────────────────────────────────────


class TestOnErrorDlqExecution:
    def test_on_error_dlq_sink_failure_writes_to_dlq_and_continues(self):
        """When on_error='dlq' and sink raises, the record goes to DLQ
        and processing continues (same behaviour as 'continue' with dlq_sink)."""
        config = _make_pipeline_with_dlq("on_error: dlq")
        executor = PipelineExecutor()

        mock_sink = MagicMock()
        mock_sink.write.side_effect = IOError("network error")

        mock_dlq = MagicMock()

        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = [{"id": 1}]
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"

        sinks = [(mock_sink, None, [])]
        ctx = PipelineRunContext(pipeline_name="bs-dlq")

        executor._process_chunk(
            b"[]", {}, mock_ser_in, [], mock_ser_out,
            sinks, ctx, "continue", dlq_sink=mock_dlq,
        )

        # DLQ should have been written to
        mock_dlq.write.assert_called_once()
        # Context should have recorded the DLQ write
        assert ctx.dlq_count == 1

    def test_on_error_dlq_parse_failure_writes_to_dlq(self):
        """Parse failure with DLQ configured writes envelope to DLQ."""
        config = _make_pipeline_with_dlq("on_error: dlq")
        executor = PipelineExecutor()

        mock_sink = MagicMock()
        mock_dlq = MagicMock()

        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = ValueError("bad JSON")
        mock_ser_out = MagicMock()

        sinks = [(mock_sink, None, [])]
        ctx = PipelineRunContext(pipeline_name="bs-dlq")

        executor._process_chunk(
            b"bad data", {}, mock_ser_in, [], mock_ser_out,
            sinks, ctx, "continue", dlq_sink=mock_dlq,
        )

        mock_dlq.write.assert_called_once()
        import json as _json
        envelope = _json.loads(mock_dlq.write.call_args[0][0])
        assert envelope["_stage"] == "parse"
        assert ctx.dlq_count == 1
