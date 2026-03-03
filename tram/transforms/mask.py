"""Mask transform — redacts, hashes, or partially obscures field values."""

from __future__ import annotations

import hashlib

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("mask")
class MaskTransform(BaseTransform):
    """Mask sensitive field values via redaction, hashing, or partial obscuring."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        fields = config.get("fields")
        if not fields:
            raise TransformError("mask: 'fields' config key is required and must not be empty")
        self.fields: list[str] = fields
        self.mode: str = config.get("mode", "redact")
        if self.mode not in ("redact", "hash", "partial"):
            raise TransformError(
                f"mask: 'mode' must be 'redact', 'hash', or 'partial', got '{self.mode}'"
            )
        self.placeholder: str = config.get("placeholder", "***")
        self.visible_start: int = config.get("visible_start", 2)
        self.visible_end: int = config.get("visible_end", 2)

    def _mask_value(self, val: str) -> str:
        if self.mode == "redact":
            return self.placeholder
        if self.mode == "hash":
            return hashlib.sha256(val.encode()).hexdigest()
        # partial
        s = len(val)
        vs = self.visible_start
        ve = self.visible_end
        if s <= vs + ve:
            return self.placeholder
        return val[:vs] + self.placeholder + val[s - ve:]

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = dict(record)
            for field in self.fields:
                if field not in new_record:
                    continue
                new_record[field] = self._mask_value(str(new_record[field]))
            result.append(new_record)
        return result
