"""Rename transform — renames fields in each record."""

from copy import deepcopy

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_utils import rename_path


@register_transform("rename")
class RenameTransform(BaseTransform):
    """Rename fields in each record according to a mapping."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: dict[str, str] = config.get("fields", {})
        sources = list(self.fields.keys())
        for i, source in enumerate(sources):
            for other in sources[i + 1:]:
                if source.startswith(f"{other}.") or other.startswith(f"{source}."):
                    raise TransformError(
                        "rename: overlapping source paths are not supported: "
                        f"{source!r} and {other!r}"
                    )

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = deepcopy(record)
            for old_key, new_key in self.fields.items():
                rename_path(new_record, old_key, new_key)
            result.append(new_record)
        return result
