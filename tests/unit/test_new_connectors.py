"""Tests for new connectors: local source/sink, rest source/sink."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.local.sink import LocalSink
from tram.connectors.local.source import LocalSource
from tram.core.exceptions import SinkError, SourceError

# ── LocalSource ────────────────────────────────────────────────────────────


class TestLocalSource:
    def test_reads_matching_files(self, tmp_path):
        (tmp_path / "a.json").write_bytes(b'[{"x":1}]')
        (tmp_path / "b.json").write_bytes(b'[{"x":2}]')
        (tmp_path / "skip.csv").write_bytes(b"a,b")

        source = LocalSource({"path": str(tmp_path), "file_pattern": "*.json"})
        results = list(source.read())

        assert len(results) == 2
        filenames = {meta["source_filename"] for _, meta in results}
        assert "a.json" in filenames
        assert "b.json" in filenames
        assert "skip.csv" not in filenames

    def test_yields_correct_bytes(self, tmp_path):
        content = b"hello world"
        (tmp_path / "test.bin").write_bytes(content)
        source = LocalSource({"path": str(tmp_path), "file_pattern": "*.bin"})
        results = list(source.read())
        assert results[0][0] == content

    def test_move_after_read(self, tmp_path):
        src = tmp_path / "in"
        dst = tmp_path / "processed"
        src.mkdir()
        (src / "f.txt").write_bytes(b"data")

        source = LocalSource({
            "path": str(src),
            "move_after_read": str(dst),
        })
        list(source.read())  # consume all

        assert not (src / "f.txt").exists()
        assert (dst / "f.txt").exists()

    def test_delete_after_read(self, tmp_path):
        (tmp_path / "f.txt").write_bytes(b"data")
        source = LocalSource({"path": str(tmp_path), "delete_after_read": True})
        list(source.read())
        assert not (tmp_path / "f.txt").exists()

    def test_missing_path_raises(self):
        source = LocalSource({"path": "/nonexistent/xyz123"})
        with pytest.raises(SourceError):
            list(source.read())

    def test_recursive(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "top.txt").write_bytes(b"top")
        (sub / "deep.txt").write_bytes(b"deep")

        source = LocalSource({"path": str(tmp_path), "file_pattern": "*.txt", "recursive": True})
        results = list(source.read())
        assert len(results) == 2

    def test_empty_dir_yields_nothing(self, tmp_path):
        source = LocalSource({"path": str(tmp_path)})
        assert list(source.read()) == []


# ── LocalSink ──────────────────────────────────────────────────────────────


class TestLocalSink:
    def test_writes_file(self, tmp_path):
        sink = LocalSink({"path": str(tmp_path), "filename_template": "out.json"})
        sink.write(b'[{"x":1}]', {"pipeline_name": "test"})
        assert (tmp_path / "out.json").read_bytes() == b'[{"x":1}]'

    def test_creates_directory(self, tmp_path):
        out = tmp_path / "deep" / "nested"
        sink = LocalSink({"path": str(out), "filename_template": "out.bin"})
        sink.write(b"data", {})
        assert (out / "out.bin").exists()

    def test_filename_template_tokens(self, tmp_path):
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "{pipeline}_{source_filename}",
        })
        sink.write(b"data", {"pipeline_name": "mypipe", "source_filename": "input.csv"})
        written = list(tmp_path.glob("mypipe_input.csv"))
        assert len(written) == 1

    def test_overwrite_false_raises_on_existing(self, tmp_path):
        (tmp_path / "out.bin").write_bytes(b"old")
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "out.bin",
            "overwrite": False,
        })
        with pytest.raises(SinkError):
            sink.write(b"new", {})

    def test_overwrite_true_replaces_file(self, tmp_path):
        (tmp_path / "out.bin").write_bytes(b"old")
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "out.bin",
            "overwrite": True,
        })
        sink.write(b"new", {})
        assert (tmp_path / "out.bin").read_bytes() == b"new"


# ── RestSource ─────────────────────────────────────────────────────────────


class TestRestSource:
    def test_simple_get(self):
        from tram.connectors.rest.source import RestSource

        mock_resp = MagicMock()
        mock_resp.content = b'[{"id": 1}]'
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.request.return_value = mock_resp

            source = RestSource({"url": "http://example.com/api/data"})
            results = list(source.read())

        assert len(results) == 1
        assert results[0][0] == b'[{"id": 1}]'
        assert results[0][1]["source_url"] == "http://example.com/api/data"

    def test_response_path_extraction(self):
        from tram.connectors.rest.source import RestSource

        payload = {"data": {"items": [{"id": 1}, {"id": 2}]}}
        mock_resp = MagicMock()
        mock_resp.content = json.dumps(payload).encode()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.request.return_value = mock_resp

            source = RestSource({
                "url": "http://example.com/api",
                "response_path": "data.items",
            })
            results = list(source.read())

        data = json.loads(results[0][0])
        assert data == [{"id": 1}, {"id": 2}]

    def test_bearer_auth_header(self):
        from tram.connectors.rest.source import RestSource

        mock_resp = MagicMock()
        mock_resp.content = b"[]"
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.request.return_value = mock_resp

            source = RestSource({
                "url": "http://example.com/api",
                "auth_type": "bearer",
                "token": "mytoken123",
            })
            list(source.read())

        call_kwargs = mock_client.request.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer mytoken123"


# ── RestSink ───────────────────────────────────────────────────────────────


class TestRestSink:
    def test_post_data(self):
        from tram.connectors.rest.sink import RestSink

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.request.return_value = mock_resp

            sink = RestSink({"url": "http://example.com/ingest"})
            sink.write(b'[{"x":1}]', {})

        mock_client.request.assert_called_once()
        call_args = mock_client.request.call_args
        assert call_args[0][0] == "POST"
        assert call_args[0][1] == "http://example.com/ingest"

    def test_unexpected_status_raises(self):
        from tram.connectors.rest.sink import RestSink

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.request.return_value = mock_resp

            sink = RestSink({"url": "http://example.com/ingest"})
            with pytest.raises(SinkError):
                sink.write(b"data", {})
