"""Cast transform — converts field values to target types."""

from __future__ import annotations

import logging
from datetime import datetime

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

logger = logging.getLogger(__name__)

_BOOL_TRUTHY = {"true", "1", "yes", "on"}


def _cast_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() in _BOOL_TRUTHY


def _cast_datetime(val) -> datetime:
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(str(val))
    except ValueError as exc:
        raise TransformError(f"Cannot parse datetime: {val!r}") from exc


_CASTERS = {
    "str": str,
    "int": int,
    "float": float,
    "bool": _cast_bool,
    "datetime": _cast_datetime,
}


@register_transform("cast")
class CastTransform(BaseTransform):
    """Cast field values to specified types."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: dict[str, str] = config.get("fields", {})
        for field_type in self.fields.values():
            if field_type not in _CASTERS:
                raise TransformError(
                    f"Unsupported cast type '{field_type}'. Supported: {list(_CASTERS)}"
                )

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = dict(record)
            for field, target_type in self.fields.items():
                if field not in new_record:
                    continue
                caster = _CASTERS[target_type]
                try:
                    new_record[field] = caster(new_record[field])
                except (TransformError, ValueError, TypeError) as exc:
                    raise TransformError(
                        f"Failed to cast field '{field}' to {target_type}: {exc}"
                    ) from exc
            result.append(new_record)
        return result
