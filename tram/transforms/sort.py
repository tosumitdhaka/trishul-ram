"""Sort transform — sorts records by one or more fields."""

from __future__ import annotations

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("sort")
class SortTransform(BaseTransform):
    """Sort records by a list of fields in priority order.

    Config keys:
        fields   (list[str], required)  Fields to sort by, highest priority first.
        reverse  (bool, default False)  Sort descending if True.

    None values always sort before non-None values regardless of direction.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        fields = config.get("fields")
        if not fields:
            raise TransformError("sort: 'fields' list is required and must not be empty")
        self.fields: list[str] = fields
        self.reverse: bool = bool(config.get("reverse", False))

    def apply(self, records: list[dict]) -> list[dict]:
        fields = self.fields

        def _key(record: dict) -> list:
            vals = []
            for f in fields:
                v = record.get(f)
                # (False, "") sorts before (True, any_value) so None always comes first
                if v is None:
                    vals.append((False, ""))
                else:
                    vals.append((True, v))
            return vals

        try:
            return sorted(records, key=_key, reverse=self.reverse)
        except TypeError as exc:
            raise TransformError(f"sort: incomparable values — {exc}") from exc
