"""Tests for Azure Blob Storage source and sink connectors."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.azure_blob.sink import AzureBlobSink
from tram.connectors.azure_blob.source import AzureBlobSource
from tram.core.exceptions import SinkError, SourceError


class TestAzureBlobSource:
    def _make_mock(self, blob_names: list[str], contents: dict[str, bytes]):
        mock_blob_props_list = []
        for name in blob_names:
            bp = MagicMock()
            bp.name = name
            mock_blob_props_list.append(bp)

        mock_container_client = MagicMock()
        mock_container_client.list_blobs.return_value = mock_blob_props_list

        created_clients = []

        def make_blob_client(container, blob):
            bc = MagicMock()
            dl = MagicMock()
            dl.readall.return_value = contents.get(blob, b"")
            bc.download_blob.return_value = dl
            bc.url = f"https://example.blob.core.windows.net/{container}/{blob}"
            created_clients.append(bc)
            return bc

        mock_service = MagicMock()
        mock_service.get_container_client.return_value = mock_container_client
        mock_service.get_blob_client.side_effect = make_blob_client
        mock_service._created_clients = created_clients

        mock_BlobServiceClient = MagicMock()
        mock_BlobServiceClient.from_connection_string.return_value = mock_service

        mock_azure_storage_blob = MagicMock()
        mock_azure_storage_blob.BlobServiceClient = mock_BlobServiceClient
        mock_azure = MagicMock()
        mock_azure.storage.blob = mock_azure_storage_blob
        return mock_azure, mock_azure_storage_blob, mock_service

    def test_reads_matching_blobs(self):
        contents = {"prefix/a.json": b'[{"x":1}]', "prefix/b.json": b'[{"x":2}]'}
        mock_azure, mock_module, _ = self._make_mock(list(contents.keys()), contents)
        with patch.dict(sys.modules, {
            "azure": mock_azure,
            "azure.storage": mock_azure.storage,
            "azure.storage.blob": mock_module,
        }):
            source = AzureBlobSource({
                "connection_string": "DefaultEndpointsProtocol=https;...",
                "container": "my-container",
                "file_pattern": "*.json",
            })
            results = list(source.read())
        assert len(results) == 2

    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {
            "azure": None, "azure.storage": None, "azure.storage.blob": None,
        }):
            source = AzureBlobSource({
                "connection_string": "cs",
                "container": "my-container",
            })
            with pytest.raises(SourceError, match="azure-storage-blob"):
                list(source.read())

    def test_meta_has_container(self):
        contents = {"f.bin": b"data"}
        mock_azure, mock_module, _ = self._make_mock(["f.bin"], contents)
        with patch.dict(sys.modules, {
            "azure": mock_azure, "azure.storage": mock_azure.storage, "azure.storage.blob": mock_module,
        }):
            source = AzureBlobSource({"connection_string": "cs", "container": "my-container"})
            _, meta = list(source.read())[0]
        assert meta["source_container"] == "my-container"

    def test_delete_after_read(self):
        contents = {"f.txt": b"data"}
        mock_azure, mock_module, mock_service = self._make_mock(["f.txt"], contents)
        with patch.dict(sys.modules, {
            "azure": mock_azure, "azure.storage": mock_azure.storage, "azure.storage.blob": mock_module,
        }):
            source = AzureBlobSource({"connection_string": "cs", "container": "c", "delete_after_read": True})
            list(source.read())
        # The blob client created during read should have had delete_blob called
        assert len(mock_service._created_clients) >= 1
        assert any(bc.delete_blob.called for bc in mock_service._created_clients)

    def test_empty_container_yields_nothing(self):
        mock_azure, mock_module, _ = self._make_mock([], {})
        with patch.dict(sys.modules, {
            "azure": mock_azure, "azure.storage": mock_azure.storage, "azure.storage.blob": mock_module,
        }):
            source = AzureBlobSource({"connection_string": "cs", "container": "c"})
            assert list(source.read()) == []


class TestAzureBlobSink:
    def _make_sink_mock(self):
        mock_blob_client = MagicMock()
        mock_service = MagicMock()
        mock_service.get_blob_client.return_value = mock_blob_client
        mock_BlobServiceClient = MagicMock()
        mock_BlobServiceClient.from_connection_string.return_value = mock_service
        mock_module = MagicMock()
        mock_module.BlobServiceClient = mock_BlobServiceClient
        mock_azure = MagicMock()
        mock_azure.storage.blob = mock_module
        return mock_azure, mock_module, mock_service, mock_blob_client

    def test_uploads_blob(self):
        mock_azure, mock_module, _, mock_blob_client = self._make_sink_mock()
        with patch.dict(sys.modules, {
            "azure": mock_azure, "azure.storage": mock_azure.storage, "azure.storage.blob": mock_module,
        }):
            sink = AzureBlobSink({"connection_string": "cs", "container": "c", "blob_template": "out.json"})
            sink.write(b'{"x":1}', {"pipeline_name": "test"})
        mock_blob_client.upload_blob.assert_called_once()

    def test_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {
            "azure": None, "azure.storage": None, "azure.storage.blob": None,
        }):
            sink = AzureBlobSink({"connection_string": "cs", "container": "c"})
            with pytest.raises(SinkError, match="azure-storage-blob"):
                sink.write(b"data", {})

    def test_blob_template_tokens(self):
        mock_azure, mock_module, mock_service, mock_blob_client = self._make_sink_mock()
        with patch.dict(sys.modules, {
            "azure": mock_azure, "azure.storage": mock_azure.storage, "azure.storage.blob": mock_module,
        }):
            sink = AzureBlobSink({
                "connection_string": "cs",
                "container": "c",
                "blob_template": "{pipeline}/{source_filename}",
            })
            sink.write(b"data", {"pipeline_name": "mypipe", "source_filename": "input.csv"})
        mock_service.get_blob_client.assert_called_with(container="c", blob="mypipe/input.csv")
