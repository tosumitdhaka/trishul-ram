"""Drop transform — removes fields from records."""

from copy import deepcopy

from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_utils import delete_path, get_path


@register_transform("drop")
class DropTransform(BaseTransform):
    """Remove specified fields from each record."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        fields = config.get("fields", [])
        self.fields: list[str] = fields if isinstance(fields, list) else []
        self.conditional_fields: dict[str, list] = fields if isinstance(fields, dict) else {}

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = deepcopy(record)
            for field in self.fields:
                delete_path(new_record, field)
            for field, drop_values in self.conditional_fields.items():
                found, value = get_path(new_record, field)
                if found and value in drop_values:
                    delete_path(new_record, field)
            result.append(new_record)
        return result
