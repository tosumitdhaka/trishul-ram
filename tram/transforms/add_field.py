"""AddField transform — adds computed fields using safe expression evaluation."""

from __future__ import annotations

import logging

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

logger = logging.getLogger(__name__)


def _make_evaluator():
    """Return a configured simpleeval EvalWithCompoundTypes instance."""
    try:
        from simpleeval import EvalWithCompoundTypes, DEFAULT_FUNCTIONS
        import math
        funcs = dict(DEFAULT_FUNCTIONS)
        funcs.update({
            "round": round,
            "abs": abs,
            "int": int,
            "float": float,
            "str": str,
            "len": len,
            "min": min,
            "max": max,
            "sum": sum,
            "bool": bool,
            "sqrt": math.sqrt,
            "log": math.log,
        })
        return EvalWithCompoundTypes, funcs
    except ImportError as exc:
        raise TransformError("simpleeval is required for add_field transform") from exc


_EvalCls, _EVAL_FUNCS = _make_evaluator()


@register_transform("add_field")
class AddFieldTransform(BaseTransform):
    """Add computed fields to records using safe expression evaluation."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: dict[str, str] = config.get("fields", {})

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = dict(record)
            for field_name, expression in self.fields.items():
                try:
                    evaluator = _EvalCls(
                        names={**new_record, "record": new_record},
                        functions=_EVAL_FUNCS,
                    )
                    new_record[field_name] = evaluator.eval(expression)
                except Exception as exc:
                    raise TransformError(
                        f"Expression error for field '{field_name}': {expression!r} — {exc}"
                    ) from exc
            result.append(new_record)
        return result
