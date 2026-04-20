"""Tests for FTP source and sink connectors."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.ftp.sink import FTPSink
from tram.connectors.ftp.source import FTPSource
from tram.core.exceptions import SinkError, SourceError

# ── FTPSource ──────────────────────────────────────────────────────────────


class TestFTPSource:
    def _make_ftp_mock(self, files: list[str], contents: dict[str, bytes]):
        """Return a mock ftplib.FTP instance."""
        ftp = MagicMock()
        ftp.nlst.return_value = files

        def retrbinary(cmd, callback):
            filename = cmd.split("RETR ", 1)[-1]
            data = contents.get(filename, b"")
            callback(data)

        ftp.retrbinary.side_effect = retrbinary
        return ftp

    def test_reads_matching_files(self):
        contents = {
            "/remote/a.json": b'[{"x":1}]',
            "/remote/b.json": b'[{"x":2}]',
        }
        mock_ftp = self._make_ftp_mock(
            ["/remote/a.json", "/remote/b.json", "/remote/skip.csv"],
            contents,
        )

        with patch("ftplib.FTP", return_value=mock_ftp):
            source = FTPSource({
                "host": "ftp.example.com",
                "username": "user",
                "password": "pass",
                "remote_path": "/remote",
                "file_pattern": "*.json",
            })
            results = list(source.read())

        assert len(results) == 2
        filenames = {meta["source_filename"] for _, meta in results}
        assert "a.json" in filenames
        assert "b.json" in filenames

    def test_yields_correct_bytes(self):
        content = b"hello ftp"
        mock_ftp = self._make_ftp_mock(["/remote/test.bin"], {"/remote/test.bin": content})

        with patch("ftplib.FTP", return_value=mock_ftp):
            source = FTPSource({
                "host": "ftp.example.com",
                "username": "user",
                "password": "pass",
                "remote_path": "/remote",
                "file_pattern": "*.bin",
            })
            results = list(source.read())

        assert results[0][0] == content

    def test_connect_failure_raises_source_error(self):
        with patch("ftplib.FTP") as mock_cls:
            mock_cls.return_value.connect.side_effect = Exception("Connection refused")
            source = FTPSource({
                "host": "bad.host",
                "username": "user",
                "password": "pass",
            })
            with pytest.raises(SourceError, match="FTP connect failed"):
                list(source.read())

    def test_delete_after_read(self):
        mock_ftp = self._make_ftp_mock(["/remote/f.txt"], {"/remote/f.txt": b"data"})

        with patch("ftplib.FTP", return_value=mock_ftp):
            source = FTPSource({
                "host": "ftp.example.com",
                "username": "user",
                "password": "pass",
                "remote_path": "/remote",
                "delete_after_read": True,
            })
            list(source.read())

        mock_ftp.delete.assert_called_once_with("/remote/f.txt")

    def test_move_after_read(self):
        mock_ftp = self._make_ftp_mock(["/remote/f.txt"], {"/remote/f.txt": b"data"})

        with patch("ftplib.FTP", return_value=mock_ftp):
            source = FTPSource({
                "host": "ftp.example.com",
                "username": "user",
                "password": "pass",
                "remote_path": "/remote",
                "move_after_read": "/processed",
            })
            list(source.read())

        mock_ftp.rename.assert_called_once_with("/remote/f.txt", "/processed/f.txt")

    def test_empty_dir_yields_nothing(self):
        mock_ftp = self._make_ftp_mock([], {})

        with patch("ftplib.FTP", return_value=mock_ftp):
            source = FTPSource({
                "host": "ftp.example.com",
                "username": "user",
                "password": "pass",
            })
            assert list(source.read()) == []

    def test_meta_contains_source_host(self):
        mock_ftp = self._make_ftp_mock(["/r/x.bin"], {"/r/x.bin": b"d"})

        with patch("ftplib.FTP", return_value=mock_ftp):
            source = FTPSource({
                "host": "ftp.myhost.com",
                "username": "u",
                "password": "p",
                "remote_path": "/r",
            })
            _, meta = list(source.read())[0]

        assert meta["source_host"] == "ftp.myhost.com"


# ── FTPSink ────────────────────────────────────────────────────────────────


class TestFTPSink:
    def test_writes_file(self):
        mock_ftp = MagicMock()

        with patch("ftplib.FTP", return_value=mock_ftp):
            sink = FTPSink({
                "host": "ftp.example.com",
                "username": "user",
                "password": "pass",
                "remote_path": "/out",
                "filename_template": "output.json",
            })
            sink.write(b'[{"x":1}]', {"pipeline_name": "test"})

        mock_ftp.storbinary.assert_called_once()
        cmd = mock_ftp.storbinary.call_args[0][0]
        assert cmd == "STOR /out/output.json"

    def test_connect_failure_raises_sink_error(self):
        with patch("ftplib.FTP") as mock_cls:
            mock_cls.return_value.connect.side_effect = Exception("refused")
            sink = FTPSink({
                "host": "bad.host",
                "username": "user",
                "password": "pass",
                "remote_path": "/out",
            })
            with pytest.raises(SinkError, match="FTP connect failed"):
                sink.write(b"data", {})

    def test_filename_template_tokens(self):
        mock_ftp = MagicMock()

        with patch("ftplib.FTP", return_value=mock_ftp):
            sink = FTPSink({
                "host": "ftp.example.com",
                "username": "user",
                "password": "pass",
                "remote_path": "/out",
                "filename_template": "{pipeline}_{source_filename}",
            })
            sink.write(b"data", {"pipeline_name": "mypipe", "source_filename": "input.csv"})

        cmd = mock_ftp.storbinary.call_args[0][0]
        assert "mypipe_input.csv" in cmd

    def test_filename_template_supports_source_stem_and_suffix(self):
        mock_ftp = MagicMock()

        with patch("ftplib.FTP", return_value=mock_ftp):
            sink = FTPSink({
                "host": "ftp.example.com",
                "username": "user",
                "password": "pass",
                "remote_path": "/out",
                "filename_template": "{source_stem}{source_suffix}",
            })
            sink.write(b"data", {"source_filename": "input.csv"})

        cmd = mock_ftp.storbinary.call_args[0][0]
        assert cmd == "STOR /out/input.csv"
