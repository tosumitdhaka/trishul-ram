"""S3/MinIO sink connector — writes objects to an S3-compatible bucket."""

from __future__ import annotations

import logging

from tram.connectors.file_sink_common import render_filename, utc_now
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("s3")
class S3Sink(BaseSink):
    """Write data to an S3-compatible bucket (AWS S3 or MinIO).

    The object key is generated from a template supporting tokens:
    - ``{pipeline}``        — pipeline name (from meta or config)
    - ``{timestamp}``       — UTC file-open timestamp
    - ``{epoch}`` / ``{epoch_m}`` / ``{epoch_ms}`` — UTC file-open epoch seconds / millis
    - ``{part}`` / ``{index}`` — file part number
    - ``{source_filename}`` — original source filename
    - ``{source_stem}`` / ``{source_suffix}`` — derived from source filename
    - ``{source_path}``     — original source path when available

    Requires the ``boto3`` optional dependency (``pip install tram[s3]``).

    Config keys:
        bucket              (str, required)       S3 bucket name.
        key_template        (str, required)       Object key template.
        endpoint_url        (str, optional)       Override endpoint (for MinIO).
        region_name         (str, default "us-east-1")
        aws_access_key_id   (str, optional)
        aws_secret_access_key (str, optional)
        content_type        (str, default "application/json")
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.bucket: str = config["bucket"]
        self.key_template: str = config.get("key_template", "{pipeline}_{timestamp}.bin")
        self.endpoint_url: str | None = config.get("endpoint_url")
        self.region_name: str = config.get("region_name", "us-east-1")
        self.aws_access_key_id: str = config.get("aws_access_key_id", "")
        self.aws_secret_access_key: str = config.get("aws_secret_access_key", "")
        self.content_type: str = config.get("content_type", "application/json")

    def _get_client(self):
        try:
            import boto3
        except ImportError as exc:
            raise SinkError(
                "S3 sink requires boto3 — install with: pip install tram[s3]"
            ) from exc
        kwargs: dict = {
            "region_name": self.region_name,
        }
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self.aws_access_key_id:
            kwargs["aws_access_key_id"] = self.aws_access_key_id
        if self.aws_secret_access_key:
            kwargs["aws_secret_access_key"] = self.aws_secret_access_key
        try:
            return boto3.client("s3", **kwargs)
        except Exception as exc:
            raise SinkError(f"S3 client creation failed: {exc}") from exc

    def test_connection(self) -> dict:
        import time
        t0 = time.monotonic()
        try:
            import boto3
        except ImportError:
            raise RuntimeError("boto3 not installed — pip install tram[s3]")
        kwargs: dict = {"region_name": self.config.get("region_name", "us-east-1")}
        if self.config.get("endpoint_url"):
            kwargs["endpoint_url"] = self.config["endpoint_url"]
        if self.config.get("aws_access_key_id"):
            kwargs["aws_access_key_id"] = self.config["aws_access_key_id"]
        if self.config.get("aws_secret_access_key"):
            kwargs["aws_secret_access_key"] = self.config["aws_secret_access_key"]
        client = boto3.client("s3", **kwargs)
        bucket = self.config.get("bucket", "")
        if bucket:
            client.head_bucket(Bucket=bucket)
            latency = int((time.monotonic() - t0) * 1000)
            return {"ok": True, "latency_ms": latency, "detail": f"S3 bucket '{bucket}' accessible"}
        client.list_buckets()
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency, "detail": "S3 endpoint reachable"}

    def _render_key(self, meta: dict) -> str:
        return render_filename(
            self.key_template,
            opened_at=utc_now(),
            part_index=1,
            max_index=1,
            meta=meta,
        )

    def write(self, data: bytes, meta: dict) -> None:
        client = self._get_client()
        key = self._render_key(meta)

        try:
            client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=self.content_type,
            )
        except Exception as exc:
            raise SinkError(f"S3 put_object failed for s3://{self.bucket}/{key}: {exc}") from exc

        logger.info(
            "Wrote object to S3",
            extra={
                "bucket": self.bucket,
                "key": key,
                "bytes": len(data),
            },
        )
