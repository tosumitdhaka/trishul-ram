"""Unit tests for ExplodeTransform, DeduplicateTransform, RegexExtractTransform,
TemplateTransform, and MaskTransform."""

from __future__ import annotations

import hashlib

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.deduplicate import DeduplicateTransform
from tram.transforms.explode import ExplodeTransform
from tram.transforms.mask import MaskTransform
from tram.transforms.regex_extract import RegexExtractTransform
from tram.transforms.template import TemplateTransform


# ── ExplodeTransform ──────────────────────────────────────────────────────────


class TestExplodeTransform:
    def test_explode_scalars(self):
        t = ExplodeTransform({"field": "items"})
        records = [{"id": 1, "items": ["a", "b", "c"]}]
        result = t.apply(records)
        assert len(result) == 3
        assert result[0] == {"id": 1, "items": "a"}
        assert result[1] == {"id": 1, "items": "b"}
        assert result[2] == {"id": 1, "items": "c"}

    def test_explode_dicts(self):
        t = ExplodeTransform({"field": "tags"})
        records = [{"id": 1, "tags": [{"name": "foo", "val": 1}, {"name": "bar", "val": 2}]}]
        result = t.apply(records)
        assert len(result) == 2
        # source field dropped by default; dict keys merged into parent
        assert result[0] == {"id": 1, "name": "foo", "val": 1}
        assert result[1] == {"id": 1, "name": "bar", "val": 2}

    def test_explode_empty_list(self):
        t = ExplodeTransform({"field": "items"})
        records = [{"id": 1, "items": []}]
        result = t.apply(records)
        assert result == []

    def test_non_list_passthrough(self):
        t = ExplodeTransform({"field": "items"})
        records = [{"id": 1, "items": "not-a-list"}]
        result = t.apply(records)
        assert result == [{"id": 1, "items": "not-a-list"}]

    def test_include_index(self):
        t = ExplodeTransform({"field": "items", "include_index": True, "index_field": "idx"})
        records = [{"items": ["x", "y"]}]
        result = t.apply(records)
        assert result[0]["idx"] == 0
        assert result[1]["idx"] == 1

    def test_missing_field_required(self):
        with pytest.raises(TransformError):
            ExplodeTransform({})


# ── DeduplicateTransform ──────────────────────────────────────────────────────


class TestDeduplicateTransform:
    def test_dedup_keep_first(self):
        t = DeduplicateTransform({"fields": ["id"], "keep": "first"})
        records = [
            {"id": 1, "v": "first"},
            {"id": 2, "v": "only"},
            {"id": 1, "v": "second"},
        ]
        result = t.apply(records)
        assert len(result) == 2
        assert result[0] == {"id": 1, "v": "first"}
        assert result[1] == {"id": 2, "v": "only"}

    def test_dedup_keep_last(self):
        t = DeduplicateTransform({"fields": ["id"], "keep": "last"})
        records = [
            {"id": 1, "v": "first"},
            {"id": 2, "v": "only"},
            {"id": 1, "v": "second"},
        ]
        result = t.apply(records)
        assert len(result) == 2
        vals = {r["id"]: r["v"] for r in result}
        assert vals[1] == "second"
        assert vals[2] == "only"

    def test_no_duplicates(self):
        t = DeduplicateTransform({"fields": ["id"]})
        records = [{"id": 1}, {"id": 2}, {"id": 3}]
        result = t.apply(records)
        assert len(result) == 3

    def test_multi_field_key(self):
        t = DeduplicateTransform({"fields": ["host", "port"]})
        records = [
            {"host": "a", "port": 80, "v": 1},
            {"host": "a", "port": 443, "v": 2},
            {"host": "a", "port": 80, "v": 3},
        ]
        result = t.apply(records)
        assert len(result) == 2
        # default keep=first
        assert result[0]["v"] == 1
        assert result[1]["v"] == 2

    def test_empty_fields_raises(self):
        with pytest.raises(TransformError):
            DeduplicateTransform({"fields": []})

    def test_missing_fields_raises(self):
        with pytest.raises(TransformError):
            DeduplicateTransform({})


# ── RegexExtractTransform ─────────────────────────────────────────────────────


class TestRegexExtractTransform:
    def test_extract_groups_toplevel(self):
        t = RegexExtractTransform(
            {"field": "msg", "pattern": r"(?P<host>\w+):(?P<port>\d+)"}
        )
        result = t.apply([{"msg": "server:8080"}])
        assert result[0]["host"] == "server"
        assert result[0]["port"] == "8080"
        assert result[0]["msg"] == "server:8080"

    def test_extract_groups_destination(self):
        t = RegexExtractTransform(
            {"field": "msg", "pattern": r"(?P<host>\w+):(?P<port>\d+)", "destination": "parsed"}
        )
        result = t.apply([{"msg": "server:8080"}])
        assert result[0]["parsed"] == {"host": "server", "port": "8080"}

    def test_no_match_keep(self):
        t = RegexExtractTransform(
            {"field": "msg", "pattern": r"(?P<host>\w+):(?P<port>\d+)", "on_no_match": "keep"}
        )
        records = [{"msg": "no-colon-here"}]
        result = t.apply(records)
        assert result == records

    def test_no_match_drop(self):
        t = RegexExtractTransform(
            {"field": "msg", "pattern": r"(?P<host>\w+):(?P<port>\d+)", "on_no_match": "drop"}
        )
        result = t.apply([{"msg": "no-colon-here"}])
        assert result == []

    def test_no_match_null(self):
        t = RegexExtractTransform(
            {"field": "msg", "pattern": r"(?P<host>\w+):(?P<port>\d+)", "on_no_match": "null"}
        )
        result = t.apply([{"msg": "no-colon-here"}])
        assert len(result) == 1
        assert result[0]["host"] is None
        assert result[0]["port"] is None

    def test_invalid_pattern_raises(self):
        with pytest.raises(TransformError):
            RegexExtractTransform({"field": "msg", "pattern": r"(?P<bad"})


# ── TemplateTransform ─────────────────────────────────────────────────────────


class TestTemplateTransform:
    def test_simple_template(self):
        t = TemplateTransform({"fields": {"address": "{host}:{port}"}})
        result = t.apply([{"host": "localhost", "port": "9200"}])
        assert result[0]["address"] == "localhost:9200"

    def test_multiple_fields(self):
        t = TemplateTransform(
            {"fields": {"label": "{env}-{app}", "url": "http://{host}/{path}"}}
        )
        result = t.apply([{"env": "prod", "app": "api", "host": "example.com", "path": "v1"}])
        assert result[0]["label"] == "prod-api"
        assert result[0]["url"] == "http://example.com/v1"

    def test_missing_key_raises(self):
        t = TemplateTransform({"fields": {"out": "{missing_key}"}})
        with pytest.raises(TransformError):
            t.apply([{"host": "localhost"}])

    def test_empty_fields_raises(self):
        with pytest.raises(TransformError):
            TemplateTransform({"fields": {}})


# ── MaskTransform ─────────────────────────────────────────────────────────────


class TestMaskTransform:
    def test_redact_mode(self):
        t = MaskTransform({"fields": ["password"]})
        result = t.apply([{"password": "s3cr3t", "user": "alice"}])
        assert result[0]["password"] == "***"
        assert result[0]["user"] == "alice"

    def test_hash_mode(self):
        t = MaskTransform({"fields": ["token"], "mode": "hash"})
        value = "mysecret"
        expected = hashlib.sha256(value.encode()).hexdigest()
        result = t.apply([{"token": value}])
        assert result[0]["token"] == expected

    def test_partial_mode(self):
        t = MaskTransform(
            {"fields": ["card"], "mode": "partial", "visible_start": 2, "visible_end": 2}
        )
        result = t.apply([{"card": "1234567890"}])
        # first 2 + *** + last 2
        assert result[0]["card"] == "12***90"

    def test_partial_short_value(self):
        # value length <= visible_start + visible_end → fully masked
        t = MaskTransform(
            {"fields": ["pin"], "mode": "partial", "visible_start": 2, "visible_end": 2}
        )
        result = t.apply([{"pin": "abc"}])  # len 3 <= 4
        assert result[0]["pin"] == "***"

    def test_custom_placeholder(self):
        t = MaskTransform({"fields": ["secret"], "mode": "redact", "placeholder": "REDACTED"})
        result = t.apply([{"secret": "value"}])
        assert result[0]["secret"] == "REDACTED"

    def test_missing_fields_raises(self):
        with pytest.raises(TransformError):
            MaskTransform({"fields": []})
