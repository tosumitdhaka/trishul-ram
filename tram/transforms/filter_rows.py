"""Filter transform — removes rows that don't match a condition."""

from __future__ import annotations

import logging

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

logger = logging.getLogger(__name__)


def _make_evaluator():
    try:
        from simpleeval import EvalWithCompoundTypes, DEFAULT_FUNCTIONS
        funcs = dict(DEFAULT_FUNCTIONS)
        funcs.update({
            "round": round, "abs": abs, "int": int, "float": float,
            "str": str, "len": len, "min": min, "max": max,
        })
        return EvalWithCompoundTypes, funcs
    except ImportError as exc:
        raise TransformError("simpleeval is required for filter transform") from exc


_EvalCls, _EVAL_FUNCS = _make_evaluator()


@register_transform("filter")
class FilterRowsTransform(BaseTransform):
    """Keep only rows where condition evaluates to truthy."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.condition: str = config["condition"]

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            try:
                evaluator = _EvalCls(names=record, functions=_EVAL_FUNCS)
                if evaluator.eval(self.condition):
                    result.append(record)
            except Exception as exc:
                raise TransformError(
                    f"Filter condition error: {self.condition!r} — {exc}"
                ) from exc
        return result
