"""AddField transform — adds computed fields using safe expression evaluation."""

from __future__ import annotations

import logging
from datetime import UTC

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

logger = logging.getLogger(__name__)


class _DotDict:
    """Wraps a dict so both dot-access and key-access work in simpleeval expressions."""
    def __init__(self, d: dict):
        for k, v in d.items():
            setattr(self, k, _DotDict(v) if isinstance(v, dict) else v)

    def __getitem__(self, key):
        return getattr(self, key)

    def __repr__(self):
        return repr(vars(self))


def _make_evaluator():
    """Return a configured simpleeval EvalWithCompoundTypes instance."""
    try:
        import math
        from datetime import datetime

        from simpleeval import DEFAULT_FUNCTIONS, EvalWithCompoundTypes

        def _now(fmt=None):
            dt = datetime.now(UTC)
            return dt.strftime(fmt) if fmt else dt.isoformat()

        def _epoch():
            return datetime.now(UTC).timestamp()

        def _epoch_ms():
            return int(datetime.now(UTC).timestamp() * 1000)

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
            "now": _now,
            "epoch": _epoch,
            "epoch_ms": _epoch_ms,
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
        self._pipeline_ctx = _DotDict(config.get("_pipeline", {}))

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = dict(record)
            for field_name, expression in self.fields.items():
                try:
                    evaluator = _EvalCls(
                        names={**new_record, "record": new_record, "pipeline": self._pipeline_ctx},
                        functions=_EVAL_FUNCS,
                    )
                    new_record[field_name] = evaluator.eval(expression)
                except Exception as exc:
                    raise TransformError(
                        f"Expression error for field '{field_name}': {expression!r} — {exc}"
                    ) from exc
            result.append(new_record)
        return result
