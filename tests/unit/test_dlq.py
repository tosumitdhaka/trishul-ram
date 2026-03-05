"""Tests for Dead-Letter Queue (DLQ) functionality in PipelineExecutor."""

from __future__ import annotations

import base64
import json
import textwrap
from unittest.mock import MagicMock, call, patch

import pytest

from tram.core.context import PipelineRunContext, RunStatus
from tram.pipeline.executor import PipelineExecutor, _write_dlq_envelope
from tram.pipeline.loader import load_pipeline_from_yaml


def _make_pipeline(extra_yaml: str = "") -> object:
    yaml_text = textwrap.dedent(f"""
        pipeline:
          name: test-dlq
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


def _make_pipeline_with_dlq() -> object:
    yaml_text = textwrap.dedent("""
        pipeline:
          name: test-dlq
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
    """)
    return load_pipeline_from_yaml(yaml_text)


class TestBuildDlqSink:
    def test_returns_none_when_no_dlq_configured(self):
        config = _make_pipeline()
        executor = PipelineExecutor()
        assert executor._build_dlq_sink(config) is None

    def test_returns_sink_when_dlq_configured(self):
        config = _make_pipeline_with_dlq()
        executor = PipelineExecutor()

        mock_sink_cls = MagicMock()
        mock_sink_instance = MagicMock()
        mock_sink_cls.return_value = mock_sink_instance

        with patch("tram.pipeline.executor.get_sink", return_value=mock_sink_cls):
            result = executor._build_dlq_sink(config)

        assert result is mock_sink_instance


class TestDlqEnvelopeHelper:
    def test_write_dlq_envelope_parse_stage(self):
        ctx = PipelineRunContext(pipeline_name="test-pipe", run_id="abc123")
        dlq_sink = MagicMock()
        raw = b'{"bad json'

        _write_dlq_envelope(dlq_sink, ctx, stage="parse", error="parse failed", raw=raw)

        dlq_sink.write.assert_called_once()
        written_bytes = dlq_sink.write.call_args[0][0]
        envelope = json.loads(written_bytes)

        assert envelope["_error"] == "parse failed"
        assert envelope["_stage"] == "parse"
        assert envelope["_pipeline"] == "test-pipe"
        assert envelope["_run_id"] == "abc123"
        assert envelope["record"] is None
        assert envelope["raw"] == base64.b64encode(raw).decode()

    def test_write_dlq_envelope_transform_stage(self):
        ctx = PipelineRunContext(pipeline_name="p", run_id="xyz")
        dlq_sink = MagicMock()
        record = {"id": 1, "val": "foo"}

        _write_dlq_envelope(dlq_sink, ctx, stage="transform", error="boom", record=record)

        written_bytes = dlq_sink.write.call_args[0][0]
        envelope = json.loads(written_bytes)

        assert envelope["_stage"] == "transform"
        assert envelope["record"] == record
        assert "raw" not in envelope

    def test_write_dlq_envelope_sink_stage(self):
        ctx = PipelineRunContext(pipeline_name="p", run_id="xyz")
        dlq_sink = MagicMock()
        records = [{"a": 1}, {"b": 2}]

        _write_dlq_envelope(dlq_sink, ctx, stage="sink", error="write error", record=records)

        written_bytes = dlq_sink.write.call_args[0][0]
        envelope = json.loads(written_bytes)

        assert envelope["_stage"] == "sink"
        assert envelope["record"] == records

    def test_write_dlq_envelope_swallows_write_error(self, caplog):
        ctx = PipelineRunContext(pipeline_name="p", run_id="x")
        dlq_sink = MagicMock()
        dlq_sink.write.side_effect = RuntimeError("disk full")

        import logging
        with caplog.at_level(logging.ERROR):
            _write_dlq_envelope(dlq_sink, ctx, stage="sink", error="e")

        assert "DLQ write failed" in caplog.text


class TestDlqInProcessChunk:
    def _run_chunk(self, config, raw, meta, parse_records=None,
                   transform_raises=None, sink_raises=None, dlq_sink=None):
        executor = PipelineExecutor()

        mock_ser_in = MagicMock()
        if parse_records is not None:
            mock_ser_in.parse.return_value = parse_records
        else:
            mock_ser_in.parse.side_effect = ValueError("bad parse")

        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b'[]'

        mock_transform = MagicMock()
        if transform_raises:
            mock_transform.apply.side_effect = transform_raises
        else:
            mock_transform.apply.side_effect = lambda r: r

        mock_sink = MagicMock()
        if sink_raises:
            mock_sink.write.side_effect = sink_raises

        sinks = [(mock_sink, None, [])]
        transforms = [mock_transform] if parse_records is not None else []
        ctx = PipelineRunContext(pipeline_name="test-dlq")

        executor._process_chunk(
            raw, meta, mock_ser_in, transforms, mock_ser_out,
            sinks, ctx, "continue", dlq_sink=dlq_sink,
        )
        return ctx, mock_sink

    def test_dlq_written_on_parse_failure(self):
        config = _make_pipeline_with_dlq()
        dlq_sink = MagicMock()
        raw = b'garbage'

        ctx, _ = self._run_chunk(config, raw, {}, dlq_sink=dlq_sink)

        dlq_sink.write.assert_called_once()
        envelope = json.loads(dlq_sink.write.call_args[0][0])
        assert envelope["_stage"] == "parse"
        assert envelope["raw"] == base64.b64encode(raw).decode()
        assert ctx.dlq_count == 1

    def test_dlq_written_on_transform_failure(self):
        config = _make_pipeline_with_dlq()
        dlq_sink = MagicMock()
        records = [{"id": 1}, {"id": 2}]

        ctx, _ = self._run_chunk(
            config, b'[]', {}, parse_records=records,
            transform_raises=RuntimeError("bad transform"),
            dlq_sink=dlq_sink,
        )

        # One DLQ entry per failing record
        assert dlq_sink.write.call_count == 2
        envelope = json.loads(dlq_sink.write.call_args_list[0][0][0])
        assert envelope["_stage"] == "transform"
        assert ctx.dlq_count == 2

    def test_dlq_written_on_sink_failure(self):
        dlq_sink = MagicMock()
        records = [{"id": 1}]

        ctx, mock_sink = self._run_chunk(
            None, b'[]', {}, parse_records=records,
            sink_raises=IOError("network error"),
            dlq_sink=dlq_sink,
        )

        dlq_sink.write.assert_called_once()
        envelope = json.loads(dlq_sink.write.call_args[0][0])
        assert envelope["_stage"] == "sink"
        assert ctx.dlq_count == 1

    def test_no_dlq_sink_parse_failure_no_crash(self):
        ctx, _ = self._run_chunk(None, b'bad', {}, dlq_sink=None)
        assert ctx.dlq_count == 0

    def test_dlq_count_incremented_correctly(self):
        dlq_sink = MagicMock()
        records = [{"id": 1}, {"id": 2}, {"id": 3}]

        ctx, _ = self._run_chunk(
            None, b'[]', {}, parse_records=records,
            transform_raises=ValueError("oops"),
            dlq_sink=dlq_sink,
        )

        assert ctx.dlq_count == 3
