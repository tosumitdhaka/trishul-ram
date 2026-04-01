"""S3/MinIO source connector — reads objects from an S3-compatible bucket."""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("s3")
class S3Source(BaseSource):
    """Read objects from an S3-compatible bucket (AWS S3 or MinIO).

    Operates in batch mode: lists matching keys, downloads them, then returns.
    Supports moving (copy + delete) or deleting objects after reading.

    Requires the ``boto3`` optional dependency (``pip install tram[s3]``).

    Config keys:
        bucket              (str, required)       S3 bucket name.
        prefix              (str, default "")     Key prefix filter.
        file_pattern        (str, default "*")    fnmatch pattern applied to key basename.
        endpoint_url        (str, optional)       Override endpoint (for MinIO).
        region_name         (str, default "us-east-1")
        aws_access_key_id   (str, optional)
        aws_secret_access_key (str, optional)
        move_after_read     (str, optional)       Destination prefix (copy + delete).
        delete_after_read   (bool, default False)
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.bucket: str = config["bucket"]
        self.prefix: str = config.get("prefix", "")
        self.file_pattern: str = config.get("file_pattern", "*")
        self.endpoint_url: str | None = config.get("endpoint_url")
        self.region_name: str = config.get("region_name", "us-east-1")
        self.aws_access_key_id: str = config.get("aws_access_key_id", "")
        self.aws_secret_access_key: str = config.get("aws_secret_access_key", "")
        self.move_after_read: str | None = config.get("move_after_read")
        self.delete_after_read: bool = bool(config.get("delete_after_read", False))
        self.skip_processed: bool = bool(config.get("skip_processed", False))
        self.read_chunk_bytes: int = int(config.get("read_chunk_bytes", 0))
        self._pipeline_name: str = config.get("_pipeline_name", "")
        self._file_tracker = config.get("_file_tracker")

    def _get_client(self):
        try:
            import boto3
        except ImportError as exc:
            raise SourceError(
                "S3 source requires boto3 — install with: pip install tram[s3]"
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
            raise SourceError(f"S3 client creation failed: {exc}") from exc

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

    def read(self) -> Iterator[tuple[bytes, dict]]:
        client = self._get_client()

        # List all matching objects
        keys: list[str] = []
        try:
            paginator = client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket, Prefix=self.prefix)
            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    basename = key.rsplit("/", 1)[-1]
                    if fnmatch.fnmatch(basename, self.file_pattern):
                        keys.append(key)
        except Exception as exc:
            raise SourceError(f"S3 list_objects_v2 failed: {exc}") from exc

        logger.info(
            "S3 source found objects",
            extra={
                "bucket": self.bucket,
                "prefix": self.prefix,
                "matched": len(keys),
            },
        )

        source_key = f"s3:{self.bucket}:{self.prefix}"
        for key in keys:
            basename = key.rsplit("/", 1)[-1]
            if self.skip_processed and self._file_tracker:
                if self._file_tracker.is_processed(self._pipeline_name, source_key, key):
                    logger.info("Skipping already-processed S3 object", extra={"key": key})
                    continue
            try:
                resp = client.get_object(Bucket=self.bucket, Key=key)
                body = resp["Body"]
                base_meta = {
                    "source_filename": basename,
                    "source_path": key,
                    "source_bucket": self.bucket,
                }
                if self.read_chunk_bytes > 0:
                    chunk_index = 0
                    while True:
                        chunk = body.read(self.read_chunk_bytes)
                        if not chunk:
                            break
                        logger.debug(
                            "Downloaded S3 chunk",
                            extra={"bucket": self.bucket, "key": key, "chunk": chunk_index, "bytes": len(chunk)},
                        )
                        yield chunk, {**base_meta, "chunk_index": chunk_index}
                        chunk_index += 1
                else:
                    content = body.read()
                    logger.debug(
                        "Downloaded S3 object",
                        extra={"bucket": self.bucket, "key": key, "bytes": len(content)},
                    )
                    yield content, base_meta
                self._post_read(client, key, basename)
                if self.skip_processed and self._file_tracker:
                    self._file_tracker.mark_processed(self._pipeline_name, source_key, key)
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError(f"Error reading s3://{self.bucket}/{key}: {exc}") from exc

    def _post_read(self, client, key: str, basename: str) -> None:
        if self.move_after_read:
            dest_prefix = self.move_after_read.rstrip("/")
            dest_key = f"{dest_prefix}/{basename}"
            try:
                client.copy_object(
                    Bucket=self.bucket,
                    CopySource={"Bucket": self.bucket, "Key": key},
                    Key=dest_key,
                )
                client.delete_object(Bucket=self.bucket, Key=key)
                logger.debug("Moved S3 object", extra={"from": key, "to": dest_key})
            except Exception as exc:
                logger.warning("Failed to move S3 object %s: %s", key, exc)
        elif self.delete_after_read:
            try:
                client.delete_object(Bucket=self.bucket, Key=key)
                logger.debug("Deleted S3 object", extra={"key": key})
            except Exception as exc:
                logger.warning("Failed to delete S3 object %s: %s", key, exc)
