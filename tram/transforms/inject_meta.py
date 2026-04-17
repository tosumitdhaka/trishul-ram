"""Inject selected runtime/source metadata fields into each record."""

from __future__ import annotations

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("inject_meta")
class InjectMetaTransform(BaseTransform):
    """Copy selected chunk metadata fields into each record.

    Config keys:
        fields      (dict[str, str], optional)  Mapping of meta_key -> output field name.
        include_all (bool, default False)       Copy all metadata keys.
        prefix      (str, default "")           Prefix applied to injected field names.
        on_missing  ("skip"|"null", default "skip")
                                               Behaviour when a requested meta key is absent.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: dict[str, str] = dict(config.get("fields", {}))
        self.include_all: bool = bool(config.get("include_all", False))
        self.prefix: str = config.get("prefix", "")
        self.on_missing: str = config.get("on_missing", "skip")
        if self.on_missing not in ("skip", "null"):
            raise TransformError("inject_meta: 'on_missing' must be 'skip' or 'null'")
        self._meta: dict = {}

    def set_runtime_meta(self, meta: dict) -> None:
        """Set per-chunk metadata before applying the transform."""
        self._meta = dict(meta)

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = dict(record)

            if self.include_all:
                for key, value in self._meta.items():
                    new_record[f"{self.prefix}{key}"] = value

            for meta_key, out_field in self.fields.items():
                target = f"{self.prefix}{out_field}"
                if meta_key in self._meta:
                    new_record[target] = self._meta[meta_key]
                elif self.on_missing == "null":
                    new_record[target] = None

            result.append(new_record)
        return result
