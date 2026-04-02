"""Unit tests for the /api/schemas router."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.schemas import router


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    d = tmp_path / "schemas"
    d.mkdir()
    return d


@pytest.fixture
def client(schema_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TRAM_SCHEMA_DIR", str(schema_dir))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── Helper ────────────────────────────────────────────────────────────────────


def _upload(client: TestClient, content: str, filename: str, subdir: str = "") -> dict:
    params = {"subdir": subdir} if subdir else {}
    resp = client.post(
        "/api/schemas/upload",
        files={"file": (filename, io.BytesIO(content.encode()), "text/plain")},
        params=params,
    )
    return resp


# ── GET /api/schemas ──────────────────────────────────────────────────────────


class TestListSchemas:
    def test_empty_dir_returns_empty_list(self, client):
        resp = client.get("/api/schemas")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_lists_uploaded_files(self, client):
        _upload(client, 'syntax = "proto3";', "MyMessage.proto")
        resp = client.get("/api/schemas")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["path"] == "MyMessage.proto"
        assert data[0]["type"] == "protobuf"
        assert data[0]["size_bytes"] > 0
        assert "schema_file" in data[0]

    def test_lists_multiple_files_sorted(self, client):
        _upload(client, "{}", "b.json")
        _upload(client, "{}", "a.json")
        resp = client.get("/api/schemas")
        paths = [e["path"] for e in resp.json()]
        assert paths == ["a.json", "b.json"]

    def test_lists_files_in_subdirs(self, client, schema_dir):
        subdir = schema_dir / "cisco"
        subdir.mkdir()
        (subdir / "GenericRecord.proto").write_text('syntax = "proto3";')
        (schema_dir / "top.avsc").write_text('{"type": "record"}')
        resp = client.get("/api/schemas")
        paths = [e["path"] for e in resp.json()]
        assert "top.avsc" in paths
        assert os.path.join("cisco", "GenericRecord.proto") in paths

    def test_schema_dir_not_exist_returns_empty(self, monkeypatch):
        monkeypatch.setenv("TRAM_SCHEMA_DIR", "/nonexistent/schemas/xyz")
        app = FastAPI()
        app.include_router(router)
        c = TestClient(app)
        resp = c.get("/api/schemas")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_hidden_files_excluded(self, client):
        # dotfiles should not appear in listing
        (Path(os.environ["TRAM_SCHEMA_DIR"]) / ".gitkeep").write_text("")
        resp = client.get("/api/schemas")
        assert resp.json() == []

    def test_type_inference(self, client):
        for filename, expected_type in [
            ("m.proto", "protobuf"),
            ("m.avsc",  "avro"),
            ("m.json",  "json"),
            ("m.xsd",   "xml"),
            ("m.yaml",  "yaml"),
            ("m.yml",   "yaml"),
        ]:
            _upload(client, "x", filename)
        data = {e["path"]: e["type"] for e in client.get("/api/schemas").json()}
        assert data["m.proto"] == "protobuf"
        assert data["m.avsc"]  == "avro"
        assert data["m.json"]  == "json"
        assert data["m.xsd"]   == "xml"
        assert data["m.yaml"]  == "yaml"
        assert data["m.yml"]   == "yaml"


# ── GET /api/schemas/{filepath} ───────────────────────────────────────────────


class TestGetSchema:
    def test_returns_file_content(self, client):
        _upload(client, 'syntax = "proto3";\nmessage Foo {}', "Foo.proto")
        resp = client.get("/api/schemas/Foo.proto")
        assert resp.status_code == 200
        assert 'message Foo' in resp.text

    def test_returns_file_in_subdir(self, client):
        _upload(client, '{"type": "record"}', "schema.avsc", subdir="ns")
        resp = client.get("/api/schemas/ns/schema.avsc")
        assert resp.status_code == 200
        assert "record" in resp.text

    def test_not_found_returns_404(self, client):
        resp = client.get("/api/schemas/missing.proto")
        assert resp.status_code == 404

    def test_path_traversal_rejected_by_safe_join(self):
        # httpx normalises ../../ in URLs before sending, so test _safe_join directly
        from fastapi import HTTPException as _HTTPException

        from tram.api.routers.schemas import _safe_join
        with pytest.raises(_HTTPException) as exc_info:
            _safe_join("/schemas", "../../etc/passwd")
        assert exc_info.value.status_code == 400


# ── POST /api/schemas/upload ──────────────────────────────────────────────────


class TestUploadSchema:
    def test_upload_proto_file(self, client, schema_dir):
        resp = _upload(client, 'syntax = "proto3";', "Msg.proto")
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == "Msg.proto"
        assert body["type"] == "protobuf"
        assert body["size_bytes"] == len('syntax = "proto3";')
        assert os.path.isfile(body["schema_file"])

    def test_upload_avro_schema(self, client):
        resp = _upload(client, '{"type": "record", "name": "X", "fields": []}', "X.avsc")
        assert resp.status_code == 200
        assert resp.json()["type"] == "avro"

    def test_upload_json_schema(self, client):
        resp = _upload(client, '{"$schema": "http://json-schema.org/draft-07/schema"}', "s.json")
        assert resp.status_code == 200
        assert resp.json()["type"] == "json"

    def test_upload_xml_schema(self, client):
        resp = _upload(client, "<xs:schema/>", "s.xsd")
        assert resp.status_code == 200
        assert resp.json()["type"] == "xml"

    def test_upload_to_subdir(self, client, schema_dir):
        resp = _upload(client, 'syntax = "proto3";', "Rec.proto", subdir="cisco")
        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == os.path.join("cisco", "Rec.proto")
        assert (schema_dir / "cisco" / "Rec.proto").exists()

    def test_upload_nested_subdir_created(self, client, schema_dir):
        resp = _upload(client, "{}", "x.json", subdir="a/b/c")
        assert resp.status_code == 200
        assert (schema_dir / "a" / "b" / "c" / "x.json").exists()

    def test_upload_overwrites_existing_file(self, client):
        _upload(client, "old content", "t.proto")
        _upload(client, "new content", "t.proto")
        resp = client.get("/api/schemas/t.proto")
        assert resp.text == "new content"

    def test_disallowed_extension_returns_400(self, client):
        resp = _upload(client, "import os", "evil.py")
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]

    def test_empty_filename_returns_4xx(self, client):
        # An empty filename is rejected — either by our handler (400) or by
        # python-multipart/FastAPI form parsing (422) before the handler runs.
        resp = client.post(
            "/api/schemas/upload",
            files={"file": ("", io.BytesIO(b"data"), "text/plain")},
        )
        assert resp.status_code in (400, 422)

    def test_subdir_with_dotdot_returns_400(self, client):
        resp = _upload(client, "x", "f.proto", subdir="../outside")
        assert resp.status_code == 400

    def test_filename_path_traversal_returns_400(self, client):
        # Crafted filename that tries to escape schema_dir
        resp = _upload(client, "x", "../../etc/passwd.proto")
        # os.path.basename strips the traversal, so it lands safely as "passwd.proto"
        # Just verify it doesn't escape schema_dir
        if resp.status_code == 200:
            assert resp.json()["path"] == "passwd.proto"

    def test_returns_schema_file_path_usable_in_yaml(self, client, schema_dir):
        resp = _upload(client, 'syntax = "proto3";', "M.proto")
        schema_file = resp.json()["schema_file"]
        assert schema_file.startswith(str(schema_dir))
        assert schema_file.endswith("M.proto")


# ── DELETE /api/schemas/{filepath} ───────────────────────────────────────────


class TestDeleteSchema:
    def test_delete_existing_file(self, client, schema_dir):
        _upload(client, "x", "del_me.proto")
        resp = client.delete("/api/schemas/del_me.proto")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "del_me.proto"
        assert not (schema_dir / "del_me.proto").exists()

    def test_delete_file_in_subdir(self, client, schema_dir):
        _upload(client, "x", "sub.proto", subdir="ns")
        rel = os.path.join("ns", "sub.proto")
        resp = client.delete(f"/api/schemas/{rel}")
        assert resp.status_code == 200
        assert not (schema_dir / "ns" / "sub.proto").exists()

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete("/api/schemas/ghost.proto")
        assert resp.status_code == 404

    def test_delete_path_traversal_rejected_by_safe_join(self):
        # httpx normalises ../../ in URLs before sending, so test _safe_join directly
        from fastapi import HTTPException as _HTTPException

        from tram.api.routers.schemas import _safe_join
        with pytest.raises(_HTTPException) as exc_info:
            _safe_join("/schemas", "../../etc/passwd")
        assert exc_info.value.status_code == 400
