"""Azure Blob Storage source connector."""
from __future__ import annotations
import fnmatch
import logging
from typing import Iterator
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

@register_source("azure_blob")
class AzureBlobSource(BaseSource):
    """Read blobs from an Azure Blob Storage container.

    Config keys:
        connection_string   (str, optional)  Azure connection string (use this OR account_name+account_key)
        account_name        (str, optional)
        account_key         (str, optional)
        container           (str, required)
        prefix              (str, default "")
        file_pattern        (str, default "*")
        move_after_read     (str, optional)  Destination container/prefix
        delete_after_read   (bool, default False)
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.connection_string: str | None = config.get("connection_string")
        self.account_name: str | None = config.get("account_name")
        self.account_key: str | None = config.get("account_key")
        self.container: str = config["container"]
        self.prefix: str = config.get("prefix", "")
        self.file_pattern: str = config.get("file_pattern", "*")
        self.move_after_read: str | None = config.get("move_after_read")
        self.delete_after_read: bool = bool(config.get("delete_after_read", False))
        self.skip_processed: bool = bool(config.get("skip_processed", False))
        self._pipeline_name: str = config.get("_pipeline_name", "")
        self._file_tracker = config.get("_file_tracker")

    def _get_service_client(self):
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise SourceError(
                "Azure Blob source requires azure-storage-blob — install with: pip install tram[azure]"
            ) from exc
        try:
            if self.connection_string:
                return BlobServiceClient.from_connection_string(self.connection_string)
            if self.account_name and self.account_key:
                account_url = f"https://{self.account_name}.blob.core.windows.net"
                return BlobServiceClient(account_url=account_url, credential=self.account_key)
            raise SourceError("Azure Blob source requires connection_string or account_name+account_key")
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"Azure Blob service client creation failed: {exc}") from exc

    def read(self) -> Iterator[tuple[bytes, dict]]:
        service_client = self._get_service_client()
        try:
            container_client = service_client.get_container_client(self.container)
            blob_list = list(container_client.list_blobs(name_starts_with=self.prefix or None))
        except Exception as exc:
            raise SourceError(f"Azure Blob list_blobs failed: {exc}") from exc

        matched = [b for b in blob_list if fnmatch.fnmatch(b.name.rsplit("/", 1)[-1], self.file_pattern)]
        logger.info("Azure Blob source found blobs", extra={"container": self.container, "matched": len(matched)})

        source_key = f"azure_blob:{self.account_name or 'conn'}:{self.container}"
        for blob_props in matched:
            blob_name = blob_props.name
            basename = blob_name.rsplit("/", 1)[-1]
            if self.skip_processed and self._file_tracker:
                if self._file_tracker.is_processed(self._pipeline_name, source_key, blob_name):
                    logger.info("Skipping already-processed Azure blob", extra={"blob": blob_name})
                    continue
            try:
                blob_client = service_client.get_blob_client(container=self.container, blob=blob_name)
                content = blob_client.download_blob().readall()
                logger.debug("Downloaded Azure blob", extra={"container": self.container, "blob": blob_name, "bytes": len(content)})
                yield content, {
                    "source_filename": basename,
                    "source_path": blob_name,
                    "source_container": self.container,
                }
                self._post_read(service_client, blob_client, blob_name, basename)
                if self.skip_processed and self._file_tracker:
                    self._file_tracker.mark_processed(self._pipeline_name, source_key, blob_name)
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError(f"Error reading azure://{self.container}/{blob_name}: {exc}") from exc

    def _post_read(self, service_client, blob_client, blob_name: str, basename: str) -> None:
        if self.move_after_read:
            dest = self.move_after_read.rstrip("/")
            dest_name = f"{dest}/{basename}"
            try:
                dest_client = service_client.get_blob_client(container=self.container, blob=dest_name)
                dest_client.start_copy_from_url(blob_client.url)
                blob_client.delete_blob()
                logger.debug("Moved Azure blob", extra={"from": blob_name, "to": dest_name})
            except Exception as exc:
                logger.warning("Failed to move Azure blob %s: %s", blob_name, exc)
        elif self.delete_after_read:
            try:
                blob_client.delete_blob()
                logger.debug("Deleted Azure blob", extra={"blob": blob_name})
            except Exception as exc:
                logger.warning("Failed to delete Azure blob %s: %s", blob_name, exc)
