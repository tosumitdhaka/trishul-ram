"""Tests for webhooks, templates, and mibs routers."""
from __future__ import annotations

import os
import queue
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.mibs import router as mibs_router
from tram.api.routers.templates import router as templates_router
from tram.api.routers.webhooks import router as webhooks_router
from tram.core.mib_compiler import MibCompileResult, MibSupportUnavailable

# ── Webhooks ───────────────────────────────────────────────────────────────────


def _make_webhook_app():
    app = FastAPI()
    app.include_router(webhooks_router)
    return app


class TestWebhooks:
    def test_no_registered_path_returns_404(self):
        app = _make_webhook_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch("tram.connectors.webhook.source._WEBHOOK_REGISTRY", {}):
            resp = client.post("/webhooks/nonexistent", content=b"data")
        assert resp.status_code == 404

    def test_registered_path_accepts_post(self):
        q = queue.Queue()
        app = _make_webhook_app()
        client = TestClient(app, raise_server_exceptions=False)
        registry = {"my-hook": q}
        secrets = {}
        with patch("tram.connectors.webhook.source._WEBHOOK_REGISTRY", registry), \
             patch("tram.connectors.webhook._WEBHOOK_SECRETS", secrets):
            resp = client.post("/webhooks/my-hook", content=b'{"event": "test"}')
        assert resp.status_code == 202
        assert not q.empty()

    def test_secret_required_rejects_wrong_auth(self):
        q = queue.Queue()
        app = _make_webhook_app()
        client = TestClient(app, raise_server_exceptions=False)
        registry = {"secure-hook": q}
        secrets = {"secure-hook": "mysecret"}
        with patch("tram.connectors.webhook.source._WEBHOOK_REGISTRY", registry), \
             patch("tram.connectors.webhook._WEBHOOK_SECRETS", secrets):
            resp = client.post(
                "/webhooks/secure-hook",
                content=b"data",
                headers={"Authorization": "Bearer wrongsecret"},
            )
        assert resp.status_code == 401

    def test_secret_required_accepts_correct_auth(self):
        q = queue.Queue()
        app = _make_webhook_app()
        client = TestClient(app, raise_server_exceptions=False)
        registry = {"secure-hook": q}
        secrets = {"secure-hook": "mysecret"}
        with patch("tram.connectors.webhook.source._WEBHOOK_REGISTRY", registry), \
             patch("tram.connectors.webhook._WEBHOOK_SECRETS", secrets):
            resp = client.post(
                "/webhooks/secure-hook",
                content=b"data",
                headers={"Authorization": "Bearer mysecret"},
            )
        assert resp.status_code == 202

    def test_full_queue_returns_503(self):
        q = queue.Queue(maxsize=1)
        q.put_nowait((b"existing", {}))  # fill the queue
        app = _make_webhook_app()
        client = TestClient(app, raise_server_exceptions=False)
        registry = {"my-hook": q}
        secrets = {}
        with patch("tram.connectors.webhook.source._WEBHOOK_REGISTRY", registry), \
             patch("tram.connectors.webhook._WEBHOOK_SECRETS", secrets):
            resp = client.post("/webhooks/my-hook", content=b"overflow")
        assert resp.status_code == 503


# ── Templates ─────────────────────────────────────────────────────────────────


def _make_templates_app(templates_dir: str):
    app = FastAPI()
    app.include_router(templates_router)
    app.state.config = MagicMock()
    app.state.config.templates_dir = templates_dir

    # Invalidate module-level cache
    import tram.api.routers.templates as tmpl_mod
    tmpl_mod._cache = None
    tmpl_mod._cache_at = 0.0

    return app


class TestTemplates:
    def test_empty_dir_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as d:
            app = _make_templates_app(d)
            client = TestClient(app)
            resp = client.get("/api/templates")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_valid_yaml_template_returned(self):
        with tempfile.TemporaryDirectory() as d:
            yaml_content = """
pipeline:
  name: demo-pipe
  description: A demo pipeline
  schedule:
    type: interval
  source:
    type: sftp
    host: localhost
  sinks:
    - type: local
      path: /tmp/out
"""
            Path(d, "demo.yaml").write_text(yaml_content)
            app = _make_templates_app(d)
            client = TestClient(app)
            resp = client.get("/api/templates")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        t = data[0]
        assert t["id"] == "demo"
        assert t["name"] == "demo-pipe"
        assert "sftp" in t["tags"]

    def test_root_level_yaml_template_returned(self):
        with tempfile.TemporaryDirectory() as d:
            yaml_content = """
name: demo-root
description: Root-level pipeline example
schedule:
  type: stream
source:
  type: snmp_trap
  host: 0.0.0.0
sinks:
  - type: kafka
    topic: demo
"""
            Path(d, "demo-root.yaml").write_text(yaml_content)
            app = _make_templates_app(d)
            client = TestClient(app)
            resp = client.get("/api/templates")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        t = data[0]
        assert t["id"] == "demo-root"
        assert t["name"] == "demo-root"
        assert t["description"] == "Root-level pipeline example"
        assert t["source_type"] == "snmp_trap"
        assert t["sink_types"] == ["kafka"]
        assert t["schedule_type"] == "stream"
        assert "snmp_trap" in t["tags"]
        assert "kafka" in t["tags"]
        assert "stream" in t["tags"]

    def test_invalid_yaml_skipped_gracefully(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "bad.yaml").write_text(": invalid: yaml: [\n")
            app = _make_templates_app(d)
            client = TestClient(app)
            resp = client.get("/api/templates")
        assert resp.status_code == 200
        # Bad file skipped, returns empty list
        assert isinstance(resp.json(), list)


# ── Mibs ──────────────────────────────────────────────────────────────────────



def _make_mibs_app():
    app = FastAPI()
    app.include_router(mibs_router)
    return app


class TestMibs:
    def test_list_no_dir_returns_empty(self):
        app = _make_mibs_app()
        client = TestClient(app)
        with patch.dict(os.environ, {"TRAM_MIB_DIR": "/nonexistent/path/xyz"}):
            resp = client.get("/api/mibs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_returns_local_source_and_compiled_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "IF-MIB.py").write_text("# compiled")
            source_dir = Path(d) / "sources"
            source_dir.mkdir(parents=True, exist_ok=True)
            Path(source_dir, "IF-MIB.mib").write_text("IF-MIB DEFINITIONS ::= BEGIN END")
            Path(d, "OTHER.txt").write_text("ignore")
            Path(d, "_private.py").write_text("skip")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": str(source_dir)}):
                resp = client.get("/api/mibs")
        assert resp.status_code == 200
        rows = {e["name"]: e for e in resp.json()}
        assert "IF-MIB" in rows
        assert rows["IF-MIB"]["raw_available"] is True
        assert rows["IF-MIB"]["raw_file"] == "IF-MIB.mib"
        assert rows["IF-MIB"]["raw_origin"] == "local"
        assert rows["IF-MIB"]["compiled_available"] is True
        assert rows["IF-MIB"]["compiled_file"] == "IF-MIB.py"
        assert "OTHER" not in rows
        assert "_private" not in rows

    def test_list_does_not_include_bundled_only_mibs(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as bundled, tempfile.TemporaryDirectory() as source_dir:
            Path(bundled, "SNMPv2-SMI.mib").write_text("SNMPv2-SMI DEFINITIONS ::= BEGIN END")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(
                os.environ,
                {
                    "TRAM_MIB_DIR": d,
                    "TRAM_MIB_SOURCE_DIR": source_dir,
                    "TRAM_MIB_BUNDLED_SOURCE_DIR": bundled,
                },
            ):
                resp = client.get("/api/mibs")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_delete_nonexistent_returns_404(self):
        with tempfile.TemporaryDirectory() as d:
            app = _make_mibs_app()
            client = TestClient(app, raise_server_exceptions=False)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.delete("/api/mibs/NONEXISTENT-MIB")
        assert resp.status_code == 404

    def test_delete_existing_mib(self):
        with tempfile.TemporaryDirectory() as d:
            mib_file = Path(d, "IF-MIB.py")
            mib_file.write_text("# compiled")
            source_dir = Path(d) / "sources"
            source_dir.mkdir(parents=True, exist_ok=True)
            raw_file = source_dir / "IF-MIB.mib"
            raw_file.write_text("IF-MIB DEFINITIONS ::= BEGIN END")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": str(source_dir)}):
                resp = client.delete("/api/mibs/IF-MIB")
            assert resp.status_code == 200
            assert not mib_file.exists()
            assert not raw_file.exists()

    def test_get_existing_mib_by_dash_name(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "IF_MIB.py").write_text("# compiled underscore")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs/IF-MIB")
        assert resp.status_code == 200
        assert "# compiled underscore" in resp.text

    def test_get_existing_mib_source_by_dash_name(self):
        with tempfile.TemporaryDirectory() as d:
            source_dir = Path(d) / "sources"
            source_dir.mkdir(parents=True, exist_ok=True)
            Path(source_dir, "IF_MIB.my").write_text("IF-MIB DEFINITIONS ::= BEGIN END")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": str(source_dir)}):
                resp = client.get("/api/mibs/IF-MIB/source")
        assert resp.status_code == 200
        assert "IF-MIB DEFINITIONS" in resp.text

    def test_get_bundled_mib_source_fallback(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as bundled, tempfile.TemporaryDirectory() as source_dir:
            Path(d, "IF-MIB.py").write_text("# compiled")
            Path(bundled, "IF-MIB.mib").write_text("IF-MIB DEFINITIONS ::= BEGIN END")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(
                os.environ,
                {
                    "TRAM_MIB_DIR": d,
                    "TRAM_MIB_SOURCE_DIR": source_dir,
                    "TRAM_MIB_BUNDLED_SOURCE_DIR": bundled,
                },
            ):
                resp = client.get("/api/mibs/IF-MIB/source")
                listing = client.get("/api/mibs")
        assert resp.status_code == 200
        assert "IF-MIB DEFINITIONS" in resp.text
        assert listing.status_code == 200
        rows = {e["name"]: e for e in listing.json()}
        assert rows["IF-MIB"]["raw_available"] is True
        assert rows["IF-MIB"]["raw_origin"] == "bundled"

    def test_get_missing_mib_source_returns_404(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as source_dir:
            app = _make_mibs_app()
            client = TestClient(app, raise_server_exceptions=False)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": source_dir}):
                resp = client.get("/api/mibs/IF-MIB/source")
        assert resp.status_code == 404

    def test_delete_existing_mib_normalizes_dash_to_underscore(self):
        with tempfile.TemporaryDirectory() as d:
            mib_file = Path(d, "IF_MIB.py")
            mib_file.write_text("# compiled underscore")
            source_dir = Path(d) / "sources"
            source_dir.mkdir(parents=True, exist_ok=True)
            raw_file = source_dir / "IF_MIB.my"
            raw_file.write_text("IF-MIB DEFINITIONS ::= BEGIN END")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": str(source_dir)}):
                resp = client.delete("/api/mibs/IF-MIB")
        assert resp.status_code == 200
        assert not mib_file.exists()
        assert not raw_file.exists()

    def test_upload_without_pysmi_returns_501(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                with patch(
                    "tram.api.routers.mibs.compile_mibs",
                    side_effect=MibSupportUnavailable("MIB compilation requires pysmi"),
                ):
                    resp = client.post(
                        "/api/mibs/upload",
                        files={"file": ("test.mib", b"MIB CONTENT", "text/plain")},
                    )
        assert resp.status_code == 501

    def test_upload_wrong_extension_returns_400(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/mibs/upload",
            files={"file": ("test.yaml", b"MIB CONTENT", "text/plain")},
        )
        assert resp.status_code == 400

    def test_upload_success_returns_compiled_results_and_persists_source(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)

        with tempfile.TemporaryDirectory() as d:
            with patch(
                "tram.api.routers.mibs.compile_mibs",
                return_value=MibCompileResult(
                    results={"TEST-MIB": "compiled"},
                    compiled=["TEST-MIB"],
                    builtin_names={"SNMPv2-SMI"},
                ),
            ):
                source_dir = Path(d) / "sources"
                with patch.dict(os.environ, {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": str(source_dir)}):
                    resp = client.post(
                        "/api/mibs/upload",
                        files={"file": ("TEST-MIB.mib", b"TEST DEFINITIONS ::= BEGIN END", "text/plain")},
                    )
                    source_path = source_dir / "TEST-MIB.mib"
                    assert source_path.exists()

        assert resp.status_code == 200
        assert resp.json()["compiled"] == ["TEST-MIB"]

    @pytest.mark.parametrize("filename", ["TEST-MIB", "TEST-MIB.my", "TEST-MIB.txt", "TEST-MIB.MIB"])
    def test_upload_accepts_supported_source_filename_variants(self, filename):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)

        with tempfile.TemporaryDirectory() as d:
            with patch(
                "tram.api.routers.mibs.compile_mibs",
                return_value=MibCompileResult(
                    results={"TEST-MIB": "compiled"},
                    compiled=["TEST-MIB"],
                    builtin_names={"SNMPv2-SMI"},
                ),
            ):
                source_dir = Path(d) / "sources"
                with patch.dict(os.environ, {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": str(source_dir)}):
                    resp = client.post(
                        "/api/mibs/upload",
                        files={"file": (filename, b"TEST DEFINITIONS ::= BEGIN END", "text/plain")},
                    )
                    source_path = source_dir / Path(filename).name
                    assert source_path.exists()

        assert resp.status_code == 200
        assert resp.json()["compiled"] == ["TEST-MIB"]

    def test_upload_classifies_builtin_local_and_unresolved_imports(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)

        with tempfile.TemporaryDirectory() as d:
            source_dir = Path(d) / "sources"
            source_dir.mkdir(parents=True, exist_ok=True)
            Path(source_dir, "CISCO-SMI.mib").write_text("CISCO-SMI DEFINITIONS ::= BEGIN END")
            with patch(
                "tram.api.routers.mibs.compile_mibs",
                return_value=MibCompileResult(
                    results={"TEST-MIB": "failed"},
                    compiled=[],
                    builtin_names={"SNMPv2-SMI", "SNMPv2-TC"},
                ),
            ):
                with patch.dict(os.environ, {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": str(source_dir)}):
                    resp = client.post(
                        "/api/mibs/upload",
                        files={"file": ("TEST-MIB.mib", (
                            b"TEST-MIB DEFINITIONS ::= BEGIN\n"
                            b"\n"
                            b"IMPORTS\n"
                            b"    MODULE-IDENTITY FROM SNMPv2-SMI\n"
                            b"    TEXTUAL-CONVENTION FROM SNMPv2-TC\n"
                            b"    ciscoMgmt FROM CISCO-SMI\n"
                            b"    foo FROM CUSTOM-MISSING-MIB;\n"
                            b"\n"
                            b"END\n"
                        ), "text/plain")},
                    )

        assert resp.status_code == 200
        body = resp.json()
        assert body["builtin_imports"] == ["SNMPv2-SMI", "SNMPv2-TC"]
        assert body["local_imports"] == ["CISCO-SMI"]
        assert body["unresolved_imports"] == ["CUSTOM-MISSING-MIB"]
        assert body["target_status"] == "unresolved_dependencies"

    def test_upload_builtin_target_reports_builtin_available(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)

        with tempfile.TemporaryDirectory() as d:
            with patch(
                "tram.api.routers.mibs.compile_mibs",
                return_value=MibCompileResult(
                    results={"INET-ADDRESS-MIB": "untouched"},
                    compiled=[],
                    builtin_names={"SNMPv2-SMI", "SNMPv2-TC", "INET-ADDRESS-MIB"},
                ),
            ):
                source_dir = Path(d) / "sources"
                with patch.dict(os.environ, {"TRAM_MIB_DIR": d, "TRAM_MIB_SOURCE_DIR": str(source_dir)}):
                    resp = client.post(
                        "/api/mibs/upload",
                        files={"file": ("INET-ADDRESS-MIB.mib", (
                            b"INET-ADDRESS-MIB DEFINITIONS ::= BEGIN\n"
                            b"\n"
                            b"IMPORTS\n"
                            b"    MODULE-IDENTITY, mib-2, Unsigned32 FROM SNMPv2-SMI\n"
                            b"    TEXTUAL-CONVENTION FROM SNMPv2-TC;\n"
                            b"\n"
                            b"END\n"
                        ), "text/plain")},
                    )

        assert resp.status_code == 200
        body = resp.json()
        assert body["builtin_imports"] == ["SNMPv2-SMI", "SNMPv2-TC"]
        assert body["unresolved_imports"] == []
        assert body["target_status"] == "builtin_available"

    def test_upload_with_resolve_missing_uses_source_store_as_remote_cache(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as bundled:
            with patch(
                "tram.api.routers.mibs.compile_mibs",
                return_value=MibCompileResult(
                    results={"TEST-MIB": "compiled"},
                    compiled=["TEST-MIB"],
                    builtin_names={"SNMPv2-SMI"},
                ),
            ) as mock_compile:
                with patch.dict(
                    os.environ,
                    {
                        "TRAM_MIB_DIR": d,
                        "TRAM_MIB_SOURCE_DIR": str(Path(d) / "sources"),
                        "TRAM_MIB_BUNDLED_SOURCE_DIR": bundled,
                    },
                ):
                    resp = client.post(
                        "/api/mibs/upload?resolve_missing=true",
                        files={"file": ("TEST-MIB.mib", b"TEST DEFINITIONS ::= BEGIN END", "text/plain")},
                    )

        assert resp.status_code == 200
        assert resp.json()["resolve_missing"] is True
        assert mock_compile.call_args.kwargs["resolve_missing"] is True
        assert mock_compile.call_args.kwargs["remote_cache_dir"] == str(Path(d) / "sources")
        assert bundled in mock_compile.call_args.kwargs["source_dirs"]

    def test_download_success_returns_compiled_results(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as bundled:
            with patch(
                "tram.api.routers.mibs.compile_mibs",
                return_value=MibCompileResult(
                    results={"IF-MIB": "compiled"},
                    compiled=["IF-MIB"],
                    builtin_names={"SNMPv2-SMI"},
                ),
            ) as mock_compile:
                with patch.dict(
                    os.environ,
                    {
                        "TRAM_MIB_DIR": d,
                        "TRAM_MIB_SOURCE_DIR": str(Path(d) / "sources"),
                        "TRAM_MIB_BUNDLED_SOURCE_DIR": bundled,
                    },
                ):
                    resp = client.post("/api/mibs/download", json={"names": ["IF-MIB"]})

        assert resp.status_code == 200
        assert resp.json()["compiled"] == ["IF-MIB"]
        assert mock_compile.call_args.kwargs["resolve_missing"] is True
        assert mock_compile.call_args.kwargs["remote_cache_dir"] == str(Path(d) / "sources")
        assert bundled in mock_compile.call_args.kwargs["source_dirs"]
