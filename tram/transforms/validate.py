"""Validate transform — filters or rejects records that fail field-level rules."""

from __future__ import annotations

import re

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

_TYPE_CHECKS: dict[str, tuple] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
}


@register_transform("validate")
class ValidateTransform(BaseTransform):
    """Validate records against per-field rules; drop or raise on invalid records.

    Config keys:
        rules       (dict[str, dict], required)  Mapping of field_name → rule spec.
        on_invalid  (str, default "drop")        "drop" | "raise"

    Rule spec keys (all optional):
        required  (bool)    Field must be present and not None.
        type      (str)     "str" | "int" | "float" | "bool"
        min       (number)  Numeric value must be >= min.
        max       (number)  Numeric value must be <= max.
        regex     (str)     String value must match this pattern (re.search).
        allowed   (list)    Value must be in this list.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        rules = config.get("rules")
        if not rules:
            raise TransformError("validate: 'rules' dict is required")
        self.rules: dict[str, dict] = rules
        self.on_invalid: str = config.get("on_invalid", "drop")
        if self.on_invalid not in ("drop", "raise"):
            raise TransformError(
                f"validate: 'on_invalid' must be 'drop' or 'raise', got {self.on_invalid!r}"
            )
        # Pre-compile regex patterns
        self._patterns: dict[str, re.Pattern] = {}
        for field, rule in self.rules.items():
            pattern = rule.get("regex")
            if pattern is not None:
                try:
                    self._patterns[field] = re.compile(pattern)
                except re.error as exc:
                    raise TransformError(
                        f"validate: invalid regex for field '{field}': {exc}"
                    ) from exc

    def _check_record(self, record: dict) -> str | None:
        """Return an error message if the record fails any rule, else None."""
        for field, rule in self.rules.items():
            val = record.get(field)

            # required
            if rule.get("required"):
                if val is None:
                    return f"field '{field}' is required but missing or None"

            # skip remaining checks if value is absent
            if val is None:
                continue

            # type
            type_name = rule.get("type")
            if type_name is not None:
                expected = _TYPE_CHECKS.get(type_name)
                if expected is None:
                    raise TransformError(
                        f"validate: unknown type '{type_name}' for field '{field}'. "
                        f"Supported: {list(_TYPE_CHECKS)}"
                    )
                if not isinstance(val, expected):
                    return (
                        f"field '{field}' expected type {type_name}, "
                        f"got {type(val).__name__}"
                    )

            # min
            min_val = rule.get("min")
            if min_val is not None:
                try:
                    if val < min_val:
                        return f"field '{field}' value {val!r} is less than min {min_val}"
                except TypeError:
                    return f"field '{field}' value {val!r} is not comparable to min {min_val}"

            # max
            max_val = rule.get("max")
            if max_val is not None:
                try:
                    if val > max_val:
                        return f"field '{field}' value {val!r} is greater than max {max_val}"
                except TypeError:
                    return f"field '{field}' value {val!r} is not comparable to max {max_val}"

            # regex
            if field in self._patterns:
                if not isinstance(val, str):
                    return (
                        f"field '{field}' regex check requires a string value, "
                        f"got {type(val).__name__}"
                    )
                if not self._patterns[field].search(val):
                    return (
                        f"field '{field}' value {val!r} does not match "
                        f"regex {self.rules[field]['regex']!r}"
                    )

            # allowed
            allowed = rule.get("allowed")
            if allowed is not None:
                if val not in allowed:
                    return f"field '{field}' value {val!r} not in allowed list {allowed}"

        return None

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            error = self._check_record(record)
            if error is None:
                result.append(record)
            elif self.on_invalid == "raise":
                raise TransformError(f"validate: record failed validation — {error}")
            # on_invalid == "drop": simply skip
        return result
