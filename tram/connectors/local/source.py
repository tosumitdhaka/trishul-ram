"""Local filesystem source connector."""

from __future__ import annotations

import fnmatch
import logging
import shutil
from pathlib import Path
from typing import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("local")
class LocalSource(BaseSource):
    """Read files from a local directory.

    Batch mode: lists matching files once, reads them all, returns.

    Config keys:
        path              (str, required)   Directory to read from.
        file_pattern      (str, default "*") Glob pattern for file matching.
        move_after_read   (str, optional)   Move files here after reading.
        delete_after_read (bool, default False) Delete files after reading.
        recursive         (bool, default False) Recurse into subdirectories.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.path = Path(config["path"])
        self.file_pattern: str = config.get("file_pattern", "*")
        self.move_after_read: Path | None = (
            Path(config["move_after_read"]) if config.get("move_after_read") else None
        )
        self.delete_after_read: bool = bool(config.get("delete_after_read", False))
        self.recursive: bool = bool(config.get("recursive", False))
        self.skip_processed: bool = bool(config.get("skip_processed", False))
        self._pipeline_name: str = config.get("_pipeline_name", "")
        self._file_tracker = config.get("_file_tracker")

    def read(self) -> Iterator[tuple[bytes, dict]]:
        if not self.path.exists():
            raise SourceError(f"Local source path does not exist: {self.path}")

        glob_fn = self.path.rglob if self.recursive else self.path.glob
        files = sorted(
            f for f in glob_fn(self.file_pattern)
            if f.is_file()
        )

        logger.info(
            "Local source found files",
            extra={"path": str(self.path), "pattern": self.file_pattern, "matched": len(files)},
        )

        source_key = f"local:{self.path}"
        for filepath in files:
            fp_str = str(filepath)
            if self.skip_processed and self._file_tracker:
                if self._file_tracker.is_processed(self._pipeline_name, source_key, fp_str):
                    logger.info("Skipping already-processed local file", extra={"filepath": fp_str})
                    continue
            try:
                content = filepath.read_bytes()
                yield content, {
                    "source_filename": filepath.name,
                    "source_path": fp_str,
                }
                self._post_read(filepath)
                if self.skip_processed and self._file_tracker:
                    self._file_tracker.mark_processed(self._pipeline_name, source_key, fp_str)
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError(f"Error reading {filepath}: {exc}") from exc

    def _post_read(self, filepath: Path) -> None:
        if self.move_after_read:
            self.move_after_read.mkdir(parents=True, exist_ok=True)
            dest = self.move_after_read / filepath.name
            shutil.move(str(filepath), str(dest))
            logger.debug("Moved file", extra={"from": str(filepath), "to": str(dest)})
        elif self.delete_after_read:
            filepath.unlink()
            logger.debug("Deleted file", extra={"filepath": str(filepath)})
