from __future__ import annotations

from datetime import UTC, datetime

from tram.agent.stats_store import StatsStore
from tram.api.routers._stream_views import build_cluster_streams
from tram.api.routers.internal import PipelineStatsPayload


def test_build_cluster_streams_uses_live_worker_streams_when_stats_store_is_empty():
    streams = build_cluster_streams(
        [],
        StatsStore(interval=30),
        live_streams=[
            {
                "worker_url": "http://w0:8766",
                "worker_id": "w0",
                "pipeline_name": "pipe-live",
                "run_id": "run-live",
                "schedule_type": "stream",
                "uptime_seconds": 5.0,
                "stats": {
                    "records_in": 25,
                    "records_out": 20,
                    "records_skipped": 0,
                    "dlq_count": 0,
                    "error_count": 1,
                    "bytes_in": 250,
                    "bytes_out": 200,
                    "errors_last_window": ["minor"],
                },
            }
        ],
    )

    assert len(streams) == 1
    stream = streams[0]
    assert stream["pipeline_name"] == "pipe-live"
    assert stream["active_slots"] == 1
    assert stream["records_out_per_sec"] == 4.0
    assert stream["bytes_out_per_sec"] == 40.0
    assert stream["slots"][0]["stats"]["stale"] is False


def test_build_cluster_streams_prefers_live_slot_stats_over_stale_manager_view():
    store = StatsStore(interval=30)
    stale = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    store.update(
        PipelineStatsPayload(
            worker_id="w0",
            pipeline_name="pipe-a",
            run_id="pg1-w0",
            schedule_type="stream",
            uptime_seconds=10.0,
            timestamp=stale,
            records_in=10,
            records_out=5,
            bytes_in=100,
            bytes_out=50,
        )
    )

    placements = [
        {
            "placement_group_id": "pg1",
            "pipeline_name": "pipe-a",
            "status": "running",
            "target_count": 1,
            "started_at": datetime.now(UTC),
            "slots": [
                {
                    "worker_index": 0,
                    "worker_url": "http://w0:8766",
                    "worker_id": "w0",
                    "run_id_prefix": "pg1-w0",
                    "current_run_id": "pg1-w0",
                    "status": "running",
                    "restart_count": 0,
                }
            ],
        }
    ]

    streams = build_cluster_streams(
        placements,
        store,
        live_streams=[
            {
                "worker_url": "http://w0:8766",
                "worker_id": "w0",
                "pipeline_name": "pipe-a",
                "run_id": "pg1-w0",
                "schedule_type": "stream",
                "uptime_seconds": 4.0,
                "stats": {
                    "records_in": 40,
                    "records_out": 16,
                    "bytes_in": 400,
                    "bytes_out": 160,
                    "error_count": 0,
                },
            }
        ],
    )

    assert len(streams) == 1
    stream = streams[0]
    assert stream["active_slots"] == 1
    assert stream["records_in"] == 40
    assert stream["records_out_per_sec"] == 4.0
    assert stream["slots"][0]["stats"]["stale"] is False
