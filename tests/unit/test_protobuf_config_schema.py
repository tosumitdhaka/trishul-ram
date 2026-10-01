"""Wave 2 (GH #83): protobuf serializer config schema — `preserve_keys` field
validation and A.1 description coverage.

No protobuf dependency: the pydantic config models and the backend config
schema are importable in a minimal `.[dev]` install.
"""
from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError


class TestPreserveKeysConfigModel:
    def test_preserve_keys_rejects_non_bool_at_model_validation(self):
        """tram validate runs PipelineConfig.model_validate — a non-bool value
        must be rejected there, not silently coerced."""
        from tram.models.pipeline import ProtobufSerializerConfig

        with pytest.raises(ValidationError):
            ProtobufSerializerConfig(
                type="protobuf",
                schema_file="x.proto",
                message_class="Foo",
                preserve_keys="banana",
            )

    def test_preserve_keys_defaults_to_false(self):
        from tram.models.pipeline import ProtobufSerializerConfig

        cfg = ProtobufSerializerConfig(
            type="protobuf", schema_file="x.proto", message_class="Foo"
        )
        assert cfg.preserve_keys is False
        assert cfg.model_dump()["preserve_keys"] is False

    def test_preserve_keys_validated_through_serializer_union(self):
        """The exact gate tram validate hits (SerializerConfig union)."""
        from tram.models.pipeline import SerializerConfig

        ok = TypeAdapter(SerializerConfig).validate_python({
            "type": "protobuf",
            "schema_file": "x.proto",
            "message_class": "Foo",
            "preserve_keys": True,
        })
        assert ok.preserve_keys is True
        with pytest.raises(ValidationError):
            TypeAdapter(SerializerConfig).validate_python({
                "type": "protobuf",
                "schema_file": "x.proto",
                "message_class": "Foo",
                "preserve_keys": "banana",
            })

    def test_preserve_keys_does_not_leak_into_other_serializers(self):
        """The new field belongs to the protobuf config only — other serializer
        models must keep rejecting it (no union-wide wildcard)."""
        from tram.models.pipeline import JsonSerializerConfig

        with pytest.raises(ValidationError):
            JsonSerializerConfig(type="json", preserve_keys=True)


class TestConfigSchemaDescription:
    def test_config_schema_has_preserve_keys_description(self):
        """A.1: every serializer config field must carry a description in the
        backend config schema (the import-time _apply_field_descriptions check
        fails the suite otherwise)."""
        import tram.api.config_schema as cs

        protobuf_fields = {
            field["name"]: field
            for field in cs.SCHEMA_FIELDS["serializer"]["protobuf"]
        }
        assert "preserve_keys" in protobuf_fields
        assert protobuf_fields["preserve_keys"]["description"]
        assert protobuf_fields["preserve_keys"]["default"] is False

    def test_config_schema_preserve_keys_wiring_reaches_plugins_payload(self):
        """/api/plugins folds the A.1 description for every serializer field."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import tram.serializers  # noqa: F401
        from tram.api.routers.health import router

        app = FastAPI()
        app.include_router(router)
        data = TestClient(app).get("/api/plugins").json()
        protobuf = next(
            item for item in data["details"]["serializers"] if item["name"] == "protobuf"
        )
        preserve_keys = next(f for f in protobuf["fields"] if f["name"] == "preserve_keys")
        assert preserve_keys["description"]
        assert preserve_keys["default"] is False