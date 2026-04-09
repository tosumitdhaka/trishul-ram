"""Unit tests for MeltTransform (wide → long pivot)."""

from __future__ import annotations

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.melt import MeltTransform


class TestMeltTransformBasic:
    def test_basic_melt(self):
        t = MeltTransform({"value_field": "_metrics"})
        records = [{"_metrics": {"a": 1, "b": 2}, "ts": "2026-01-01"}]
        result = t.apply(records)
        assert len(result) == 2
        assert {"ts": "2026-01-01", "metric_name": "a", "metric_value": 1} in result
        assert {"ts": "2026-01-01", "metric_name": "b", "metric_value": 2} in result

    def test_value_field_required(self):
        with pytest.raises(TransformError, match="value_field"):
            MeltTransform({})

    def test_missing_value_field_passthrough(self):
        t = MeltTransform({"value_field": "_metrics"})
        record = {"other": "data"}
        result = t.apply([record])
        assert result == [record]

    def test_non_dict_value_field_passthrough(self):
        t = MeltTransform({"value_field": "_metrics"})
        record = {"_metrics": "not_a_dict", "ts": "x"}
        result = t.apply([record])
        assert result == [record]

    def test_empty_value_dict(self):
        t = MeltTransform({"value_field": "_metrics"})
        result = t.apply([{"_metrics": {}}])
        assert result == []

    def test_empty_records(self):
        t = MeltTransform({"value_field": "_metrics"})
        assert t.apply([]) == []


class TestMeltTransformLabelFields:
    def test_label_fields_unnested(self):
        t = MeltTransform({"value_field": "_metrics", "label_fields": ["_labels"]})
        records = [{
            "_metrics": {"ifInOctets": 1000, "ifOutOctets": 2000},
            "_labels": {"ifIndex": "1", "ifDescr": "lo"},
            "_polled_at": "2026-04-09T10:00:00Z",
        }]
        result = t.apply(records)
        assert len(result) == 2
        for row in result:
            assert row["ifIndex"] == "1"
            assert row["ifDescr"] == "lo"
            assert row["_polled_at"] == "2026-04-09T10:00:00Z"
            assert "_metrics" not in row
            assert "_labels" not in row

    def test_label_field_not_dict_ignored(self):
        t = MeltTransform({"value_field": "_metrics", "label_fields": ["_labels"]})
        records = [{"_metrics": {"a": 1}, "_labels": "not_a_dict"}]
        result = t.apply(records)
        assert len(result) == 1
        assert result[0]["metric_name"] == "a"

    def test_multiple_label_fields(self):
        t = MeltTransform({
            "value_field": "_metrics",
            "label_fields": ["_labels", "_tags"],
        })
        records = [{
            "_metrics": {"cpu": 50},
            "_labels": {"host": "h1"},
            "_tags": {"env": "prod"},
        }]
        result = t.apply(records)
        assert len(result) == 1
        assert result[0]["host"] == "h1"
        assert result[0]["env"] == "prod"


class TestMeltTransformDropSource:
    def test_drop_source_true_default(self):
        t = MeltTransform({"value_field": "_metrics"})
        result = t.apply([{"_metrics": {"a": 1}}])
        assert "_metrics" not in result[0]

    def test_drop_source_false(self):
        t = MeltTransform({"value_field": "_metrics", "drop_source": False})
        result = t.apply([{"_metrics": {"a": 1}}])
        assert "_metrics" in result[0]
        assert result[0]["_metrics"] == {"a": 1}

    def test_drop_source_false_keeps_label_fields(self):
        t = MeltTransform({
            "value_field": "_metrics",
            "label_fields": ["_labels"],
            "drop_source": False,
        })
        result = t.apply([{"_metrics": {"a": 1}, "_labels": {"x": "y"}}])
        assert "_labels" in result[0]
        assert result[0]["x"] == "y"


class TestMeltTransformCustomColumns:
    def test_custom_metric_name_col(self):
        t = MeltTransform({"value_field": "_metrics", "metric_name_col": "counter"})
        result = t.apply([{"_metrics": {"cpu": 80}}])
        assert "counter" in result[0]
        assert "metric_name" not in result[0]
        assert result[0]["counter"] == "cpu"

    def test_custom_metric_value_col(self):
        t = MeltTransform({"value_field": "_metrics", "metric_value_col": "value"})
        result = t.apply([{"_metrics": {"cpu": 80}}])
        assert "value" in result[0]
        assert "metric_value" not in result[0]
        assert result[0]["value"] == 80


class TestMeltTransformFiltering:
    def test_include_only(self):
        t = MeltTransform({"value_field": "_metrics", "include_only": ["a", "b"]})
        result = t.apply([{"_metrics": {"a": 1, "b": 2, "c": 3}}])
        assert len(result) == 2
        names = {r["metric_name"] for r in result}
        assert names == {"a", "b"}

    def test_exclude(self):
        t = MeltTransform({"value_field": "_metrics", "exclude": ["c"]})
        result = t.apply([{"_metrics": {"a": 1, "b": 2, "c": 3}}])
        assert len(result) == 2
        names = {r["metric_name"] for r in result}
        assert names == {"a", "b"}

    def test_include_only_and_exclude_include_wins(self):
        # include_only takes precedence — only matching keys pass first check
        t = MeltTransform({"value_field": "_metrics", "include_only": ["a"], "exclude": ["a"]})
        # "a" is in include_only but also in exclude — exclude removes it
        result = t.apply([{"_metrics": {"a": 1, "b": 2}}])
        assert result == []


class TestMeltTransformMultipleRecords:
    def test_multiple_input_records(self):
        t = MeltTransform({"value_field": "_m"})
        records = [
            {"_m": {"x": 1}, "node": "n1"},
            {"_m": {"x": 2}, "node": "n2"},
        ]
        result = t.apply(records)
        assert len(result) == 2
        assert result[0]["node"] == "n1"
        assert result[1]["node"] == "n2"

    def test_mixed_valid_and_passthrough_records(self):
        t = MeltTransform({"value_field": "_m"})
        records = [
            {"_m": {"a": 1}},
            {"no_m": True},   # passthrough
        ]
        result = t.apply(records)
        assert len(result) == 2
        # First becomes a melted row, second passthrough
        assert result[0]["metric_name"] == "a"
        assert result[1] == {"no_m": True}

    def test_parent_fields_preserved_per_row(self):
        t = MeltTransform({"value_field": "_metrics"})
        records = [{"_metrics": {"a": 10, "b": 20}, "host": "h1", "region": "us"}]
        result = t.apply(records)
        for row in result:
            assert row["host"] == "h1"
            assert row["region"] == "us"

    def test_base_record_not_mutated(self):
        t = MeltTransform({"value_field": "_metrics"})
        original = {"_metrics": {"a": 1, "b": 2}, "ts": "x"}
        t.apply([original])
        assert "_metrics" in original  # original dict not modified
