"""Unit tests for tram/agent/assets.py — schema and MIB sync from manager."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import respx

from tram.agent.assets import (
    _sync_all_schemas,
    _sync_mib,
    collect_mib_modules,
    sync_assets,
)

# ── collect_mib_modules ────────────────────────────────────────────────────


class TestCollectMibModules:
    def _cfg(self, source_mibs=(), sink_mibs=()):
        src = MagicMock()
        src.mib_modules = list(source_mibs)
        sink = MagicMock()
        sink.mib_modules = list(sink_mibs)
        cfg = MagicMock()
        cfg.source = src
        cfg.sinks = [sink]
        return cfg

    def test_empty(self):
        cfg = self._cfg()
        assert collect_mib_modules(cfg) == []

    def test_source_mibs(self):
        cfg = self._cfg(source_mibs=["IF-MIB", "ENTITY-MIB"])
        assert collect_mib_modules(cfg) == ["IF-MIB", "ENTITY-MIB"]

    def test_sink_mibs(self):
        cfg = self._cfg(sink_mibs=["CISCO-MIB"])
        assert collect_mib_modules(cfg) == ["CISCO-MIB"]

    def test_deduplication(self):
        cfg = self._cfg(source_mibs=["IF-MIB", "IF-MIB"], sink_mibs=["IF-MIB"])
        assert collect_mib_modules(cfg) == ["IF-MIB"]

    def test_no_mib_modules_attr(self):
        """Source without mib_modules (e.g. SFTP) should not raise."""
        cfg = MagicMock()
        del cfg.source.mib_modules   # attribute doesn't exist
        cfg.sinks = []
        assert collect_mib_modules(cfg) == []

    def test_none_mib_modules(self):
        """mib_modules=None should be treated as empty."""
        src = MagicMock()
        src.mib_modules = None
        cfg = MagicMock()
        cfg.source = src
        cfg.sinks = []
        assert collect_mib_modules(cfg) == []


# ── sync_assets: top-level no-op cases ────────────────────────────────────


class TestSyncAssetsNoOp:
    def test_no_manager_url(self, tmp_path):
        """No manager URL → function returns without making any HTTP calls."""
        cfg = MagicMock()
        cfg.source.mib_modules = []
        cfg.sinks = []
        # Should not raise
        sync_assets(cfg, manager_url="", data_dir=str(tmp_path))
        # No files created
        assert list(tmp_path.iterdir()) == []

    def test_connection_error_is_swallowed(self, tmp_path):
        """Network error during sync must not propagate."""
        cfg = MagicMock()
        cfg.source.mib_modules = ["IF-MIB"]
        cfg.sinks = []
        with patch("httpx.Client") as mock_cls:
            mock_cls.side_effect = httpx.ConnectError("refused")
            sync_assets(cfg, manager_url="http://manager:8765", data_dir=str(tmp_path))
        # No exception raised


# ── _sync_all_schemas ──────────────────────────────────────────────────────


class TestSyncAllSchemas:
    @respx.mock
    def test_fetches_and_writes_files(self, tmp_path):
        schema_dir = tmp_path / "schemas"

        respx.get("http://manager/api/schemas").mock(
            return_value=httpx.Response(200, json=[
                {"path": "cisco/GenericRecord.proto"},
                {"path": "ericsson/event.avsc"},
            ])
        )
        respx.get("http://manager/api/schemas/cisco/GenericRecord.proto").mock(
            return_value=httpx.Response(200, content=b"syntax = \"proto3\";")
        )
        respx.get("http://manager/api/schemas/ericsson/event.avsc").mock(
            return_value=httpx.Response(200, content=b'{"type":"record"}')
        )

        with httpx.Client(base_url="http://manager") as client:
            _sync_all_schemas(client, schema_dir)

        assert (schema_dir / "cisco" / "GenericRecord.proto").read_bytes() == b'syntax = "proto3";'
        assert (schema_dir / "ericsson" / "event.avsc").read_bytes() == b'{"type":"record"}'

    @respx.mock
    def test_empty_list_is_noop(self, tmp_path):
        schema_dir = tmp_path / "schemas"
        respx.get("http://manager/api/schemas").mock(
            return_value=httpx.Response(200, json=[])
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_all_schemas(client, schema_dir)
        assert not schema_dir.exists()

    @respx.mock
    def test_list_failure_is_swallowed(self, tmp_path):
        schema_dir = tmp_path / "schemas"
        respx.get("http://manager/api/schemas").mock(
            return_value=httpx.Response(500)
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_all_schemas(client, schema_dir)   # must not raise

    @respx.mock
    def test_individual_fetch_failure_continues(self, tmp_path):
        """One failing schema should not abort the rest."""
        schema_dir = tmp_path / "schemas"
        respx.get("http://manager/api/schemas").mock(
            return_value=httpx.Response(200, json=[
                {"path": "a.proto"},
                {"path": "b.proto"},
            ])
        )
        respx.get("http://manager/api/schemas/a.proto").mock(
            return_value=httpx.Response(500)
        )
        respx.get("http://manager/api/schemas/b.proto").mock(
            return_value=httpx.Response(200, content=b"ok")
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_all_schemas(client, schema_dir)

        assert not (schema_dir / "a.proto").exists()
        assert (schema_dir / "b.proto").read_bytes() == b"ok"


# ── _sync_mib ──────────────────────────────────────────────────────────────


class TestSyncMib:
    @respx.mock
    def test_fetches_and_writes_py_file(self, tmp_path):
        mib_dir = tmp_path / "mibs"
        respx.get("http://manager/api/mibs/IF-MIB").mock(
            return_value=httpx.Response(200, content=b"# IF-MIB compiled")
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_mib(client, "IF-MIB", mib_dir)

        assert (mib_dir / "IF-MIB.py").read_bytes() == b"# IF-MIB compiled"

    @respx.mock
    def test_404_is_noop(self, tmp_path):
        """Standard MIBs not on manager return 404 — must not raise."""
        mib_dir = tmp_path / "mibs"
        respx.get("http://manager/api/mibs/IF-MIB").mock(
            return_value=httpx.Response(404)
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_mib(client, "IF-MIB", mib_dir)   # must not raise

        assert not (mib_dir / "IF-MIB.py").exists()

    @respx.mock
    def test_server_error_is_swallowed(self, tmp_path):
        mib_dir = tmp_path / "mibs"
        respx.get("http://manager/api/mibs/CUSTOM-MIB").mock(
            return_value=httpx.Response(500)
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_mib(client, "CUSTOM-MIB", mib_dir)   # must not raise


# ── sync_assets: end-to-end ────────────────────────────────────────────────


class TestSyncAssetsIntegration:
    @respx.mock
    def test_full_sync_with_api_key(self, tmp_path):
        """API key is forwarded as X-API-Key header."""
        cfg = MagicMock()
        cfg.source.mib_modules = ["CUSTOM-MIB"]
        cfg.sinks = []

        respx.get("http://manager/api/schemas").mock(
            return_value=httpx.Response(200, json=[{"path": "foo.proto"}])
        )
        respx.get("http://manager/api/schemas/foo.proto").mock(
            return_value=httpx.Response(200, content=b"proto content")
        )
        respx.get("http://manager/api/mibs/CUSTOM-MIB").mock(
            return_value=httpx.Response(200, content=b"# mib content")
        )

        sync_assets(
            cfg,
            manager_url="http://manager",
            data_dir=str(tmp_path),
            api_key="secret-key",
        )

        assert (tmp_path / "schemas" / "foo.proto").read_bytes() == b"proto content"
        assert (tmp_path / "mibs" / "CUSTOM-MIB.py").read_bytes() == b"# mib content"

        # Verify API key was sent
        for req in respx.calls:
            assert req.request.headers.get("x-api-key") == "secret-key"

    @respx.mock
    def test_no_mibs_skips_mib_fetch(self, tmp_path):
        """Pipeline with no mib_modules → no /api/mibs/* calls."""
        cfg = MagicMock()
        cfg.source.mib_modules = []
        cfg.sinks = []

        respx.get("http://manager/api/schemas").mock(
            return_value=httpx.Response(200, json=[])
        )

        sync_assets(cfg, manager_url="http://manager", data_dir=str(tmp_path))

        # Only one call: GET /api/schemas
        assert len(respx.calls) == 1
