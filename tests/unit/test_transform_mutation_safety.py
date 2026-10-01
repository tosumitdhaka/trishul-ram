"""Per-transform mutation-safety tests for the deepcopy elimination (issue #80
cost center 3).

Each test builds a record with NESTED containers (dicts and lists), runs the
transform, and asserts the ORIGINAL input record is unchanged — demonstrating
that the copy strategy the transform now uses (shallow ``dict(record)`` where
provably safe) is sufficient for that transform's mutation pattern.

Transforms with dotted-path configs (rename, cast, value_map, drop, unnest,
explode, json_flatten, counter_delta) keep ``copy.deepcopy`` for those configs
and get a second test exercising the dotted path — proving the deepcopy is
still in place exactly where a shallow copy would corrupt the caller's data.
"""

from __future__ import annotations

import copy

from tram.transforms.cast import CastTransform
from tram.transforms.coalesce_fields import CoalesceFieldsTransform
from tram.transforms.counter_delta import CounterDeltaTransform
from tram.transforms.drop import DropTransform
from tram.transforms.explode import ExplodeTransform
from tram.transforms.json_flatten import JsonFlattenTransform
from tram.transforms.rename import RenameTransform
from tram.transforms.select_from_list import SelectFromListTransform
from tram.transforms.unnest import UnnestTransform
from tram.transforms.value_map import ValueMapTransform


def _nested_record() -> dict:
    """A record whose values include nested dicts and lists — the mutation
    probes. Fresh containers per call so tests never share state."""
    return {
        "a": 1,
        "b": "x",
        "meta": {"deep": 1, "tags": ["t1", "t2"]},
        "list_field": [{"k": 1}, {"k": 2}],
    }


class _Snapshot:
    def __init__(self, record: dict):
        self.record = record
        self.before = copy.deepcopy(record)

    def assert_unchanged(self) -> None:
        assert self.record == self.before


# ── rename ─────────────────────────────────────────────────────────────────


class TestRenameMutationSafety:
    def test_top_level_shallow_path_leaves_input_untouched(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = RenameTransform({"fields": {"a": "renamed"}}).apply([rec])
        assert out == [{
            "renamed": 1, "b": "x",
            "meta": {"deep": 1, "tags": ["t1", "t2"]},
            "list_field": [{"k": 1}, {"k": 2}],
        }]
        snap.assert_unchanged()

    def test_dotted_path_keeps_deepcopy_isolation(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = RenameTransform({"fields": {"meta.deep": "hoisted"}}).apply([rec])
        assert out[0]["hoisted"] == 1
        assert rec["meta"]["deep"] == 1  # original's nested path untouched
        snap.assert_unchanged()


# ── cast ───────────────────────────────────────────────────────────────────


class TestCastMutationSafety:
    def test_top_level_shallow_path_leaves_input_untouched(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = CastTransform({"fields": {"a": "str"}}).apply([rec])
        assert out[0]["a"] == "1"
        assert out[0]["meta"] == {"deep": 1, "tags": ["t1", "t2"]}
        snap.assert_unchanged()

    def test_dotted_path_keeps_deepcopy_isolation(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = CastTransform({"fields": {"meta.deep": "str"}}).apply([rec])
        assert out[0]["meta"]["deep"] == "1"
        assert rec["meta"]["deep"] == 1
        snap.assert_unchanged()


# ── value_map ──────────────────────────────────────────────────────────────


class TestValueMapMutationSafety:
    def test_top_level_shallow_path_leaves_input_untouched(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = ValueMapTransform({
            "field": "a", "mapping": {"1": "ONE"},
        }).apply([rec])
        assert out[0]["a"] == "ONE"
        snap.assert_unchanged()

    def test_dotted_path_keeps_deepcopy_isolation(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = ValueMapTransform({
            "field": "meta.deep", "mapping": {"1": "ONE"},
        }).apply([rec])
        assert out[0]["meta"]["deep"] == "ONE"
        assert rec["meta"]["deep"] == 1
        snap.assert_unchanged()


# ── coalesce_fields (always shallow — top-level writes only) ───────────────


class TestCoalesceFieldsMutationSafety:
    def test_never_mutates_input(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = CoalesceFieldsTransform({
            "fields": {"out": {"sources": ["zz", "meta.deep"], "default": 0}},
        }).apply([rec])
        assert out[0]["out"] == 1
        snap.assert_unchanged()


# ── drop ───────────────────────────────────────────────────────────────────


class TestDropMutationSafety:
    def test_top_level_shallow_path_leaves_input_untouched(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = DropTransform({"fields": ["a"]}).apply([rec])
        assert out[0].get("a") is None
        snap.assert_unchanged()

    def test_dotted_path_keeps_deepcopy_isolation(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = DropTransform({"fields": ["meta.deep"]}).apply([rec])
        assert out[0]["meta"] == {"tags": ["t1", "t2"]}
        assert rec["meta"]["deep"] == 1
        snap.assert_unchanged()

    def test_conditional_dotted_path_keeps_deepcopy_isolation(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = DropTransform({"fields": {"meta.tags": [["t1", "t2"]]}}).apply([rec])
        assert out[0]["meta"] == {"deep": 1}
        assert rec["meta"]["tags"] == ["t1", "t2"]
        snap.assert_unchanged()


# ── unnest ─────────────────────────────────────────────────────────────────


class TestUnnestMutationSafety:
    def test_top_level_shallow_path_leaves_input_untouched(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = UnnestTransform({"field": "meta"}).apply([rec])
        assert out[0]["deep"] == 1
        assert out[0]["tags"] == ["t1", "t2"]
        assert "meta" not in out[0]
        snap.assert_unchanged()

    def test_dotted_path_keeps_deepcopy_isolation(self):
        rec = {"meta": {"inner": {"x": 1, "y": 2}, "keep": 3}}
        snap = _Snapshot(rec)
        out = UnnestTransform({"field": "meta.inner"}).apply([rec])
        assert out[0] == {"meta": {"keep": 3}, "x": 1, "y": 2}
        assert rec == {"meta": {"inner": {"x": 1, "y": 2}, "keep": 3}}
        snap.assert_unchanged()


# ── explode ────────────────────────────────────────────────────────────────


class TestExplodeMutationSafety:
    def test_top_level_shallow_path_leaves_input_untouched(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = ExplodeTransform({"field": "list_field"}).apply([rec])
        assert len(out) == 2
        assert out[0]["k"] == 1
        assert out[1]["k"] == 2
        snap.assert_unchanged()

    def test_dotted_path_keeps_deepcopy_isolation(self):
        rec = {"id": 7, "meta": {"tags": ["t1", "t2"]}}
        snap = _Snapshot(rec)
        out = ExplodeTransform({"field": "meta.tags"}).apply([rec])
        assert len(out) == 2
        assert out[0] == {"id": 7, "meta": {"tags": "t1"}}
        assert out[1] == {"id": 7, "meta": {"tags": "t2"}}
        assert rec["meta"]["tags"] == ["t1", "t2"]
        snap.assert_unchanged()


# ── json_flatten ───────────────────────────────────────────────────────────


class TestJsonFlattenMutationSafety:
    def test_pure_flatten_never_mutates_input(self):
        """The pure-flatten path makes NO record copy at all — flattening is
        read-only and builds fresh output dicts."""
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = JsonFlattenTransform({}).apply([rec])
        assert out[0]["meta.deep"] == 1
        assert out[0]["meta.tags"] == ["t1", "t2"]
        assert out[0]["list_field"] == [{"k": 1}, {"k": 2}]
        snap.assert_unchanged()

    def test_explode_path_never_mutates_input(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = JsonFlattenTransform({"explode_paths": ["list_field"]}).apply([rec])
        assert len(out) == 2
        assert out[0]["k"] == 1
        assert out[1]["k"] == 2
        snap.assert_unchanged()

    def test_choice_unwrap_never_mutates_input(self):
        rec = {"pGWAddress": {"type": "ipv4", "value": {"host": "1.2.3.4"}}}
        snap = _Snapshot(rec)
        out = JsonFlattenTransform({
            "choice_unwrap": {"paths": ["pGWAddress"], "mode": "both"},
        }).apply([rec])
        assert out[0]["pGWAddress_type"] == "ipv4"
        assert out[0]["pGWAddress.host"] == "1.2.3.4"
        snap.assert_unchanged()


# ── select_from_list (always shallow — reads + top-level writes only) ──────


class TestSelectFromListMutationSafety:
    def test_never_mutates_input(self):
        rec = _nested_record()
        snap = _Snapshot(rec)
        out = SelectFromListTransform({
            "field": "list_field",
            "select": [{"match": {"k": 2}, "output": {"k": "chosen"}}],
        }).apply([rec])
        assert out[0]["chosen"] == 2
        assert rec["list_field"] == [{"k": 1}, {"k": 2}]
        snap.assert_unchanged()


# ── counter_delta ──────────────────────────────────────────────────────────


def _counter_delta_config(fields: list[str]) -> dict:
    return {
        "fields": fields,
        "key_fields": ["_index"],
        "timestamp_field": ["_polled_at", "timestamp"],
        "width": "auto",
        "output": "both",
        "keep_raw": True,
        "first_sample": "pass",
        "reset_threshold": 0.5,
        "on_error": "raise",
        "_pipeline": {"name": "cd-pipe", "source": {"type": "snmp_poll"}},
    }


class TestCounterDeltaMutationSafety:
    def test_top_level_shallow_path_leaves_input_untouched(self):
        rec = {
            "v": 100, "_index": "1", "_polled_at": "2026-09-16T09:00:00+00:00",
            "meta": {"deep": [1]},
        }
        snap = _Snapshot(rec)
        t = CounterDeltaTransform(_counter_delta_config(["v"]))
        out = t.apply([rec])
        assert out[0]["v_delta"] is None
        assert out[0]["meta"] == {"deep": [1]}
        snap.assert_unchanged()

        rec2 = {
            "v": 300, "_index": "1", "_polled_at": "2026-09-16T09:05:00+00:00",
            "meta": {"deep": [1]},
        }
        snap2 = _Snapshot(rec2)
        out = t.apply([rec2])
        assert out[0]["v_delta"] == 200
        assert out[0]["meta"] == {"deep": [1]}
        snap2.assert_unchanged()

    def test_dotted_path_keeps_deepcopy_isolation(self):
        rec = {
            "_metrics": {"ifInOctets": 100},
            "_index": "1", "_polled_at": "2026-09-16T09:00:00+00:00",
        }
        snap = _Snapshot(rec)
        t = CounterDeltaTransform(_counter_delta_config(["_metrics.ifInOctets"]))
        t.apply([rec])
        snap.assert_unchanged()

        rec2 = {
            "_metrics": {"ifInOctets": 300},
            "_index": "1", "_polled_at": "2026-09-16T09:05:00+00:00",
        }
        snap2 = _Snapshot(rec2)
        out = t.apply([rec2])
        assert out[0]["_metrics"]["ifInOctets_delta"] == 200
        assert rec2["_metrics"]["ifInOctets"] == 300
        snap2.assert_unchanged()