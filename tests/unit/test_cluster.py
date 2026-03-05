"""Tests for v0.8.0 cluster: NodeRegistry, ClusterCoordinator, DB node methods."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tram.cluster.coordinator import ClusterCoordinator, _stable_hash, detect_ordinal
from tram.cluster.registry import NodeRegistry
from tram.persistence.db import TramDB


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    d = TramDB(url=f"sqlite:///{tmp_path}/cluster.db")
    yield d
    d.close()


def _make_registry(db, node_id="node-a", ordinal=0, heartbeat=1, ttl=5):
    return NodeRegistry(db, node_id, ordinal, heartbeat_seconds=heartbeat, ttl_seconds=ttl)


# ── detect_ordinal ────────────────────────────────────────────────────────────


def test_detect_ordinal_statefulset():
    assert detect_ordinal("tram-0") == 0
    assert detect_ordinal("tram-3") == 3
    assert detect_ordinal("worker-12") == 12


def test_detect_ordinal_no_suffix():
    assert detect_ordinal("tram") == 0
    assert detect_ordinal("my-node") == 0


# ── _stable_hash ──────────────────────────────────────────────────────────────


def test_stable_hash_is_deterministic():
    assert _stable_hash("pm-ingest") == _stable_hash("pm-ingest")


def test_stable_hash_different_names():
    assert _stable_hash("pipe-a") != _stable_hash("pipe-b")


# ── DB node_registry methods ──────────────────────────────────────────────────


def test_register_and_get_live_nodes(db):
    db.register_node("n1", ordinal=0)
    nodes = db.get_live_nodes(ttl_seconds=30)
    assert any(n["node_id"] == "n1" for n in nodes)


def test_register_multiple_nodes(db):
    db.register_node("n1", 0)
    db.register_node("n2", 1)
    nodes = db.get_live_nodes(ttl_seconds=30)
    ids = {n["node_id"] for n in nodes}
    assert ids == {"n1", "n2"}


def test_heartbeat_updates_timestamp(db):
    db.register_node("n1", 0)
    from sqlalchemy import text
    with db._engine.connect() as conn:
        t1 = conn.execute(text("SELECT last_heartbeat FROM node_registry WHERE node_id='n1'")).scalar()
    time.sleep(0.01)
    db.heartbeat("n1")
    with db._engine.connect() as conn:
        t2 = conn.execute(text("SELECT last_heartbeat FROM node_registry WHERE node_id='n1'")).scalar()
    assert t2 > t1


def test_deregister_node(db):
    db.register_node("n1", 0)
    db.deregister_node("n1")
    nodes = db.get_live_nodes(ttl_seconds=30)
    assert not any(n["node_id"] == "n1" for n in nodes)


def test_expire_nodes(db):
    db.register_node("n1", 0)
    # Manually backdate the heartbeat to simulate a stale node
    from sqlalchemy import text
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    with db._engine.begin() as conn:
        conn.execute(text("UPDATE node_registry SET last_heartbeat = :ts WHERE node_id = 'n1'"), {"ts": stale_ts})

    db.expire_nodes(ttl_seconds=30)
    nodes = db.get_live_nodes(ttl_seconds=30)
    assert not any(n["node_id"] == "n1" for n in nodes)


def test_register_idempotent(db):
    db.register_node("n1", 0)
    db.register_node("n1", 0)  # should not raise
    nodes = db.get_live_nodes(ttl_seconds=30)
    assert len([n for n in nodes if n["node_id"] == "n1"]) == 1


# ── NodeRegistry ──────────────────────────────────────────────────────────────


def test_node_registry_start_registers(db):
    r = _make_registry(db, "node-a", ordinal=0)
    r.start()
    nodes = db.get_live_nodes(ttl_seconds=10)
    assert any(n["node_id"] == "node-a" for n in nodes)
    r.stop()


def test_node_registry_stop_deregisters(db):
    r = _make_registry(db, "node-b", ordinal=1)
    r.start()
    r.stop()
    nodes = db.get_live_nodes(ttl_seconds=10)
    assert not any(n["node_id"] == "node-b" for n in nodes)


def test_node_registry_get_live_nodes(db):
    r = _make_registry(db, "node-c", ordinal=0)
    r.start()
    live = r.get_live_nodes()
    assert any(n["node_id"] == "node-c" for n in live)
    r.stop()


# ── ClusterCoordinator ────────────────────────────────────────────────────────


def _make_coordinator(db, node_id="node-a"):
    registry = _make_registry(db, node_id, ordinal=0)
    return ClusterCoordinator(registry, node_id), registry


def test_coordinator_owns_all_when_only_node(db):
    db.register_node("solo", 0)
    registry = MagicMock()
    registry.get_live_nodes.return_value = [{"node_id": "solo"}]
    coord = ClusterCoordinator(registry, "solo")
    coord.refresh()
    # With 1 node, it owns everything
    assert coord.owns("pipeline-a")
    assert coord.owns("pipeline-b")
    assert coord.owns("any-pipeline")


def test_coordinator_splits_ownership_two_nodes(db):
    nodes = [{"node_id": "tram-0"}, {"node_id": "tram-1"}]

    registry_0 = MagicMock()
    registry_0.get_live_nodes.return_value = nodes
    coord_0 = ClusterCoordinator(registry_0, "tram-0")
    coord_0.refresh()

    registry_1 = MagicMock()
    registry_1.get_live_nodes.return_value = nodes
    coord_1 = ClusterCoordinator(registry_1, "tram-1")
    coord_1.refresh()

    # Every pipeline is owned by exactly one node
    pipeline_names = [f"pipe-{i}" for i in range(20)]
    for name in pipeline_names:
        owned_by_0 = coord_0.owns(name)
        owned_by_1 = coord_1.owns(name)
        assert owned_by_0 != owned_by_1, f"{name} not owned by exactly one node"


def test_coordinator_splits_ownership_three_nodes(db):
    nodes = [{"node_id": "tram-0"}, {"node_id": "tram-1"}, {"node_id": "tram-2"}]
    coordinators = []
    for nid in ["tram-0", "tram-1", "tram-2"]:
        r = MagicMock()
        r.get_live_nodes.return_value = nodes
        c = ClusterCoordinator(r, nid)
        c.refresh()
        coordinators.append(c)

    for name in [f"pipe-{i}" for i in range(30)]:
        owners = sum(1 for c in coordinators if c.owns(name))
        assert owners == 1, f"{name} has {owners} owners (expected 1)"


def test_coordinator_rebalances_on_node_failure(db):
    """When a node disappears, remaining nodes absorb its pipelines."""
    three_nodes = [{"node_id": "tram-0"}, {"node_id": "tram-1"}, {"node_id": "tram-2"}]
    two_nodes = [{"node_id": "tram-0"}, {"node_id": "tram-2"}]  # tram-1 died

    r0 = MagicMock()
    r2 = MagicMock()
    c0 = ClusterCoordinator(r0, "tram-0")
    c2 = ClusterCoordinator(r2, "tram-2")

    # Initially 3 nodes
    r0.get_live_nodes.return_value = three_nodes
    r2.get_live_nodes.return_value = three_nodes
    c0.refresh()
    c2.refresh()

    # After tram-1 dies, 2 nodes
    r0.get_live_nodes.return_value = two_nodes
    r2.get_live_nodes.return_value = two_nodes
    assert c0.refresh() is True   # topology changed
    assert c2.refresh() is True

    # Every pipeline is still owned by exactly one surviving node
    for name in [f"pipe-{i}" for i in range(20)]:
        owners = sum(1 for c in [c0, c2] if c.owns(name))
        assert owners == 1, f"After rebalance: {name} has {owners} owners"


def test_coordinator_owns_all_when_no_live_nodes(db):
    """If DB is empty (startup race), node owns everything as safe fallback."""
    registry = MagicMock()
    registry.get_live_nodes.return_value = []
    coord = ClusterCoordinator(registry, "node-x")
    coord.refresh()
    assert coord.owns("any-pipeline") is True


def test_coordinator_get_state(db):
    nodes = [{"node_id": "tram-0"}, {"node_id": "tram-1"}]
    registry = MagicMock()
    registry.get_live_nodes.return_value = nodes
    coord = ClusterCoordinator(registry, "tram-0")
    coord.refresh()
    state = coord.get_state()
    assert state["node_id"] == "tram-0"
    assert state["live_node_count"] == 2
    assert state["my_position"] == 0


def test_coordinator_refresh_returns_true_on_change(db):
    registry = MagicMock()
    registry.get_live_nodes.return_value = [{"node_id": "n1"}]
    coord = ClusterCoordinator(registry, "n1")
    assert coord.refresh() is True  # first call: changed from [] to [n1]
    assert coord.refresh() is False  # same set: no change
    registry.get_live_nodes.return_value = [{"node_id": "n1"}, {"node_id": "n2"}]
    assert coord.refresh() is True  # n2 joined: changed


# ── config: cluster env vars ──────────────────────────────────────────────────


def test_config_cluster_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TRAM_CLUSTER_ENABLED", raising=False)
    from importlib import reload
    import tram.core.config as m
    reload(m)
    assert m.AppConfig.from_env().cluster_enabled is False


def test_config_cluster_enabled(monkeypatch):
    monkeypatch.setenv("TRAM_CLUSTER_ENABLED", "true")
    monkeypatch.setenv("TRAM_NODE_ORDINAL", "2")
    monkeypatch.setenv("TRAM_HEARTBEAT_SECONDS", "5")
    monkeypatch.setenv("TRAM_NODE_TTL_SECONDS", "15")
    from importlib import reload
    import tram.core.config as m
    reload(m)
    cfg = m.AppConfig.from_env()
    assert cfg.cluster_enabled is True
    assert cfg.node_ordinal == 2
    assert cfg.heartbeat_seconds == 5
    assert cfg.node_ttl_seconds == 15
