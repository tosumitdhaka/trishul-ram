"""json_flatten transform — explicit ordered row shaping for nested records."""

from __future__ import annotations

from copy import deepcopy

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_patterns import has_path_pattern, path_matches_pattern
from tram.transforms.path_utils import delete_path, get_path, set_path


def _is_choice_dict(value: object) -> bool:
    return isinstance(value, dict) and set(value.keys()) == {"type", "value"}


def _prune_empty_parents(record: dict, path: str) -> None:
    if "." not in path:
        return

    tokens = path.split(".")
    stack: list[tuple[dict, str]] = []
    current = record
    for token in tokens[:-1]:
        next_value = current.get(token)
        if not isinstance(next_value, dict):
            return
        stack.append((current, token))
        current = next_value

    for parent, token in reversed(stack):
        child = parent.get(token)
        if isinstance(child, dict) and not child:
            del parent[token]
        else:
            break


def _delete_path_pruned(record: dict, path: str) -> bool:
    deleted = delete_path(record, path)
    if deleted:
        _prune_empty_parents(record, path)
    return deleted


@register_transform("json_flatten")
class JsonFlattenTransform(BaseTransform):
    """Explicit row-shaping transform for nested dict/list payloads."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.explode_paths: list[str] = list(config.get("explode_paths", []))
        self.separator: str = config.get("separator", ".")
        self.keep_empty_rows: bool = config.get("keep_empty_rows", True)
        self.preserve_lists: bool = config.get("preserve_lists", True)
        self.max_depth: int = int(config.get("max_depth", 0))
        self.zip_groups: list[dict] = list(config.get("zip_groups", []))
        self.choice_unwrap: dict | None = config.get("choice_unwrap")
        self.drop_paths: list[str] = list(config.get("drop_paths", []))
        self._drop_set: set[str] = {path for path in self.drop_paths if not has_path_pattern(path)}
        self._drop_patterns: list[str] = [path for path in self.drop_paths if has_path_pattern(path)]

        if not self.separator:
            raise TransformError("json_flatten: separator must not be empty")
        if self.max_depth < 0:
            raise TransformError("json_flatten: max_depth must be >= 0")

        if self.choice_unwrap is not None:
            mode = self.choice_unwrap.get("mode", "value")
            if mode not in {"keep", "value", "both"}:
                raise TransformError("json_flatten: choice_unwrap.mode must be keep, value, or both")

        # Issue #80 cost center 3: rows are only deep-copied when a stage can
        # mutate nested containers in place (explode/zip delete paths,
        # choice_unwrap rewrites paths). The pure flatten path is read-only
        # (``_flatten_record``/``_drop_flattened_paths`` build new dicts), so
        # the input records can flow through untouched. The per-stage shallow
        # copies (see ``_apply_explodes``/``_apply_zip_groups``) are safe
        # because the base rows they derive from are private deepcopies.
        self._needs_private_rows = bool(
            self.explode_paths or self.zip_groups or self.choice_unwrap
        )
        # Dotted paths written via set_path during explode/zip need per-row
        # deepcopies (a shallow copy would alias the parent dict across rows).
        self._explode_needs_deepcopy = ["." in p for p in self.explode_paths]
        self._zip_output_dotted = {
            group_index
            for group_index, group in enumerate(self.zip_groups)
            if any("." in out for out in group.get("fields", {}).values())
        }

    def apply(self, records: list[dict]) -> list[dict]:
        if self._needs_private_rows:
            rows = [deepcopy(record) for record in records]
        else:
            rows = list(records)
        rows = self._apply_explodes(rows)
        rows = self._apply_zip_groups(rows)
        rows = self._apply_choice_unwrap(rows)
        rows = [self._flatten_record(row, "", 0) for row in rows]
        rows = [self._drop_flattened_paths(row) for row in rows]
        return rows

    def _apply_explodes(self, rows: list[dict]) -> list[dict]:
        for path_index, path in enumerate(self.explode_paths):
            next_rows: list[dict] = []
            for row in rows:
                found, value = get_path(row, path)
                if not found or value == []:
                    if self.keep_empty_rows:
                        next_rows.append(row)
                    continue
                if not isinstance(value, list):
                    raise TransformError(f"json_flatten: explode path '{path}' is not a list")

                # The base copy may carry the (large) exploded list; it is made
                # exactly once per row (the O(n²) per-element deepcopy pattern
                # was removed in GH #18). Per-element rows are shallow copies
                # of this private base unless the explode path is dotted.
                base_row = deepcopy(row)
                _delete_path_pruned(base_row, path)

                per_element_deepcopy = self._explode_needs_deepcopy[path_index]
                for element in value:
                    new_row = deepcopy(base_row) if per_element_deepcopy else dict(base_row)
                    if isinstance(element, dict):
                        new_row.update(element)
                    else:
                        set_path(new_row, path, element, create_missing=True)
                    next_rows.append(new_row)
            rows = next_rows
        return rows

    def _apply_zip_groups(self, rows: list[dict]) -> list[dict]:
        for group_index, group in enumerate(self.zip_groups):
            next_rows: list[dict] = []
            fields = group.get("fields", {})
            strict = group.get("strict", True)
            if not isinstance(fields, dict) or not fields:
                raise TransformError("json_flatten: zip_groups[].fields must be a non-empty mapping")

            for row in rows:
                found_values: dict[str, list] = {}
                any_found = False
                for source_field in fields:
                    found, value = get_path(row, source_field)
                    any_found = any_found or found
                    if not found:
                        found_values = {}
                        break
                    if not isinstance(value, list):
                        raise TransformError(
                            f"json_flatten: zip group field '{source_field}' is not a list"
                        )
                    found_values[source_field] = value

                if not any_found or not found_values:
                    next_rows.append(row)
                    continue

                lengths = {len(values) for values in found_values.values()}
                if len(lengths) != 1:
                    if strict:
                        raise TransformError(
                            f"json_flatten: zip group fields {list(fields)} have different lengths"
                        )
                    next_rows.append(row)
                    continue

                # Base copy per row per group (source paths are deleted from
                # it; keeping it a private deepcopy preserves the old
                # per-group-row isolation exactly, including for later zip
                # groups that re-read the same row).
                base_row = deepcopy(row)
                for source_field in fields:
                    _delete_path_pruned(base_row, source_field)

                per_idx_deepcopy = group_index in self._zip_output_dotted
                length = lengths.pop()
                for idx in range(length):
                    new_row = deepcopy(base_row) if per_idx_deepcopy else dict(base_row)
                    for source_field, output_field in fields.items():
                        set_path(
                            new_row,
                            output_field,
                            found_values[source_field][idx],
                            create_missing=True,
                        )
                    next_rows.append(new_row)
            rows = next_rows
        return rows

    def _apply_choice_unwrap(self, rows: list[dict]) -> list[dict]:
        if not self.choice_unwrap:
            return rows

        mode = self.choice_unwrap.get("mode", "value")
        type_suffix = self.choice_unwrap.get("type_suffix", "_type")
        value_suffix = self.choice_unwrap.get("value_suffix", "")

        for row in rows:
            for path in self.choice_unwrap.get("paths", []):
                found, value = get_path(row, path)
                if not found or not _is_choice_dict(value):
                    continue
                if mode == "keep":
                    continue
                if mode == "value":
                    set_path(row, path, deepcopy(value["value"]), create_missing=True)
                    continue

                _delete_path_pruned(row, path)
                set_path(row, f"{path}{type_suffix}", deepcopy(value["type"]), create_missing=True)
                value_path = f"{path}{value_suffix}" if value_suffix else path
                set_path(row, value_path, deepcopy(value["value"]), create_missing=True)
        return rows

    def _flatten_record(self, record: dict, prefix: str, depth: int) -> dict:
        result: dict = {}
        for key, value in record.items():
            output_key = f"{prefix}{self.separator}{key}" if prefix else key
            if (
                isinstance(value, dict)
                and value
                and (self.max_depth == 0 or depth < self.max_depth)
            ):
                result.update(self._flatten_record(value, output_key, depth + 1))
                continue
            if isinstance(value, list) and not self.preserve_lists:
                raise TransformError(
                    f"json_flatten: list value at '{output_key}' requires preserve_lists=true"
                )
            result[output_key] = value
        return result

    def _drop_flattened_paths(self, row: dict) -> dict:
        if not self.drop_paths:
            return row
        result: dict = {}
        for key, value in row.items():
            if key in self._drop_set:
                continue
            if any(path_matches_pattern(key, pattern) for pattern in self._drop_patterns):
                continue
            result[key] = value
        return result
