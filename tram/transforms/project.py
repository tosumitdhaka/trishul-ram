"""Project transform — build final output rows from selected source paths."""

from __future__ import annotations

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_utils import get_path


@register_transform("project")
class ProjectTransform(BaseTransform):
    """Project records to a declared output schema."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: dict[str, str | dict] = dict(config.get("fields", {}))
        if not self.fields:
            raise TransformError("project: 'fields' config key is required and must not be empty")

    def _resolve_rule(self, record: dict, output_field: str, rule: str | dict):
        if isinstance(rule, str):
            found, value = get_path(record, rule)
            return value if found else None

        default = rule.get("default")
        required = bool(rule.get("required", False))

        if rule.get("source") is not None:
            found, value = get_path(record, rule["source"])
            if found:
                return value
        else:
            for source in rule.get("source_any", []):
                found, value = get_path(record, source)
                if found:
                    return value

        if "default" in rule:
            return default
        if required:
            raise TransformError(f"project: required field '{output_field}' could not be resolved")
        return None

    def apply(self, records: list[dict]) -> list[dict]:
        result: list[dict] = []
        for record in records:
            new_record: dict = {}
            for output_field, rule in self.fields.items():
                new_record[output_field] = self._resolve_rule(record, output_field, rule)
            result.append(new_record)
        return result
