"""Tests for PipelineExecutor — batch and dry run modes."""

from __future__ import annotations

import json
import textwrap
from unittest.mock import MagicMock, patch

import pytest

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
            patch.object(executor, "_build_sink", return_value=mock_sink),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_in == 2
        assert result.records_out == 2
        assert result.records_skipped == 0
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
            patch.object(executor, "_build_sink", return_value=mock_sink),
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
            patch.object(executor, "_build_sink", return_value=mock_sink),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_skipped > 0


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
            patch.object(executor, "_build_sink", return_value=mock_sink),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[t1, t2, t3]),
        ):
            executor.batch_run(config)

        assert call_order == ["t1", "t2", "t3"]
