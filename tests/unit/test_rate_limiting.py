"""Tests for token-bucket rate limiter in PipelineExecutor (v0.5.0)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from tram.pipeline.executor import PipelineExecutor
from tram.core.context import PipelineRunContext


def test_rate_limit_no_sleep_first_token():
    """First call should not sleep (bucket starts full)."""
    executor = PipelineExecutor()
    executor._tokens = 1.0
    executor._last_refill = time.monotonic()

    t0 = time.monotonic()
    executor._rate_limit(100.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, "First call should not block"


def test_rate_limit_sleeps_when_empty():
    """When bucket is empty, we should sleep ~1/rps seconds."""
    executor = PipelineExecutor()
    # Drain the bucket
    executor._tokens = 0.0
    executor._last_refill = time.monotonic()

    rps = 10.0  # 100ms per token
    t0 = time.monotonic()
    executor._rate_limit(rps)
    elapsed = time.monotonic() - t0

    # Should sleep roughly 1/rps = 0.1s
    assert elapsed >= 0.05, f"Expected ~0.1s sleep, got {elapsed:.3f}s"
    assert elapsed < 0.5, f"Slept too long: {elapsed:.3f}s"


def test_process_chunk_with_rate_limit():
    """process_chunk passes rate_limit_rps to _rate_limit."""
    executor = PipelineExecutor()

    called_with = []
    original_rate_limit = executor._rate_limit

    def mock_rate_limit(rps):
        called_with.append(rps)

    executor._rate_limit = mock_rate_limit

    sink = MagicMock()
    sink.write = MagicMock()
    serializer_in = MagicMock()
    serializer_in.parse.return_value = [{"x": 1}]
    serializer_out = MagicMock()
    serializer_out.serialize.return_value = b"data"

    ctx = PipelineRunContext(pipeline_name="test")
    executor._process_chunk(
        b"raw", {}, serializer_in, [], serializer_out,
        [(sink, None, [])], ctx, "continue",
        rate_limit_rps=50.0,
    )

    assert 50.0 in called_with


def test_process_chunk_no_rate_limit():
    """Without rate_limit_rps, _rate_limit is not called."""
    executor = PipelineExecutor()
    rate_limit_called = []

    original = executor._rate_limit
    executor._rate_limit = lambda rps: rate_limit_called.append(rps)

    sink = MagicMock()
    sink.write = MagicMock()
    serializer_in = MagicMock()
    serializer_in.parse.return_value = [{"x": 1}]
    serializer_out = MagicMock()
    serializer_out.serialize.return_value = b"data"

    ctx = PipelineRunContext(pipeline_name="test")
    executor._process_chunk(
        b"raw", {}, serializer_in, [], serializer_out,
        [(sink, None, [])], ctx, "continue",
        rate_limit_rps=None,
    )

    assert rate_limit_called == []
