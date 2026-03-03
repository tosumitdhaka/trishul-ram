"""Tests for GCS source and sink connectors."""
from __future__ import annotations
import sys
from unittest.mock import MagicMock, patch
import pytest
from tram.connectors.gcs.source import GcsSource
from tram.connectors.gcs.sink import GcsSink
from tram.core.exceptions import SinkError, SourceError


class TestGcsSource:
    def _make_mock(self, blob_names: list[str], contents: dict[str, bytes]):
        mock_blob_list = []
        for name in blob_names:
            b = MagicMock()
            b.name = name
            b.download_as_bytes.return_value = contents.get(name, b"")
            mock_blob_list.append(b)
        mock_client = MagicMock()
        mock_client.list_blobs.return_value = mock_blob_list
        mock_storage = MagicMock()
        mock_storage.Client.return_value = mock_client
        mock_google_cloud = MagicMock()
        mock_google_cloud.storage = mock_storage
        return mock_google_cloud, mock_client

    def test_reads_matching_blobs(self):
        contents = {"prefix/a.json": b'[{"x":1}]', "prefix/b.json": b'[{"x":2}]'}
        mock_gcs, _ = self._make_mock(list(contents.keys()), contents)
        with patch.dict(sys.modules, {"google.cloud": mock_gcs, "google.cloud.storage": mock_gcs.storage}):
            source = GcsSource({"bucket": "my-bucket", "prefix": "prefix/", "file_pattern": "*.json"})
            results = list(source.read())
        assert len(results) == 2

    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"google.cloud": None, "google.cloud.storage": None}):
            source = GcsSource({"bucket": "my-bucket"})
            with pytest.raises(SourceError, match="google-cloud-storage"):
                list(source.read())

    def test_delete_after_read(self):
        contents = {"f.txt": b"data"}
        mock_gcs, mock_client = self._make_mock(["f.txt"], contents)
        with patch.dict(sys.modules, {"google.cloud": mock_gcs, "google.cloud.storage": mock_gcs.storage}):
            source = GcsSource({"bucket": "my-bucket", "delete_after_read": True})
            list(source.read())
        # blob.delete() should be called
        blob = mock_client.list_blobs.return_value[0]
        blob.delete.assert_called()

    def test_meta_has_bucket(self):
        contents = {"f.bin": b"data"}
        mock_gcs, _ = self._make_mock(["f.bin"], contents)
        with patch.dict(sys.modules, {"google.cloud": mock_gcs, "google.cloud.storage": mock_gcs.storage}):
            source = GcsSource({"bucket": "my-bucket"})
            _, meta = list(source.read())[0]
        assert meta["source_bucket"] == "my-bucket"

    def test_empty_bucket_yields_nothing(self):
        mock_gcs, mock_client = self._make_mock([], {})
        with patch.dict(sys.modules, {"google.cloud": mock_gcs, "google.cloud.storage": mock_gcs.storage}):
            source = GcsSource({"bucket": "my-bucket"})
            assert list(source.read()) == []


class TestGcsSink:
    def test_upload_blob(self):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage = MagicMock()
        mock_storage.Client.return_value = mock_client
        mock_gcs = MagicMock()
        mock_gcs.storage = mock_storage
        with patch.dict(sys.modules, {"google.cloud": mock_gcs, "google.cloud.storage": mock_gcs.storage}):
            sink = GcsSink({"bucket": "my-bucket", "blob_template": "output.json"})
            sink.write(b'{"x":1}', {"pipeline_name": "test"})
        mock_blob.upload_from_string.assert_called_once()

    def test_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {"google.cloud": None, "google.cloud.storage": None}):
            sink = GcsSink({"bucket": "my-bucket"})
            with pytest.raises(SinkError, match="google-cloud-storage"):
                sink.write(b"data", {})

    def test_blob_template_tokens(self):
        mock_blob = MagicMock()
        mock_bucket = MagicMock()
        mock_bucket.blob.return_value = mock_blob
        mock_client = MagicMock()
        mock_client.bucket.return_value = mock_bucket
        mock_storage = MagicMock()
        mock_storage.Client.return_value = mock_client
        mock_gcs = MagicMock()
        mock_gcs.storage = mock_storage
        with patch.dict(sys.modules, {"google.cloud": mock_gcs, "google.cloud.storage": mock_gcs.storage}):
            sink = GcsSink({"bucket": "my-bucket", "blob_template": "{pipeline}/{source_filename}"})
            sink.write(b"data", {"pipeline_name": "mypipe", "source_filename": "input.csv"})
        mock_bucket.blob.assert_called_once_with("mypipe/input.csv")
