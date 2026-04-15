"""Unit tests for tram/serializers/bytes_serializer.py — BytesSerializer."""
from __future__ import annotations

import base64
import json

import pytest

from tram.core.exceptions import SerializerError
from tram.serializers.bytes_serializer import BytesSerializer


# ── Construction / config validation ──────────────────────────────────────────


class TestBytesSerializerInit:
    def test_default_encoding_is_base64(self):
        s = BytesSerializer({})
        assert s.encoding == "base64"

    def test_explicit_base64_encoding(self):
        s = BytesSerializer({"encoding": "base64"})
        assert s.encoding == "base64"

    def test_hex_encoding_accepted(self):
        s = BytesSerializer({"encoding": "hex"})
        assert s.encoding == "hex"

    def test_none_encoding_accepted(self):
        s = BytesSerializer({"encoding": "none"})
        assert s.encoding == "none"

    def test_invalid_encoding_raises(self):
        with pytest.raises(SerializerError, match="invalid encoding"):
            BytesSerializer({"encoding": "utf8"})

    def test_invalid_encoding_message_contains_value(self):
        with pytest.raises(SerializerError, match="'binary'"):
            BytesSerializer({"encoding": "binary"})


# ── parse() — lines 50-57 ──────────────────────────────────────────────────────


class TestBytesSerializerParse:
    def test_parse_base64_returns_single_record(self):
        s = BytesSerializer({"encoding": "base64"})
        result = s.parse(b"hello")
        assert len(result) == 1

    def test_parse_base64_size_field(self):
        s = BytesSerializer({"encoding": "base64"})
        result = s.parse(b"hello")
        assert result[0]["_size"] == 5

    def test_parse_base64_raw_field_is_valid_base64(self):
        s = BytesSerializer({"encoding": "base64"})
        data = b"\x00\x01\x02\x03"
        result = s.parse(data)
        assert result[0]["_raw"] == base64.b64encode(data).decode("ascii")

    def test_parse_base64_roundtrip(self):
        s = BytesSerializer({"encoding": "base64"})
        data = b"test payload \xff\xfe"
        record = s.parse(data)[0]
        decoded = base64.b64decode(record["_raw"])
        assert decoded == data

    def test_parse_hex_raw_field(self):
        s = BytesSerializer({"encoding": "hex"})
        data = b"\xde\xad\xbe\xef"
        result = s.parse(data)
        assert result[0]["_raw"] == "deadbeef"

    def test_parse_hex_size_field(self):
        s = BytesSerializer({"encoding": "hex"})
        result = s.parse(b"abcd")
        assert result[0]["_size"] == 4

    def test_parse_none_encoding_no_raw_field(self):
        """encoding=none must not add a _raw key."""
        s = BytesSerializer({"encoding": "none"})
        result = s.parse(b"some bytes")
        assert "_raw" not in result[0]

    def test_parse_none_encoding_has_size(self):
        s = BytesSerializer({"encoding": "none"})
        result = s.parse(b"some bytes")
        assert result[0]["_size"] == 10

    def test_parse_empty_bytes_base64(self):
        s = BytesSerializer({"encoding": "base64"})
        result = s.parse(b"")
        assert result[0]["_size"] == 0
        assert result[0]["_raw"] == ""

    def test_parse_empty_bytes_hex(self):
        s = BytesSerializer({"encoding": "hex"})
        result = s.parse(b"")
        assert result[0]["_size"] == 0
        assert result[0]["_raw"] == ""

    def test_parse_empty_bytes_none(self):
        s = BytesSerializer({"encoding": "none"})
        result = s.parse(b"")
        assert result[0]["_size"] == 0
        assert "_raw" not in result[0]

    def test_parse_large_payload_size(self):
        s = BytesSerializer({"encoding": "base64"})
        data = b"x" * 10_000
        result = s.parse(data)
        assert result[0]["_size"] == 10_000


# ── serialize() — lines 59-79 ─────────────────────────────────────────────────


class TestBytesSerializerSerialize:
    # ── Empty records list (line 60-61) ────────────────────────────────────────

    def test_serialize_empty_list_returns_empty_bytes(self):
        s = BytesSerializer({})
        assert s.serialize([]) == b""

    # ── _raw absent → JSON fallback (lines 64-70) ──────────────────────────────

    def test_serialize_no_raw_falls_back_to_json(self):
        s = BytesSerializer({})
        records = [{"a": 1, "b": "hello"}]
        result = s.serialize(records)
        assert json.loads(result) == records

    def test_serialize_no_raw_multiple_records(self):
        s = BytesSerializer({})
        records = [{"x": 1}, {"x": 2}]
        result = s.serialize(records)
        assert json.loads(result) == records

    def test_serialize_json_fallback_is_utf8(self):
        s = BytesSerializer({})
        result = s.serialize([{"k": "v"}])
        assert isinstance(result, bytes)
        result.decode("utf-8")  # must not raise

    def test_serialize_json_fallback_unserializable_raises(self):
        """A record with a non-JSON-serialisable value must raise SerializerError."""
        s = BytesSerializer({})

        class _Unserializable:
            pass

        with pytest.raises(SerializerError, match="fallback JSON error"):
            s.serialize([{"bad": _Unserializable()}])

    # ── base64 decode path (lines 72-73) ───────────────────────────────────────

    def test_serialize_base64_roundtrip(self):
        data = b"\x00\xff\xfe binary \x80"
        s = BytesSerializer({"encoding": "base64"})
        records = s.parse(data)
        result = s.serialize(records)
        assert result == data

    def test_serialize_base64_explicit_raw(self):
        s = BytesSerializer({"encoding": "base64"})
        raw = base64.b64encode(b"explicit").decode("ascii")
        result = s.serialize([{"_raw": raw, "_size": 8}])
        assert result == b"explicit"

    def test_serialize_base64_empty_payload(self):
        s = BytesSerializer({"encoding": "base64"})
        records = s.parse(b"")
        assert s.serialize(records) == b""

    def test_serialize_base64_invalid_raw_raises(self):
        """Corrupt base64 data must raise SerializerError."""
        s = BytesSerializer({"encoding": "base64"})
        with pytest.raises(SerializerError, match="decode error"):
            s.serialize([{"_raw": "!!!not-valid-base64!!!"}])

    # ── hex decode path (lines 74-75) ──────────────────────────────────────────

    def test_serialize_hex_roundtrip(self):
        data = b"\xde\xad\xbe\xef"
        s = BytesSerializer({"encoding": "hex"})
        records = s.parse(data)
        result = s.serialize(records)
        assert result == data

    def test_serialize_hex_explicit_raw(self):
        s = BytesSerializer({"encoding": "hex"})
        result = s.serialize([{"_raw": "cafebabe", "_size": 4}])
        assert result == b"\xca\xfe\xba\xbe"

    def test_serialize_hex_invalid_raw_raises(self):
        """Corrupt hex data must raise SerializerError."""
        s = BytesSerializer({"encoding": "hex"})
        with pytest.raises(SerializerError, match="decode error"):
            s.serialize([{"_raw": "zzzz"}])

    def test_serialize_hex_empty_payload(self):
        s = BytesSerializer({"encoding": "hex"})
        records = s.parse(b"")
        assert s.serialize(records) == b""

    # ── encoding=none path with _raw present (lines 76-77) ────────────────────

    def test_serialize_none_encoding_raw_str_encodes_as_utf8(self):
        """When encoding=none and _raw is a str, it is returned as UTF-8 bytes."""
        s = BytesSerializer({"encoding": "none"})
        result = s.serialize([{"_raw": "hello", "_size": 5}])
        assert result == b"hello"

    def test_serialize_none_encoding_raw_bytes_returned(self):
        """When encoding=none and _raw is already bytes-like, it is converted."""
        s = BytesSerializer({"encoding": "none"})
        result = s.serialize([{"_raw": bytearray(b"data"), "_size": 4}])
        assert result == b"data"

    # ── Only first record is used for _raw (line 62) ──────────────────────────

    def test_serialize_uses_only_first_record(self):
        """serialize() reads _raw only from the first record."""
        data = b"first"
        s = BytesSerializer({"encoding": "base64"})
        first_raw = base64.b64encode(data).decode("ascii")
        second_raw = base64.b64encode(b"second").decode("ascii")
        result = s.serialize([
            {"_raw": first_raw, "_size": 5},
            {"_raw": second_raw, "_size": 6},
        ])
        assert result == data


# ── Full roundtrip — parse then serialize ─────────────────────────────────────


class TestBytesSerializerRoundtrip:
    @pytest.mark.parametrize("encoding", ["base64", "hex"])
    def test_roundtrip_various_payloads(self, encoding):
        payloads = [
            b"simple ascii text",
            b"\x00\x01\x02\x03\x04\xff\xfe\xfd",
            b"",
            b"x" * 1024,
        ]
        s = BytesSerializer({"encoding": encoding})
        for payload in payloads:
            assert s.serialize(s.parse(payload)) == payload

    def test_parse_base64_serialize_hex_is_not_roundtrip(self):
        """Mismatched encoding means the values are not the same after roundtrip."""
        data = b"mismatch test"
        s_b64 = BytesSerializer({"encoding": "base64"})
        s_hex = BytesSerializer({"encoding": "hex"})
        records = s_b64.parse(data)
        # hex serializer cannot decode a base64 _raw string
        with pytest.raises(SerializerError):
            s_hex.serialize(records)
