"""Rename transform — renames fields in each record."""

from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("rename")
class RenameTransform(BaseTransform):
    """Rename fields in each record according to a mapping."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: dict[str, str] = config.get("fields", {})

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = {}
            for key, val in record.items():
                new_key = self.fields.get(key, key)
                new_record[new_key] = val
            result.append(new_record)
        return result
