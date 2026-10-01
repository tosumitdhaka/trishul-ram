"""Filter transform — removes rows that don't match a condition."""

from __future__ import annotations

import logging
import threading

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

logger = logging.getLogger(__name__)


def _make_evaluator():
    try:
        from simpleeval import DEFAULT_FUNCTIONS, EvalWithCompoundTypes
        funcs = dict(DEFAULT_FUNCTIONS)
        funcs.update({
            "round": round, "abs": abs, "int": int, "float": float,
            "str": str, "len": len, "min": min, "max": max,
        })
        return EvalWithCompoundTypes, funcs
    except ImportError as exc:
        raise TransformError("simpleeval is required for filter transform") from exc


_EvalCls, _EVAL_FUNCS = _make_evaluator()

# One evaluator instance PER THREAD (issue #80 cost center 2): the executor
# shares one transform across ``thread_workers`` threads and simpleeval reads
# ``.names`` off the shared instance, so per-record name binding must never
# race. Each thread owns its instance; the parsed condition tree is immutable
# and shared read-only.
_thread_local = threading.local()


def _thread_evaluator():
    evaluator = getattr(_thread_local, "evaluator", None)
    if evaluator is None:
        evaluator = _EvalCls(names={}, functions=_EVAL_FUNCS)
        _thread_local.evaluator = evaluator
    return evaluator


@register_transform("filter")
class FilterRowsTransform(BaseTransform):
    """Keep only rows where condition evaluates to truthy."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.condition: str = config["condition"]
        # Compile the condition once at init. Parse errors are kept and raised
        # from apply() so a bad condition still surfaces as a TransformError at
        # record time, exactly as before.
        self._parsed = None
        self._parse_error: Exception | None = None
        try:
            self._parsed = _EvalCls.parse(self.condition)
        except Exception as exc:  # noqa: BLE001 — surfaced at apply time
            self._parse_error = exc

    def apply(self, records: list[dict]) -> list[dict]:
        if self._parse_error is not None:
            raise TransformError(
                f"Filter condition error: {self.condition!r} — {self._parse_error}"
            ) from self._parse_error
        evaluator = _thread_evaluator()
        result = []
        for record in records:
            evaluator.names = record
            try:
                if evaluator.eval(self.condition, previously_parsed=self._parsed):
                    result.append(record)
            except Exception as exc:
                raise TransformError(
                    f"Filter condition error: {self.condition!r} — {exc}"
                ) from exc
        return result
