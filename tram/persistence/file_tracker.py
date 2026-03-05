"""ProcessedFileTracker — thin wrapper around TramDB for skip_processed logic."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tram.persistence.db import TramDB

logger = logging.getLogger(__name__)


class ProcessedFileTracker:
    """Provides is_processed / mark_processed backed by TramDB.

    Injected into file-based source connectors (SFTP, local, S3, FTP, GCS,
    AzureBlob, CORBA) via the ``_file_tracker`` key in the config dict when
    ``skip_processed: true`` is set on the source config.

    Usage in a source connector::

        tracker = config.get("_file_tracker")
        pipeline = config.get("_pipeline_name", "")
        source_key = f"sftp:{self.host}:{self.remote_path}"

        if self.skip_processed and tracker and tracker.is_processed(pipeline, source_key, filepath):
            logger.info("Skipping already-processed file", extra={"filepath": filepath})
            continue

        yield content, meta

        if self.skip_processed and tracker:
            tracker.mark_processed(pipeline, source_key, filepath)
    """

    def __init__(self, db: "TramDB") -> None:
        self._db = db

    def is_processed(self, pipeline_name: str, source_key: str, filepath: str) -> bool:
        try:
            return self._db.is_processed(pipeline_name, source_key, filepath)
        except Exception as exc:
            logger.warning(
                "ProcessedFileTracker.is_processed error — treating as not processed",
                extra={"pipeline": pipeline_name, "filepath": filepath, "error": str(exc)},
            )
            return False

    def mark_processed(self, pipeline_name: str, source_key: str, filepath: str) -> None:
        try:
            self._db.mark_processed(pipeline_name, source_key, filepath)
        except Exception as exc:
            logger.warning(
                "ProcessedFileTracker.mark_processed error",
                extra={"pipeline": pipeline_name, "filepath": filepath, "error": str(exc)},
            )
