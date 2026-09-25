"""PipelineWatcher — file-system watcher that reloads pipelines on YAML changes.

Requires ``watchdog>=3.0`` (``pip install tram[watch]``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tram.models.pipeline import PipelineConfig
    from tram.pipeline.controller import PipelineController

logger = logging.getLogger(__name__)


class PipelineWatcherController:
    """Thin delegating façade over ``PipelineController`` for the watcher's needs.

    The watcher needs exactly three lifecycle operations — register (file
    created), reload/update (file modified), and remove/stop (file deleted) —
    plus existence checks. All real work is delegated to the controller, which
    is the single authority for stopping/starting pipelines, deregistering them,
    and persisting changes to the DB.
    """

    def __init__(self, controller: PipelineController) -> None:
        self._controller = controller

    def exists(self, name: str) -> bool:
        return self._controller.exists(name)

    def register(self, config: PipelineConfig, yaml_text: str) -> None:
        """Register a brand-new pipeline discovered on disk (persists to DB)."""
        self._controller.register(config, yaml_text=yaml_text, source="disk")

    def reload(self, name: str, yaml_text: str) -> None:
        """Reload an existing pipeline, mirroring ``controller.update`` persistence."""
        self._controller.update(name, yaml_text)

    def remove(self, name: str) -> None:
        """Stop execution and remove a pipeline whose file was deleted."""
        self._controller.delete(name)


class PipelineWatcher:
    """Watch *pipeline_dir* for YAML changes and reload/remove pipelines automatically.

    Events:
        - File created / modified → load or reload the pipeline
        - File deleted → stop and deregister the pipeline
    """

    def __init__(self, pipeline_dir: str, controller: PipelineController) -> None:
        self._pipeline_dir = pipeline_dir
        self._controller = PipelineWatcherController(controller)
        self._observer = None

    def start(self) -> None:
        """Start the watchdog Observer thread."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as exc:
            raise ImportError(
                "watchdog is required for pipeline file watching — "
                "install with: pip install tram[watch]"
            ) from exc

        controller = self._controller

        class _Handler(FileSystemEventHandler):
            def __init__(self) -> None:
                # Path → pipeline name, populated on every successful load.
                # Deletion removes by NAME, not filename stem, so a YAML whose
                # `name:` differs from its filename removes the right pipeline
                # (review §2.15).
                self._path_to_name: dict[str, str] = {}

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
                name = self._path_to_name.pop(event.src_path, None)
                if name is None:
                    # The file was never seen by this watcher (started after the
                    # file existed, then deleted without a modify event) — the
                    # filename stem is the best available guess.
                    name = Path(event.src_path).stem
                if not controller.exists(name):
                    return
                try:
                    controller.remove(name)
                    logger.info("Pipeline removed (file deleted)", extra={"pipeline": name})
                except Exception as exc:
                    logger.error(
                        "Failed to stop and remove pipeline %s after its file was deleted: %s",
                        name, exc, exc_info=True,
                    )

            def _reload(self, path: str):
                from tram.core.exceptions import ConfigError
                from tram.pipeline.loader import load_pipeline
                try:
                    config, yaml_text = load_pipeline(path)
                    self._path_to_name[path] = config.name
                    # Drop entries for files that no longer exist (renamed or
                    # removed outside the watcher) so the mapping stays bounded.
                    for stale in [p for p in self._path_to_name if not Path(p).exists()]:
                        del self._path_to_name[stale]
                    if controller.exists(config.name):
                        controller.reload(config.name, yaml_text)
                    else:
                        controller.register(config, yaml_text)
                    logger.info("Pipeline reloaded (file changed)",
                                extra={"pipeline": config.name, "path": path})
                except ConfigError as exc:
                    logger.warning("Pipeline reload failed (config error): %s — %s", path, exc)
                except Exception as exc:
                    logger.error("Pipeline reload failed: %s — %s", path, exc, exc_info=True)

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
