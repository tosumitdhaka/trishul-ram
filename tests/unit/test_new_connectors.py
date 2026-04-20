"""Tests for new connectors: local source/sink, rest source/sink."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.local.sink import LocalSink
from tram.connectors.local.source import LocalSource
from tram.connectors.sftp.sink import SFTPSink
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

    def test_filename_template_supports_source_stem_and_suffix(self, tmp_path):
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "{source_stem}{source_suffix}",
        })
        sink.write(b"data", {"source_filename": "input.csv"})
        written = list(tmp_path.glob("input.csv"))
        assert len(written) == 1

    def test_single_mode_overwrite_false_raises_on_existing(self, tmp_path):
        (tmp_path / "out.bin").write_bytes(b"old")
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "out.bin",
            "file_mode": "single",
            "overwrite": False,
        })
        with pytest.raises(SinkError):
            sink.write(b"new", {})

    def test_single_mode_overwrite_true_replaces_file(self, tmp_path):
        (tmp_path / "out.bin").write_bytes(b"old")
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "out.bin",
            "file_mode": "single",
            "overwrite": True,
        })
        sink.write(b"new", {})
        assert (tmp_path / "out.bin").read_bytes() == b"new"

    def test_append_mode_is_default(self, tmp_path):
        (tmp_path / "out.bin").write_bytes(b"old")
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "out.bin",
        })
        sink.write(b"new", {})
        assert (tmp_path / "out.bin").read_bytes() == b"oldnew"

    def test_append_ndjson_rolls_by_max_records(self, tmp_path):
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "events.ndjson",
            "file_mode": "append",
            "max_records": 2,
        })

        for idx in range(3):
            sink.write(
                json.dumps({"seq": idx + 1}).encode(),
                {
                    "pipeline_name": "mypipe",
                    "serializer_type": "ndjson",
                    "serializer_config": {"type": "ndjson"},
                    "output_record_count": 1,
                },
            )

        files = sorted(tmp_path.glob("events_*.ndjson"))
        assert [path.name for path in files] == ["events_00001.ndjson", "events_00002.ndjson"]
        assert files[0].read_text() == '{"seq": 1}\n{"seq": 2}\n'
        assert files[1].read_text() == '{"seq": 3}\n'

    def test_append_csv_strips_header_after_first_write(self, tmp_path):
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "rows.csv",
            "file_mode": "append",
        })

        sink.write(
            b"id,name\r\n1,alpha\r\n",
            {
                "serializer_type": "csv",
                "serializer_config": {"type": "csv", "has_header": True},
                "output_record_count": 1,
            },
        )
        sink.write(
            b"id,name\r\n2,beta\r\n",
            {
                "serializer_type": "csv",
                "serializer_config": {"type": "csv", "has_header": True},
                "output_record_count": 1,
            },
        )

        assert (tmp_path / "rows.csv").read_text() == "id,name\n1,alpha\n2,beta\n"


# ── SFTPSink ───────────────────────────────────────────────────────────────


class _FakeRemoteFile:
    def __init__(self, files: dict[str, bytes], path: str, mode: str):
        self._files = files
        self._path = path
        self._mode = mode

    def write(self, data: bytes) -> None:
        if "a" in self._mode:
            self._files[self._path] = self._files.get(self._path, b"") + data
        else:
            self._files[self._path] = data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeSFTPClient:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = set()

    def stat(self, path: str) -> None:
        if path not in self.directories and path not in self.files:
            raise FileNotFoundError(path)

    def mkdir(self, path: str) -> None:
        self.directories.add(path)

    def open(self, path: str, mode: str):
        return _FakeRemoteFile(self.files, path, mode)

    def close(self) -> None:
        return None


class _FakeTransport:
    def close(self) -> None:
        return None


class TestSFTPSink:
    def test_filename_template_supports_source_stem_and_suffix(self):
        sftp = _FakeSFTPClient()
        sink = SFTPSink({
            "host": "example.com",
            "username": "user",
            "password": "pass",
            "remote_path": "/out",
            "filename_template": "{source_stem}{source_suffix}",
        })

        with patch.object(sink, "_connect", return_value=(_FakeTransport(), sftp)):
            sink.write(b"data", {"source_filename": "input.csv"})

        assert sftp.files["/out/input.csv"] == b"data"

    def test_append_mode_is_default(self):
        sftp = _FakeSFTPClient()
        sink = SFTPSink({
            "host": "example.com",
            "username": "user",
            "password": "pass",
            "remote_path": "/out",
            "filename_template": "out.bin",
        })

        with patch.object(sink, "_connect", return_value=(_FakeTransport(), sftp)):
            sink.write(b"old", {})
            sink.write(b"new", {})

        assert sftp.files["/out/out.bin"] == b"oldnew"

    def test_append_ndjson_rolls_by_max_records(self):
        sftp = _FakeSFTPClient()
        sink = SFTPSink({
            "host": "example.com",
            "username": "user",
            "password": "pass",
            "remote_path": "/out",
            "filename_template": "events.ndjson",
            "file_mode": "append",
            "max_records": 2,
        })

        with patch.object(sink, "_connect", return_value=(_FakeTransport(), sftp)):
            for idx in range(3):
                sink.write(
                    json.dumps({"seq": idx + 1}).encode(),
                    {
                        "pipeline_name": "mypipe",
                        "serializer_type": "ndjson",
                        "serializer_config": {"type": "ndjson"},
                        "output_record_count": 1,
                    },
                )

        assert sftp.files["/out/events_00001.ndjson"] == b'{"seq": 1}\n{"seq": 2}\n'
        assert sftp.files["/out/events_00002.ndjson"] == b'{"seq": 3}\n'

    def test_append_csv_strips_header_after_first_write(self):
        sftp = _FakeSFTPClient()
        sink = SFTPSink({
            "host": "example.com",
            "username": "user",
            "password": "pass",
            "remote_path": "/out",
            "filename_template": "rows.csv",
            "file_mode": "append",
        })

        with patch.object(sink, "_connect", return_value=(_FakeTransport(), sftp)):
            sink.write(
                b"id,name\r\n1,alpha\r\n",
                {
                    "serializer_type": "csv",
                    "serializer_config": {"type": "csv", "has_header": True},
                    "output_record_count": 1,
                },
            )
            sink.write(
                b"id,name\r\n2,beta\r\n",
                {
                    "serializer_type": "csv",
                    "serializer_config": {"type": "csv", "has_header": True},
                    "output_record_count": 1,
                },
            )

        assert sftp.files["/out/rows.csv"] == b"id,name\r\n1,alpha\r\n2,beta\r\n"

    def test_append_mode_tracks_partition_state_per_field_value(self):
        sftp = _FakeSFTPClient()
        sink = SFTPSink({
            "host": "example.com",
            "username": "user",
            "password": "pass",
            "remote_path": "/out",
            "filename_template": "{field.nf_name}_{part}.ndjson",
            "file_mode": "append",
            "max_records": 2,
        })

        with patch.object(sink, "_connect", return_value=(_FakeTransport(), sftp)):
            sink.write(
                b'{"nf_name": "MSC", "value": 1}\n{"nf_name": "MSC", "value": 2}\n',
                {
                    "field_values": {"nf_name": "MSC"},
                    "serializer_type": "ndjson",
                    "serializer_config": {"type": "ndjson"},
                    "output_record_count": 2,
                },
            )
            sink.write(
                b'{"nf_name": "SMSC", "value": 10}\n',
                {
                    "field_values": {"nf_name": "SMSC"},
                    "serializer_type": "ndjson",
                    "serializer_config": {"type": "ndjson"},
                    "output_record_count": 1,
                },
            )
            sink.write(
                b'{"nf_name": "MSC", "value": 3}\n',
                {
                    "field_values": {"nf_name": "MSC"},
                    "serializer_type": "ndjson",
                    "serializer_config": {"type": "ndjson"},
                    "output_record_count": 1,
                },
            )

        assert sftp.files["/out/MSC_00001.ndjson"] == (
            b'{"nf_name": "MSC", "value": 1}\n{"nf_name": "MSC", "value": 2}\n'
        )
        assert sftp.files["/out/MSC_00002.ndjson"] == b'{"nf_name": "MSC", "value": 3}\n'
        assert sftp.files["/out/SMSC_00001.ndjson"] == b'{"nf_name": "SMSC", "value": 10}\n'


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
