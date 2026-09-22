"""Tests for file-done semantics guards (plan F.2 part 1) on local + SFTP sources.

Covers: stability guard (two-phase scan), min-age gate, ``.done``-suffix
filtering + strip behavior, and config-model defaults. Defaults (0 / 0 / None)
must preserve the previous behavior exactly.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from tram.connectors.local.source import LocalSource
from tram.models.pipeline import LocalSourceConfig, SFTPSourceConfig

NOW = time.time()


# ── Config models ───────────────────────────────────────────────────────────


class TestFileDoneConfigModels:
    def test_local_source_config_defaults_off(self):
        cfg = LocalSourceConfig(type="local", path="/data")
        assert cfg.file_stability_seconds == 0
        assert cfg.file_min_age_seconds == 0
        assert cfg.file_done_suffix is None

    def test_sftp_source_config_defaults_off(self):
        cfg = SFTPSourceConfig(type="sftp", host="h", username="u", password="p", remote_path="/d")
        assert cfg.file_stability_seconds == 0
        assert cfg.file_min_age_seconds == 0
        assert cfg.file_done_suffix is None

    def test_local_source_config_accepts_new_fields(self):
        cfg = LocalSourceConfig(
            type="local",
            path="/data",
            file_stability_seconds=45,
            file_min_age_seconds=10,
            file_done_suffix=".done",
        )
        assert cfg.file_stability_seconds == 45
        assert cfg.file_min_age_seconds == 10
        assert cfg.file_done_suffix == ".done"

    def test_sftp_source_config_accepts_new_fields(self):
        cfg = SFTPSourceConfig(
            type="sftp",
            host="h",
            username="u",
            password="p",
            remote_path="/d",
            file_stability_seconds=45,
            file_min_age_seconds=10,
            file_done_suffix=".done",
        )
        assert cfg.file_stability_seconds == 45
        assert cfg.file_min_age_seconds == 10
        assert cfg.file_done_suffix == ".done"


# ── LocalSource ─────────────────────────────────────────────────────────────


class TestLocalSourceFileDone:
    def test_defaults_preserve_existing_behavior(self, tmp_path):
        (tmp_path / "f.txt").write_bytes(b"data")
        source = LocalSource({"path": str(tmp_path)})
        assert source.file_stability_seconds == 0
        assert source.file_min_age_seconds == 0
        assert source.file_done_suffix is None

        results = list(source.read())
        assert len(results) == 1
        assert results[0][0] == b"data"
        assert results[0][1]["source_filename"] == "f.txt"

    def test_stability_guard_reads_stable_file(self, tmp_path):
        f = tmp_path / "pm.xml"
        f.write_bytes(b"complete")

        source = LocalSource({"path": str(tmp_path), "file_stability_seconds": 30})
        with patch("tram.connectors.local.source.time.sleep", return_value=None):
            results = list(source.read())

        assert len(results) == 1
        assert results[0][0] == b"complete"

    def test_stability_guard_skips_file_growing_between_scans(self, tmp_path):
        f = tmp_path / "pm.xml"
        f.write_bytes(b"<partial/>")

        source = LocalSource({"path": str(tmp_path), "file_stability_seconds": 30})

        def _grow_between_scans(_seconds):
            # Second scan observes a bigger file with a different mtime.
            f.write_bytes(b"<partial/>and-more-data")
            os.utime(f, (NOW + 10, NOW + 10))

        with patch("tram.connectors.local.source.time.sleep", side_effect=_grow_between_scans):
            results = list(source.read())

        assert results == []

    def test_stability_guard_skips_file_removed_between_scans(self, tmp_path):
        f = tmp_path / "pm.xml"
        f.write_bytes(b"<partial/>")

        source = LocalSource({"path": str(tmp_path), "file_stability_seconds": 30})

        def _remove_between_scans(_seconds):
            f.unlink()

        with patch("tram.connectors.local.source.time.sleep", side_effect=_remove_between_scans):
            results = list(source.read())

        assert results == []

    def test_min_age_gate_skips_young_file(self, tmp_path):
        (tmp_path / "new.xml").write_bytes(b"data")
        source = LocalSource({"path": str(tmp_path), "file_min_age_seconds": 60})
        assert list(source.read()) == []

    def test_min_age_gate_reads_old_file(self, tmp_path):
        f = tmp_path / "old.xml"
        f.write_bytes(b"data")
        os.utime(f, (NOW - 3600, NOW - 3600))

        source = LocalSource({"path": str(tmp_path), "file_min_age_seconds": 60})
        results = list(source.read())
        assert len(results) == 1
        assert results[0][1]["source_filename"] == "old.xml"

    def test_done_suffix_filters_and_strips(self, tmp_path):
        (tmp_path / "a.done").write_bytes(b'[{"a":1}]')
        (tmp_path / "b.txt").write_bytes(b"nope")
        (tmp_path / "c.done").write_bytes(b'[{"c":2}]')

        source = LocalSource({"path": str(tmp_path), "file_done_suffix": ".done"})
        results = list(source.read())

        assert len(results) == 2
        names = {meta["source_filename"] for _, meta in results}
        assert names == {"a", "c"}
        # Tokens derived from the stripped name carry no marker.
        assert all(Path(name).suffix == "" for name in names)

    def test_done_suffix_move_after_read_strips_marker(self, tmp_path):
        src = tmp_path / "in"
        dst = tmp_path / "done"
        src.mkdir()
        (src / "pm.done").write_bytes(b"data")

        source = LocalSource({
            "path": str(src),
            "file_done_suffix": ".done",
            "move_after_read": str(dst),
        })
        results = list(source.read())
        for _, meta in results:
            source.finalize(meta, success=True)

        assert not (src / "pm.done").exists()
        assert (dst / "pm").exists()
        assert not (dst / "pm.done").exists()

    def test_done_suffix_with_delete_after_read(self, tmp_path):
        (tmp_path / "pm.done").write_bytes(b"data")
        source = LocalSource({
            "path": str(tmp_path),
            "file_done_suffix": ".done",
            "delete_after_read": True,
        })
        results = list(source.read())
        for _, meta in results:
            source.finalize(meta, success=True)
        assert not (tmp_path / "pm.done").exists()


# ── SFTPSource (mocked paramiko) ────────────────────────────────────────────


class TestSFTPSourceFileDone:
    def _make_source(self, config_extras, mock_sftp, mock_transport):
        from tram.connectors.sftp.source import SFTPSource

        config = {
            "host": "test-host",
            "port": 22,
            "username": "user",
            "password": "pass",
            "remote_path": "/data",
            "file_pattern": "*",
            **config_extras,
        }
        source = SFTPSource(config)
        source._connect = MagicMock(return_value=(mock_transport, mock_sftp))
        return source

    def _make_client(self, files: dict[str, bytes], stats: dict[str, tuple[int, float]] | None = None):
        """Mock SFTP client. ``stats`` maps filename -> (size, mtime); when
        provided, ``sftp.stat`` returns those values (deterministically)."""
        mock_sftp = MagicMock()
        mock_transport = MagicMock()
        mock_sftp.listdir.return_value = list(files.keys())

        def open_file(path, mode):
            fname = path.rsplit("/", 1)[-1]
            content = files.get(fname, b"")
            fh = MagicMock()
            fh.__enter__ = MagicMock(return_value=fh)
            fh.__exit__ = MagicMock(return_value=False)
            buf = bytearray(content)

            def read(n=None):
                if not buf:
                    return b""
                chunk = bytes(buf[:n])
                del buf[:n]
                return chunk

            fh.read.side_effect = read
            return fh

        mock_sftp.open.side_effect = open_file
        if stats is not None:
            def stat(path):
                fname = path.rsplit("/", 1)[-1]
                size, mtime = stats[fname]
                att = MagicMock()
                att.st_size = size
                att.st_mtime = mtime
                return att

            mock_sftp.stat.side_effect = stat
        return mock_sftp, mock_transport

    def test_defaults_preserve_existing_behavior(self):
        mock_sftp, mock_transport = self._make_client({"f.json": b'[{"x":1}]'})
        source = self._make_source({}, mock_sftp, mock_transport)
        assert source.file_stability_seconds == 0
        assert source.file_min_age_seconds == 0
        assert source.file_done_suffix is None

        results = list(source.read())
        assert len(results) == 1
        assert results[0][1]["source_filename"] == "f.json"
        assert results[0][1]["source_path"] == "/data/f.json"

    def test_stability_guard_reads_stable_file(self):
        stats = {"pm.xml": (100, 1000.0)}
        mock_sftp, mock_transport = self._make_client({"pm.xml": b"x" * 100}, stats=stats)
        source = self._make_source({"file_stability_seconds": 30}, mock_sftp, mock_transport)

        with patch("tram.connectors.sftp.source.time.sleep", return_value=None):
            results = list(source.read())

        assert len(results) == 1

    def test_stability_guard_skips_file_growing_between_scans(self):
        stats = {"pm.xml": (100, 1000.0)}
        mock_sftp, mock_transport = self._make_client({"pm.xml": b"x" * 100}, stats=stats)
        source = self._make_source({"file_stability_seconds": 30}, mock_sftp, mock_transport)

        def _grow_between_scans(_seconds):
            # Second scan observes a bigger file with a different mtime.
            stats["pm.xml"] = (250, 2000.0)

        with patch("tram.connectors.sftp.source.time.sleep", side_effect=_grow_between_scans):
            results = list(source.read())

        assert results == []

    def test_min_age_gate_skips_young_and_reads_old(self):
        # Fresh now: a module-level NOW captured at collection can be minutes
        # stale by the time this test executes in the full suite, aging the
        # "young" file past the 60s gate and inverting the assertion.
        now = time.time()
        stats = {"old.xml": (10, now - 3600), "new.xml": (10, now - 5)}
        mock_sftp, mock_transport = self._make_client(
            {"old.xml": b"old", "new.xml": b"new"}, stats=stats
        )
        source = self._make_source({"file_min_age_seconds": 60}, mock_sftp, mock_transport)

        results = list(source.read())
        assert [meta["source_filename"] for _, meta in results] == ["old.xml"]

    def test_min_age_gate_future_mtime_is_eligible_and_warns_once(self):
        """Server clock ahead of the manager: future-mtime files must NOT be
        starved by the min-age gate, and the clock-skew warning fires once per
        source instance (never once per file)."""
        stats = {"future1.xml": (10, NOW + 3600), "future2.xml": (10, NOW + 7200)}
        mock_sftp, mock_transport = self._make_client(
            {"future1.xml": b"a", "future2.xml": b"b"}, stats=stats
        )
        source = self._make_source({"file_min_age_seconds": 60}, mock_sftp, mock_transport)

        with patch("tram.connectors.sftp.source.logger") as mock_logger:
            results = list(source.read())

        assert [meta["source_filename"] for _, meta in results] == ["future1.xml", "future2.xml"]
        mock_logger.warning.assert_called_once()

    def test_done_suffix_filters_and_strips(self):
        mock_sftp, mock_transport = self._make_client(
            {"pm.done": b'[{"pm":1}]', "raw.xml": b"nope"}
        )
        source = self._make_source({"file_done_suffix": ".done"}, mock_sftp, mock_transport)

        results = list(source.read())
        assert len(results) == 1
        content, meta = results[0]
        assert content == b'[{"pm":1}]'
        assert meta["source_filename"] == "pm"
        assert meta["source_path"] == "/data/pm.done"

    def test_done_suffix_strip_applies_to_chunk_meta(self):
        mock_sftp, mock_transport = self._make_client({"pm.done": b"0123456789"})
        source = self._make_source(
            {"file_done_suffix": ".done", "read_chunk_bytes": 4}, mock_sftp, mock_transport
        )

        results = list(source.read())
        assert len(results) == 3
        for _, meta in results:
            assert meta["source_filename"] == "pm"
        assert [meta["chunk_index"] for _, meta in results] == [0, 1, 2]

    def test_done_suffix_move_after_read_uses_stripped_name(self):
        mock_sftp, mock_transport = self._make_client({"pm.done": b"data"})
        source = self._make_source(
            {"file_done_suffix": ".done", "move_after_read": "/archive"},
            mock_sftp,
            mock_transport,
        )
        results = list(source.read())
        source.finalize(results[0][1], success=True)
        mock_sftp.rename.assert_called_once_with("/data/pm.done", "/archive/pm")