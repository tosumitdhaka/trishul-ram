"""Schema Registry client — Confluent-compatible REST API (also works with Apicurio)."""

from __future__ import annotations

import struct
from typing import Optional


_MAGIC_BYTE = b"\x00"


def encode_with_magic(schema_id: int, payload: bytes) -> bytes:
    """Prepend Confluent magic byte + 4-byte BE schema ID to payload."""
    return _MAGIC_BYTE + struct.pack(">I", schema_id) + payload


def decode_magic(data: bytes) -> tuple[int, bytes]:
    """Strip Confluent magic byte framing. Returns (schema_id, payload)."""
    if len(data) < 5:
        raise ValueError("Data too short to contain magic byte framing")
    if data[0:1] != _MAGIC_BYTE:
        raise ValueError(f"Expected magic byte 0x00, got 0x{data[0]:02x}")
    schema_id = struct.unpack(">I", data[1:5])[0]
    return schema_id, data[5:]


class SchemaRegistryClient:
    """HTTP client for Confluent Schema Registry (and Apicurio with Confluent API).

    Uses httpx (already a required TRAM dependency).
    Caches schemas by ID in memory.
    """

    def __init__(self, url: str, username: Optional[str] = None, password: Optional[str] = None) -> None:
        import httpx

        self._url = url.rstrip("/")
        self._cache: dict[int, dict] = {}
        auth = (username, password) if username else None
        self._client = httpx.Client(base_url=self._url, auth=auth, timeout=10)

    def get_schema_by_id(self, schema_id: int) -> dict:
        """Fetch schema dict by numeric ID. Cached."""
        if schema_id in self._cache:
            return self._cache[schema_id]

        resp = self._client.get(f"/schemas/ids/{schema_id}")
        resp.raise_for_status()
        data = resp.json()
        import json as _json
        schema = _json.loads(data["schema"])
        self._cache[schema_id] = schema
        return schema

    def get_latest_schema(self, subject: str) -> tuple[int, dict]:
        """Return (schema_id, schema_dict) for the latest version of a subject."""
        resp = self._client.get(f"/subjects/{subject}/versions/latest")
        resp.raise_for_status()
        data = resp.json()
        schema_id: int = data["id"]
        import json as _json
        schema = _json.loads(data["schema"])
        self._cache[schema_id] = schema
        return schema_id, schema

    def close(self) -> None:
        self._client.close()
