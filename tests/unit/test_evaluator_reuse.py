"""Evaluator-reuse tests for add_field / filter (issue #80 cost center 2).

Pins the compile-once contract: expressions are parsed once at transform
init and re-evaluated with per-record names at apply time. Specifically:

* per-record name binding with NO cross-record state leakage (a field absent
  from a later record must fail, not inherit the previous record's value);
* chained fields still see earlier fields computed in the same record;
* the ``record`` and ``pipeline`` names keep their exact bindings;
* invalid expressions still surface as ``TransformError`` from apply() (not
  init) — the pre-refactor behavior;
* thread safety: the executor shares one transform across ``thread_workers``
  threads, so each thread owns its own evaluator instance (thread-local) and
  binds per-record names onto it; parsed expression trees are immutable and
  shared read-only;
* the L015 linter contract: ``_EvalCls`` / ``_EVAL_FUNCS`` stay importable
  from both modules and behave identically (the linter builds its own
  evaluators from them for runtime-replication).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.add_field import AddFieldTransform
from tram.transforms.filter_rows import FilterRowsTransform

# ── Per-record binding / no leakage ────────────────────────────────────────


class TestPerRecordBinding:
    def test_add_field_no_state_leak_between_records(self):
        t = AddFieldTransform({"fields": {"double": "x * 2"}})
        assert t.apply([{"x": 5}])[0]["double"] == 10
        # The next record has no 'x': a stale evaluator/names dict would
        # resurrect x=5; the correct behavior is the unbound-name error.
        with pytest.raises(TransformError, match="Expression error for field 'double'"):
            t.apply([{"y": 1}])

    def test_add_field_rebinds_per_record(self):
        t = AddFieldTransform({"fields": {"double": "x * 2"}})
        assert t.apply([{"x": 1}])[0]["double"] == 2
        assert t.apply([{"x": 100}])[0]["double"] == 200
        assert t.apply([{"x": 3}])[0]["double"] == 6

    def test_filter_no_state_leak_between_records(self):
        t = FilterRowsTransform({"condition": "x > 0"})
        assert len(t.apply([{"x": 1}])) == 1
        # A record without 'x' must raise the unbound-name error — it must not
        # inherit the previous record's binding.
        with pytest.raises(TransformError, match="Filter condition error"):
            t.apply([{"y": 1}])

    def test_chained_fields_still_visible(self):
        """Field 2 may reference field 1 computed earlier in the same record."""
        t = AddFieldTransform({"fields": {
            "rx_mbps": "rx_bytes / 1_000_000",
            "load_pct": "round(rx_mbps / 1000 * 100, 2)",
        }})
        result = t.apply([{"rx_bytes": 1_500_000}])
        assert result[0]["rx_mbps"] == 1.5
        assert result[0]["load_pct"] == 0.15

    def test_record_container_still_bound(self):
        t = AddFieldTransform({"fields": {"v": "record['x'] + 1"}})
        assert t.apply([{"x": 41}])[0]["v"] == 42

    def test_pipeline_context_still_bound(self):
        t = AddFieldTransform({
            "fields": {"name": "pipeline.name", "src_type": "pipeline.source.type"},
            "_pipeline": {"name": "pp", "source": {"type": "snmp_poll"}},
        })
        assert t.apply([{}])[0] == {"name": "pp", "src_type": "snmp_poll"}


# ── Parse-once (compile-once) pin ──────────────────────────────────────────


class TestCompileOnce:
    def test_expressions_parsed_once_at_init(self, monkeypatch):
        from tram.transforms import add_field

        calls = {"n": 0}
        original_parse = add_field._EvalCls.parse

        def counting_parse(expr):
            calls["n"] += 1
            return original_parse(expr)

        monkeypatch.setattr(
            add_field._EvalCls, "parse", staticmethod(counting_parse)
        )
        t = AddFieldTransform({"fields": {"a": "x + 1", "b": "x * 2"}})
        assert calls["n"] == 2  # compiled at init
        t.apply([{"x": 1}])
        t.apply([{"x": 2}])
        t.apply([{"x": 3}])
        assert calls["n"] == 2  # no re-parse at eval time

    def test_invalid_expression_fails_at_apply_like_before(self):
        t = AddFieldTransform({"fields": {"bad": "import os"}})
        with pytest.raises(TransformError, match="Expression error for field 'bad'"):
            t.apply([{"x": 1}])

    def test_invalid_condition_fails_at_apply_like_before(self):
        t = FilterRowsTransform({"condition": "import os"})
        with pytest.raises(TransformError, match="Filter condition error"):
            t.apply([{"x": 1}])


# ── Thread safety (thread_workers > 1) ─────────────────────────────────────


class TestThreadSafety:
    def test_evaluator_instances_are_per_thread(self):
        """The design guarantee: each thread owns its own evaluator instance,
        so per-record ``names`` writes never race on a shared object."""
        from tram.transforms.add_field import _thread_evaluator as af_evaluator
        from tram.transforms.filter_rows import _thread_evaluator as f_evaluator

        main_add_field = id(af_evaluator())
        main_filter = id(f_evaluator())
        collected = {"add_field": [], "filter": []}
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            collected["add_field"].append(id(af_evaluator()))
            collected["filter"].append(id(f_evaluator()))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert main_add_field not in collected["add_field"]
        assert main_filter not in collected["filter"]
        assert len(set(collected["add_field"])) == 4
        assert len(set(collected["filter"])) == 4

    def test_concurrent_apply_keeps_per_record_names(self):
        """Shared transform instance, concurrent apply() calls: every thread's
        records must be evaluated against their own names."""
        t = AddFieldTransform({"fields": {"double": "x * 2", "tag": "marker"}})
        errors: list[Exception] = []
        barrier = threading.Barrier(6)

        def worker(base: int) -> None:
            barrier.wait()
            try:
                for _round in range(200):
                    records = [{"x": base + j, "marker": base} for j in range(5)]
                    out = t.apply(records)
                    for j, rec in enumerate(out):
                        if rec["double"] != 2 * (base + j):
                            raise AssertionError(
                                f"wrong double: {rec['double']} != {2 * (base + j)}"
                            )
                        if rec["tag"] != base:
                            raise AssertionError(
                                f"cross-thread name leak: tag={rec['tag']} != {base}"
                            )
                        if rec["marker"] != base:
                            raise AssertionError(
                                f"cross-thread marker leak: {rec['marker']} != {base}"
                            )
            except Exception as exc:  # noqa: BLE001 — collected for the assert
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(worker, 1000 * n) for n in range(1, 7)]
            for fut in futures:
                fut.result()
        assert errors == []

    def test_concurrent_filter_apply(self):
        t = FilterRowsTransform({"condition": "x > 0"})
        errors: list[Exception] = []
        barrier = threading.Barrier(6)

        def worker(base: int) -> None:
            barrier.wait()
            try:
                for _round in range(200):
                    # x spans base-5..base+4, all > 0 for these bases.
                    records = [{"x": base + j} for j in range(-5, 5)]
                    out = t.apply(records)
                    if len(out) != 10 or any(r["x"] <= 0 for r in out):
                        raise AssertionError("filter evaluated with wrong names")
            except Exception as exc:  # noqa: BLE001 — collected for the assert
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(worker, 1000 * n) for n in range(1, 7)]
            for fut in futures:
                fut.result()
        assert errors == []


# ── L015 linter contract (issue #85) ───────────────────────────────────────


class TestLinterContract:
    def test_module_level_names_importable_and_identical(self):
        """The Wave-1 L015 rule imports ``_EvalCls``/``_EVAL_FUNCS`` and builds
        its own evaluators for runtime-replication — that contract must be
        unchanged by the compile-once refactor."""
        from tram.transforms.add_field import (
            _EVAL_FUNCS as _ADD_FIELD_FUNCS,
        )
        from tram.transforms.add_field import (
            _EvalCls as _AddFieldEvalCls,
        )
        from tram.transforms.filter_rows import (
            _EVAL_FUNCS as _FILTER_FUNCS,
        )
        from tram.transforms.filter_rows import (
            _EvalCls as _FilterEvalCls,
        )

        # add_field style: names include the record container + pipeline ctx.
        evaluator = _AddFieldEvalCls(
            names={"x": 2, "record": {"x": 2}, "pipeline": object()},
            functions=_ADD_FIELD_FUNCS,
        )
        assert evaluator.eval("x * 3 + record['x']") == 8
        # filter style: names are the record fields only.
        fevaluator = _FilterEvalCls(names={"x": 2}, functions=_FILTER_FUNCS)
        assert fevaluator.eval("x > 1") is True