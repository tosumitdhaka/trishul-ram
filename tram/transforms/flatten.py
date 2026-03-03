"""Flatten transform — recursively flattens nested dicts."""

from __future__ import annotations

from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


def _flatten_dict(record: dict, separator: str, prefix: str, max_depth: int, depth: int) -> dict:
    result: dict = {}
    for key, val in record.items():
        new_key = f"{prefix}{separator}{key}" if prefix else key
        if (
            isinstance(val, dict)
            and val  # don't flatten empty dicts
            and (max_depth == 0 or depth < max_depth)
        ):
            result.update(_flatten_dict(val, separator, new_key, max_depth, depth + 1))
        else:
            result[new_key] = val
    return result


@register_transform("flatten")
class FlattenTransform(BaseTransform):
    """Recursively flatten nested dicts in each record.

    Example::

        input:  {"a": {"b": {"c": 1}}, "d": 2}
        output: {"a_b_c": 1, "d": 2}   (separator="_")

    Config keys:
        separator  (str, default "_")      Key separator between levels.
        max_depth  (int, default 0)        Maximum nesting depth to flatten.
                                           0 means unlimited.
        prefix     (str, default "")       Optional prefix prepended to all keys.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.separator: str = config.get("separator", "_")
        self.max_depth: int = int(config.get("max_depth", 0))
        self.prefix: str = config.get("prefix", "")

    def apply(self, records: list[dict]) -> list[dict]:
        return [
            _flatten_dict(record, self.separator, self.prefix, self.max_depth, 0)
            for record in records
        ]
