"""Drop transform — removes fields from records."""

from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("drop")
class DropTransform(BaseTransform):
    """Remove specified fields from each record."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: list[str] = config.get("fields", [])
        self._drop_set = set(self.fields)

    def apply(self, records: list[dict]) -> list[dict]:
        return [
            {k: v for k, v in record.items() if k not in self._drop_set}
            for record in records
        ]
