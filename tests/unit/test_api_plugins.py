from fastapi import FastAPI
from fastapi.testclient import TestClient

import tram.api.config_schema as cs
import tram.registry.registry as reg
from tram.api.routers.health import _schema_mismatch, router


def test_plugins_endpoint_returns_legacy_lists_and_details():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/plugins")

    assert response.status_code == 200
    data = response.json()

    assert isinstance(data["sources"], list)
    assert isinstance(data["sinks"], list)
    assert isinstance(data["serializers"], list)
    assert isinstance(data["transforms"], list)

    assert "details" in data
    assert isinstance(data["details"]["sources"], list)
    assert isinstance(data["details"]["sinks"], list)
    assert isinstance(data["details"]["serializers"], list)
    assert isinstance(data["details"]["transforms"], list)

    sftp_source = next(item for item in data["details"]["sources"] if item["name"] == "sftp")
    assert "remote_path" in sftp_source["required_fields"]
    assert "password" in sftp_source["common_optional_fields"]
    assert any(field["name"] == "password" for field in sftp_source["fields"])
    assert sftp_source["summary"]

    json_serializer = next(item for item in data["details"]["serializers"] if item["name"] == "json")
    assert json_serializer["summary"]
    assert json_serializer["class_name"] == "JsonSerializer"
    assert any(field["name"] == "ensure_ascii" for field in json_serializer["fields"])


# ── Issue #24 / Option A: schema_version + registry↔union cross-check ──────


def test_plugins_endpoint_exposes_schema_version_and_mismatch():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    data = client.get("/api/plugins").json()

    assert data["schema_version"] == cs.schema_version()
    assert len(data["schema_version"]) == 12

    assert "schema_mismatch" in data
    for category in ("sources", "sinks", "serializers", "transforms"):
        assert isinstance(data["schema_mismatch"][category], dict)
        assert set(data["schema_mismatch"][category].keys()) <= {"union_only", "registry_only"}


def test_schema_mismatch_flags_registry_only_type(monkeypatch):
    # A registered type missing from the Pydantic union surfaces as
    # registry_only (fails validation; AI context shows "no schema available").
    monkeypatch.setitem(cs.SCHEMA_FIELDS, "source", {"sftp": []})
    mismatch = _schema_mismatch("source", {"sftp": object, "zzz_registered_only": object})
    assert mismatch == {"registry_only": ["zzz_registered_only"]}


def test_schema_mismatch_flags_union_only_type(monkeypatch):
    # A union member with no registered class surfaces as union_only (passes
    # validation, fails at runtime with PluginNotFoundError).
    monkeypatch.setitem(cs.SCHEMA_FIELDS, "source", {"sftp": [], "zzz_union_only": []})
    mismatch = _schema_mismatch("source", {"sftp": object})
    assert mismatch == {"union_only": ["zzz_union_only"]}


def test_schema_mismatch_empty_when_in_sync(monkeypatch):
    monkeypatch.setitem(cs.SCHEMA_FIELDS, "source", {"sftp": []})
    assert _schema_mismatch("source", {"sftp": object}) == {}


def test_plugins_endpoint_flags_constructed_registry_divergence(monkeypatch):
    # Endpoint-level: register a fake plugin at runtime; the cross-check must
    # surface it as registry_only in the response.
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    reg._sources["zzz_endpoint_fake"] = object
    try:
        data = client.get("/api/plugins").json()
    finally:
        del reg._sources["zzz_endpoint_fake"]
    assert "zzz_endpoint_fake" in data["schema_mismatch"]["sources"]["registry_only"]
