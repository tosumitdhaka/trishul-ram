"""Tests for PipelineWatcher — file-system event handling."""
from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from tram.pipeline.loader import load_pipeline_from_yaml

# ── YAML fixtures ──────────────────────────────────────────────────────────


_MANUAL_YAML = """\
name: {name}
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
name: my-interval
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

# ── watchdog mock setup ────────────────────────────────────────────────────


def _make_watchdog_mocks():
    """Return fake watchdog.events and watchdog.observers modules."""
    # events module
    events_mod = ModuleType("watchdog.events")

    class FileSystemEventHandler:
        pass

    events_mod.FileSystemEventHandler = FileSystemEventHandler

    # observers module
    observers_mod = ModuleType("watchdog.observers")
    mock_observer_cls = MagicMock()
    observers_mod.Observer = mock_observer_cls

    # top-level watchdog module
    watchdog_mod = ModuleType("watchdog")

    return watchdog_mod, events_mod, observers_mod, mock_observer_cls


@pytest.fixture()
def watchdog_mocks():
    """Patch sys.modules with fake watchdog so PipelineWatcher.start() can run."""
    watchdog_mod, events_mod, observers_mod, observer_cls = _make_watchdog_mocks()
    with patch.dict(sys.modules, {
        "watchdog": watchdog_mod,
        "watchdog.events": events_mod,
        "watchdog.observers": observers_mod,
    }):
        yield observer_cls


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_watcher(pipeline_dir="/tmp/pipes", controller=None):
    from tram.watcher.pipeline_watcher import PipelineWatcher
    if controller is None:
        controller = MagicMock()
    watcher = PipelineWatcher(pipeline_dir=pipeline_dir, controller=controller)
    return watcher, controller


def _make_event(src_path: str, is_directory: bool = False):
    ev = MagicMock()
    ev.src_path = src_path
    ev.is_directory = is_directory
    return ev


def _write_file(tmp_path, filename: str, content: str):
    path = tmp_path / filename
    path.write_text(content)
    return path


def _start_and_get_handler(watcher, observer_cls):
    """Start the watcher and extract the _Handler instance that was scheduled."""
    watcher.start()
    # observer_cls() returns the mock observer instance; its .schedule() was called
    mock_observer = observer_cls.return_value
    handler_arg = mock_observer.schedule.call_args[0][0]
    return handler_arg


# ── Lifecycle tests ────────────────────────────────────────────────────────


class TestPipelineWatcherLifecycle:
    def test_start_creates_observer(self, tmp_path, watchdog_mocks):
        watcher, _ = _make_watcher(str(tmp_path))
        watcher.start()
        assert watcher._observer is not None

    def test_start_schedules_handler(self, tmp_path, watchdog_mocks):
        watcher, _ = _make_watcher(str(tmp_path))
        watcher.start()
        observer_cls = watchdog_mocks
        mock_observer = observer_cls.return_value
        mock_observer.schedule.assert_called_once()
        path_arg = mock_observer.schedule.call_args[0][1]
        assert path_arg == str(tmp_path)

    def test_start_sets_daemon_and_starts(self, tmp_path, watchdog_mocks):
        watcher, _ = _make_watcher(str(tmp_path))
        watcher.start()
        mock_observer = watchdog_mocks.return_value
        assert mock_observer.daemon is True
        mock_observer.start.assert_called_once()

    def test_stop_when_not_started(self):
        watcher, _ = _make_watcher()
        watcher.stop()  # should not raise

    def test_stop_clears_observer(self, tmp_path, watchdog_mocks):
        watcher, _ = _make_watcher(str(tmp_path))
        watcher.start()
        watcher.stop()
        assert watcher._observer is None

    def test_stop_calls_stop_and_join(self, tmp_path, watchdog_mocks):
        watcher, _ = _make_watcher(str(tmp_path))
        watcher.start()
        mock_observer = watchdog_mocks.return_value
        watcher.stop()
        mock_observer.stop.assert_called_once()
        mock_observer.join.assert_called_once()

    def test_stop_handles_observer_exception(self, tmp_path, watchdog_mocks):
        watcher, _ = _make_watcher(str(tmp_path))
        watcher.start()
        mock_observer = watchdog_mocks.return_value
        mock_observer.stop.side_effect = RuntimeError("boom")
        watcher.stop()  # should not raise
        assert watcher._observer is None

    def test_start_raises_on_missing_watchdog(self):
        watcher, _ = _make_watcher()
        with patch.dict(sys.modules, {
            "watchdog": None, "watchdog.events": None, "watchdog.observers": None
        }):
            with pytest.raises(ImportError, match="watchdog"):
                watcher.start()


# ── Handler event tests ────────────────────────────────────────────────────


class TestHandlerEvents:
    def _handler(self, tmp_path, watchdog_mocks):
        """Get the real _Handler instance from the real watcher.start() code."""
        watcher, controller = _make_watcher(str(tmp_path))
        handler = _start_and_get_handler(watcher, watchdog_mocks)
        return handler, controller

    def test_modified_yaml_reloads_existing_pipeline(self, tmp_path, watchdog_mocks):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        yaml_file = _write_file(tmp_path, "my-pipe.yaml", _MANUAL_YAML.format(name="my-pipe"))
        controller.exists.return_value = True
        ev = _make_event(str(yaml_file))
        handler.on_modified(ev)
        controller.update.assert_called_once()
        name, yaml_text = controller.update.call_args.args
        assert name == "my-pipe"
        assert yaml_text == yaml_file.read_text()

    def test_created_yaml_registers_new_pipeline(self, tmp_path, watchdog_mocks):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        yaml_file = _write_file(tmp_path, "new-pipe.yaml", _MANUAL_YAML.format(name="new-pipe"))
        controller.exists.return_value = False
        ev = _make_event(str(yaml_file))
        handler.on_created(ev)
        controller.register.assert_called_once()
        call = controller.register.call_args
        assert call.args[0].name == "new-pipe"
        assert call.kwargs["yaml_text"] == yaml_file.read_text()
        assert call.kwargs["source"] == "disk"

    def test_deleted_yaml_stops_and_removes_pipeline(self, tmp_path, watchdog_mocks):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        controller.exists.return_value = True
        ev = _make_event(str(tmp_path / "my-pipe.yaml"))
        handler.on_deleted(ev)
        controller.delete.assert_called_once_with("my-pipe")
        controller.deregister.assert_not_called()

    def test_deleted_yaml_unknown_pipeline(self, tmp_path, watchdog_mocks):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        controller.exists.return_value = False
        ev = _make_event(str(tmp_path / "unknown.yaml"))
        handler.on_deleted(ev)
        controller.delete.assert_not_called()

    def test_deleted_yaml_removes_by_pipeline_name_not_filename_stem(self, tmp_path, watchdog_mocks):
        """§2.15: a YAML whose `name:` differs from its filename must delete
        the pipeline by NAME — the filename stem would remove the wrong (or
        no) pipeline."""
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        # Simulate the watcher having loaded the file earlier: the mapping
        # records pipeline name per path.
        yaml_file = _write_file(tmp_path, "disk-file.yaml", _MANUAL_YAML.format(name="real-name"))
        controller.exists.return_value = True
        handler._reload(str(yaml_file))
        assert handler._path_to_name[str(yaml_file)] == "real-name"

        handler.on_deleted(_make_event(str(yaml_file)))
        controller.delete.assert_called_once_with("real-name")
        # The mapping entry is consumed on delete.
        assert str(yaml_file) not in handler._path_to_name

    def test_deleted_yaml_without_mapping_falls_back_to_stem(self, tmp_path, watchdog_mocks):
        """§2.15: when the file was never seen by the watcher (started after it
        existed, then deleted without a modify event) there is no name mapping —
        the filename stem is the best available guess."""
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        controller.exists.return_value = True
        ev = _make_event(str(tmp_path / "my-pipe.yaml"))
        handler.on_deleted(ev)
        controller.delete.assert_called_once_with("my-pipe")

    def test_reload_prunes_mappings_for_missing_files(self, tmp_path, watchdog_mocks):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        first = _write_file(tmp_path, "gone.yaml", _MANUAL_YAML.format(name="gone-pipe"))
        second = _write_file(tmp_path, "kept.yaml", _MANUAL_YAML.format(name="kept-pipe"))
        controller.exists.return_value = True
        handler._reload(str(first))
        handler._reload(str(second))
        assert set(handler._path_to_name) == {str(first), str(second)}
        first.unlink()  # file removed outside the watcher
        handler._reload(str(second))
        assert set(handler._path_to_name) == {str(second)}

    def test_deleted_remove_failure_is_logged_not_swallowed(self, tmp_path, watchdog_mocks, caplog):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        controller.exists.return_value = True
        controller.delete.side_effect = RuntimeError("stop failed")
        ev = _make_event(str(tmp_path / "my-pipe.yaml"))
        with caplog.at_level(logging.ERROR):
            handler.on_deleted(ev)  # must surface, not silently pass
        assert "Failed to stop and remove pipeline my-pipe" in caplog.text

    def test_non_yaml_file_ignored_on_modified(self, tmp_path, watchdog_mocks):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        ev = _make_event(str(tmp_path / "readme.txt"))
        handler.on_modified(ev)
        controller.update.assert_not_called()
        controller.register.assert_not_called()

    def test_directory_event_ignored(self, tmp_path, watchdog_mocks):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        ev = _make_event(str(tmp_path / "subdir"), is_directory=True)
        handler.on_modified(ev)
        controller.update.assert_not_called()
        controller.register.assert_not_called()

    def test_reload_config_error_is_logged_not_raised(self, tmp_path, watchdog_mocks, caplog):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("not: valid: {{{")
        with caplog.at_level(logging.WARNING):
            handler._reload(str(bad_file))  # should not raise
        controller.update.assert_not_called()
        controller.register.assert_not_called()
        assert "Pipeline reload failed" in caplog.text

    def test_reload_unexpected_error_is_logged_not_swallowed(self, tmp_path, watchdog_mocks, caplog):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        yaml_file = _write_file(tmp_path, "my-pipe.yaml", _MANUAL_YAML.format(name="my-pipe"))
        controller.exists.return_value = True
        controller.update.side_effect = RuntimeError("unexpected failure")
        with caplog.at_level(logging.ERROR):
            handler._reload(str(yaml_file))  # should not raise
        assert "Pipeline reload failed" in caplog.text
        assert "unexpected failure" in caplog.text

    def test_yml_extension_is_also_handled(self, tmp_path, watchdog_mocks):
        handler, controller = self._handler(tmp_path, watchdog_mocks)
        yaml_file = _write_file(tmp_path, "pipe.yml", _MANUAL_YAML.format(name="yml-pipe"))
        controller.exists.return_value = False
        ev = _make_event(str(yaml_file))
        handler.on_created(ev)
        controller.register.assert_called_once()


# ── Real-controller integration (B.2 lifecycle fix) ────────────────────────


class TestWatcherControllerIntegration:
    """The watcher drives a real PipelineController: deleted files stop pipelines."""

    def test_deleted_yaml_stops_and_removes_pipeline(self, tmp_path, watchdog_mocks):
        from tram.pipeline.controller import PipelineController

        recent_run = MagicMock()
        recent_run.finished_at = datetime.now(UTC)
        recent_run.status.value = "success"
        db = MagicMock()
        db.get_runs.return_value = [recent_run]
        db.is_pipeline_stopped.return_value = False
        db.get_stopped_pipeline_names.return_value = []
        db.get_all_pipelines.return_value = []
        db.get_active_broadcast_placements.return_value = []

        ctrl = PipelineController(db=db, node_id="test-node")
        ctrl.start()
        try:
            yaml_file = _write_file(tmp_path, "my-interval.yaml", _INTERVAL_YAML)
            config = load_pipeline_from_yaml(_INTERVAL_YAML)
            ctrl.register(config, yaml_text=_INTERVAL_YAML, source="disk")
            assert ctrl._scheduler.get_job("batch-my-interval") is not None

            watcher, _ = _make_watcher(str(tmp_path), controller=ctrl)
            handler = _start_and_get_handler(watcher, watchdog_mocks)
            handler.on_deleted(_make_event(str(yaml_file)))

            # The pipeline is deregistered, its DB record removed, and its
            # APScheduler job is gone — i.e. deleting the file actually stops it.
            assert ctrl.manager.exists("my-interval") is False
            assert ctrl.exists("my-interval") is False
            assert ctrl._scheduler.get_job("batch-my-interval") is None
            db.delete_pipeline.assert_called_once_with("my-interval")
        finally:
            ctrl.stop()

    def test_modified_yaml_persists_reload_to_db(self, tmp_path, watchdog_mocks):
        from tram.persistence.db import TramDB
        from tram.pipeline.controller import PipelineController

        db = TramDB(url="sqlite:///:memory:", node_id="test-node")
        ctrl = PipelineController(db=db, node_id="test-node")
        try:
            original = _MANUAL_YAML.format(name="my-pipe")
            config = load_pipeline_from_yaml(original)
            ctrl.register(config, yaml_text=original, source="disk")

            # Edit the watched file, then simulate the watcher seeing the change.
            edited = original + "description: edited-by-watcher\n"
            yaml_file = _write_file(tmp_path, "my-pipe.yaml", edited)

            watcher, _ = _make_watcher(str(tmp_path), controller=ctrl)
            handler = _start_and_get_handler(watcher, watchdog_mocks)
            handler.on_modified(_make_event(str(yaml_file)))

            # Reload is persisted via db.save_pipeline (+ version save pattern).
            persisted = dict(db.get_all_pipelines())
            assert persisted["my-pipe"] == edited
            assert db.get_pipeline_versions("my-pipe")
            assert ctrl.manager.get("my-pipe").yaml_text == edited
        finally:
            ctrl.stop()
            db.close()