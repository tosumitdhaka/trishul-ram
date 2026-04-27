from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.schemas import config_router


def test_config_schema_endpoint_returns_backend_generated_schema():
    app = FastAPI()
    app.include_router(config_router)
    client = TestClient(app)

    response = client.get("/api/config/schema")

    assert response.status_code == 200
    data = response.json()
    assert "sources" in data
    assert "sinks" in data
    assert "serializers" in data
    assert "transforms" in data
    assert "local" in data["sources"]
    assert "json" in data["serializers"]
    assert any(field["name"] == "path" for field in data["sources"]["local"]["fields"])


def test_config_schema_omits_pydantic_undefined_and_exposes_optional_simple_fields():
    app = FastAPI()
    app.include_router(config_router)
    client = TestClient(app)

    response = client.get("/api/config/schema")

    assert response.status_code == 200
    data = response.json()
    sftp_source_fields = {field["name"]: field for field in data["sources"]["sftp"]["fields"]}
    sftp_sink_fields = {field["name"]: field for field in data["sinks"]["sftp"]["fields"]}

    assert sftp_source_fields["host"]["default"] is None
    assert sftp_source_fields["password"]["kind"] == "text"
    assert sftp_source_fields["private_key_path"]["kind"] == "text"
    assert sftp_sink_fields["max_records"]["kind"] == "integer"
    assert sftp_sink_fields["max_time"]["kind"] == "integer"
    assert sftp_sink_fields["max_bytes"]["kind"] == "integer"
