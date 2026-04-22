"""Select-from-list transform — project fields from matching list elements without exploding."""

from __future__ import annotations

from copy import deepcopy

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_utils import get_path


@register_transform("select_from_list")
class SelectFromListTransform(BaseTransform):
    """Select list elements by exact match or first-item rule and project fields upward."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.field: str = config["field"]
        self.select: list[dict] = list(config.get("select", []))
        self.on_no_match: str = config.get("on_no_match", "null_fields")

    def _matches(self, item: object, expected: dict) -> bool:
        if not isinstance(item, dict):
            return False
        return all(item.get(key) == value for key, value in expected.items())

    def _pick_item(self, items: list, selection: dict):
        if selection.get("first_item"):
            return items[0] if items else None

        expected = selection.get("match", {})
        for item in items:
            if self._matches(item, expected):
                return item
        return None

    def _project(self, item: object, source_path: str):
        found, value = get_path(item, source_path) if isinstance(item, dict) else (False, None)
        return value if found else None

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = deepcopy(record)
            found, value = get_path(new_record, self.field)
            items = value if found and isinstance(value, list) else None

            for selection in self.select:
                selected_item = self._pick_item(items or [], selection) if items is not None else None
                if selected_item is None:
                    if self.on_no_match == "raise":
                        label = selection.get("name") or selection.get("match") or "selection"
                        raise TransformError(
                            f"select_from_list: no match for {label!r} in field '{self.field}'"
                        )
                    for _, output_field in selection.get("output", {}).items():
                        new_record[output_field] = None
                    continue

                for source_field, output_field in selection.get("output", {}).items():
                    new_record[output_field] = self._project(selected_item, source_field)

            result.append(new_record)
        return result
