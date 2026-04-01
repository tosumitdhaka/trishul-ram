"""Azure Blob Storage sink connector."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

@register_sink("azure_blob")
class AzureBlobSink(BaseSink):
    """Write data to an Azure Blob Storage container.

    Config keys:
        connection_string   (str, optional)
        account_name        (str, optional)
        account_key         (str, optional)
        container           (str, required)
        blob_template       (str, default "{pipeline}_{timestamp}.bin")
        content_type        (str, default "application/json")
        overwrite           (bool, default True)
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.connection_string: str | None = config.get("connection_string")
        self.account_name: str | None = config.get("account_name")
        self.account_key: str | None = config.get("account_key")
        self.container: str = config["container"]
        self.blob_template: str = config.get("blob_template", "{pipeline}_{timestamp}.bin")
        self.content_type: str = config.get("content_type", "application/json")
        self.overwrite: bool = bool(config.get("overwrite", True))

    def test_connection(self) -> dict:
        import time
        t0 = time.monotonic()
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise RuntimeError("azure-storage-blob not installed — pip install tram[azure]")
        conn_str = self.config.get("connection_string")
        account_name = self.config.get("account_name")
        account_key = self.config.get("account_key")
        if conn_str:
            client = BlobServiceClient.from_connection_string(conn_str)
        elif account_name and account_key:
            account_url = f"https://{account_name}.blob.core.windows.net"
            client = BlobServiceClient(account_url=account_url, credential=account_key)
        else:
            raise RuntimeError("Requires connection_string or account_name+account_key")
        client.get_account_information()
        latency = int((time.monotonic() - t0) * 1000)
        container = self.config.get("container", "")
        return {"ok": True, "latency_ms": latency, "detail": f"Azure Blob account reachable, container '{container}'"}

    def _get_service_client(self):
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise SinkError(
                "Azure Blob sink requires azure-storage-blob — install with: pip install tram[azure]"
            ) from exc
        try:
            if self.connection_string:
                return BlobServiceClient.from_connection_string(self.connection_string)
            if self.account_name and self.account_key:
                account_url = f"https://{self.account_name}.blob.core.windows.net"
                return BlobServiceClient(account_url=account_url, credential=self.account_key)
            raise SinkError("Azure Blob sink requires connection_string or account_name+account_key")
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Azure Blob service client creation failed: {exc}") from exc

    def _render_blob_name(self, meta: dict) -> str:
        ts = datetime.now(UTC).isoformat().replace(":", "-")
        return self.blob_template.format(
            pipeline=meta.get("pipeline_name", "tram"),
            timestamp=ts,
            source_filename=meta.get("source_filename", "data"),
        )

    def write(self, data: bytes, meta: dict) -> None:
        service_client = self._get_service_client()
        blob_name = self._render_blob_name(meta)
        try:
            blob_client = service_client.get_blob_client(container=self.container, blob=blob_name)
            blob_client.upload_blob(data, overwrite=self.overwrite, content_settings=None)
        except Exception as exc:
            raise SinkError(f"Azure Blob upload failed for {self.container}/{blob_name}: {exc}") from exc
        logger.info("Wrote blob to Azure", extra={"container": self.container, "blob": blob_name, "bytes": len(data)})
