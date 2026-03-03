"""GCS sink connector — writes blobs to a Google Cloud Storage bucket."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

@register_sink("gcs")
class GcsSink(BaseSink):
    """Write data to a Google Cloud Storage bucket.

    Config keys:
        bucket                  (str, required)
        blob_template           (str, default "{pipeline}_{timestamp}.bin")
        service_account_json    (str, optional)
        content_type            (str, default "application/json")
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.bucket: str = config["bucket"]
        self.blob_template: str = config.get("blob_template", "{pipeline}_{timestamp}.bin")
        self.service_account_json: str | None = config.get("service_account_json")
        self.content_type: str = config.get("content_type", "application/json")

    def _get_client(self):
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise SinkError(
                "GCS sink requires google-cloud-storage — install with: pip install tram[gcs]"
            ) from exc
        try:
            if self.service_account_json:
                return storage.Client.from_service_account_json(self.service_account_json)
            return storage.Client()
        except Exception as exc:
            raise SinkError(f"GCS client creation failed: {exc}") from exc

    def _render_blob_name(self, meta: dict) -> str:
        ts = datetime.now(timezone.utc).isoformat().replace(":", "-")
        return self.blob_template.format(
            pipeline=meta.get("pipeline_name", "tram"),
            timestamp=ts,
            source_filename=meta.get("source_filename", "data"),
        )

    def write(self, data: bytes, meta: dict) -> None:
        client = self._get_client()
        blob_name = self._render_blob_name(meta)
        try:
            bucket = client.bucket(self.bucket)
            blob = bucket.blob(blob_name)
            blob.upload_from_string(data, content_type=self.content_type)
        except Exception as exc:
            raise SinkError(f"GCS upload failed for gs://{self.bucket}/{blob_name}: {exc}") from exc
        logger.info("Wrote blob to GCS", extra={"bucket": self.bucket, "blob": blob_name, "bytes": len(data)})
