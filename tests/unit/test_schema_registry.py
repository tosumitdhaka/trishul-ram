"""Tests for Schema Registry client and magic-byte helpers (v0.5.0)."""
from __future__ import annotations

import struct
from unittest.mock import MagicMock, patch

import pytest

from tram.schema_registry.client import (
    SchemaRegistryClient,
    decode_magic,
    encode_with_magic,
)

# sys imported locally where needed


# ── Magic byte helpers ─────────────────────────────────────────────────────


def test_encode_with_magic_format():
    payload = b"avro-data"
    schema_id = 42
    result = encode_with_magic(schema_id, payload)

    assert result[0:1] == b"\x00"
    assert struct.unpack(">I", result[1:5])[0] == schema_id
    assert result[5:] == payload


def test_decode_magic_roundtrip():
    payload = b"hello world"
    schema_id = 99
    encoded = encode_with_magic(schema_id, payload)

    decoded_id, decoded_payload = decode_magic(encoded)
    assert decoded_id == schema_id
    assert decoded_payload == payload


def test_decode_magic_wrong_byte():
    data = b"\x01" + b"\x00" * 4 + b"data"
    with pytest.raises(ValueError, match="magic byte"):
        decode_magic(data)


def test_decode_magic_too_short():
    with pytest.raises(ValueError, match="too short"):
        decode_magic(b"\x00\x01")


def test_encode_decode_large_id():
    schema_id = 2**24 - 1  # large but valid
    payload = b"x" * 100
    encoded = encode_with_magic(schema_id, payload)
    sid, pay = decode_magic(encoded)
    assert sid == schema_id
    assert pay == payload


# ── SchemaRegistryClient ───────────────────────────────────────────────────


def _make_mock_httpx_client(responses: dict):
    """Create a mock httpx.Client where .get(path) returns preset responses."""
    import json

    mock_client = MagicMock()

    def mock_get(path, **kwargs):
        resp = MagicMock()
        if path in responses:
            resp.json.return_value = responses[path]
            resp.raise_for_status.return_value = None
        else:
            resp.raise_for_status.side_effect = Exception(f"404 Not Found: {path}")
        return resp

    mock_client.get.side_effect = mock_get
    return mock_client


def test_get_schema_by_id_cached():
    import json
    import httpx

    schema_dict = {"type": "record", "name": "Test", "fields": []}
    responses = {
        "/schemas/ids/5": {"schema": json.dumps(schema_dict)},
    }

    mock_client_instance = _make_mock_httpx_client(responses)

    with patch.object(httpx, "Client", return_value=mock_client_instance):
        client = SchemaRegistryClient("http://registry:8081")
        result1 = client.get_schema_by_id(5)
        result2 = client.get_schema_by_id(5)  # from cache

    assert result1 == schema_dict
    assert result2 == schema_dict
    # Only one HTTP call (second was cached)
    assert mock_client_instance.get.call_count == 1


def test_get_latest_schema():
    import json
    import httpx

    schema_dict = {"type": "record", "name": "Event", "fields": [{"name": "id", "type": "int"}]}
    responses = {
        "/subjects/my-topic-value/versions/latest": {
            "id": 7,
            "schema": json.dumps(schema_dict),
        }
    }

    mock_client_instance = _make_mock_httpx_client(responses)

    with patch.object(httpx, "Client", return_value=mock_client_instance):
        client = SchemaRegistryClient("http://registry:8081")
        schema_id, result = client.get_latest_schema("my-topic-value")

    assert schema_id == 7
    assert result == schema_dict
