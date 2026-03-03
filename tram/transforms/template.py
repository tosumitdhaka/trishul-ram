"""Template transform — renders new fields using Python format-string templates."""

from __future__ import annotations

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("template")
class TemplateTransform(BaseTransform):
    """Produce new fields by rendering format-string templates against each record."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        fields = config.get("fields")
        if not fields:
            raise TransformError("template: 'fields' config key is required and must not be empty")
        self.fields: dict[str, str] = fields

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = dict(record)
            for output_field, tmpl in self.fields.items():
                try:
                    new_record[output_field] = tmpl.format_map(record)
                except (KeyError, ValueError, TypeError) as exc:
                    raise TransformError(
                        f"template: cannot render '{tmpl}': {exc}"
                    ) from exc
            result.append(new_record)
        return result
