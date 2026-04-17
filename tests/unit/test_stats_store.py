"""Unit tests for manager-side active stats storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tram.agent.stats_store import StatsStore
from tram.api.routers.internal import PipelineStatsPayload


def _payload(**overrides) -> PipelineStatsPayload:
    now = datetime.now(UTC)
    data = {
        "worker_id": "w0",
        "pipeline_name": "pipe-a",
        "run_id": "run-1",
        "schedule_type": "stream",
        "uptime_seconds": 10.0,
        "timestamp": now,
        "records_in": 1,
        "records_out": 1,
        "records_skipped": 0,
        "dlq_count": 0,
        "error_count": 0,
        "bytes_in": 10,
        "bytes_out": 20,
        "errors_last_window": [],
        "is_final": False,
    }
    data.update(overrides)
    return PipelineStatsPayload(**data)


class TestStatsStore:
    def test_update_and_get_by_run_id(self):
        store = StatsStore(interval=30)
        payload = _payload()
        store.update(payload)
        assert store.get_by_run_id("run-1") == payload

    def test_remove_is_noop_for_unknown_run(self):
        store = StatsStore(interval=30)
        store.remove("missing")
        assert store.get_by_run_id("missing") is None

    def test_for_worker_excludes_stale_entries(self):
        store = StatsStore(interval=10)
        fresh = _payload(run_id="fresh")
        stale = _payload(
            run_id="stale",
            timestamp=datetime.now(UTC) - timedelta(seconds=31),
        )
        store.update(fresh)
        store.update(stale)
        assert [entry.run_id for entry in store.for_worker("w0")] == ["fresh"]
        assert store.get_by_run_id("stale") == stale

    def test_for_pipeline_excludes_stale_entries(self):
        store = StatsStore(interval=10)
        store.update(_payload(run_id="fresh"))
        store.update(_payload(
            run_id="stale",
            timestamp=datetime.now(UTC) - timedelta(seconds=31),
        ))
        assert [entry.run_id for entry in store.for_pipeline("pipe-a")] == ["fresh"]

    def test_all_active_returns_non_stale_only(self):
        store = StatsStore(interval=10)
        store.update(_payload(run_id="fresh"))
        store.update(_payload(
            run_id="stale",
            timestamp=datetime.now(UTC) - timedelta(seconds=31),
        ))
        assert [entry.run_id for entry in store.all_active()] == ["fresh"]
