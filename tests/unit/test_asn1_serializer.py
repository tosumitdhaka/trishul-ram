"""Tests for the ASN.1 serializer."""

from __future__ import annotations

import os
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

    def test_parse_chunks_split_records_yields_bounded_batches(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        record1 = b"\x30\x03abc"
        record2 = b"\x30\x03def"
        record3 = b"\x30\x03ghi"
        compiled.decode.side_effect = [{"id": 1}, {"id": 2}, {"id": 3}]
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
            result = list(serializer.parse_chunks(record1 + record2 + record3, 2))

        assert result == [
            [{"id": 1}, {"id": 2}],
            [{"id": 3}],
        ]
        assert compiled.decode.call_args_list[0].args == ("Foo", record1)
        assert compiled.decode.call_args_list[1].args == ("Foo", record2)
        assert compiled.decode.call_args_list[2].args == ("Foo", record3)

    def test_parse_chunks_without_split_records_decodes_single_document_once(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        compiled.decode.return_value = {"id": 1}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = Asn1Serializer(
            {
                "schema_file": str(schema_file),
                "message_class": "Foo",
                "split_records": False,
            }
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = list(serializer.parse_chunks(b"payload", 2))

        assert result == [[{"id": 1}]]
        assert compiled.decode.call_count == 1
        assert compiled.decode.call_args.args == ("Foo", b"payload")

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

    def test_same_content_varying_mtime_keeps_cache_at_one(self, tmp_path):
        """Identical schema bytes with churning mtimes must not grow the cache."""
        schema_file = _schema_file(tmp_path)
        compiled = MagicMock()
        compiled.decode.return_value = {"x": 1}
        compile_files = MagicMock(return_value=compiled)
        fake_asn1tools = SimpleNamespace(compile_files=compile_files)

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            for mtime in (1000.0, 2000.0, 3000.0):
                os.utime(schema_file, (mtime, mtime))
                serializer = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})
                assert serializer.parse(b"payload") == [{"x": 1}]

        assert len(_SCHEMA_CACHE) == 1
        compile_files.assert_called_once()

    def test_identical_schema_content_in_different_dirs_shares_cache(self, tmp_path):
        """Content-hash keys make identical bytes at different paths a single entry."""
        for name in ("a", "b"):
            schema_dir = tmp_path / name
            schema_dir.mkdir()
            (schema_dir / "test.asn").write_text("Test DEFINITIONS ::= BEGIN END")
        compiled = MagicMock()
        compiled.decode.return_value = {"x": 1}
        compile_files = MagicMock(return_value=compiled)
        fake_asn1tools = SimpleNamespace(compile_files=compile_files)

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            for name in ("a", "b"):
                serializer = Asn1Serializer(
                    {"schema_file": str(tmp_path / name), "message_class": "Foo"}
                )
                serializer.parse(b"payload")

        assert len(_SCHEMA_CACHE) == 1
        compile_files.assert_called_once()

    def test_serialize_is_explicitly_unsupported(self, tmp_path):
        schema_file = _schema_file(tmp_path)
        serializer = Asn1Serializer({"schema_file": str(schema_file), "message_class": "Foo"})
        with pytest.raises(SerializerError, match="decode-only"):
            serializer.serialize([{"x": 1}])


class TestAsn1SplitPath:
    """GH #19: split_path / split_path_context for single-frame BER inputs."""

    def _make_serializer(self, tmp_path, config: dict) -> Asn1Serializer:
        schema_file = _schema_file(tmp_path)
        base = {"schema_file": str(schema_file), "message_class": "Foo"}
        base.update(config)
        return Asn1Serializer(base)

    def test_split_path_and_split_records_are_mutually_exclusive(self, tmp_path):
        with pytest.raises(SerializerError, match="mutually exclusive"):
            self._make_serializer(
                tmp_path, {"split_records": True, "split_path": "records"}
            )

    def test_split_path_context_requires_split_path(self, tmp_path):
        with pytest.raises(SerializerError, match="requires 'split_path'"):
            self._make_serializer(tmp_path, {"split_path_context": {"vendor": "E"}})

    def test_parse_split_path_fans_out_records(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {
            "stats": {"measurement": [{"id": 1}, {"id": 2}, {"id": 3}]},
        }
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(tmp_path, {"split_path": "stats.measurement"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = serializer.parse(b"payload")

        assert result == [{"id": 1}, {"id": 2}, {"id": 3}]
        assert compiled.decode.call_args.args == ("Foo", b"payload")

    def test_parse_split_path_context_is_copied_per_record(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {
            "records": [{"id": 1}, {"id": 2}],
        }
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(
            tmp_path,
            {
                "split_path": "records",
                "split_path_context": {"vendor": "E", "nested": {"k": 0}},
            },
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = serializer.parse(b"payload")

        assert result == [
            {"id": 1, "vendor": "E", "nested": {"k": 0}},
            {"id": 2, "vendor": "E", "nested": {"k": 0}},
        ]
        # Mutating one record's context must not leak into the others
        # (RCA #19 aliasing hazard).
        result[0]["nested"]["k"] = 99
        assert result[1]["nested"]["k"] == 0

    def test_parse_split_path_record_fields_take_precedence(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {"records": [{"id": 1, "vendor": "own"}]}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(
            tmp_path,
            {"split_path": "records", "split_path_context": {"vendor": "context"}},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = serializer.parse(b"payload")

        assert result == [{"id": 1, "vendor": "own"}]

    def test_parse_split_path_scalar_record_is_wrapped(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {"records": [5, 6]}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(
            tmp_path,
            {"split_path": "records", "split_path_context": {"vendor": "E"}},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = serializer.parse(b"payload")

        assert result == [
            {"vendor": "E", "value": 5},
            {"vendor": "E", "value": 6},
        ]

    def test_parse_split_path_missing_target_fails_loud(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {"stats": {"measurement": []}}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(tmp_path, {"split_path": "stats.missing"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            with pytest.raises(SerializerError, match="split_path 'stats.missing'"):
                serializer.parse(b"payload")

    def test_parse_split_path_non_list_target_fails_loud(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {"stats": {"measurement": {"id": 1}}}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(tmp_path, {"split_path": "stats.measurement"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            with pytest.raises(SerializerError, match="does not resolve to a list"):
                serializer.parse(b"payload")

    def test_parse_split_path_empty_list_returns_empty(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {"stats": {"measurement": []}}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(tmp_path, {"split_path": "stats.measurement"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            assert serializer.parse(b"payload") == []

    def test_parse_chunks_split_path_yields_bounded_batches(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {"records": [{"id": i} for i in range(5)]}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(tmp_path, {"split_path": "records"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = list(serializer.parse_chunks(b"payload", 2))

        assert result == [
            [{"id": 0}, {"id": 1}],
            [{"id": 2}, {"id": 3}],
            [{"id": 4}],
        ]
        # One decode for the whole document; the fan-out is sliced lazily.
        assert compiled.decode.call_count == 1

    def test_parse_chunks_split_path_fans_out_incrementally(self, tmp_path, monkeypatch):
        """The generator must merge records batch-by-batch, never building the
        full merged list up front (GH #19 memory-bound point)."""
        import tram.serializers.asn1_serializer as mod

        compiled = MagicMock()
        compiled.decode.return_value = {"records": [{"id": i} for i in range(5)]}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(tmp_path, {"split_path": "records"})

        merged_ids: list[int] = []
        orig = mod._emit_split_record

        def spy(element, context):
            merged_ids.append(element["id"])
            return orig(element, context)

        monkeypatch.setattr(mod, "_emit_split_record", spy)

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            gen = serializer.parse_chunks(b"payload", 2)
            assert iter(gen) is gen  # a generator, not a pre-built list

            first = next(gen)
            assert first == [{"id": 0}, {"id": 1}]
            assert merged_ids == [0, 1]  # only the first batch was merged so far

            second = next(gen)
            assert second == [{"id": 2}, {"id": 3}]
            assert merged_ids == [0, 1, 2, 3]

            third = next(gen)
            assert third == [{"id": 4}]
            assert merged_ids == [0, 1, 2, 3, 4]

    def test_parse_chunks_split_path_context_is_deepcopied_between_batches(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {"records": [{"id": 1}, {"id": 2}]}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(
            tmp_path,
            {"split_path": "records", "split_path_context": {"nested": {"k": 0}}},
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            gen = serializer.parse_chunks(b"payload", 1)
            batch1 = next(gen)
            batch2 = next(gen)

        batch1[0]["nested"]["k"] = 99
        assert batch2[0]["nested"]["k"] == 0

    def test_parse_chunks_split_path_empty_target_yields_nothing(self, tmp_path):
        compiled = MagicMock()
        compiled.decode.return_value = {"records": []}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(tmp_path, {"split_path": "records"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            assert list(serializer.parse_chunks(b"payload", 2)) == []

    def test_parse_chunks_without_chunk_size_returns_single_split_batch(self, tmp_path):
        """record_chunk_size<=0 falls back to parse(), which still applies the
        split (parity between the two entry points, RCA #19 threaded path)."""
        compiled = MagicMock()
        compiled.decode.return_value = {"records": [{"id": 1}, {"id": 2}]}
        fake_asn1tools = SimpleNamespace(compile_files=MagicMock(return_value=compiled))
        serializer = self._make_serializer(tmp_path, {"split_path": "records"})

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "asn1tools", fake_asn1tools)
            result = list(serializer.parse_chunks(b"payload", 0))

        assert result == [[{"id": 1}, {"id": 2}]]
