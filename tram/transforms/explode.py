"""Explode transform — expands an array field into one record per element."""

from __future__ import annotations

from copy import deepcopy

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_utils import delete_path, get_path, set_path


@register_transform("explode")
class ExplodeTransform(BaseTransform):
    """Emit one record per element of an array field."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if not config.get("field"):
            raise TransformError("explode: 'field' config key is required")
        self.field: str = config["field"]
        self.include_index: bool = config.get("include_index", False)
        self.index_field: str = config.get("index_field", "index")
        self.drop_source: bool = config.get("drop_source", True)

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            found, value = get_path(record, self.field)
            if not found or not isinstance(value, list):
                result.append(record)
                continue
            elements = value
            for i, element in enumerate(elements):
                new_record = deepcopy(record)
                if self.drop_source:
                    delete_path(new_record, self.field)
                if isinstance(element, dict):
                    new_record.update(element)
                else:
                    set_path(new_record, self.field, element, create_missing=True)
                if self.include_index:
                    new_record[self.index_field] = i
                result.append(new_record)
        return result
