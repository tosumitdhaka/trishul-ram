"""Tests for S3/MinIO source and sink connectors."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.s3.sink import S3Sink
from tram.connectors.s3.source import S3Source
from tram.core.exceptions import SinkError, SourceError


def _make_boto3_mock(keys: list[str], contents: dict[str, bytes]) -> MagicMock:
    """Return a fake boto3 module with a pre-configured S3 client."""
    mock_client = MagicMock()

    pages = [{"Contents": [{"Key": k} for k in keys]}] if keys else [{}]
    paginator = MagicMock()
    paginator.paginate.return_value = pages
    mock_client.get_paginator.return_value = paginator

    def get_object(Bucket, Key):
        data = contents.get(Key, b"")
        body = MagicMock()
        body.read.return_value = data
        return {"Body": body}

    mock_client.get_object.side_effect = get_object

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = mock_client
    return mock_boto3, mock_client


# ── S3Source ───────────────────────────────────────────────────────────────


class TestS3Source:
    def test_reads_matching_objects(self):
        contents = {
            "prefix/a.json": b'[{"x":1}]',
            "prefix/b.json": b'[{"x":2}]',
        }
        mock_boto3, _ = _make_boto3_mock(list(contents.keys()), contents)

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            source = S3Source({
                "bucket": "my-bucket",
                "prefix": "prefix/",
                "file_pattern": "*.json",
            })
            results = list(source.read())

        assert len(results) == 2
        filenames = {meta["source_filename"] for _, meta in results}
        assert "a.json" in filenames
        assert "b.json" in filenames

    def test_yields_correct_bytes(self):
        contents = {"data/test.bin": b"hello s3"}
        mock_boto3, _ = _make_boto3_mock(list(contents.keys()), contents)

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            source = S3Source({"bucket": "my-bucket"})
            results = list(source.read())

        assert results[0][0] == b"hello s3"

    def test_boto3_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"boto3": None}):
            source = S3Source({"bucket": "my-bucket"})
            with pytest.raises(SourceError, match="boto3"):
                list(source.read())

    def test_delete_after_read(self):
        contents = {"f.txt": b"data"}
        mock_boto3, mock_client = _make_boto3_mock(["f.txt"], contents)

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            source = S3Source({"bucket": "my-bucket", "delete_after_read": True})
            list(source.read())

        mock_client.delete_object.assert_called_once_with(Bucket="my-bucket", Key="f.txt")

    def test_move_after_read(self):
        contents = {"f.txt": b"data"}
        mock_boto3, mock_client = _make_boto3_mock(["f.txt"], contents)

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            source = S3Source({
                "bucket": "my-bucket",
                "move_after_read": "processed",
            })
            list(source.read())

        mock_client.copy_object.assert_called_once()
        mock_client.delete_object.assert_called_once()

    def test_empty_bucket_yields_nothing(self):
        mock_boto3, _ = _make_boto3_mock([], {})

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            source = S3Source({"bucket": "my-bucket"})
            assert list(source.read()) == []

    def test_meta_contains_bucket(self):
        contents = {"key.bin": b"d"}
        mock_boto3, _ = _make_boto3_mock(["key.bin"], contents)

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            source = S3Source({"bucket": "my-bucket"})
            _, meta = list(source.read())[0]

        assert meta["source_bucket"] == "my-bucket"


# ── S3Sink ─────────────────────────────────────────────────────────────────


class TestS3Sink:
    def test_puts_object(self):
        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            sink = S3Sink({
                "bucket": "my-bucket",
                "key_template": "output/data.json",
            })
            sink.write(b'[{"x":1}]', {"pipeline_name": "test"})

        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "my-bucket"
        assert call_kwargs["Key"] == "output/data.json"
        assert call_kwargs["Body"] == b'[{"x":1}]'

    def test_boto3_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {"boto3": None}):
            sink = S3Sink({"bucket": "my-bucket"})
            with pytest.raises(SinkError, match="boto3"):
                sink.write(b"data", {})

    def test_key_template_tokens(self):
        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            sink = S3Sink({
                "bucket": "my-bucket",
                "key_template": "{pipeline}/{source_filename}",
            })
            sink.write(b"data", {"pipeline_name": "mypipe", "source_filename": "input.csv"})

        key = mock_client.put_object.call_args[1]["Key"]
        assert key == "mypipe/input.csv"

    def test_key_template_supports_source_stem_and_suffix(self):
        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client

        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            sink = S3Sink({
                "bucket": "my-bucket",
                "key_template": "{source_stem}{source_suffix}",
            })
            sink.write(b"data", {"source_filename": "input.csv"})

        key = mock_client.put_object.call_args[1]["Key"]
        assert key == "input.csv"
