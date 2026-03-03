"""Tests for new transforms: flatten, timestamp_normalize, aggregate, enrich."""

from __future__ import annotations

import csv
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tram.transforms.aggregate import AggregateTransform
from tram.transforms.enrich import EnrichTransform
from tram.transforms.flatten import FlattenTransform
from tram.transforms.timestamp_normalize import TimestampNormalizeTransform
from tram.core.exceptions import TransformError


# ── FlattenTransform ───────────────────────────────────────────────────────


class TestFlattenTransform:
    def test_basic_flatten(self):
        t = FlattenTransform({"separator": "_"})
        result = t.apply([{"a": {"b": 1, "c": 2}, "d": 3}])
        assert result == [{"a_b": 1, "a_c": 2, "d": 3}]

    def test_deep_flatten(self):
        t = FlattenTransform({"separator": "_"})
        result = t.apply([{"a": {"b": {"c": {"d": 42}}}}])
        assert result == [{"a_b_c_d": 42}]

    def test_max_depth(self):
        t = FlattenTransform({"separator": "_", "max_depth": 1})
        result = t.apply([{"a": {"b": {"c": 1}}}])
        assert result == [{"a_b": {"c": 1}}]  # only one level deep

    def test_custom_separator(self):
        t = FlattenTransform({"separator": "."})
        result = t.apply([{"a": {"b": 1}}])
        assert result == [{"a.b": 1}]

    def test_prefix(self):
        t = FlattenTransform({"separator": "_", "prefix": "root"})
        result = t.apply([{"x": 1}])
        assert result == [{"root_x": 1}]

    def test_non_nested_passthrough(self):
        t = FlattenTransform({})
        records = [{"a": 1, "b": "hello", "c": [1, 2, 3]}]
        result = t.apply(records)
        assert result == records  # lists not flattened

    def test_empty_dict_not_flattened(self):
        t = FlattenTransform({})
        result = t.apply([{"a": {}}])
        assert result == [{"a": {}}]

    def test_empty_records(self):
        t = FlattenTransform({})
        assert t.apply([]) == []


# ── TimestampNormalizeTransform ────────────────────────────────────────────


class TestTimestampNormalizeTransform:
    def test_iso_string(self):
        t = TimestampNormalizeTransform({"fields": ["ts"]})
        result = t.apply([{"ts": "2024-01-15T10:30:00"}])
        assert result[0]["ts"].endswith("Z")
        assert "2024-01-15" in result[0]["ts"]

    def test_iso_with_timezone(self):
        t = TimestampNormalizeTransform({"fields": ["ts"]})
        result = t.apply([{"ts": "2024-01-15T12:30:00+02:00"}])
        # +02:00 → UTC = 10:30
        assert "10:30" in result[0]["ts"]

    def test_unix_seconds(self):
        t = TimestampNormalizeTransform({"fields": ["ts"]})
        result = t.apply([{"ts": 1705312200}])  # 2024-01-15T10:30:00Z
        assert "2024-01-15" in result[0]["ts"]

    def test_unix_milliseconds(self):
        t = TimestampNormalizeTransform({"fields": ["ts"]})
        result = t.apply([{"ts": 1705312200000}])  # ms
        assert "2024-01-15" in result[0]["ts"]

    def test_unix_nanoseconds(self):
        t = TimestampNormalizeTransform({"fields": ["ts"]})
        result = t.apply([{"ts": 1705312200000000000}])  # ns
        assert "2024-01-15" in result[0]["ts"]

    def test_numeric_string(self):
        t = TimestampNormalizeTransform({"fields": ["ts"]})
        result = t.apply([{"ts": "1705312200"}])
        assert "2024-01-15" in result[0]["ts"]

    def test_datetime_output(self):
        t = TimestampNormalizeTransform({"fields": ["ts"], "output_format": "datetime"})
        result = t.apply([{"ts": "2024-01-15T10:30:00Z"}])
        assert isinstance(result[0]["ts"], datetime)
        assert result[0]["ts"].tzinfo is not None

    def test_already_datetime(self):
        t = TimestampNormalizeTransform({"fields": ["ts"]})
        dt = datetime(2024, 1, 15, 10, 30, tzinfo=timezone.utc)
        result = t.apply([{"ts": dt}])
        assert "2024-01-15" in result[0]["ts"]

    def test_on_error_null(self):
        t = TimestampNormalizeTransform({"fields": ["ts"], "on_error": "null"})
        result = t.apply([{"ts": "not-a-timestamp"}])
        assert result[0]["ts"] is None

    def test_on_error_keep(self):
        t = TimestampNormalizeTransform({"fields": ["ts"], "on_error": "keep"})
        result = t.apply([{"ts": "not-a-timestamp"}])
        assert result[0]["ts"] == "not-a-timestamp"

    def test_on_error_raise(self):
        t = TimestampNormalizeTransform({"fields": ["ts"], "on_error": "raise"})
        with pytest.raises(TransformError):
            t.apply([{"ts": "not-a-timestamp"}])

    def test_missing_field_ignored(self):
        t = TimestampNormalizeTransform({"fields": ["ts"]})
        result = t.apply([{"other": "x"}])
        assert result == [{"other": "x"}]

    def test_multiple_fields(self):
        t = TimestampNormalizeTransform({"fields": ["created", "updated"]})
        result = t.apply([{"created": "2024-01-01T00:00:00", "updated": "2024-06-01T00:00:00"}])
        assert result[0]["created"].endswith("Z")
        assert result[0]["updated"].endswith("Z")

    def test_missing_fields_raises_on_init(self):
        with pytest.raises(TransformError):
            TimestampNormalizeTransform({"fields": []})


# ── AggregateTransform ─────────────────────────────────────────────────────


class TestAggregateTransform:
    def test_global_sum(self):
        t = AggregateTransform({"group_by": [], "operations": {"total": "sum:value"}})
        result = t.apply([{"value": 10}, {"value": 20}, {"value": 30}])
        assert len(result) == 1
        assert result[0]["total"] == 60

    def test_global_avg(self):
        t = AggregateTransform({"group_by": [], "operations": {"mean": "avg:value"}})
        result = t.apply([{"value": 10}, {"value": 20}, {"value": 30}])
        assert result[0]["mean"] == 20.0

    def test_global_min_max(self):
        t = AggregateTransform({"group_by": [], "operations": {"lo": "min:v", "hi": "max:v"}})
        result = t.apply([{"v": 5}, {"v": 1}, {"v": 9}])
        assert result[0]["lo"] == 1
        assert result[0]["hi"] == 9

    def test_global_count(self):
        t = AggregateTransform({"group_by": [], "operations": {"n": "count:value"}})
        result = t.apply([{"value": 1}, {"value": None}, {"value": 3}])
        assert result[0]["n"] == 2  # None excluded

    def test_groupby(self):
        t = AggregateTransform({
            "group_by": ["ne"],
            "operations": {"total": "sum:bytes"},
        })
        result = t.apply([
            {"ne": "A", "bytes": 100},
            {"ne": "B", "bytes": 200},
            {"ne": "A", "bytes": 50},
        ])
        by_ne = {r["ne"]: r["total"] for r in result}
        assert by_ne["A"] == 150
        assert by_ne["B"] == 200

    def test_first_last(self):
        t = AggregateTransform({
            "group_by": [],
            "operations": {"first_val": "first:v", "last_val": "last:v"},
        })
        result = t.apply([{"v": 1}, {"v": 2}, {"v": 3}])
        assert result[0]["first_val"] == 1
        assert result[0]["last_val"] == 3

    def test_empty_records(self):
        t = AggregateTransform({"group_by": [], "operations": {"n": "count:x"}})
        assert t.apply([]) == []

    def test_missing_operations_raises(self):
        with pytest.raises(TransformError):
            AggregateTransform({"group_by": []})

    def test_unsupported_op_raises(self):
        with pytest.raises(TransformError):
            AggregateTransform({"group_by": [], "operations": {"x": "median:y"}})

    def test_dict_spec(self):
        t = AggregateTransform({
            "group_by": [],
            "operations": {"total": {"op": "sum", "field": "bytes"}},
        })
        result = t.apply([{"bytes": 100}, {"bytes": 200}])
        assert result[0]["total"] == 300


# ── EnrichTransform ────────────────────────────────────────────────────────


class TestEnrichTransform:
    def _write_csv(self, tmp_path: Path, rows: list[dict]) -> str:
        p = tmp_path / "lookup.csv"
        with open(p, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return str(p)

    def _write_json(self, tmp_path: Path, rows: list[dict]) -> str:
        p = tmp_path / "lookup.json"
        p.write_text(json.dumps(rows))
        return str(p)

    def test_csv_enrichment(self, tmp_path):
        lookup_path = self._write_csv(tmp_path, [
            {"ne_id": "NE001", "site": "London", "region": "UK"},
            {"ne_id": "NE002", "site": "Paris", "region": "FR"},
        ])
        t = EnrichTransform({
            "lookup_file": lookup_path,
            "lookup_format": "csv",
            "join_key": "ne_id",
        })
        result = t.apply([{"ne_id": "NE001", "rx": 100}])
        assert result[0]["site"] == "London"
        assert result[0]["region"] == "UK"
        assert result[0]["rx"] == 100

    def test_json_enrichment(self, tmp_path):
        lookup_path = self._write_json(tmp_path, [
            {"id": "A", "label": "Alpha"},
            {"id": "B", "label": "Beta"},
        ])
        t = EnrichTransform({
            "lookup_file": lookup_path,
            "lookup_format": "json",
            "join_key": "id",
        })
        result = t.apply([{"id": "A"}, {"id": "B"}, {"id": "C"}])
        assert result[0]["label"] == "Alpha"
        assert result[1]["label"] == "Beta"
        assert "label" not in result[2]  # miss → keep original

    def test_add_fields_filter(self, tmp_path):
        lookup_path = self._write_csv(tmp_path, [
            {"ne_id": "NE001", "site": "London", "region": "UK", "internal": "secret"},
        ])
        t = EnrichTransform({
            "lookup_file": lookup_path,
            "join_key": "ne_id",
            "add_fields": ["site"],
        })
        result = t.apply([{"ne_id": "NE001"}])
        assert result[0]["site"] == "London"
        assert "region" not in result[0]
        assert "internal" not in result[0]

    def test_prefix(self, tmp_path):
        lookup_path = self._write_csv(tmp_path, [{"ne_id": "NE001", "city": "London"}])
        t = EnrichTransform({
            "lookup_file": lookup_path,
            "join_key": "ne_id",
            "prefix": "geo_",
        })
        result = t.apply([{"ne_id": "NE001"}])
        assert "geo_city" in result[0]

    def test_on_miss_null_fields(self, tmp_path):
        lookup_path = self._write_csv(tmp_path, [{"id": "A", "label": "Alpha"}])
        t = EnrichTransform({
            "lookup_file": lookup_path,
            "join_key": "id",
            "add_fields": ["label"],
            "on_miss": "null_fields",
        })
        result = t.apply([{"id": "MISSING"}])
        assert result[0]["label"] is None

    def test_missing_lookup_file_raises(self):
        with pytest.raises(TransformError):
            EnrichTransform({
                "lookup_file": "/nonexistent/path/lookup.csv",
                "join_key": "id",
            })
