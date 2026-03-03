"""Aggregate transform — groupby + sum/avg/min/max/count across records."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

_SUPPORTED_OPS = {"sum", "avg", "min", "max", "count", "first", "last"}


@register_transform("aggregate")
class AggregateTransform(BaseTransform):
    """Group records by key field(s) and compute aggregate functions.

    The entire input batch is collapsed into one output record per group.

    Config keys:
        group_by    (list[str], required)   Fields to group by. Use [] for global aggregation.
        operations  (dict, required)        Mapping of output_field → {op: ..., field: ...}
                                            or shorthand output_field → "op:source_field"

    Operations: sum, avg, min, max, count, first, last

    Example::

        group_by: [ne_id, interval]
        operations:
          total_rx: "sum:rx_bytes"
          avg_rx:   "avg:rx_bytes"
          samples:  "count:rx_bytes"
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.group_by: list[str] = config.get("group_by", [])
        raw_ops = config.get("operations", {})
        if not raw_ops:
            raise TransformError("aggregate: 'operations' dict is required")
        self.operations: dict[str, tuple[str, str]] = self._parse_ops(raw_ops)

    def _parse_ops(self, raw: dict) -> dict[str, tuple[str, str]]:
        parsed = {}
        for out_field, spec in raw.items():
            if isinstance(spec, str) and ":" in spec:
                op, src_field = spec.split(":", 1)
            elif isinstance(spec, dict):
                op = spec["op"]
                src_field = spec.get("field", out_field)
            else:
                raise TransformError(
                    f"aggregate: invalid operation spec for '{out_field}': {spec!r}. "
                    f"Use 'op:field' string or {{op: ..., field: ...}} dict."
                )
            op = op.strip().lower()
            if op not in _SUPPORTED_OPS:
                raise TransformError(
                    f"aggregate: unsupported operation '{op}'. Supported: {sorted(_SUPPORTED_OPS)}"
                )
            parsed[out_field] = (op, src_field.strip())
        return parsed

    def _group_key(self, record: dict) -> tuple:
        return tuple(record.get(k) for k in self.group_by)

    def apply(self, records: list[dict]) -> list[dict]:
        if not records:
            return []

        # Build groups: key → {group_fields, accumulators}
        groups: dict[tuple, dict[str, list[Any]]] = {}
        group_meta: dict[tuple, dict] = {}

        for record in records:
            key = self._group_key(record)
            if key not in groups:
                groups[key] = defaultdict(list)
                group_meta[key] = {k: record.get(k) for k in self.group_by}
            for out_field, (op, src_field) in self.operations.items():
                val = record.get(src_field)
                groups[key][out_field].append(val)

        result = []
        for key, accumulators in groups.items():
            out_record = dict(group_meta[key])
            for out_field, (op, src_field) in self.operations.items():
                values = accumulators[out_field]
                numeric = [v for v in values if v is not None and isinstance(v, (int, float))]
                if op == "sum":
                    out_record[out_field] = sum(numeric) if numeric else None
                elif op == "avg":
                    out_record[out_field] = sum(numeric) / len(numeric) if numeric else None
                elif op == "min":
                    out_record[out_field] = min(numeric) if numeric else None
                elif op == "max":
                    out_record[out_field] = max(numeric) if numeric else None
                elif op == "count":
                    out_record[out_field] = len([v for v in values if v is not None])
                elif op == "first":
                    out_record[out_field] = values[0] if values else None
                elif op == "last":
                    out_record[out_field] = values[-1] if values else None
            result.append(out_record)

        return result
