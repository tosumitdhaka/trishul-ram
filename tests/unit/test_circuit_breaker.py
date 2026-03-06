"""Tests for per-sink circuit breaker in PipelineExecutor."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from tram.core.context import PipelineRunContext
from tram.pipeline.executor import PipelineExecutor


def _ctx(name="cb-test"):
    return PipelineRunContext(pipeline_name=name)


def _sink_tuple(sink, threshold=0, retry_count=0):
    cfg = MagicMock()
    cfg.retry_count = retry_count
    cfg.retry_delay_seconds = 0.0
    cfg.circuit_breaker_threshold = threshold
    return (sink, None, [], cfg)


def _ser(records=None):
    if records is None:
        records = [{"x": 1}]
    s = MagicMock()
    s.parse.return_value = records
    s.serialize.return_value = b'[{"x":1}]'
    return s


class TestCircuitBreaker:
    def test_no_circuit_breaker_when_threshold_zero(self):
        """threshold=0 means circuit breaker is disabled."""
        executor = PipelineExecutor()
        sink = MagicMock()
        sink.write.side_effect = RuntimeError("fail")
        sinks = [_sink_tuple(sink, threshold=0)]
        ser = _ser()

        # Two calls: both should invoke write (no circuit open)
        for _ in range(2):
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, _ctx(), "continue")

        assert sink.write.call_count == 2

    def test_circuit_opens_after_threshold_failures(self):
        """After threshold failures, sink is skipped for the open window."""
        executor = PipelineExecutor()
        sink = MagicMock()
        sink.write.side_effect = RuntimeError("fail")
        sinks = [_sink_tuple(sink, threshold=2)]
        ser = _ser()

        # First 2 calls: write attempted (threshold not yet reached after first)
        executor._process_chunk(b"[]", {}, ser, [], ser, sinks, _ctx(), "continue")
        executor._process_chunk(b"[]", {}, ser, [], ser, sinks, _ctx(), "continue")

        # Circuit now open — 3rd call should skip write
        ctx_after = _ctx()
        with patch("time.monotonic", return_value=time.monotonic() + 1):
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx_after, "continue")

        # write should still be called exactly twice (open circuit skips 3rd)
        assert sink.write.call_count == 2
        assert "Circuit breaker open" in ctx_after.errors[0]

    def test_circuit_resets_on_success(self):
        """A successful write resets the failure count."""
        executor = PipelineExecutor()
        sink = MagicMock()
        # Fail once, then succeed
        sink.write.side_effect = [RuntimeError("fail"), None]
        sinks = [_sink_tuple(sink, threshold=3)]
        ser = _ser()

        executor._process_chunk(b"[]", {}, ser, [], ser, sinks, _ctx(), "continue")
        executor._process_chunk(b"[]", {}, ser, [], ser, sinks, _ctx(), "continue")

        # After success, failure count should be 0
        with executor._cb_lock:
            failures, _ = executor._cb_state.get(id(sink), (0, 0.0))
        assert failures == 0

    def test_circuit_open_window_expires(self):
        """After open window expires, circuit closes and allows writes again."""
        executor = PipelineExecutor()
        sink = MagicMock()
        sink.write.side_effect = RuntimeError("fail")
        sinks = [_sink_tuple(sink, threshold=1)]
        ser = _ser()

        # Trip the circuit
        executor._process_chunk(b"[]", {}, ser, [], ser, sinks, _ctx(), "continue")

        # Simulate 61s elapsed (window expired)
        sink.write.side_effect = [None]  # succeeds this time
        with patch("time.monotonic", return_value=time.monotonic() + 61):
            ctx = _ctx()
            executor._process_chunk(b"[]", {}, ser, [], ser, sinks, ctx, "continue")

        # Write should have been attempted again
        assert sink.write.call_count == 2
