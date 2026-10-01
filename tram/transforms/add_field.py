"""AddField transform — adds computed fields using safe expression evaluation."""

from __future__ import annotations

import logging
import threading
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

# One evaluator instance PER THREAD (issue #80 cost center 2). The executor
# shares one transform instance across ``thread_workers`` threads, and
# simpleeval reads per-eval state (``.names``) off the instance, so a single
# shared evaluator with per-record ``.names`` writes would race. Each thread
# instead owns its instance and binds per-record names onto it; the parsed
# expression trees are immutable and shared read-only across threads.
_thread_local = threading.local()


def _thread_evaluator():
    evaluator = getattr(_thread_local, "evaluator", None)
    if evaluator is None:
        evaluator = _EvalCls(names={}, functions=_EVAL_FUNCS)
        _thread_local.evaluator = evaluator
    return evaluator


@register_transform("add_field")
class AddFieldTransform(BaseTransform):
    """Add computed fields to records using safe expression evaluation."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: dict[str, str] = config.get("fields", {})
        self._pipeline_ctx = _DotDict(config.get("_pipeline", {}))
        # Compile each expression once at init (simpleeval's eval() re-parses
        # the AST on every call otherwise). Parse errors are kept and raised
        # from apply() so a bad expression still surfaces as a TransformError
        # at record time, exactly as before.
        self._parsed: dict[str, object] = {}
        self._parse_errors: dict[str, Exception] = {}
        for field_name, expression in self.fields.items():
            try:
                self._parsed[field_name] = _EvalCls.parse(expression)
            except Exception as exc:  # noqa: BLE001 — surfaced at apply time
                self._parsed[field_name] = None
                self._parse_errors[field_name] = exc

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        evaluator = _thread_evaluator()
        for record in records:
            new_record = dict(record)
            # One names-dict per record (not per field): chained fields are
            # kept visible by writing each computed value into the same names
            # dict, preserving the previous per-field merge semantics.
            names = {**new_record, "record": new_record, "pipeline": self._pipeline_ctx}
            evaluator.names = names
            for field_name, expression in self.fields.items():
                parse_error = self._parse_errors.get(field_name)
                if parse_error is not None:
                    raise TransformError(
                        f"Expression error for field '{field_name}': {expression!r} — "
                        f"{parse_error}"
                    ) from parse_error
                try:
                    new_record[field_name] = evaluator.eval(
                        expression, previously_parsed=self._parsed[field_name]
                    )
                except Exception as exc:
                    raise TransformError(
                        f"Expression error for field '{field_name}': {expression!r} — {exc}"
                    ) from exc
                # Keep chained fields visible to later fields. The reserved
                # container bindings ("record"/"pipeline") must keep WINNING
                # over a same-named computed field, exactly as the old
                # per-field names merge behaved.
                if field_name not in ("record", "pipeline"):
                    names[field_name] = new_record[field_name]
            result.append(new_record)
        return result
