"""GCS source connector — reads blobs from a Google Cloud Storage bucket."""
from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

@register_source("gcs")
class GcsSource(BaseSource):
    """Read blobs from a Google Cloud Storage bucket.

    Config keys:
        bucket                  (str, required)
        prefix                  (str, default "")
        file_pattern            (str, default "*")
        service_account_json    (str, optional)  Path to service account JSON. Uses ADC if omitted.
        move_after_read         (str, optional)  Destination prefix (copy + delete).
        delete_after_read       (bool, default False)
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.bucket: str = config["bucket"]
        self.prefix: str = config.get("prefix", "")
        self.file_pattern: str = config.get("file_pattern", "*")
        self.service_account_json: str | None = config.get("service_account_json")
        self.move_after_read: str | None = config.get("move_after_read")
        self.delete_after_read: bool = bool(config.get("delete_after_read", False))
        self.skip_processed: bool = bool(config.get("skip_processed", False))
        self._pipeline_name: str = config.get("_pipeline_name", "")
        self._file_tracker = config.get("_file_tracker")

    def _get_client(self):
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise SourceError(
                "GCS source requires google-cloud-storage — install with: pip install tram[gcs]"
            ) from exc
        try:
            if self.service_account_json:
                return storage.Client.from_service_account_json(self.service_account_json)
            return storage.Client()
        except Exception as exc:
            raise SourceError(f"GCS client creation failed: {exc}") from exc

    def test_connection(self) -> dict:
        import time
        t0 = time.monotonic()
        try:
            from google.cloud import storage
        except ImportError:
            raise RuntimeError("google-cloud-storage not installed — pip install tram[gcs]")
        sa = self.config.get("service_account_json")
        try:
            client = storage.Client.from_service_account_json(sa) if sa else storage.Client()
        except Exception as exc:
            raise RuntimeError(f"GCS client creation failed: {exc}")
        bucket = self.config.get("bucket", "")
        client.get_bucket(bucket)
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "detail": f"GCS bucket '{bucket}' accessible"}

    def read(self) -> Iterator[tuple[bytes, dict]]:
        client = self._get_client()
        try:
            bucket = client.bucket(self.bucket)
            blobs = list(client.list_blobs(self.bucket, prefix=self.prefix))
        except Exception as exc:
            raise SourceError(f"GCS list_blobs failed: {exc}") from exc

        matched = [b for b in blobs if fnmatch.fnmatch(b.name.rsplit("/", 1)[-1], self.file_pattern)]
        logger.info("GCS source found blobs", extra={"bucket": self.bucket, "matched": len(matched)})

        source_key = f"gcs:{self.bucket}:{self.prefix}"
        for blob in matched:
            basename = blob.name.rsplit("/", 1)[-1]
            if self.skip_processed and self._file_tracker:
                if self._file_tracker.is_processed(self._pipeline_name, source_key, blob.name):
                    logger.info("Skipping already-processed GCS blob", extra={"blob": blob.name})
                    continue
            try:
                content = blob.download_as_bytes()
                logger.debug("Downloaded GCS blob", extra={"bucket": self.bucket, "blob": blob.name, "bytes": len(content)})
                yield content, {
                    "source_filename": basename,
                    "source_path": blob.name,
                    "source_bucket": self.bucket,
                }
                self._post_read(client, bucket, blob, basename)
                if self.skip_processed and self._file_tracker:
                    self._file_tracker.mark_processed(self._pipeline_name, source_key, blob.name)
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError(f"Error reading gs://{self.bucket}/{blob.name}: {exc}") from exc

    def _post_read(self, client, bucket, blob, basename: str) -> None:
        if self.move_after_read:
            dest_prefix = self.move_after_read.rstrip("/")
            dest_name = f"{dest_prefix}/{basename}"
            try:
                bucket.copy_blob(blob, bucket, dest_name)
                blob.delete()
                logger.debug("Moved GCS blob", extra={"from": blob.name, "to": dest_name})
            except Exception as exc:
                logger.warning("Failed to move GCS blob %s: %s", blob.name, exc)
        elif self.delete_after_read:
            try:
                blob.delete()
                logger.debug("Deleted GCS blob", extra={"blob": blob.name})
            except Exception as exc:
                logger.warning("Failed to delete GCS blob %s: %s", blob.name, exc)
