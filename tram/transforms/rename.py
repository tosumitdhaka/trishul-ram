"""Rename transform — renames fields in each record."""

from __future__ import annotations

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
        # Issue #80 cost center 3: a dotted path renames nested containers via
        # rename_path (set_path/delete_path), so those need a full deepcopy to
        # leave the caller's record untouched. All-top-level renames only touch
        # the record's own keys — a shallow ``dict(record)`` copy is provably
        # safe there (gated by the mutation tests in test_transform_mutation_safety).
        self._needs_deepcopy = any(
            "." in path for path in list(self.fields) + list(self.fields.values())
        )

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = deepcopy(record) if self._needs_deepcopy else dict(record)
            for old_key, new_key in self.fields.items():
                rename_path(new_record, old_key, new_key)
            result.append(new_record)
        return result
