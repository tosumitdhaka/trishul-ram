"""Coalesce fields transform — pick the first non-empty value from candidate paths."""

from __future__ import annotations

from copy import deepcopy

from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_utils import get_path


@register_transform("coalesce_fields")
class CoalesceFieldsTransform(BaseTransform):
    """Write output fields from the first non-empty source path."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: dict[str, dict] = config.get("fields", {})

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = deepcopy(record)
            for output_field, rule in self.fields.items():
                chosen = rule.get("default")
                empty_values = list(rule.get("empty_values", [None, ""]))
                for source in rule.get("sources", []):
                    found, value = get_path(new_record, source)
                    if found and value not in empty_values:
                        chosen = value
                        break
                new_record[output_field] = chosen
            result.append(new_record)
        return result
