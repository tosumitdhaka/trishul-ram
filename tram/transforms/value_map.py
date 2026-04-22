"""ValueMap transform — maps field values via lookup table."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_utils import get_path, set_path

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
            new_record = deepcopy(record)
            found, value = get_path(new_record, self.field)
            if found:
                key = str(value)
                if key in self.mapping:
                    set_path(new_record, self.field, self.mapping[key], create_missing=True)
                elif self.default is not _SENTINEL:
                    set_path(new_record, self.field, self.default, create_missing=True)
                # else: keep original value
            result.append(new_record)
        return result
