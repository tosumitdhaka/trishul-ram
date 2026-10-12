from fastapi import FastAPI
from fastapi.testclient import TestClient

import tram.api.config_schema as cs
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


# ── Issue #24 / Option A: schema_version content hash ───────────────────────


def test_config_schema_endpoint_exposes_schema_version():
    app = FastAPI()
    app.include_router(config_router)
    client = TestClient(app)

    data = client.get("/api/config/schema").json()

    assert "schema_version" in data
    assert data["schema_version"] == cs.schema_version()
    assert len(data["schema_version"]) == 12
    assert all(c in "0123456789abcdef" for c in data["schema_version"])


def test_schema_version_stable_for_same_schema():
    first = cs.schema_version()
    second = cs.schema_version()
    assert first == second
    assert len(first) == 12


def test_schema_version_changes_when_schema_changes(monkeypatch):
    # Contract test: the hash is a content hash of SCHEMA_FIELDS — mutating
    # the schema must rotate it (reset the module cache: production never
    # mutates SCHEMA_FIELDS after import, so the cache is stable there).
    before = cs.schema_version()

    monkeypatch.setitem(
        cs.SCHEMA_FIELDS["source"],
        "zzz_dummy_source",
        [{"name": "dummy_field", "type": "str"}],
    )
    cs._schema_version_cache = None
    try:
        after = cs.schema_version()
    finally:
        cs._schema_version_cache = None
    assert before != after


# ── A.1: per-field descriptions ───────────────────────────────────────────────


def test_every_schema_field_has_a_description():
    """A.1: every built-in field descriptor in SCHEMA_FIELDS carries a non-empty,
    operator-facing description (the wizard's form help text and the AI
    schema consumers rely on it; gaps fail loudly at import already).

    Boundary: this cache is built from the pipeline model unions at import,
    not the runtime plugin registry. External plugin registration does not
    add model descriptors here; this guard makes no claim about external
    plugin help text or configuration fields.
    """
    total = 0
    missing: list[str] = []
    for category, types in cs.SCHEMA_FIELDS.items():
        for type_name, fields in types.items():
            for field in fields:
                total += 1
                if not (field.get("description") or "").strip():
                    missing.append(f"{category}/{type_name}/{field['name']}")
    assert total > 0
    assert missing == [], f"fields without a description: {missing}"


def test_config_schema_endpoint_serves_field_descriptions():
    app = FastAPI()
    app.include_router(config_router)
    client = TestClient(app)

    data = client.get("/api/config/schema").json()

    sftp_fields = {f["name"]: f for f in data["sources"]["sftp"]["fields"]}
    assert sftp_fields["host"]["description"] == "Server hostname or IP address"
    assert "directory" in sftp_fields["remote_path"]["description"]
    # The description key is additive — existing metadata keys are intact.
    assert sftp_fields["password"]["kind"] == "text"
    assert sftp_fields["password"]["secret"] is True


def test_filter_condition_and_literal_choices_are_grounded():
    fields = {field["name"]: field for field in cs.SCHEMA_FIELDS["transform"]["filter"]}
    assert fields["condition"]["required"] is True
    context = "\n".join(cs.SCHEMA_LINES["transform"]["hex_decode"])
    assert "utf8_or_hex" in context
    assert "latin1_or_hex" in context
    assert "Literal" not in context


def test_empty_defaults_and_resolved_nested_models_are_available_to_ai():
    lines = "\n".join(cs.SCHEMA_LINES["transform"]["json_flatten"])
    assert "explode_paths: list[str] = []" in lines
    assert "value_suffix: str = \"\"" in lines
    assert "ForwardRef" not in lines
    fields = {field["name"]: field for field in cs.SCHEMA_FIELDS["transform"]["json_flatten"]}
    nested = fields["zip_groups"]["nested_models"]["JsonFlattenZipGroupConfig"]
    assert any(field["name"] == "fields" and field["required"] for field in nested)
    assert "Align paired list fields" in lines


def test_nested_selection_and_projection_have_expanded_shapes():
    selection = "\n".join(cs.SCHEMA_LINES["transform"]["select_from_list"])
    projection = "\n".join(cs.SCHEMA_LINES["transform"]["project"])
    assert "match: dict[str, Any] | None" in selection
    assert "output: dict[str, str]" in selection
    assert "source_any: list[str] = []" in projection
    assert "default:" in projection
    assert "Item source path to output field name mapping" in selection
    assert "existing null is preserved" in projection



def test_nested_varbind_type_is_data_not_plugin_discriminator():
    fields = {field["name"]: field for field in cs.SCHEMA_FIELDS["sink"]["snmp_trap"]}
    nested = {field["name"]: field for field in fields["varbinds"]["nested_models"]["VarbindConfig"]}
    assert "type" not in fields
    assert nested["type"]["default"] == "OctetString"
    assert nested["type"]["description"].startswith("SNMP value type")
    assert nested["type"]["choices"] == [
        "Integer32", "OctetString", "Counter32", "Gauge32", "TimeTicks",
    ]
    lines = "\n".join(cs.SCHEMA_LINES["sink"]["snmp_trap"])
    assert 'type: \'Integer32\' | \'OctetString\'' in lines
