"""PipelineWatcher — file-system watcher that reloads pipelines on YAML changes.

Requires ``watchdog>=3.0`` (``pip install tram[watch]``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tram.pipeline.manager import PipelineManager

logger = logging.getLogger(__name__)


class PipelineWatcher:
    """Watch *pipeline_dir* for YAML changes and reload/remove pipelines automatically.

    Events:
        - File created / modified → load or reload the pipeline
        - File deleted → stop and deregister the pipeline
    """

    def __init__(self, pipeline_dir: str, manager: "PipelineManager") -> None:
        self._pipeline_dir = pipeline_dir
        self._manager = manager
        self._observer = None

    def start(self) -> None:
        """Start the watchdog Observer thread."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent
        except ImportError as exc:
            raise ImportError(
                "watchdog is required for pipeline file watching — "
                "install with: pip install tram[watch]"
            ) from exc

        manager = self._manager
        pipeline_dir = self._pipeline_dir

        class _Handler(FileSystemEventHandler):
            def _is_yaml(self, path: str) -> bool:
                return path.endswith(".yaml") or path.endswith(".yml")

            def on_modified(self, event):
                if event.is_directory or not self._is_yaml(event.src_path):
                    return
                self._reload(event.src_path)

            def on_created(self, event):
                if event.is_directory or not self._is_yaml(event.src_path):
                    return
                self._reload(event.src_path)

            def on_deleted(self, event):
                if event.is_directory or not self._is_yaml(event.src_path):
                    return
                name = Path(event.src_path).stem
                if manager.exists(name):
                    try:
                        manager.stop_pipeline(name)
                    except Exception:
                        pass
                    try:
                        manager.deregister(name)
                        logger.info("Pipeline removed (file deleted)", extra={"pipeline": name})
                    except Exception as exc:
                        logger.warning("Failed to deregister pipeline %s: %s", name, exc)

            def _reload(self, path: str):
                from tram.pipeline.loader import load_pipeline
                from tram.core.exceptions import ConfigError
                try:
                    config = load_pipeline(path)
                    manager.register(config)
                    logger.info("Pipeline reloaded (file changed)", extra={"pipeline": config.name, "path": path})
                except ConfigError as exc:
                    logger.warning("Pipeline reload failed (config error): %s — %s", path, exc)
                except Exception as exc:
                    logger.warning("Pipeline reload failed: %s — %s", path, exc)

        self._observer = Observer()
        self._observer.schedule(_Handler(), self._pipeline_dir, recursive=False)
        self._observer.daemon = True
        self._observer.start()
        logger.info("PipelineWatcher started", extra={"dir": self._pipeline_dir})

    def stop(self) -> None:
        """Stop the watchdog Observer."""
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception as exc:
                logger.warning("PipelineWatcher stop error: %s", exc)
            finally:
                self._observer = None
        logger.info("PipelineWatcher stopped")
