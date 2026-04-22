"""Unnest transform — hoists the keys of a nested dict field into the parent record."""

from __future__ import annotations

from copy import deepcopy

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_utils import delete_path, get_path


@register_transform("unnest")
class UnnestTransform(BaseTransform):
    """Hoist the contents of a dict-valued field up into the parent record.

    Config keys:
        field        (str, required)          Field containing the dict to hoist.
        prefix       (str, default "")        Prefix prepended to each hoisted key name.
        drop_source  (bool, default True)     Remove the source field after unnesting.
        on_non_dict  (str, default "keep")    Behaviour when the field value is not a dict:
                                              "keep"  — pass the record through unchanged,
                                              "drop"  — discard the record entirely,
                                              "raise" — raise TransformError.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        field = config.get("field")
        if not field:
            raise TransformError("unnest: 'field' is required")
        self.field: str = field
        self.prefix: str = config.get("prefix", "")
        self.drop_source: bool = bool(config.get("drop_source", True))
        self.on_non_dict: str = config.get("on_non_dict", "keep")
        if self.on_non_dict not in ("keep", "drop", "raise"):
            raise TransformError(
                f"unnest: 'on_non_dict' must be 'keep', 'drop', or 'raise', "
                f"got {self.on_non_dict!r}"
            )

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            found, val = get_path(record, self.field)

            if not found:
                result.append(record)
                continue

            if not isinstance(val, dict):
                if self.on_non_dict == "keep":
                    result.append(record)
                elif self.on_non_dict == "drop":
                    pass  # discard record
                else:  # "raise"
                    raise TransformError(
                        f"unnest: field '{self.field}' is not a dict "
                        f"(got {type(val).__name__!r})"
                    )
                continue

            new_record = deepcopy(record)
            if self.drop_source:
                delete_path(new_record, self.field)
            for k, v in val.items():
                new_record[self.prefix + k] = v
            result.append(new_record)

        return result
