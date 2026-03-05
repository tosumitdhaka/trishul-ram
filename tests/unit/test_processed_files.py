"""Tests for processed-file tracking: TramDB, ProcessedFileTracker, SFTPSource (v0.9.0)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from tram.persistence.db import TramDB
from tram.persistence.file_tracker import ProcessedFileTracker


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """In-memory SQLite database for all tests."""
    d = TramDB(url="sqlite:///:memory:", node_id="test")
    yield d
    d.close()


@pytest.fixture
def tracker(db):
    return ProcessedFileTracker(db=db)


# ── TramDB.is_processed / mark_processed ─────────────────────────────────


class TestTramDBProcessedFiles:
    def test_is_processed_returns_false_for_new_file(self, db):
        result = db.is_processed("my-pipeline", "sftp:host:/data", "/data/file.json")
        assert result is False

    def test_mark_then_is_processed_returns_true(self, db):
        db.mark_processed("my-pipeline", "sftp:host:/data", "/data/file.json")
        result = db.is_processed("my-pipeline", "sftp:host:/data", "/data/file.json")
        assert result is True

    def test_mark_processed_twice_is_idempotent(self, db):
        """Calling mark_processed twice should not raise."""
        db.mark_processed("pipe", "sk", "/path/file.csv")
        db.mark_processed("pipe", "sk", "/path/file.csv")  # no exception
        assert db.is_processed("pipe", "sk", "/path/file.csv") is True

    def test_different_pipelines_do_not_interfere(self, db):
        """Same file path processed under two different pipeline names is tracked separately."""
        db.mark_processed("pipeline-a", "sk", "/data/f.json")
        assert db.is_processed("pipeline-a", "sk", "/data/f.json") is True
        assert db.is_processed("pipeline-b", "sk", "/data/f.json") is False

    def test_different_source_keys_do_not_interfere(self, db):
        """Same file path with two different source keys is tracked separately."""
        db.mark_processed("pipe", "sftp:host-a:/in", "/data/file.bin")
        assert db.is_processed("pipe", "sftp:host-a:/in", "/data/file.bin") is True
        assert db.is_processed("pipe", "sftp:host-b:/in", "/data/file.bin") is False

    def test_different_filepaths_tracked_independently(self, db):
        db.mark_processed("pipe", "sk", "/data/a.json")
        assert db.is_processed("pipe", "sk", "/data/a.json") is True
        assert db.is_processed("pipe", "sk", "/data/b.json") is False


# ── ProcessedFileTracker ──────────────────────────────────────────────────


class TestProcessedFileTracker:
    def test_is_processed_returns_false_for_new_file(self, tracker):
        assert tracker.is_processed("pipe", "sk", "/data/new.json") is False

    def test_mark_then_is_processed_returns_true(self, tracker):
        tracker.mark_processed("pipe", "sk", "/data/file.json")
        assert tracker.is_processed("pipe", "sk", "/data/file.json") is True

    def test_db_error_in_is_processed_returns_false(self, caplog):
        """DB error during is_processed should be swallowed; returns False."""
        mock_db = MagicMock()
        mock_db.is_processed.side_effect = RuntimeError("DB connection lost")
        t = ProcessedFileTracker(db=mock_db)

        with caplog.at_level(logging.WARNING):
            result = t.is_processed("pipe", "sk", "/data/file.json")

        assert result is False
        assert "is_processed error" in caplog.text

    def test_db_error_in_mark_processed_is_swallowed(self, caplog):
        """DB error during mark_processed should be logged and swallowed."""
        mock_db = MagicMock()
        mock_db.mark_processed.side_effect = RuntimeError("DB write failed")
        t = ProcessedFileTracker(db=mock_db)

        with caplog.at_level(logging.WARNING):
            t.mark_processed("pipe", "sk", "/data/file.json")  # should not raise

        assert "mark_processed error" in caplog.text

    def test_mark_processed_delegates_to_db(self, db):
        tracker = ProcessedFileTracker(db=db)
        tracker.mark_processed("pipe", "sk", "/data/file.json")
        # Verify via db directly
        assert db.is_processed("pipe", "sk", "/data/file.json") is True


# ── SFTPSource skip_processed ────────────────────────────────────────────


class TestSFTPSourceSkipProcessed:
    """Tests for SFTP source skip_processed behaviour with mocked paramiko."""

    def _make_sftp_source(self, config_extras: dict, mock_sftp, mock_transport):
        """Create an SFTPSource whose _connect returns (mock_transport, mock_sftp)."""
        from tram.connectors.sftp.source import SFTPSource

        config = {
            "host": "test-host",
            "port": 22,
            "username": "user",
            "password": "pass",
            "remote_path": "/data",
            "file_pattern": "*.json",
            **config_extras,
        }
        source = SFTPSource(config)
        # Patch _connect to return our mocks
        source._connect = MagicMock(return_value=(mock_transport, mock_sftp))
        return source

    def _make_sftp_client(self, filenames: list[str], file_contents: dict[str, bytes]):
        """Create a mock SFTP client that lists and opens files."""
        mock_sftp = MagicMock()
        mock_transport = MagicMock()

        mock_sftp.listdir.return_value = filenames

        def open_file(path, mode):
            fname = path.rsplit("/", 1)[-1]
            content = file_contents.get(fname, b"")
            fh = MagicMock()
            fh.__enter__ = MagicMock(return_value=fh)
            fh.__exit__ = MagicMock(return_value=False)
            fh.read.return_value = content
            return fh

        mock_sftp.open.side_effect = open_file
        mock_sftp.close = MagicMock()
        mock_transport.close = MagicMock()
        return mock_sftp, mock_transport

    def test_skip_processed_false_ignores_tracker(self):
        """With skip_processed=False, even if tracker says 'processed', file is yielded."""
        mock_sftp, mock_transport = self._make_sftp_client(
            ["file.json"], {"file.json": b'[{"x":1}]'}
        )
        mock_tracker = MagicMock()
        mock_tracker.is_processed.return_value = True  # says it's processed

        source = self._make_sftp_source(
            {
                "skip_processed": False,
                "_pipeline_name": "test-pipe",
                "_file_tracker": mock_tracker,
            },
            mock_sftp,
            mock_transport,
        )

        results = list(source.read())

        # File should still be yielded because skip_processed=False
        assert len(results) == 1
        # tracker.is_processed should NOT have been called
        mock_tracker.is_processed.assert_not_called()

    def test_skip_processed_true_skips_already_processed_file(self):
        """With skip_processed=True and tracker returning True, file is skipped."""
        mock_sftp, mock_transport = self._make_sftp_client(
            ["already_done.json"], {"already_done.json": b'[{"x":1}]'}
        )
        mock_tracker = MagicMock()
        mock_tracker.is_processed.return_value = True

        source = self._make_sftp_source(
            {
                "skip_processed": True,
                "_pipeline_name": "test-pipe",
                "_file_tracker": mock_tracker,
            },
            mock_sftp,
            mock_transport,
        )

        results = list(source.read())

        # Should be skipped
        assert len(results) == 0
        mock_tracker.is_processed.assert_called_once_with(
            "test-pipe", "sftp:test-host:/data", "/data/already_done.json"
        )
        # mark_processed should not have been called
        mock_tracker.mark_processed.assert_not_called()

    def test_skip_processed_true_yields_and_marks_new_file(self):
        """With skip_processed=True and tracker returning False, file is yielded
        and mark_processed is called afterwards."""
        mock_sftp, mock_transport = self._make_sftp_client(
            ["new_file.json"], {"new_file.json": b'[{"y":2}]'}
        )
        mock_tracker = MagicMock()
        mock_tracker.is_processed.return_value = False

        source = self._make_sftp_source(
            {
                "skip_processed": True,
                "_pipeline_name": "test-pipe",
                "_file_tracker": mock_tracker,
            },
            mock_sftp,
            mock_transport,
        )

        results = list(source.read())

        # File should be yielded
        assert len(results) == 1
        content, meta = results[0]
        assert content == b'[{"y":2}]'
        assert meta["source_filename"] == "new_file.json"

        # mark_processed must be called after yielding
        mock_tracker.mark_processed.assert_called_once_with(
            "test-pipe", "sftp:test-host:/data", "/data/new_file.json"
        )

    def test_skip_processed_true_skips_one_yields_another(self):
        """Mixed scenario: one processed file is skipped, one new file is yielded."""
        mock_sftp, mock_transport = self._make_sftp_client(
            ["old.json", "new.json"],
            {"old.json": b'[{"old":1}]', "new.json": b'[{"new":1}]'},
        )

        def is_processed_side_effect(pipeline, source_key, filepath):
            return filepath.endswith("old.json")

        mock_tracker = MagicMock()
        mock_tracker.is_processed.side_effect = is_processed_side_effect

        source = self._make_sftp_source(
            {
                "skip_processed": True,
                "_pipeline_name": "test-pipe",
                "_file_tracker": mock_tracker,
            },
            mock_sftp,
            mock_transport,
        )

        results = list(source.read())

        # Only the new file should be yielded
        assert len(results) == 1
        assert results[0][1]["source_filename"] == "new.json"

        # mark_processed called only for the new file
        mock_tracker.mark_processed.assert_called_once_with(
            "test-pipe", "sftp:test-host:/data", "/data/new.json"
        )
