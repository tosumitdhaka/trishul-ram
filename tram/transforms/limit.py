"""Limit transform — keeps only the first N records from the batch."""

from __future__ import annotations

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("limit")
class LimitTransform(BaseTransform):
    """Return at most `count` records from the input batch.

    Config keys:
        count  (int, required)  Maximum number of records to keep. Must be > 0.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        count = config.get("count")
        if count is None:
            raise TransformError("limit: 'count' is required")
        try:
            count = int(count)
        except (TypeError, ValueError) as exc:
            raise TransformError(f"limit: 'count' must be an integer, got {count!r}") from exc
        if count <= 0:
            raise TransformError(f"limit: 'count' must be > 0, got {count}")
        self.count: int = count

    def apply(self, records: list[dict]) -> list[dict]:
        return records[: self.count]
