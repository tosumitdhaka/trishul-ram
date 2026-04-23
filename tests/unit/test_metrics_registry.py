"""Tests for tram/metrics/registry.py — existing and new tram_mgr_* series."""
from __future__ import annotations

import importlib
import sys
from unittest.mock import patch


def _reload_registry():
    if "tram.metrics.registry" in sys.modules:
        del sys.modules["tram.metrics.registry"]
    return importlib.import_module("tram.metrics.registry")


# ── All new series are importable ─────────────────────────────────────────


def test_mgr_series_importable():
    from tram.metrics.registry import (
        MGR_DISPATCH_TOTAL,
        MGR_PIPELINE_STATS_RECEIVED_TOTAL,
        MGR_PLACEMENT_STATUS,
        MGR_RECONCILE_ACTION_TOTAL,
        MGR_REDISPATCH_TOTAL,
        MGR_RUN_COMPLETE_RECEIVED_TOTAL,
        MGR_WORKER_HEALTHY,
        MGR_WORKER_TOTAL,
    )
    assert MGR_DISPATCH_TOTAL is not None
    assert MGR_REDISPATCH_TOTAL is not None
    assert MGR_RECONCILE_ACTION_TOTAL is not None
    assert MGR_PLACEMENT_STATUS is not None
    assert MGR_WORKER_HEALTHY is not None
    assert MGR_WORKER_TOTAL is not None
    assert MGR_RUN_COMPLETE_RECEIVED_TOTAL is not None
    assert MGR_PIPELINE_STATS_RECEIVED_TOTAL is not None


# ── No-op behaviour when prometheus_client is absent ──────────────────────


def test_mgr_series_are_noop_when_prometheus_absent():
    with patch.dict(sys.modules, {"prometheus_client": None}):
        registry = _reload_registry()

    # Should not raise; no-op objects silently accept calls
    registry.MGR_DISPATCH_TOTAL.labels(pipeline="p", result="accepted").inc()
    registry.MGR_REDISPATCH_TOTAL.labels(pipeline="p").inc()
    registry.MGR_RECONCILE_ACTION_TOTAL.labels(pipeline="p", action="mark_stale").inc()
    registry.MGR_PLACEMENT_STATUS.labels(pipeline="p", status="running").set(1)
    registry.MGR_WORKER_HEALTHY.set(2)
    registry.MGR_WORKER_TOTAL.set(3)
    registry.MGR_RUN_COMPLETE_RECEIVED_TOTAL.labels(pipeline="p", status="success").inc()
    registry.MGR_PIPELINE_STATS_RECEIVED_TOTAL.inc()
    assert registry._PROMETHEUS_AVAILABLE is False
