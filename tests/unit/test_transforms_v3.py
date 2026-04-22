"""Unit tests for ValidateTransform, SortTransform, LimitTransform,
JmesPathExtractTransform, UnnestTransform, and TimestampNormalize epoch output formats."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.jmespath_extract import JmesPathExtractTransform
from tram.transforms.limit import LimitTransform
from tram.transforms.sort import SortTransform
from tram.transforms.timestamp_normalize import TimestampNormalizeTransform
from tram.transforms.unnest import UnnestTransform
from tram.transforms.validate import ValidateTransform

# ── ValidateTransform ─────────────────────────────────────────────────────────


class TestValidateTransform:
    def test_required_field_missing_drops(self):
        t = ValidateTransform({"rules": {"name": {"required": True}}})
        result = t.apply([{"age": 30}])
        assert result == []

    def test_type_mismatch_drops(self):
        t = ValidateTransform({"rules": {"age": {"type": "int"}}})
        result = t.apply([{"age": "not-an-int"}])
        assert result == []

    def test_min_max_pass(self):
        t = ValidateTransform({"rules": {"score": {"min": 0, "max": 100}}})
        result = t.apply([{"score": 50}])
        assert len(result) == 1

    def test_min_max_fail_drops(self):
        t = ValidateTransform({"rules": {"score": {"min": 0, "max": 100}}})
        result = t.apply([{"score": 150}])
        assert result == []

    def test_regex_rule_match(self):
        t = ValidateTransform({"rules": {"email": {"regex": r"@"}}})
        result = t.apply([{"email": "user@example.com"}])
        assert len(result) == 1

    def test_regex_rule_mismatch(self):
        t = ValidateTransform({"rules": {"email": {"regex": r"@"}}})
        result = t.apply([{"email": "not-an-email"}])
        assert result == []

    def test_allowed_rule_pass(self):
        t = ValidateTransform({"rules": {"status": {"allowed": ["active", "inactive"]}}})
        result = t.apply([{"status": "active"}])
        assert len(result) == 1

    def test_allowed_rule_fail(self):
        t = ValidateTransform({"rules": {"status": {"allowed": ["active", "inactive"]}}})
        result = t.apply([{"status": "unknown"}])
        assert result == []

    def test_on_invalid_raise(self):
        t = ValidateTransform(
            {"rules": {"name": {"required": True}}, "on_invalid": "raise"}
        )
        with pytest.raises(TransformError):
            t.apply([{"age": 30}])

    def test_valid_record_kept(self):
        t = ValidateTransform(
            {"rules": {"name": {"required": True, "type": "str"}, "age": {"min": 0}}}
        )
        records = [{"name": "Alice", "age": 25}]
        result = t.apply(records)
        assert result == records


# ── SortTransform ─────────────────────────────────────────────────────────────


class TestSortTransform:
    def test_sort_ascending(self):
        t = SortTransform({"fields": ["age"]})
        records = [{"age": 30}, {"age": 10}, {"age": 20}]
        result = t.apply(records)
        assert [r["age"] for r in result] == [10, 20, 30]

    def test_sort_descending(self):
        t = SortTransform({"fields": ["age"], "reverse": True})
        records = [{"age": 10}, {"age": 30}, {"age": 20}]
        result = t.apply(records)
        assert [r["age"] for r in result] == [30, 20, 10]

    def test_sort_multi_field(self):
        t = SortTransform({"fields": ["dept", "name"]})
        records = [
            {"dept": "eng", "name": "zara"},
            {"dept": "eng", "name": "alice"},
            {"dept": "art", "name": "bob"},
        ]
        result = t.apply(records)
        assert result[0] == {"dept": "art", "name": "bob"}
        assert result[1] == {"dept": "eng", "name": "alice"}
        assert result[2] == {"dept": "eng", "name": "zara"}

    def test_sort_none_values(self):
        t = SortTransform({"fields": ["score"]})
        records = [{"score": 5}, {"score": None}, {"score": 3}]
        # None should sort first (before all real values)
        result = t.apply(records)
        assert result[0]["score"] is None

    def test_empty_fields_raises(self):
        with pytest.raises(TransformError):
            SortTransform({"fields": []})


# ── LimitTransform ────────────────────────────────────────────────────────────


class TestLimitTransform:
    def test_limit_trims(self):
        t = LimitTransform({"count": 3})
        records = [{"n": i} for i in range(5)]
        result = t.apply(records)
        assert len(result) == 3
        assert result == [{"n": 0}, {"n": 1}, {"n": 2}]

    def test_limit_larger_than_batch(self):
        t = LimitTransform({"count": 10})
        records = [{"n": i} for i in range(3)]
        result = t.apply(records)
        assert len(result) == 3

    def test_limit_zero_raises(self):
        with pytest.raises(TransformError):
            LimitTransform({"count": 0})


# ── JmesPathExtractTransform ──────────────────────────────────────────────────


class TestJmesPathExtractTransform:
    def test_extract_nested(self):
        mock_jmespath = MagicMock()
        compiled = MagicMock()
        compiled.search.return_value = "extracted"
        mock_jmespath.compile.return_value = compiled
        with patch.dict(sys.modules, {"jmespath": mock_jmespath}):
            t = JmesPathExtractTransform({"fields": {"out": "a.b"}})
            result = t.apply([{"a": {"b": "extracted"}}])
        assert result[0]["out"] == "extracted"

    def test_multiple_expressions(self):
        mock_jmespath = MagicMock()

        def fake_compile(expr):
            m = MagicMock()
            m.search.side_effect = lambda rec: rec.get(expr.split(".")[-1])
            return m

        mock_jmespath.compile.side_effect = fake_compile
        with patch.dict(sys.modules, {"jmespath": mock_jmespath}):
            t = JmesPathExtractTransform({"fields": {"x": "data.x", "y": "data.y"}})
            result = t.apply([{"x": 1, "y": 2}])
        assert "x" in result[0]
        assert "y" in result[0]

    def test_missing_path_returns_none(self):
        mock_jmespath = MagicMock()
        compiled = MagicMock()
        compiled.search.return_value = None
        mock_jmespath.compile.return_value = compiled
        with patch.dict(sys.modules, {"jmespath": mock_jmespath}):
            t = JmesPathExtractTransform({"fields": {"out": "nonexistent.path"}})
            result = t.apply([{"other": "value"}])
        assert result[0]["out"] is None

    def test_import_error(self):
        with patch.dict(sys.modules, {"jmespath": None}):
            t = JmesPathExtractTransform({"fields": {"out": "a.b"}})
            with pytest.raises(TransformError, match="jmespath"):
                t.apply([{"a": {"b": 1}}])


# ── UnnestTransform ───────────────────────────────────────────────────────────


class TestUnnestTransform:
    def test_basic_unnest(self):
        t = UnnestTransform({"field": "meta"})
        records = [{"id": 1, "meta": {"host": "srv1", "env": "prod"}}]
        result = t.apply(records)
        assert result[0] == {"id": 1, "host": "srv1", "env": "prod"}
        assert "meta" not in result[0]

    def test_prefix(self):
        t = UnnestTransform({"field": "meta", "prefix": "m_"})
        records = [{"id": 1, "meta": {"host": "srv1"}}]
        result = t.apply(records)
        assert result[0]["m_host"] == "srv1"
        assert "meta" not in result[0]

    def test_drop_source_false(self):
        t = UnnestTransform({"field": "meta", "drop_source": False})
        records = [{"id": 1, "meta": {"host": "srv1"}}]
        result = t.apply(records)
        assert result[0]["host"] == "srv1"
        assert "meta" in result[0]

    def test_on_non_dict_keep(self):
        t = UnnestTransform({"field": "meta", "on_non_dict": "keep"})
        records = [{"id": 1, "meta": "not-a-dict"}]
        result = t.apply(records)
        assert result == records

    def test_on_non_dict_drop(self):
        t = UnnestTransform({"field": "meta", "on_non_dict": "drop"})
        records = [{"id": 1, "meta": "not-a-dict"}]
        result = t.apply(records)
        assert result == []

    def test_on_non_dict_raise(self):
        t = UnnestTransform({"field": "meta", "on_non_dict": "raise"})
        with pytest.raises(TransformError):
            t.apply([{"id": 1, "meta": "not-a-dict"}])

    def test_missing_field_raises(self):
        with pytest.raises(TransformError):
            UnnestTransform({})

    def test_nested_path_unnest(self):
        t = UnnestTransform({"field": "a.meta"})
        records = [{"id": 1, "a": {"meta": {"host": "srv1"}, "x": 2}}]
        result = t.apply(records)
        assert result == [{"id": 1, "a": {"x": 2}, "host": "srv1"}]

    def test_nested_scalar_raise(self):
        t = UnnestTransform({"field": "a.meta", "on_non_dict": "raise"})
        with pytest.raises(TransformError):
            t.apply([{"a": {"meta": "not-a-dict"}}])


# ── TimestampNormalize epoch output ───────────────────────────────────────────


class TestTimestampNormalizeEpochOutput:
    def test_iso_to_epoch_s(self):
        t = TimestampNormalizeTransform({"fields": ["ts"], "output_format": "epoch_s"})
        result = t.apply([{"ts": "1970-01-01T00:00:01Z"}])
        assert result[0]["ts"] == pytest.approx(1.0, abs=0.001)

    def test_iso_to_epoch_ms(self):
        t = TimestampNormalizeTransform({"fields": ["ts"], "output_format": "epoch_ms"})
        result = t.apply([{"ts": "1970-01-01T00:00:01Z"}])
        assert result[0]["ts"] == 1000

    def test_epoch_to_epoch_ms(self):
        t = TimestampNormalizeTransform({"fields": ["ts"], "output_format": "epoch_ms"})
        result = t.apply([{"ts": 1000}])  # 1000 seconds epoch
        assert result[0]["ts"] == 1_000_000

    def test_epoch_s_to_iso(self):
        t = TimestampNormalizeTransform({"fields": ["ts"], "output_format": "iso"})
        result = t.apply([{"ts": 0}])
        assert result[0]["ts"] == "1970-01-01T00:00:00.000Z"

    def test_epoch_ns(self):
        t = TimestampNormalizeTransform({"fields": ["ts"], "output_format": "epoch_ns"})
        result = t.apply([{"ts": "1970-01-01T00:00:01Z"}])
        assert result[0]["ts"] == 1_000_000_000
