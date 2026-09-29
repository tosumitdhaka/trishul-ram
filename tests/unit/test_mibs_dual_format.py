"""v1.5.0 lane A — dual-format MIB serving (routers/mibs) tests.

Covers the flag-period serving contract: the pysmi ``.py`` module and the
tsmi JSON IR bundle coexist in ``TRAM_MIB_DIR``, the list endpoint reports
both, GET /api/mibs/{name} serves either format (``format=auto|py|json``),
and DELETE removes both. Everything here is flag-off safe — it exercises the
serving surface that legacy callers also use.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.mibs import router as mibs_router


def _make_mibs_app():
    app = FastAPI()
    app.include_router(mibs_router)
    return app


def _write_dual_format(d: Path) -> None:
    """Populate a TRAM_MIB_DIR with both compiled formats for IF-MIB."""
    (d / "IF_MIB.py").write_text("# legacy compiled underscore")
    (d / "IF-MIB.json").write_text(json.dumps({"module": "IF-MIB", "objects": {"ifNumber": {}}}))
    # tsmi sidecars must never surface as modules
    (d / "manifest.json").write_text('{"modules": []}')
    (d / "oid_index.json").write_text('{"oids": {}}')


class TestDualFormatListing:
    def test_list_reports_both_formats(self):
        with tempfile.TemporaryDirectory() as d:
            _write_dual_format(Path(d))
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs")
        assert resp.status_code == 200
        rows = {e["name"]: e for e in resp.json()}
        assert "IF-MIB" in rows
        row = rows["IF-MIB"]
        assert row["compiled_available"] is True
        assert row["compiled_file"] == "IF_MIB.py", "the .py artifact stays the compat primary"
        assert sorted(row["compiled_formats"]) == ["json", "py"]
        assert "manifest" not in rows and "oid_index" not in rows

    def test_list_json_only_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "VENDOR-MIB.json").write_text(
                json.dumps({"module": "VENDOR-MIB", "objects": {}})
            )
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs")
        assert resp.status_code == 200
        rows = {e["name"]: e for e in resp.json()}
        assert "VENDOR-MIB" in rows
        assert rows["VENDOR-MIB"]["compiled_available"] is True
        assert rows["VENDOR-MIB"]["compiled_file"] == "VENDOR-MIB.json"
        assert rows["VENDOR-MIB"]["compiled_formats"] == ["json"]


class TestDualFormatServing:
    def test_auto_prefers_py(self):
        with tempfile.TemporaryDirectory() as d:
            _write_dual_format(Path(d))
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs/IF-MIB")
        assert resp.status_code == 200
        assert resp.text == "# legacy compiled underscore"

    def test_format_py_serves_py(self):
        with tempfile.TemporaryDirectory() as d:
            _write_dual_format(Path(d))
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs/IF-MIB", params={"format": "py"})
        assert resp.status_code == 200
        assert resp.text == "# legacy compiled underscore"

    def test_format_json_serves_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            _write_dual_format(Path(d))
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs/IF-MIB", params={"format": "json"})
        assert resp.status_code == 200
        assert json.loads(resp.text)["module"] == "IF-MIB"

    def test_auto_falls_back_to_json_when_only_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "VENDOR-MIB.json").write_text(
                json.dumps({"module": "VENDOR-MIB", "objects": {}})
            )
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs/VENDOR-MIB")
        assert resp.status_code == 200
        assert json.loads(resp.text)["module"] == "VENDOR-MIB"

    def test_format_json_404_when_only_py(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "IF_MIB.py").write_text("# legacy")
            app = _make_mibs_app()
            client = TestClient(app, raise_server_exceptions=False)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs/IF-MIB", params={"format": "json"})
        assert resp.status_code == 404

    def test_format_py_404_when_only_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "VENDOR-MIB.json").write_text(
                json.dumps({"module": "VENDOR-MIB", "objects": {}})
            )
            app = _make_mibs_app()
            client = TestClient(app, raise_server_exceptions=False)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs/VENDOR-MIB", params={"format": "py"})
        assert resp.status_code == 404

    def test_unknown_format_is_422(self):
        with tempfile.TemporaryDirectory() as d:
            _write_dual_format(Path(d))
            app = _make_mibs_app()
            client = TestClient(app, raise_server_exceptions=False)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs/IF-MIB", params={"format": "yaml"})
        assert resp.status_code == 422


class TestDualFormatDelete:
    def test_delete_removes_both_formats(self):
        with tempfile.TemporaryDirectory() as d:
            _write_dual_format(Path(d))
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.delete("/api/mibs/IF-MIB")
                listing = client.get("/api/mibs")
            assert resp.status_code == 200
            assert resp.json()["compiled_files"] == ["IF-MIB.json", "IF_MIB.py"]
            assert not (Path(d) / "IF_MIB.py").exists()
            assert not (Path(d) / "IF-MIB.json").exists()
            assert (Path(d) / "manifest.json").exists(), "sidecars survive a module delete"
            assert "IF-MIB" not in {e["name"] for e in listing.json()}


class TestFlagOnUpload:
    def test_upload_with_trishul_flag_compiles_json_bundle(self):
        """Real trishul-smi compile through the upload endpoint (flag on)."""
        pytest.importorskip("trishul_smi")
        fixture = (
            b"SELF-MIB DEFINITIONS ::= BEGIN\n\n"
            b"selfMib OBJECT IDENTIFIER ::= { enterprises 99999 }\n\n"
            b"selfObject OBJECT-TYPE\n"
            b"    SYNTAX INTEGER\n"
            b"    MAX-ACCESS read-only\n"
            b"    STATUS current\n"
            b"    DESCRIPTION \"test\"\n"
            b"    ::= { selfMib 1 }\n\n"
            b"END\n"
        )
        with tempfile.TemporaryDirectory() as d:
            source_dir = Path(d) / "sources"
            app = _make_mibs_app()
            client = TestClient(app, raise_server_exceptions=False)
            with patch.dict(
                os.environ,
                {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": str(source_dir), "TRAM_SNMP_STACK": "trishul"},
            ):
                resp = client.post(
                    "/api/mibs/upload",
                    files={"file": ("SELF-MIB.mib", fixture, "text/plain")},
                )
            assert resp.status_code == 200
            body = resp.json()
            assert body["compiled"] == ["SELF-MIB"]
            assert body["stack"] == "trishul"
            assert body["target_status"] == "compiled"
            bundle_path = Path(d) / "SELF-MIB.json"
            assert bundle_path.is_file()
            assert json.loads(bundle_path.read_text())["module"] == "SELF-MIB"
            assert not (Path(d) / "SELF_MIB.py").exists()