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


def test_plugins_endpoint_fields_carry_schema_metadata():
    """A.4/A.1: /api/plugins field descriptors fold the full schema metadata
    (kind/choices/secret/multiline plus the A.1 descriptions) so the UI's
    field tables and sample YAML no longer need a second /api/config/schema
    fetch to enrich them. Each field must carry the exact metadata
    SCHEMA_FIELDS computed."""
    import tram.connectors  # noqa: F401
    import tram.serializers  # noqa: F401
    import tram.transforms  # noqa: F401

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    data = client.get("/api/plugins").json()
    for category, payload_key in (
        ("source", "sources"),
        ("sink", "sinks"),
        ("serializer", "serializers"),
        ("transform", "transforms"),
    ):
        for item in data["details"][payload_key]:
            schema_fields = {
                field["name"]: field
                for field in cs.SCHEMA_FIELDS[category].get(item["name"], [])
                if field["name"] not in {"condition", "serializer_out", "transforms"}
            }
            for field in item["fields"]:
                schema_field = schema_fields[field["name"]]
                assert field["kind"] == schema_field["kind"]
                assert field["choices"] == schema_field["choices"]
                assert field["secret"] == schema_field["secret"]
                assert field["multiline"] == schema_field["multiline"]
                assert field["description"] == schema_field["description"]
                assert field["description"], f"{payload_key}/{item['name']}/{field['name']}"


def test_plugins_endpoint_flags_sftp_password_secret():
    """The secret/multiline/choices metadata must survive the payload fold for
    representative fields (sftp password → secret)."""
    import tram.connectors  # noqa: F401

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    data = client.get("/api/plugins").json()
    sftp_source = next(item for item in data["details"]["sources"] if item["name"] == "sftp")
    fields_by_name = {field["name"]: field for field in sftp_source["fields"]}
    assert fields_by_name["password"]["secret"] is True
    assert set(fields_by_name["password"].keys()) >= {"kind", "choices", "secret", "multiline"}


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


def test_plugins_endpoint_reports_transforms_in_sync():
    """The real /api/plugins cross-check must report schema_mismatch empty for
    every category — melt was registered but missing from the TransformConfig
    union (any `type: melt` pipeline failed Pydantic validation), so its fix
    is pinned at the endpoint level too."""
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    data = client.get("/api/plugins").json()
    for category in ("sources", "sinks", "serializers", "transforms"):
        assert data["schema_mismatch"][category] == {}


def test_real_registry_matches_schema_union():
    """Pin the REAL union↔registry state (no mocks): every category's registry
    keys must equal its SCHEMA_FIELDS (union) keys. A registered type missing
    from the union fails Pydantic validation for real pipelines (the melt bug);
    a union-only type fails at runtime with PluginNotFoundError. Either
    divergence fails CI here instead of shipping."""
    import tram.connectors  # noqa: F401
    import tram.serializers  # noqa: F401
    import tram.transforms  # noqa: F401

    for category, registry in (
        ("source", reg._sources),
        ("sink", reg._sinks),
        ("serializer", reg._serializers),
        ("transform", reg._transforms),
    ):
        schema_keys = set(cs.SCHEMA_FIELDS.get(category, {}).keys())
        registry_keys = set(registry.keys())
        assert schema_keys == registry_keys, (
            f"union↔registry divergence in {category}: "
            f"{_schema_mismatch(category, registry)}"
        )
