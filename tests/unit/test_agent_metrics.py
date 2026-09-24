"""Unit tests for worker-side PipelineStats (tram/agent/metrics.py)."""

from __future__ import annotations

from collections import deque

from tram.agent.metrics import _ERRORS_WINDOW_MAXLEN, PipelineStats


def _stats(**overrides) -> PipelineStats:
    data = {
        "run_id": "run-1",
        "pipeline_name": "pipe-a",
        "schedule_type": "stream",
    }
    data.update(overrides)
    return PipelineStats(**data)


class TestErrorsLastWindowBounded:
    """B7 (GH #55): the error tail must stay bounded between 30s snapshots —
    an error-storm stream must not accumulate tens of MB of strings."""

    def test_window_is_capped_at_maxlen(self):
        stats = _stats()
        errors = [f"error-{i}" for i in range(20)]

        # 50 increments × 10 errors each would previously grow to 500 entries.
        for _ in range(50):
            stats.increment(errors=errors)

        assert len(stats.errors_last_window) == _ERRORS_WINDOW_MAXLEN
        # Newest errors are kept (deque maxlen evicts from the left).
        assert stats.errors_last_window[-1] == "error-19"

    def test_window_type_is_bounded_deque(self):
        stats = _stats()
        stats.increment(errors=["boom"])
        assert isinstance(stats.errors_last_window, deque)
        assert stats.errors_last_window.maxlen == _ERRORS_WINDOW_MAXLEN

    def test_snapshot_and_reset_still_round_trips(self):
        stats = _stats()
        stats.increment(records_in=5, errors=["boom", "bam"])

        snapshot = stats.snapshot_and_reset_window()

        assert snapshot["errors_last_window"] == ["boom", "bam"]
        assert stats.errors_last_window == deque()
        assert snapshot["records_in"] == 5

    def test_error_count_tracks_total_not_window(self):
        stats = _stats()
        for _ in range(40):
            stats.increment(errors=["e"])
        # error_count is cumulative even though the window is capped.
        assert stats.error_count == 40
        assert len(stats.errors_last_window) == 40

    def test_error_storm_bounded_between_snapshots(self):
        """40 increments × 10 errors each = 400 strings; the window holds
        only the newest _ERRORS_WINDOW_MAXLEN."""
        stats = _stats()
        for i in range(40):
            stats.increment(errors=[f"e-{i}-{j}" for j in range(10)])
        assert len(stats.errors_last_window) == _ERRORS_WINDOW_MAXLEN
        assert stats.errors_last_window[-1] == "e-39-9"

    def test_reset_clears_window(self):
        stats = _stats()
        stats.increment(errors=["boom"] * 5)
        stats.reset()
        assert stats.errors_last_window == deque()