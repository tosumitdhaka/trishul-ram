"""Tests for all transform implementations."""

from __future__ import annotations

from datetime import datetime

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.add_field import AddFieldTransform
from tram.transforms.cast import CastTransform
from tram.transforms.drop import DropTransform
from tram.transforms.filter_rows import FilterRowsTransform
from tram.transforms.inject_meta import InjectMetaTransform
from tram.transforms.rename import RenameTransform
from tram.transforms.value_map import ValueMapTransform

# ── RenameTransform ────────────────────────────────────────────────────────


class TestRenameTransform:
    def test_renames_fields(self):
        t = RenameTransform({"fields": {"old": "new", "ts": "timestamp"}})
        result = t.apply([{"old": "val", "ts": "2024"}])
        assert result == [{"new": "val", "timestamp": "2024"}]

    def test_unknown_fields_passed_through(self):
        t = RenameTransform({"fields": {"a": "b"}})
        result = t.apply([{"a": 1, "c": 2}])
        assert result == [{"b": 1, "c": 2}]

    def test_empty_records(self):
        t = RenameTransform({"fields": {"a": "b"}})
        assert t.apply([]) == []


# ── CastTransform ──────────────────────────────────────────────────────────


class TestCastTransform:
    def test_cast_int(self):
        t = CastTransform({"fields": {"x": "int"}})
        result = t.apply([{"x": "42"}])
        assert result[0]["x"] == 42

    def test_cast_float(self):
        t = CastTransform({"fields": {"x": "float"}})
        result = t.apply([{"x": "3.14"}])
        assert abs(result[0]["x"] - 3.14) < 0.001

    def test_cast_bool_truthy(self):
        t = CastTransform({"fields": {"x": "bool"}})
        for val in ["true", "True", "1", "yes", "YES", "on"]:
            result = t.apply([{"x": val}])
            assert result[0]["x"] is True, f"Expected True for {val!r}"

    def test_cast_bool_falsy(self):
        t = CastTransform({"fields": {"x": "bool"}})
        for val in ["false", "False", "0", "no", "off"]:
            result = t.apply([{"x": val}])
            assert result[0]["x"] is False, f"Expected False for {val!r}"

    def test_cast_datetime(self):
        t = CastTransform({"fields": {"ts": "datetime"}})
        result = t.apply([{"ts": "2024-01-15T10:30:00"}])
        assert isinstance(result[0]["ts"], datetime)
        assert result[0]["ts"].year == 2024

    def test_cast_str(self):
        t = CastTransform({"fields": {"x": "str"}})
        result = t.apply([{"x": 42}])
        assert result[0]["x"] == "42"

    def test_missing_field_ignored(self):
        t = CastTransform({"fields": {"missing": "int"}})
        result = t.apply([{"other": "val"}])
        assert result == [{"other": "val"}]

    def test_invalid_cast_raises(self):
        t = CastTransform({"fields": {"x": "int"}})
        with pytest.raises(TransformError):
            t.apply([{"x": "not-a-number"}])

    def test_unknown_type_raises_on_init(self):
        with pytest.raises(TransformError):
            CastTransform({"fields": {"x": "uuid"}})


# ── AddFieldTransform ──────────────────────────────────────────────────────


class TestAddFieldTransform:
    def test_basic_arithmetic(self):
        t = AddFieldTransform({"fields": {"double": "x * 2"}})
        result = t.apply([{"x": 5}])
        assert result[0]["double"] == 10

    def test_chained_fields(self):
        t = AddFieldTransform({"fields": {
            "rx_mbps": "rx_bytes / 1_000_000",
            "load_pct": "round(rx_mbps / 1000 * 100, 2)",
        }})
        result = t.apply([{"rx_bytes": 1_500_000}])
        assert result[0]["rx_mbps"] == 1.5
        assert result[0]["load_pct"] == 0.15

    def test_string_expression(self):
        t = AddFieldTransform({"fields": {"label": "'high' if x > 100 else 'normal'"}})
        result = t.apply([{"x": 200}])
        assert result[0]["label"] == "high"

    def test_round_function(self):
        t = AddFieldTransform({"fields": {"rounded": "round(x, 2)"}})
        result = t.apply([{"x": 3.14159}])
        assert result[0]["rounded"] == 3.14

    def test_invalid_expression_raises(self):
        t = AddFieldTransform({"fields": {"bad": "import os"}})
        with pytest.raises(TransformError):
            t.apply([{"x": 1}])


# ── DropTransform ──────────────────────────────────────────────────────────


class TestDropTransform:
    def test_drops_fields(self):
        t = DropTransform({"fields": ["a", "b"]})
        result = t.apply([{"a": 1, "b": 2, "c": 3}])
        assert result == [{"c": 3}]

    def test_missing_fields_ignored(self):
        t = DropTransform({"fields": ["missing"]})
        result = t.apply([{"a": 1}])
        assert result == [{"a": 1}]

    def test_empty_list(self):
        t = DropTransform({"fields": ["a"]})
        assert t.apply([]) == []


# ── ValueMapTransform ──────────────────────────────────────────────────────


class TestValueMapTransform:
    def test_maps_values(self):
        t = ValueMapTransform({
            "field": "severity",
            "mapping": {"1": "CRITICAL", "2": "MAJOR"},
        })
        result = t.apply([{"severity": "1"}, {"severity": "2"}])
        assert result[0]["severity"] == "CRITICAL"
        assert result[1]["severity"] == "MAJOR"

    def test_default_for_unknown(self):
        t = ValueMapTransform({
            "field": "severity",
            "mapping": {"1": "CRITICAL"},
            "default": "UNKNOWN",
        })
        result = t.apply([{"severity": "99"}])
        assert result[0]["severity"] == "UNKNOWN"

    def test_preserves_original_when_no_default(self):
        t = ValueMapTransform({
            "field": "severity",
            "mapping": {"1": "CRITICAL"},
        })
        result = t.apply([{"severity": "99"}])
        assert result[0]["severity"] == "99"

    def test_missing_field_ignored(self):
        t = ValueMapTransform({"field": "severity", "mapping": {"1": "CRITICAL"}})
        result = t.apply([{"other": "x"}])
        assert result == [{"other": "x"}]


# ── FilterRowsTransform ────────────────────────────────────────────────────


class TestFilterRowsTransform:
    def test_filters_rows(self):
        t = FilterRowsTransform({"condition": "x > 0"})
        result = t.apply([{"x": 5}, {"x": 0}, {"x": -1}, {"x": 3}])
        assert len(result) == 2
        assert all(r["x"] > 0 for r in result)

    def test_complex_condition(self):
        t = FilterRowsTransform({"condition": "status == 'active' and x > 0"})
        result = t.apply([
            {"status": "active", "x": 5},
            {"status": "inactive", "x": 5},
            {"status": "active", "x": -1},
        ])
        assert len(result) == 1
        assert result[0]["status"] == "active"

    def test_all_filtered(self):
        t = FilterRowsTransform({"condition": "x > 1000"})
        result = t.apply([{"x": 1}, {"x": 2}])
        assert result == []

    def test_invalid_condition_raises(self):
        t = FilterRowsTransform({"condition": "import os"})
        with pytest.raises(TransformError):
            t.apply([{"x": 1}])


# ── InjectMetaTransform ────────────────────────────────────────────────────


class TestInjectMetaTransform:
    def test_injects_selected_meta_fields(self):
        t = InjectMetaTransform({"fields": {"source_filename": "file_name", "run_id": "run_id"}})
        t.set_runtime_meta({"source_filename": "input.asn1", "run_id": "abc123"})
        result = t.apply([{"x": 1}])
        assert result == [{"x": 1, "file_name": "input.asn1", "run_id": "abc123"}]

    def test_include_all_copies_all_meta(self):
        t = InjectMetaTransform({"include_all": True})
        t.set_runtime_meta({"source_filename": "input.asn1", "source_host": "sftp1"})
        result = t.apply([{"x": 1}])
        assert result == [{"x": 1, "source_filename": "input.asn1", "source_host": "sftp1"}]

    def test_prefix_applies_to_injected_fields(self):
        t = InjectMetaTransform({"fields": {"source_filename": "filename"}, "prefix": "meta_"})
        t.set_runtime_meta({"source_filename": "input.asn1"})
        result = t.apply([{"x": 1}])
        assert result == [{"x": 1, "meta_filename": "input.asn1"}]

    def test_missing_meta_can_emit_null(self):
        t = InjectMetaTransform({"fields": {"source_filename": "file_name"}, "on_missing": "null"})
        t.set_runtime_meta({})
        result = t.apply([{"x": 1}])
        assert result == [{"x": 1, "file_name": None}]
