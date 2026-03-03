"""Deduplicate transform — removes duplicate records based on key fields."""

from __future__ import annotations

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("deduplicate")
class DeduplicateTransform(BaseTransform):
    """Remove duplicate records, keeping either the first or last occurrence."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        fields = config.get("fields")
        if not fields:
            raise TransformError("deduplicate: 'fields' config key is required and must not be empty")
        self.fields: list[str] = fields
        self.keep: str = config.get("keep", "first")
        if self.keep not in ("first", "last"):
            raise TransformError(
                f"deduplicate: 'keep' must be 'first' or 'last', got '{self.keep}'"
            )

    def apply(self, records: list[dict]) -> list[dict]:
        seen: dict[tuple, int] = {}
        for i, record in enumerate(records):
            key = tuple(record.get(f) for f in self.fields)
            if self.keep == "first" and key not in seen:
                seen[key] = i
            elif self.keep == "last":
                seen[key] = i
        kept = set(seen.values())
        return [r for i, r in enumerate(records) if i in kept]
