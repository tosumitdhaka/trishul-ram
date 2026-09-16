"""Tests for GET /api/pipelines/{name}/placement — standalone synthetic view (v1.3.2)."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.agent.stats_store import StatsStore
from tram.api.routers.internal import PipelineStatsPayload
from tram.api.routers.pipelines import router as pipelines_router
from tram.core.exceptions import PipelineNotFoundError
from tram.pipeline.loader import load_pipeline_from_yaml

_STREAM_YAML = """\
name: my-stream
schedule:
  type: stream
source:
  type: webhook
  path: /ingest
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""

_BATCH_YAML = """\
name: my-batch
schedule:
  type: interval
  interval_seconds: 3600
source:
  type: local
  path: /dev/null
  file_pattern: "*.noop"
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""


def _make_app(controller, stats_store=None):
    app = FastAPI()
    app.include_router(pipelines_router)
    app.state.controller = controller
    app.state.stats_store = stats_store
    return app


def _make_pipeline_state(yaml_str):
    config = load_pipeline_from_yaml(yaml_str)
    state = MagicMock()
    state.config = config
    return state


def _make_stats_entry(pipeline_name: str, run_id: str, records_in: int = 5):
    return PipelineStatsPayload(
        worker_id="node-1",
        pipeline_name=pipeline_name,
        run_id=run_id,
        schedule_type="stream",
        uptime_seconds=60.0,
        timestamp=datetime.now(UTC),
        records_in=records_in,
        records_out=records_in,
    )


# ── standalone stream active → synthetic single-slot view ─────────────────


def test_placement_standalone_stream_active_returns_synthetic_view():
    store = StatsStore(interval=30)
    entry = _make_stats_entry("my-stream", "run-abc", records_in=42)
    store.update(entry)

    ctrl = MagicMock()
    ctrl.get.return_value = _make_pipeline_state(_STREAM_YAML)
    ctrl.get_active_broadcast_placements.return_value = []
    ctrl._worker_pool = None

    client = TestClient(_make_app(ctrl, stats_store=store), raise_server_exceptions=False)
    resp = client.get("/api/pipelines/my-stream/placement")

    assert resp.status_code == 200
    data = resp.json()
    assert data["pipeline_name"] == "my-stream"
    assert data["placement_group_id"] is None
    assert data["status"] == "running"
    assert data["target_count"] == 1
    assert data["started_at"] is None
    assert data["slot_count"] == 1
    assert data["active_slots"] == 1
    assert data["records_in"] == 42
    assert len(data["slots"]) == 1
    assert data["slots"][0]["stats"]["stale"] is False


# ── standalone stream stopped → 404 ───────────────────────────────────────


def test_placement_standalone_stream_stopped_returns_404():
    store = StatsStore(interval=30)
    # No entry in store

    ctrl = MagicMock()
    ctrl.get.return_value = _make_pipeline_state(_STREAM_YAML)
    ctrl.get_active_broadcast_placements.return_value = []
    ctrl._worker_pool = None

    client = TestClient(_make_app(ctrl, stats_store=store), raise_server_exceptions=False)
    resp = client.get("/api/pipelines/my-stream/placement")

    assert resp.status_code == 404


# ── non-stream pipeline → 404 ─────────────────────────────────────────────


def test_placement_batch_pipeline_returns_404():
    store = StatsStore(interval=30)

    ctrl = MagicMock()
    ctrl.get.return_value = _make_pipeline_state(_BATCH_YAML)
    ctrl.get_active_broadcast_placements.return_value = []
    ctrl._worker_pool = None

    client = TestClient(_make_app(ctrl, stats_store=store), raise_server_exceptions=False)
    resp = client.get("/api/pipelines/my-batch/placement")

    assert resp.status_code == 404


# ── pipeline not found → 404 ──────────────────────────────────────────────


def test_placement_pipeline_not_found_returns_404():
    store = StatsStore(interval=30)

    ctrl = MagicMock()
    ctrl.get.side_effect = PipelineNotFoundError("my-missing")
    ctrl.get_active_broadcast_placements.return_value = []

    client = TestClient(_make_app(ctrl, stats_store=store), raise_server_exceptions=False)
    resp = client.get("/api/pipelines/my-missing/placement")

    assert resp.status_code == 404


# ── manager mode without a placement row → synthetic view when stats exist ─


def test_placement_manager_mode_no_placement_with_stats_returns_synthetic_view():
    """The `worker_pool is None` gate is dropped (RCA #17 / plan D.3): a
    manager-mode stream with live stats but no durable placement row (pre-D.2
    dispatch, or the feature flag off) renders the single-slot synthetic view
    instead of 404ing."""
    store = StatsStore(interval=30)
    entry = _make_stats_entry("my-stream", "run-xyz", records_in=7)
    store.update(entry)

    ctrl = MagicMock()
    ctrl.get.return_value = _make_pipeline_state(_STREAM_YAML)
    ctrl.get_active_broadcast_placements.return_value = []
    ctrl._worker_pool = MagicMock()  # non-None → manager mode

    client = TestClient(_make_app(ctrl, stats_store=store), raise_server_exceptions=False)
    resp = client.get("/api/pipelines/my-stream/placement")

    assert resp.status_code == 200
    data = resp.json()
    assert data["placement_group_id"] is None
    assert data["status"] == "running"
    assert data["target_count"] == 1
    assert data["slot_count"] == 1
    assert data["active_slots"] == 1
    assert data["records_in"] == 7
    assert len(data["slots"]) == 1


def test_placement_manager_mode_no_placement_no_stats_returns_404():
    """Manager mode, no placement row and no live stats entry: clean 404 (the
    UI treats a 404 as "no active placement" and hides the card)."""
    store = StatsStore(interval=30)

    ctrl = MagicMock()
    ctrl.get.return_value = _make_pipeline_state(_STREAM_YAML)
    ctrl.get_active_broadcast_placements.return_value = []
    ctrl._worker_pool = MagicMock()

    client = TestClient(_make_app(ctrl, stats_store=store), raise_server_exceptions=False)
    resp = client.get("/api/pipelines/my-stream/placement")

    assert resp.status_code == 404


# ── manager mode with a D.2 count=1 placement row → placement view ─────────


def test_placement_manager_mode_count1_placement_row_served():
    """D.2 (GH #17): a count=1 stream now has a durable 1-slot placement row
    (target_count="1"); the endpoint serves it as the placement view."""
    store = StatsStore(interval=30)
    entry = _make_stats_entry("my-stream", "pg1", records_in=12)
    store.update(entry)

    ctrl = MagicMock()
    ctrl.get.return_value = _make_pipeline_state(_STREAM_YAML)
    ctrl.get_active_broadcast_placements.return_value = [{
        "placement_group_id": "pg1",
        "pipeline_name": "my-stream",
        "status": "running",
        "target_count": "1",
        "started_at": datetime.now(UTC),
        "slots": [{
            "worker_index": 0,
            "worker_id": "w0",
            "worker_url": "http://w0:8766",
            "run_id_prefix": "pg1",
            "current_run_id": "pg1",
            "status": "running",
            "restart_count": 0,
        }],
    }]
    ctrl._worker_pool = MagicMock()
    ctrl._worker_pool.live_streams.return_value = []

    client = TestClient(_make_app(ctrl, stats_store=store), raise_server_exceptions=False)
    resp = client.get("/api/pipelines/my-stream/placement")

    assert resp.status_code == 200
    data = resp.json()
    assert data["placement_group_id"] == "pg1"
    assert data["status"] == "running"
    assert data["target_count"] == "1"
    assert data["slot_count"] == 1
    assert data["active_slots"] == 1
    assert data["records_in"] == 12
    slot = data["slots"][0]
    assert slot["worker_index"] == 0
    assert slot["worker_url"] == "http://w0:8766"
    assert slot["current_run_id"] == "pg1"
    assert slot["stats"]["stale"] is False
