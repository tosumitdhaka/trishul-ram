"""Tests for per-sink retry logic in PipelineExecutor."""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, call, patch

import pytest

from tram.core.context import PipelineRunContext
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


def _ctx():
    return PipelineRunContext(pipeline_name="retry-test")


def _executor():
    return PipelineExecutor()


def _make_ser(records):
    s = MagicMock()
    s.parse.return_value = records
    s.serialize.return_value = b'[{"x":1}]'
    return s


def _sink_tuple(sink, retry_count=0, retry_delay_seconds=0.0, circuit_breaker_threshold=0, condition=None):
    cfg = MagicMock()
    cfg.retry_count = retry_count
    cfg.retry_delay_seconds = retry_delay_seconds
    cfg.circuit_breaker_threshold = circuit_breaker_threshold
    return (sink, condition, [], cfg)


class TestPerSinkRetry:
    def test_no_retry_on_success(self):
        """Sink succeeds on first try — no retries needed."""
        executor = _executor()
        mock_sink = MagicMock()
        sinks = [_sink_tuple(mock_sink, retry_count=2)]
        ctx = _ctx()

        ser = _make_ser([{"x": 1}])
        with patch("time.sleep"):
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx, "continue")

        assert mock_sink.write.call_count == 1

    def test_retry_once_on_failure_then_success(self):
        """Sink fails once then succeeds on retry."""
        executor = _executor()
        mock_sink = MagicMock()
        mock_sink.write.side_effect = [RuntimeError("fail"), None]
        sinks = [_sink_tuple(mock_sink, retry_count=2, retry_delay_seconds=0.0)]
        ctx = _ctx()

        ser = _make_ser([{"x": 1}])
        with patch("time.sleep"):
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx, "continue")

        assert mock_sink.write.call_count == 2

    def test_retry_exhausted_routes_to_dlq(self):
        """All retries fail → DLQ receives record."""
        executor = _executor()
        mock_sink = MagicMock()
        mock_sink.write.side_effect = RuntimeError("always fail")
        dlq_sink = MagicMock()
        sinks = [_sink_tuple(mock_sink, retry_count=1, retry_delay_seconds=0.0)]
        ctx = _ctx()

        ser = _make_ser([{"x": 1}])
        with patch("time.sleep"):
            executor._process_chunk(
                b"[]", {}, ser, [], ser, sinks, ctx, "continue", dlq_sink=dlq_sink
            )

        # Tried 2 times total (initial + 1 retry)
        assert mock_sink.write.call_count == 2
        dlq_sink.write.assert_called_once()

    def test_retry_on_abort_raises_after_exhaustion(self):
        """on_error=abort raises TramError after retries exhausted."""
        from tram.core.exceptions import TramError

        executor = _executor()
        mock_sink = MagicMock()
        mock_sink.write.side_effect = RuntimeError("fail")
        sinks = [_sink_tuple(mock_sink, retry_count=1, retry_delay_seconds=0.0)]
        ctx = _ctx()

        ser = _make_ser([{"x": 1}])
        with patch("time.sleep"), pytest.raises(TramError):
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx, "abort")

    def test_retry_count_zero_means_no_retry(self):
        """retry_count=0 → only one attempt, no retry."""
        executor = _executor()
        mock_sink = MagicMock()
        mock_sink.write.side_effect = RuntimeError("fail")
        sinks = [_sink_tuple(mock_sink, retry_count=0)]
        ctx = _ctx()

        ser = _make_ser([{"x": 1}])
        executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx, "continue")

        assert mock_sink.write.call_count == 1

    def test_retry_delay_applied_between_attempts(self):
        """Exponential back-off delay is applied between retries."""
        executor = _executor()
        mock_sink = MagicMock()
        mock_sink.write.side_effect = [RuntimeError("fail"), None]
        sinks = [_sink_tuple(mock_sink, retry_count=3, retry_delay_seconds=1.0)]
        ctx = _ctx()

        ser = _make_ser([{"x": 1}])
        with patch("time.sleep") as mock_sleep, patch("random.uniform", return_value=0):
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx, "continue")

        # First failure: delay = 1.0 * 2^0 + 0 = 1.0
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == pytest.approx(1.0, abs=0.2)

    def test_retry_resets_on_second_successful_write(self):
        """Circuit breaker state resets on success after failure."""
        executor = _executor()
        mock_sink = MagicMock()
        mock_sink.write.side_effect = [RuntimeError("fail"), None, None]
        sinks = [_sink_tuple(mock_sink, retry_count=1, circuit_breaker_threshold=3)]
        ctx1 = _ctx()
        ctx2 = _ctx()

        ser = _make_ser([{"x": 1}])
        with patch("time.sleep"):
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx1, "continue")
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx2, "continue")

        # Second chunk: no errors
        assert ctx2.errors == []

    def test_multiple_sinks_retry_independently(self):
        """Two sinks fail independently — each respects its own retry_count."""
        executor = _executor()
        sink_a = MagicMock()
        sink_a.write.side_effect = [RuntimeError("a fail"), None]
        sink_b = MagicMock()
        cfg_a = MagicMock()
        cfg_a.retry_count = 1
        cfg_a.retry_delay_seconds = 0.0
        cfg_a.circuit_breaker_threshold = 0
        cfg_b = MagicMock()
        cfg_b.retry_count = 0
        cfg_b.retry_delay_seconds = 0.0
        cfg_b.circuit_breaker_threshold = 0
        sinks = [(sink_a, None, [], cfg_a), (sink_b, None, [], cfg_b)]
        ctx = _ctx()
        ser = _make_ser([{"x": 1}])

        with patch("time.sleep"):
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx, "continue")

        assert sink_a.write.call_count == 2
        assert sink_b.write.call_count == 1
