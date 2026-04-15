"""Tests for PipelineWatcher — file-system event handling."""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


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


def _make_watcher(pipeline_dir="/tmp/pipes"):
    from tram.watcher.pipeline_watcher import PipelineWatcher
    manager = MagicMock()
    return PipelineWatcher(pipeline_dir=pipeline_dir, manager=manager), manager


def _make_event(src_path: str, is_directory: bool = False):
    ev = MagicMock()
    ev.src_path = src_path
    ev.is_directory = is_directory
    return ev


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
        watcher, manager = _make_watcher(str(tmp_path))
        handler = _start_and_get_handler(watcher, watchdog_mocks)
        return handler, manager

    def test_modified_yaml_calls_reload(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        yaml_file = tmp_path / "my-pipe.yaml"
        yaml_file.write_text("""\
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
""")
        ev = _make_event(str(yaml_file))
        handler.on_modified(ev)
        mgr.register.assert_called_once()
        call_kwargs = mgr.register.call_args
        assert call_kwargs.kwargs.get("replace") is True

    def test_created_yaml_calls_reload(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        yaml_file = tmp_path / "new-pipe.yaml"
        yaml_file.write_text("""\
name: new-pipe
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
""")
        ev = _make_event(str(yaml_file))
        handler.on_created(ev)
        mgr.register.assert_called_once()

    def test_deleted_yaml_deregisters_pipeline(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        mgr.exists.return_value = True
        ev = _make_event(str(tmp_path / "my-pipe.yaml"))
        handler.on_deleted(ev)
        mgr.deregister.assert_called_once_with("my-pipe")

    def test_deleted_yaml_unknown_pipeline(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        mgr.exists.return_value = False
        ev = _make_event(str(tmp_path / "unknown.yaml"))
        handler.on_deleted(ev)
        mgr.deregister.assert_not_called()

    def test_non_yaml_file_ignored_on_modified(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        ev = _make_event(str(tmp_path / "readme.txt"))
        handler.on_modified(ev)
        mgr.register.assert_not_called()

    def test_directory_event_ignored(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        ev = _make_event(str(tmp_path / "subdir"), is_directory=True)
        handler.on_modified(ev)
        mgr.register.assert_not_called()

    def test_reload_config_error_is_swallowed(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("not: valid: {{{")
        handler._reload(str(bad_file))  # should not raise
        mgr.register.assert_not_called()

    def test_reload_general_exception_is_swallowed(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        yaml_file = tmp_path / "pipe.yaml"
        yaml_file.write_text("""\
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
""")
        mgr.register.side_effect = RuntimeError("unexpected failure")
        handler._reload(str(yaml_file))  # should not raise

    def test_deleted_stop_exception_swallowed(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        mgr.exists.return_value = True
        mgr.stop_pipeline.side_effect = RuntimeError("already stopped")
        ev = _make_event(str(tmp_path / "my-pipe.yaml"))
        handler.on_deleted(ev)
        mgr.deregister.assert_called_once_with("my-pipe")

    def test_yml_extension_is_also_handled(self, tmp_path, watchdog_mocks):
        handler, mgr = self._handler(tmp_path, watchdog_mocks)
        yaml_file = tmp_path / "pipe.yml"
        yaml_file.write_text("""\
name: yml-pipe
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
""")
        ev = _make_event(str(yaml_file))
        handler.on_created(ev)
        mgr.register.assert_called_once()
