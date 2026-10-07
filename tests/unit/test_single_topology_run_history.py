"""Tests for issue #81 — single-topology observability.

Covers both halves of the fix:
  (a) ``TRAM_MANAGER_URL`` defaults to ``http://localhost:8765`` in standalone
      mode only (worker/manager modes are never defaulted; an explicit value
      always wins) — config tests.
  (b) standalone stream runs reach run history: periodic segment rollups
      (DELTA counts, ``<run_id>-seg<N>``) plus a final lifecycle row under the
      stream's own ``run_id`` — controller tests.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import tram.core.config as cfg_mod
from tram.agent.stats_store import StatsStore
from tram.core.context import RunStatus
from tram.pipeline.controller import PipelineController
from tram.pipeline.loader import load_pipeline_from_yaml
from tram.pipeline.manager import PipelineManager

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


# ── (a) TRAM_MANAGER_URL default ──────────────────────────────────────────


def test_standalone_defaults_manager_url_to_localhost(monkeypatch):
    """Standalone mode with no TRAM_MANAGER_URL defaults to the local daemon
    so the run-complete callback URL is never empty (run history was silently
    dropped before v1.6.0)."""
    monkeypatch.delenv("TRAM_MANAGER_URL", raising=False)
    monkeypatch.delenv("TRAM_MODE", raising=False)
    config = cfg_mod.AppConfig.from_env()
    assert config.tram_mode == "standalone"
    assert config.manager_url == "http://localhost:8765"


def test_worker_mode_not_defaulted(monkeypatch):
    """Worker-mode processes must NOT get the localhost default — their manager
    is remote, and defaulting would send run callbacks to the worker itself."""
    monkeypatch.delenv("TRAM_MANAGER_URL", raising=False)
    monkeypatch.setenv("TRAM_MODE", "worker")
    config = cfg_mod.AppConfig.from_env()
    assert config.manager_url == ""


def test_manager_mode_not_defaulted(monkeypatch):
    """Manager mode is never defaulted either — the manager's own URL is an
    operator-set value (workers POST callbacks to it)."""
    monkeypatch.delenv("TRAM_MANAGER_URL", raising=False)
    monkeypatch.setenv("TRAM_MODE", "manager")
    config = cfg_mod.AppConfig.from_env()
    assert config.manager_url == ""


def test_explicit_manager_url_wins_in_standalone(monkeypatch):
    """An explicitly-set TRAM_MANAGER_URL always wins — including a remote
    manager in a hybrid standalone setup."""
    monkeypatch.setenv("TRAM_MANAGER_URL", "http://remote-mgr:8765")
    monkeypatch.setenv("TRAM_MODE", "standalone")
    config = cfg_mod.AppConfig.from_env()
    assert config.manager_url == "http://remote-mgr:8765"


# ── (b) standalone stream run history ─────────────────────────────────────


def _make_controller(stats_store: StatsStore | None = None):
    """Controller with a REAL PipelineManager (so run-history rows actually
    land) and a mocked executor."""
    ctrl = PipelineController(node_id="test-node", stats_store=stats_store)
    ctrl.manager = PipelineManager(db=None)
    ctrl.executor = MagicMock()
    return ctrl


def _register_stream(ctrl, yaml_text: str = _STREAM_YAML):
    config = load_pipeline_from_yaml(yaml_text)
    ctrl.manager.register(config, save_version=False)
    ctrl.manager.set_status(config.name, "running")
    return config


def _run_worker(ctrl, config, fake_stream_run, timeout: float = 5.0) -> str | None:
    """Run _stream_worker to completion; returns the lifecycle run_id (or None
    when the worker had no stats wiring)."""
    stop_event = threading.Event()
    captured: dict = {}

    def wrapper(cfg, stop_ev, stats=None, config_sha256=""):
        captured["stats"] = stats
        fake_stream_run(cfg, stop_ev, stats=stats, config_sha256=config_sha256)

    ctrl.executor.stream_run.side_effect = wrapper
    t = threading.Thread(target=ctrl._stream_worker, args=(config, stop_event))
    t.start()
    t.join(timeout=timeout)
    assert not t.is_alive(), "stream worker did not stop within timeout"
    stats = captured.get("stats")
    return stats.run_id if stats is not None else None


def test_stream_stop_records_final_lifecycle_row():
    """A stream start→stop records exactly one run-history row carrying the
    lifecycle run_id and the cumulative counts (no rollup ticks ran)."""
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    config = _register_stream(ctrl)

    def fake_stream_run(cfg, stop_ev, stats=None, config_sha256=""):
        stats.increment(records_in=42, records_out=40, skipped=2, errors=["bad record"])
        stop_ev.set()

    run_id = _run_worker(ctrl, config, fake_stream_run)

    rows = ctrl.manager.get_runs(pipeline_name=config.name)
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == run_id
    assert row.status == RunStatus.SUCCESS
    assert row.records_in == 42
    assert row.records_out == 40
    assert row.records_skipped == 2
    assert row.errors == ["bad record"]
    assert row.node_id == "test-node"


def test_stream_stop_without_stats_store_records_nothing():
    """No StatsStore → no _LocalRun → no run-history rows (unchanged behavior
    for controllers built without the standalone stats wiring)."""
    ctrl = _make_controller(stats_store=None)
    config = _register_stream(ctrl)

    def fake_stream_run(cfg, stop_ev, stats=None, config_sha256=""):
        stop_ev.set()

    _run_worker(ctrl, config, fake_stream_run)

    assert ctrl.manager.get_runs(pipeline_name=config.name) == []


def test_stream_crash_records_failed_row():
    """A stream that raises records a FAILED lifecycle row with the crash
    text — run history shows the failure instead of a silent disappearance."""
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    config = _register_stream(ctrl)

    def fake_stream_run(cfg, stop_ev, stats=None, config_sha256=""):
        stats.increment(records_in=5, records_out=3)
        raise RuntimeError("kafka consumer died")

    run_id = _run_worker(ctrl, config, fake_stream_run)

    rows = ctrl.manager.get_runs(pipeline_name=config.name)
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == run_id
    assert row.status == RunStatus.FAILED
    assert "kafka consumer died" in (row.error or "")
    assert row.records_in == 5
    assert row.records_out == 3


def test_stream_rollups_record_segment_rows_with_counts():
    """Periodic rollups produce SUCCESS segment rows with DELTA counts; the
    final lifecycle row closes the run. The sum of all rows equals the
    lifecycle totals (so /api/stats aggregations stay correct)."""
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    config = _register_stream(ctrl)
    seen_run_ids: list[str] = []

    def fake_stream_run(cfg, stop_ev, stats=None, config_sha256=""):
        seen_run_ids.append(stats.run_id)
        stats.increment(records_in=10, records_out=9, skipped=1)
        ctrl._emit_local_stats_once()  # tick 1 → seg1: 10/9/1
        stats.increment(records_in=15, records_out=14)
        ctrl._emit_local_stats_once()  # tick 2 → seg2: 15/14
        stop_ev.set()

    run_id = _run_worker(ctrl, config, fake_stream_run)
    assert seen_run_ids == [run_id]

    rows = ctrl.manager.get_runs(pipeline_name=config.name)
    by_run_id = {r.run_id: r for r in rows}
    assert set(by_run_id) == {run_id, f"{run_id}-seg1", f"{run_id}-seg2"}

    seg1 = by_run_id[f"{run_id}-seg1"]
    assert seg1.status == RunStatus.SUCCESS
    assert (seg1.records_in, seg1.records_out, seg1.records_skipped) == (10, 9, 1)

    seg2 = by_run_id[f"{run_id}-seg2"]
    assert (seg2.records_in, seg2.records_out, seg2.records_skipped) == (15, 14, 0)

    final = by_run_id[run_id]
    assert final.status == RunStatus.SUCCESS
    # Final partial segment carried no further work.
    assert (final.records_in, final.records_out, final.records_skipped) == (0, 0, 0)

    # Sum of all rows == lifecycle totals.
    assert sum(r.records_in for r in rows) == 25
    assert sum(r.records_out for r in rows) == 23
    assert sum(r.records_skipped for r in rows) == 1


def test_stream_rollups_bounded_by_cap(monkeypatch):
    """A long-lived stream cannot spam run history: rollup rows are hard-capped
    per lifecycle (the final lifecycle row still lands)."""
    monkeypatch.setattr(
        "tram.pipeline.controller._STREAM_ROLLUP_ROWS_MAX", 3
    )
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    config = _register_stream(ctrl)

    def fake_stream_run(cfg, stop_ev, stats=None, config_sha256=""):
        for _ in range(5):
            stats.increment(records_in=1)
            ctrl._emit_local_stats_once()  # ticks 1-5; only 3 produce rows
        stop_ev.set()

    run_id = _run_worker(ctrl, config, fake_stream_run)

    rows = ctrl.manager.get_runs(pipeline_name=config.name)
    segment_rows = [r for r in rows if "-seg" in r.run_id]
    final_rows = [r for r in rows if r.run_id == run_id]
    assert len(segment_rows) == 3, "rollup rows must be capped at 3"
    assert len(final_rows) == 1
    # The final row carries the work of the capped ticks (delta since seg3).
    assert sum(r.records_in for r in rows) == 5
    assert final_rows[0].status == RunStatus.SUCCESS


def test_quiet_segments_produce_no_rollup_rows():
    """Segments without activity are skipped — a stream that only ran for one
    tick with zero records leaves just the final lifecycle row (no 0-count
    noise rows)."""
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    config = _register_stream(ctrl)

    def fake_stream_run(cfg, stop_ev, stats=None, config_sha256=""):
        ctrl._emit_local_stats_once()  # quiet tick → no rollup row
        stop_ev.set()

    run_id = _run_worker(ctrl, config, fake_stream_run)

    rows = ctrl.manager.get_runs(pipeline_name=config.name)
    assert len(rows) == 1
    assert rows[0].run_id == run_id
    assert rows[0].status == RunStatus.SUCCESS
    assert rows[0].records_in == 0


def test_deleted_pipeline_not_resurrected_by_final_row():
    """B10 guard: a pipeline deleted while the stream was in flight is not
    resurrected by a late lifecycle row."""
    store = StatsStore(interval=30)
    ctrl = _make_controller(stats_store=store)
    config = _register_stream(ctrl)

    def fake_stream_run(cfg, stop_ev, stats=None, config_sha256=""):
        stats.increment(records_in=7)
        stop_ev.set()

    stop_event = threading.Event()
    ctrl.executor.stream_run.side_effect = fake_stream_run

    # Deregister mid-run so the finally's commit guard fires.
    ctrl.manager.deregister(config.name)
    t = threading.Thread(target=ctrl._stream_worker, args=(config, stop_event))
    t.start()
    t.join(timeout=5)

    assert ctrl.manager.get_runs(pipeline_name=config.name) == []