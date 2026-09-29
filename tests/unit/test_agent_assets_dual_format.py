"""v1.5.0 lane A — dual-format worker MIB sync (agent/assets) tests.

The worker pulls BOTH compiled formats for every referenced MIB — the pysmi
``.py`` module (``?format=py``) and the tsmi JSON IR bundle (``?format=json``)
— with no per-worker format negotiation; each worker's SNMP stack loads
whichever file it understands. A 404 for a format is fine (that MIB is either
baked into the image or only exists in the other format) and is skipped.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import respx

from tram.agent.assets import _sync_mib, sync_assets


class TestSyncMibDualFormat:
    @respx.mock
    def test_fetches_both_formats(self, tmp_path):
        mib_dir = tmp_path / "mibs"
        respx.get("http://manager/api/mibs/CUSTOM-MIB", params={"format": "py"}).mock(
            return_value=httpx.Response(200, content=b"# CUSTOM-MIB compiled")
        )
        respx.get("http://manager/api/mibs/CUSTOM-MIB", params={"format": "json"}).mock(
            return_value=httpx.Response(200, content=b'{"module": "CUSTOM-MIB", "objects": {}}')
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_mib(client, "CUSTOM-MIB", mib_dir)

        assert (mib_dir / "CUSTOM-MIB.py").read_bytes() == b"# CUSTOM-MIB compiled"
        assert (mib_dir / "CUSTOM-MIB.json").read_bytes() == b'{"module": "CUSTOM-MIB", "objects": {}}'

    @respx.mock
    def test_404_for_both_formats_is_noop(self, tmp_path):
        """Standard MIBs baked into the image 404 on the manager."""
        mib_dir = tmp_path / "mibs"
        respx.get("http://manager/api/mibs/IF-MIB", params={"format": "py"}).mock(
            return_value=httpx.Response(404)
        )
        respx.get("http://manager/api/mibs/IF-MIB", params={"format": "json"}).mock(
            return_value=httpx.Response(404)
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_mib(client, "IF-MIB", mib_dir)

        assert not (mib_dir / "IF-MIB.py").exists()
        assert not (mib_dir / "IF-MIB.json").exists()

    @respx.mock
    def test_json_404_with_py_present_syncs_py_only(self, tmp_path):
        """Legacy-only managers serve the .py and 404 the bundle."""
        mib_dir = tmp_path / "mibs"
        respx.get("http://manager/api/mibs/LEGACY-MIB", params={"format": "py"}).mock(
            return_value=httpx.Response(200, content=b"# legacy")
        )
        respx.get("http://manager/api/mibs/LEGACY-MIB", params={"format": "json"}).mock(
            return_value=httpx.Response(404)
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_mib(client, "LEGACY-MIB", mib_dir)

        assert (mib_dir / "LEGACY-MIB.py").read_bytes() == b"# legacy"
        assert not (mib_dir / "LEGACY-MIB.json").exists()

    @respx.mock
    def test_py_404_with_json_present_syncs_json_only(self, tmp_path):
        """Trishul-only managers serve the bundle and 404 the .py."""
        mib_dir = tmp_path / "mibs"
        respx.get("http://manager/api/mibs/TSMI-MIB", params={"format": "py"}).mock(
            return_value=httpx.Response(404)
        )
        respx.get("http://manager/api/mibs/TSMI-MIB", params={"format": "json"}).mock(
            return_value=httpx.Response(200, content=b'{"module": "TSMI-MIB", "objects": {}}')
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_mib(client, "TSMI-MIB", mib_dir)

        assert (mib_dir / "TSMI-MIB.json").read_bytes() == b'{"module": "TSMI-MIB", "objects": {}}'
        assert not (mib_dir / "TSMI-MIB.py").exists()

    @respx.mock
    def test_unchanged_files_skip_write(self, tmp_path):
        """Identical content must not be rewritten — avoids mtime churn."""
        mib_dir = tmp_path / "mibs"
        mib_dir.mkdir(parents=True)
        py_dest = mib_dir / "STABLE-MIB.py"
        json_dest = mib_dir / "STABLE-MIB.json"
        py_dest.write_bytes(b"# stable")
        json_dest.write_bytes(b'{"module": "STABLE-MIB"}')
        py_mtime = py_dest.stat().st_mtime
        json_mtime = json_dest.stat().st_mtime

        respx.get("http://manager/api/mibs/STABLE-MIB", params={"format": "py"}).mock(
            return_value=httpx.Response(200, content=b"# stable")
        )
        respx.get("http://manager/api/mibs/STABLE-MIB", params={"format": "json"}).mock(
            return_value=httpx.Response(200, content=b'{"module": "STABLE-MIB"}')
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_mib(client, "STABLE-MIB", mib_dir)

        assert py_dest.stat().st_mtime == py_mtime
        assert json_dest.stat().st_mtime == json_mtime

    @respx.mock
    def test_server_error_is_swallowed(self, tmp_path):
        mib_dir = tmp_path / "mibs"
        respx.get("http://manager/api/mibs/CUSTOM-MIB", params={"format": "py"}).mock(
            return_value=httpx.Response(500)
        )
        respx.get("http://manager/api/mibs/CUSTOM-MIB", params={"format": "json"}).mock(
            return_value=httpx.Response(500)
        )
        with httpx.Client(base_url="http://manager") as client:
            _sync_mib(client, "CUSTOM-MIB", mib_dir)  # must not raise


class TestSyncAssetsDualFormat:
    @respx.mock
    def test_full_sync_pulls_both_mib_formats(self, tmp_path):
        """sync_assets syncs schemas plus both compiled MIB formats."""
        cfg = MagicMock()
        cfg.source.mib_modules = ["CUSTOM-MIB"]
        cfg.sinks = []

        respx.get("http://manager/api/schemas").mock(return_value=httpx.Response(200, json=[]))
        respx.get("http://manager/api/mibs/CUSTOM-MIB", params={"format": "py"}).mock(
            return_value=httpx.Response(200, content=b"# mib py")
        )
        respx.get("http://manager/api/mibs/CUSTOM-MIB", params={"format": "json"}).mock(
            return_value=httpx.Response(200, content=b'{"module": "CUSTOM-MIB"}')
        )

        sync_assets(cfg, manager_url="http://manager", data_dir=str(tmp_path), api_key="k")

        assert (tmp_path / "mibs" / "CUSTOM-MIB.py").read_bytes() == b"# mib py"
        assert (tmp_path / "mibs" / "CUSTOM-MIB.json").read_bytes() == b'{"module": "CUSTOM-MIB"}'
        assert len(respx.calls) == 3  # schemas + py + json
        for call in respx.calls:
            assert call.request.headers.get("x-api-key") == "k"

    @respx.mock
    def test_legacy_only_manager_404s_json(self, tmp_path):
        """A legacy-only corpus syncs the .py and skips the 404'd bundle."""
        cfg = MagicMock()
        cfg.source.mib_modules = ["CUSTOM-MIB"]
        cfg.sinks = []

        respx.get("http://manager/api/schemas").mock(return_value=httpx.Response(200, json=[]))
        respx.get("http://manager/api/mibs/CUSTOM-MIB", params={"format": "py"}).mock(
            return_value=httpx.Response(200, content=b"# mib py")
        )
        respx.get("http://manager/api/mibs/CUSTOM-MIB", params={"format": "json"}).mock(
            return_value=httpx.Response(404)
        )

        sync_assets(cfg, manager_url="http://manager", data_dir=str(tmp_path))

        assert (tmp_path / "mibs" / "CUSTOM-MIB.py").read_bytes() == b"# mib py"
        assert not (tmp_path / "mibs" / "CUSTOM-MIB.json").exists()