"""Avro serializer with optional Schema Registry support."""
from __future__ import annotations

import io
import os

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer


@register_serializer("avro")
class AvroSerializer(BaseSerializer):
    """Serialize/deserialize Avro data. Requires fastavro>=1.9.

    Optional Schema Registry config:
        schema_registry_url (str): Base URL of the registry.
        schema_registry_subject (str): Subject name for latest schema lookup.
        schema_registry_id (int): Specific schema ID to fetch.
        use_magic_bytes (bool): Strip/prepend Confluent magic framing. Default True.
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.schema_str: str | None = config.get("avro_schema") or config.get("schema")
        self.schema_file: str | None = config.get("schema_file")
        self.registry_url: str | None = (
            config.get("schema_registry_url") or os.environ.get("TRAM_SCHEMA_REGISTRY_URL")
        )
        self.registry_username: str | None = (
            config.get("schema_registry_username") or os.environ.get("TRAM_SCHEMA_REGISTRY_USERNAME")
        )
        self.registry_password: str | None = (
            config.get("schema_registry_password") or os.environ.get("TRAM_SCHEMA_REGISTRY_PASSWORD")
        )
        self.registry_subject: str | None = config.get("schema_registry_subject")
        self.registry_id: int | None = config.get("schema_registry_id")
        self.use_magic_bytes: bool = config.get("use_magic_bytes", True)
        self._parsed_schema = None
        self._registry_schema_id: int | None = None

        if not self.schema_str and not self.schema_file and not self.registry_url:
            raise SerializerError(
                "Avro serializer requires either 'schema'/'schema_file', 'schema_registry_url', "
                "or the TRAM_SCHEMA_REGISTRY_URL environment variable"
            )

    def _get_registry_client(self):
        from tram.schema_registry.client import SchemaRegistryClient
        return SchemaRegistryClient(
            self.registry_url,
            username=self.registry_username,
            password=self.registry_password,
        )

    def _get_schema(self):
        try:
            import fastavro
        except ImportError as exc:
            raise SerializerError("Avro serializer requires fastavro — install with: pip install tram[avro]") from exc

        if self._parsed_schema is not None:
            return self._parsed_schema

        if self.registry_url:
            client = self._get_registry_client()
            try:
                if self.registry_id is not None:
                    schema_dict = client.get_schema_by_id(self.registry_id)
                    self._registry_schema_id = self.registry_id
                elif self.registry_subject:
                    self._registry_schema_id, schema_dict = client.get_latest_schema(self.registry_subject)
                else:
                    raise SerializerError(
                        "Schema registry configured but neither 'schema_registry_id' "
                        "nor 'schema_registry_subject' provided"
                    )
            finally:
                client.close()
        elif self.schema_str:
            import json
            schema_dict = json.loads(self.schema_str)
        else:
            with open(self.schema_file) as f:
                import json
                schema_dict = json.load(f)

        self._parsed_schema = fastavro.parse_schema(schema_dict)
        return self._parsed_schema

    def parse(self, data: bytes) -> list[dict]:
        try:
            import fastavro
        except ImportError as exc:
            raise SerializerError("Avro serializer requires fastavro — install with: pip install tram[avro]") from exc

        # Strip magic bytes if present
        if self.use_magic_bytes and self.registry_url and len(data) >= 5 and data[0:1] == b"\x00":
            from tram.schema_registry.client import decode_magic
            _, data = decode_magic(data)

        schema = self._get_schema()
        try:
            buf = io.BytesIO(data)
            return list(fastavro.reader(buf, schema))
        except Exception as exc:
            raise SerializerError(f"Avro parse error: {exc}") from exc

    def serialize(self, records: list[dict]) -> bytes:
        try:
            import fastavro
        except ImportError as exc:
            raise SerializerError("Avro serializer requires fastavro — install with: pip install tram[avro]") from exc

        schema = self._get_schema()
        try:
            buf = io.BytesIO()
            fastavro.writer(buf, schema, records)
            payload = buf.getvalue()
        except Exception as exc:
            raise SerializerError(f"Avro serialize error: {exc}") from exc

        # Add magic bytes if using registry
        if self.use_magic_bytes and self.registry_url and self._registry_schema_id is not None:
            from tram.schema_registry.client import encode_with_magic
            payload = encode_with_magic(self._registry_schema_id, payload)

        return payload
