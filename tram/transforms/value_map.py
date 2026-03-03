"""ValueMap transform — maps field values via lookup table."""

from __future__ import annotations

from typing import Any

from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

_SENTINEL = object()


@register_transform("value_map")
class ValueMapTransform(BaseTransform):
    """Replace field values using a lookup table."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.field: str = config["field"]
        self.mapping: dict[str, Any] = {str(k): v for k, v in config.get("mapping", {}).items()}
        self.default: Any = config.get("default", _SENTINEL)

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = dict(record)
            if self.field in new_record:
                key = str(new_record[self.field])
                if key in self.mapping:
                    new_record[self.field] = self.mapping[key]
                elif self.default is not _SENTINEL:
                    new_record[self.field] = self.default
                # else: keep original value
            result.append(new_record)
        return result
