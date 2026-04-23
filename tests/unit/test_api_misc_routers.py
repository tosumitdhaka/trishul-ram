"""Tests for webhooks, templates, and mibs routers."""
from __future__ import annotations

import os
import queue
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.mibs import router as mibs_router
from tram.api.routers.templates import router as templates_router
from tram.api.routers.webhooks import router as webhooks_router

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

    def test_list_returns_py_files(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "IF-MIB.py").write_text("# compiled")
            Path(d, "OTHER.txt").write_text("ignore")
            Path(d, "_private.py").write_text("skip")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs")
        assert resp.status_code == 200
        names = [e["name"] for e in resp.json()]
        assert "IF-MIB" in names
        assert "OTHER" not in names
        assert "_private" not in names

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
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.delete("/api/mibs/IF-MIB")
            assert resp.status_code == 200
            assert not mib_file.exists()

    def test_get_existing_mib_by_dash_name(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "IF_MIB.py").write_text("# compiled underscore")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.get("/api/mibs/IF-MIB")
        assert resp.status_code == 200
        assert "# compiled underscore" in resp.text

    def test_delete_existing_mib_normalizes_dash_to_underscore(self):
        with tempfile.TemporaryDirectory() as d:
            mib_file = Path(d, "IF_MIB.py")
            mib_file.write_text("# compiled underscore")
            app = _make_mibs_app()
            client = TestClient(app)
            with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                resp = client.delete("/api/mibs/IF-MIB")
        assert resp.status_code == 200
        assert not mib_file.exists()

    def test_upload_without_pysmi_returns_501(self):
        import sys
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch.dict(sys.modules, {"pysmi": None, "pysmi.compiler": None}):
            resp = client.post(
                "/api/mibs/upload",
                files={"file": ("test.mib", b"MIB CONTENT", "text/plain")},
            )
        assert resp.status_code == 501

    def test_upload_wrong_extension_returns_400(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)
        # Mock pysmi as available
        mock_pysmi = MagicMock()
        import sys
        with patch.dict(sys.modules, {
            "pysmi": mock_pysmi,
            "pysmi.codegen": mock_pysmi,
            "pysmi.codegen.pysnmp": mock_pysmi,
            "pysmi.compiler": mock_pysmi,
            "pysmi.parser": mock_pysmi,
            "pysmi.parser.smi": mock_pysmi,
            "pysmi.reader": mock_pysmi,
            "pysmi.searcher": mock_pysmi,
            "pysmi.writer": mock_pysmi,
        }):
            resp = client.post(
                "/api/mibs/upload",
                files={"file": ("test.txt", b"MIB CONTENT", "text/plain")},
            )
        assert resp.status_code == 400

    def test_upload_success_returns_compiled_results(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_codegen = MagicMock()
        mock_codegen.baseMibs = ("SNMPv2-SMI",)
        mock_codegen.fakeMibs = ("__FAKE__",)
        mock_compiler = MagicMock()
        mock_compiler.compile.return_value = {"TEST-MIB": "compiled"}

        import sys
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(sys.modules, {
                "pysmi": MagicMock(),
                "pysmi.codegen": MagicMock(),
                "pysmi.codegen.pysnmp": MagicMock(PySnmpCodeGen=MagicMock(return_value=mock_codegen)),
                "pysmi.compiler": MagicMock(MibCompiler=MagicMock(return_value=mock_compiler)),
                "pysmi.parser": MagicMock(),
                "pysmi.parser.smi": MagicMock(parserFactory=lambda: (lambda: MagicMock())),
                "pysmi.reader": MagicMock(FileReader=MagicMock()),
                "pysmi.searcher": MagicMock(PyFileSearcher=MagicMock(), StubSearcher=MagicMock()),
                "pysmi.writer": MagicMock(PyFileWriter=MagicMock()),
            }):
                with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                    resp = client.post(
                        "/api/mibs/upload",
                        files={"file": ("TEST-MIB.mib", b"TEST DEFINITIONS ::= BEGIN END", "text/plain")},
                    )

        assert resp.status_code == 200
        assert resp.json()["compiled"] == ["TEST-MIB"]

    def test_download_success_returns_compiled_results(self):
        app = _make_mibs_app()
        client = TestClient(app, raise_server_exceptions=False)

        mock_codegen = MagicMock()
        mock_codegen.baseMibs = ("SNMPv2-SMI",)
        mock_codegen.fakeMibs = ("__FAKE__",)
        mock_compiler = MagicMock()
        mock_compiler.compile.return_value = {"IF-MIB": "compiled"}

        import sys
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(sys.modules, {
                "pysmi": MagicMock(),
                "pysmi.codegen": MagicMock(),
                "pysmi.codegen.pysnmp": MagicMock(PySnmpCodeGen=MagicMock(return_value=mock_codegen)),
                "pysmi.compiler": MagicMock(MibCompiler=MagicMock(return_value=mock_compiler)),
                "pysmi.parser": MagicMock(),
                "pysmi.parser.smi": MagicMock(parserFactory=lambda: (lambda: MagicMock())),
                "pysmi.reader": MagicMock(HttpReader=MagicMock()),
                "pysmi.searcher": MagicMock(PyFileSearcher=MagicMock(), StubSearcher=MagicMock()),
                "pysmi.writer": MagicMock(PyFileWriter=MagicMock()),
            }):
                with patch.dict(os.environ, {"TRAM_MIB_DIR": d}):
                    resp = client.post("/api/mibs/download", json={"names": ["IF-MIB"]})

        assert resp.status_code == 200
        assert resp.json()["compiled"] == ["IF-MIB"]
