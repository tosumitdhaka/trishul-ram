"""Tests for the ASN.1 serializer."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tram.core.exceptions import SerializerError
from tram.serializers.asn1_serializer import _SCHEMA_CACHE, Asn1Serializer


@pytest.fixture(autouse=True)
def clear_asn1_cache():
    _SCHEMA_CACHE.clear()
    yield
    _SCHEMA_CACHE.clear()


def _schema_file(tmp_path):
    path = tmp_path / "test.asn"
    path.write_text("Test DEFINITIONS ::= BEGIN END")
    return path


class TestAsn1Serializer:
    def test_missing_schema_file_raises(self):
        with pytest.raises(SerializerError, match="schema_file"):
            Asn1Serializer({"message_class": "Foo"})

    def test_missing_message_class_raises(self):
        with pytest.raises(SerializerError, match="message_class"):
            Asn1Serializer({"schema_file": "/tmp/test.asn"})

    def test_message_class_and_message_classes_are_mutually_exclusive(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        with pytest.raises(SerializerError, match="exactly one"):
            Asn1Serializer(
                {
                    "schema_file": str(schema_file),
                    "message_class": "Foo",
                    "message_classes": ["Bar"],
                }
            )

    def test_split_records_requires_ber(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        with pytest.raises(SerializerError, match="only supported for BER"):
            Asn1Serializer(
                {
                    "schema_file": str(schema_file),
                    "message_class": "Foo",
                    "encoding": "der",
                    "split_records": True,
                }
            )

    def test_import_error_raises(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        serializer = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})
        with pytest.raises(SerializerError, match="asn1tools"):
            with pytest.MonkeyPatch.context() as mp:
                mp.setitem(sys.modules, "asn1tools", None)
                serializer.parse(b"payload")

    def test_schema_not_found_raises(self, tmp_path):
        serializer = Asn1Serializer(
            {"schema_file": str(tmp_path / "missing.asn"), "message_class": "Foo"}
        )
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock())
        with pytest.raises(SerializerError, match="schema not found"):
            with pytest.MonkeyPatch.context() as mp:
                mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
                serializer.parse(b"payload")

    def test_empty_schema_directory_raises(self, tmp_path):
        schema_dir = tmp_path / "schemas"
        schema_dir.mkdir()
        serializer = Asn1Serializer({"schema_file": str(schema_dir), "message_class": "Foo"})
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock())
        with pytest.raises(SerializerError, match="No .asn files found"):
            with pytest.MonkeyPatch.context() as mp:
                mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
                serializer.parse(b"payload")

    def test_compile_error_raises(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        fake_asn1tools = SimpleNamespace(
            compile_files=MagicMock(side_effect=RuntimeError("bad schema"))
        )
        serializer = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})
        with pytest.raises(SerializerError, match="schema compile error"):
            with pytest.MonkeyPatch.context() as mp:
                mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
                serializer.parse(b"payload")

    def test_parse_success_converts_json_safe_values(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        compiled.decode.return_value = {
            "ts": datetime(2026, 4, 16, 10, 0, tzinfo=UTC),
            "choice": ("iValue", 7),
            "blob": b"\xde\xad",
            "items": (1, 2, 3),
        }
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = serializer.parse(b"payload")

        assert result == [{
            "ts": "2026-04-16T10:00:00+00:00",
            "choice": {"type": "iValue", "value": 7},
            "blob": "dead",
            "items": [1, 2, 3],
        }]

    def test_parse_wraps_scalar_result(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        compiled.decode.return_value = 5
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = serializer.parse(b"payload")

        assert result == [{"value": 5}]

    def test_parse_decode_error_raises(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        compiled.decode.side_effect = ValueError("malformed")
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})

        with pytest.raises(SerializerError, match="decode error"):
            with pytest.MonkeyPatch.context() as mp:
                mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
                serializer.parse(b"payload")

    def test_parse_tries_message_classes_in_order(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        compiled.decode.side_effect = [
            ValueError("bad Foo"),
            {"kind": "bar"},
        ]
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = Asn1Serializer(
            {
                "schema_file": str(schema_file),
                "message_classes": ["Foo", "Bar"],
            }
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = serializer.parse(b"payload")

        assert result == [{"kind": "bar"}]
        assert compiled.decode.call_args_list[0].args == ("Foo", b"payload")
        assert compiled.decode.call_args_list[1].args == ("Bar", b"payload")

    def test_parse_raises_when_all_message_classes_fail(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        compiled.decode.side_effect = [ValueError("bad Foo"), ValueError("bad Bar")]
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = Asn1Serializer(
            {
                "schema_file": str(schema_file),
                "message_classes": ["Foo", "Bar"],
            }
        )

        with pytest.raises(SerializerError, match="Foo"):
            with pytest.MonkeyPatch.context() as mp:
                mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
                serializer.parse(b"payload")

    def test_parse_split_records_decodes_each_top_level_tlv(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        record1 = b"\x30\x03abc"
        record2 = b"\x30\x03def"
        compiled.decode.side_effect = [{"id": 1}, {"id": 2}]
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = Asn1Serializer(
            {
                "schema_file": str(schema_file),
                "message_class": "Foo",
                "split_records": True,
            }
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = serializer.parse(record1 + record2)

        assert result == [{"id": 1}, {"id": 2}]
        assert compiled.decode.call_args_list[0].args == ("Foo", record1)
        assert compiled.decode.call_args_list[1].args == ("Foo", record2)

    def test_parse_split_records_supports_indefinite_length(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        record = b"\x30\x80\x02\x01\x05\x00\x00"
        compiled.decode.return_value = {"value": 5}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = Asn1Serializer(
            {
                "schema_file": str(schema_file),
                "message_class": "Foo",
                "split_records": True,
            }
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = serializer.parse(record)

        assert result == [{"value": 5}]
        assert compiled.decode.call_args.args == ("Foo", record)

    def test_compiled_schema_is_cached(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        compiled.decode.return_value = {"x": 1}
        compile_files = MagicMock(return_value=compiled)
        fake_asn1tools = SimpleNamespace(compile_files=compile_files)

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            first = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})
            second = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})
            assert first.parse(b"one") == [{"x": 1}]
            assert second.parse(b"two") == [{"x": 1}]

        compile_files.assert_called_once()

    def test_serialize_is_explicitly_unsupported(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        serializer = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})
        with pytest.raises(SerializerError, match="decode-only"):
            serializer.serialize([{"x": 1}])
