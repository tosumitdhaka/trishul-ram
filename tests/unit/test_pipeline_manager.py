"""Tests for PipelineManager — CRUD, state transitions, and run history."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from tram.core.context import RunResult, RunStatus
from tram.core.exceptions import PipelineAlreadyExistsError, PipelineNotFoundError
from tram.pipeline.loader import load_pipeline_from_yaml
from tram.pipeline.manager import PipelineManager, PipelineState

# ── Fixtures ───────────────────────────────────────────────────────────────


_MINIMAL_YAML = """\
name: my-pipe
schedule:
  type: manual
source:
  type: local
  path: /tmp/in
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""

_INTERVAL_YAML = """\
name: interval-pipe
schedule:
  type: interval
  interval_seconds: 60
source:
  type: local
  path: /tmp/in
serializer_in:
  type: json
sinks:
  - type: local
    path: /tmp/out
"""


def _config(yaml=_MINIMAL_YAML):
    return load_pipeline_from_yaml(yaml)


def _run_result(name="my-pipe", status=RunStatus.SUCCESS, run_id="abc123"):
    return RunResult(
        run_id=run_id,
        pipeline_name=name,
        status=status,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        records_in=10,
        records_out=10,
        records_skipped=0,
    )


# ── PipelineState ──────────────────────────────────────────────────────────


class TestPipelineState:
    def test_initial_state(self):
        config = _config()
        state = PipelineState(config)
        assert state.status == "stopped"
        assert state.last_run is None
        assert state.last_run_status is None
        assert len(state.run_history) == 0

    def test_record_run_updates_state(self):
        config = _config()
        state = PipelineState(config)
        result = _run_result()
        state.record_run(result)
        assert state.last_run == result.finished_at
        assert state.last_run_status == "success"
        assert len(state.run_history) == 1

    def test_to_dict(self):
        config = _config()
        state = PipelineState(config)
        d = state.to_dict()
        assert d["name"] == "my-pipe"
        assert d["status"] == "stopped"
        assert d["schedule_type"] == "manual"
        assert d["interval_seconds"] is None
        assert d["cron_expr"] is None

    def test_to_dict_includes_schedule_fields(self):
        config = _config(_INTERVAL_YAML)
        state = PipelineState(config)
        d = state.to_dict()
        assert d["schedule_type"] == "interval"
        assert d["interval_seconds"] == 60
        assert d["cron_expr"] is None

    def test_to_detail_dict(self):
        config = _config()
        state = PipelineState(config, yaml_text=_MINIMAL_YAML)
        d = state.to_detail_dict()
        assert d["name"] == "my-pipe"
        assert d["yaml"] == _MINIMAL_YAML


# ── PipelineManager CRUD ───────────────────────────────────────────────────


class TestPipelineManagerCRUD:
    def test_register_and_get(self):
        mgr = PipelineManager()
        config = _config()
        state = mgr.register(config)
        assert state.config.name == "my-pipe"
        assert mgr.get("my-pipe") is state

    def test_register_duplicate_raises(self):
        mgr = PipelineManager()
        config = _config()
        mgr.register(config)
        with pytest.raises(PipelineAlreadyExistsError):
            mgr.register(config)

    def test_register_replace_succeeds(self):
        mgr = PipelineManager()
        config = _config()
        mgr.register(config)
        new_state = mgr.register(config, replace=True)
        assert mgr.get("my-pipe") is new_state

    def test_deregister_removes_pipeline(self):
        mgr = PipelineManager()
        mgr.register(_config())
        mgr.deregister("my-pipe")
        assert not mgr.exists("my-pipe")

    def test_deregister_unknown_raises(self):
        mgr = PipelineManager()
        with pytest.raises(PipelineNotFoundError):
            mgr.deregister("nonexistent")

    def test_get_unknown_raises(self):
        mgr = PipelineManager()
        with pytest.raises(PipelineNotFoundError):
            mgr.get("nonexistent")

    def test_exists(self):
        mgr = PipelineManager()
        mgr.register(_config())
        assert mgr.exists("my-pipe")
        assert not mgr.exists("other")

    def test_list_all(self):
        mgr = PipelineManager()
        mgr.register(_config(_MINIMAL_YAML))
        mgr.register(_config(_INTERVAL_YAML))
        names = [s.config.name for s in mgr.list_all()]
        assert "my-pipe" in names
        assert "interval-pipe" in names

    def test_set_status(self):
        mgr = PipelineManager()
        mgr.register(_config())
        mgr.set_status("my-pipe", "running")
        assert mgr.get("my-pipe").status == "running"

    def test_register_with_yaml_text(self):
        mgr = PipelineManager()
        mgr.register(_config(), yaml_text=_MINIMAL_YAML)
        assert mgr.get("my-pipe").yaml_text == _MINIMAL_YAML


# ── DB integration paths ───────────────────────────────────────────────────


class TestPipelineManagerWithDB:
    def _make_db(self):
        db = MagicMock()
        db.get_runs.return_value = []
        return db

    def test_register_hydrates_last_run_from_db(self):
        db = self._make_db()
        now = datetime.now(UTC)
        result = _run_result()
        result.finished_at = now
        db.get_runs.return_value = [result]
        mgr = PipelineManager(db=db)
        mgr.register(_config())
        state = mgr.get("my-pipe")
        assert state.last_run == now
        assert state.last_run_status == "success"

    def test_register_saves_version_when_yaml_text_provided(self):
        db = self._make_db()
        mgr = PipelineManager(db=db)
        mgr.register(_config(), yaml_text=_MINIMAL_YAML)
        db.save_pipeline_version.assert_called_once_with("my-pipe", _MINIMAL_YAML)

    def test_register_can_skip_version_save(self):
        db = self._make_db()
        mgr = PipelineManager(db=db)
        mgr.register(_config(), yaml_text=_MINIMAL_YAML, save_version=False)
        db.save_pipeline_version.assert_not_called()

    def test_rollback_activates_existing_version_without_saving_new_one(self):
        db = self._make_db()
        db.get_pipeline_version.return_value = _MINIMAL_YAML
        mgr = PipelineManager(db=db)

        config = mgr.rollback("my-pipe", 2)

        assert config.name == "my-pipe"
        db.activate_pipeline_version.assert_called_once_with("my-pipe", 2)
        db.save_pipeline.assert_called_once_with("my-pipe", _MINIMAL_YAML, source="api")
        db.save_pipeline_version.assert_not_called()
        assert mgr.get("my-pipe").yaml_text == _MINIMAL_YAML

    def test_record_run_saves_to_db(self):
        db = self._make_db()
        mgr = PipelineManager(db=db)
        mgr.register(_config())
        result = _run_result()
        mgr.record_run("my-pipe", result)
        db.save_run.assert_called_once_with(result)

    def test_record_run_triggers_alert_evaluator(self):
        evaluator = MagicMock()
        mgr = PipelineManager(alert_evaluator=evaluator)
        mgr.register(_config())
        result = _run_result()
        mgr.record_run("my-pipe", result)
        evaluator.check.assert_called_once()

    def test_record_run_alert_evaluator_exception_is_swallowed(self):
        evaluator = MagicMock()
        evaluator.check.side_effect = RuntimeError("alert boom")
        mgr = PipelineManager(alert_evaluator=evaluator)
        mgr.register(_config())
        result = _run_result()
        mgr.record_run("my-pipe", result)  # should not raise

    def test_get_runs_uses_db(self):
        db = self._make_db()
        r = _run_result()
        db.get_runs.return_value = [r]
        mgr = PipelineManager(db=db)
        results = mgr.get_runs(pipeline_name="my-pipe")
        db.get_runs.assert_called_once()
        assert results == [r]

    def test_get_run_uses_db(self):
        db = self._make_db()
        r = _run_result()
        db.get_run.return_value = r
        mgr = PipelineManager(db=db)
        assert mgr.get_run("abc123") is r

    def test_save_version_without_db_raises(self):
        mgr = PipelineManager()
        mgr.register(_config())
        with pytest.raises(RuntimeError, match="Persistence not configured"):
            mgr.save_version("my-pipe", _MINIMAL_YAML)

    def test_get_versions_without_db_returns_empty(self):
        mgr = PipelineManager()
        mgr.register(_config())
        assert mgr.get_versions("my-pipe") == []

    def test_get_versions_with_db(self):
        db = self._make_db()
        db.get_pipeline_versions.return_value = [{"version": 1}]
        mgr = PipelineManager(db=db)
        mgr.register(_config())
        versions = mgr.get_versions("my-pipe")
        assert versions == [{"version": 1}]


# ── In-memory run history ──────────────────────────────────────────────────


class TestInMemoryRunHistory:
    def test_get_runs_in_memory(self):
        mgr = PipelineManager()
        mgr.register(_config())
        result = _run_result()
        mgr.record_run("my-pipe", result)
        runs = mgr.get_runs()
        assert len(runs) == 1

    def test_get_runs_filtered_by_pipeline(self):
        mgr = PipelineManager()
        mgr.register(_config(_MINIMAL_YAML))
        mgr.register(_config(_INTERVAL_YAML))
        mgr.record_run("my-pipe", _run_result("my-pipe"))
        mgr.record_run("interval-pipe", _run_result("interval-pipe"))
        runs = mgr.get_runs(pipeline_name="my-pipe")
        assert all(r.pipeline_name == "my-pipe" for r in runs)

    def test_get_runs_filtered_by_status(self):
        mgr = PipelineManager()
        mgr.register(_config())
        mgr.record_run("my-pipe", _run_result(status=RunStatus.SUCCESS, run_id="r1"))
        mgr.record_run("my-pipe", _run_result(status=RunStatus.FAILED, run_id="r2"))
        runs = mgr.get_runs(status="failed")
        assert all(r.status == RunStatus.FAILED for r in runs)

    def test_get_runs_with_offset_and_limit(self):
        mgr = PipelineManager()
        mgr.register(_config())
        for i in range(5):
            mgr.record_run("my-pipe", _run_result(run_id=f"r{i}"))
        runs = mgr.get_runs(offset=2, limit=2)
        assert len(runs) == 2

    def test_get_run_in_memory_found(self):
        mgr = PipelineManager()
        mgr.register(_config())
        result = _run_result(run_id="xyz789")
        mgr.record_run("my-pipe", result)
        found = mgr.get_run("xyz789")
        assert found is result

    def test_get_run_in_memory_not_found(self):
        mgr = PipelineManager()
        mgr.register(_config())
        assert mgr.get_run("nonexistent") is None
