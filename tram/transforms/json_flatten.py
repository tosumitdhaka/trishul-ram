"""json_flatten transform — recursively hoist dicts and expand nested record structures."""

from __future__ import annotations

import re
from collections.abc import Mapping

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

_SNAKE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_2 = re.compile(r"([a-z0-9])([A-Z])")


def _snake_case(name: str) -> str:
    return _SNAKE_2.sub(r"\1_\2", _SNAKE_1.sub(r"\1_\2", name)).replace("-", "_").lower()


def _is_choice_dict(value) -> bool:
    return isinstance(value, dict) and set(value.keys()) == {"type", "value"}


def _path_matches(path: str, patterns: list[str]) -> bool:
    return any(path == pattern or path.startswith(f"{pattern}.") for pattern in patterns)


def _path_allowed(path: str, keep_paths: list[str]) -> bool:
    if not keep_paths:
        return True
    return any(
        path == pattern
        or path.startswith(f"{pattern}.")
        or pattern.startswith(f"{path}.")
        for pattern in keep_paths
    )


def _merge_records(left: list[dict], right: list[dict]) -> list[dict]:
    result: list[dict] = []
    for left_rec in left:
        for right_rec in right:
            merged = dict(left_rec)
            merged.update(right_rec)
            result.append(merged)
    return result


@register_transform("json_flatten")
class JsonFlattenTransform(BaseTransform):
    """Collapse nested dict/list records into flat row-oriented records."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.explode_mode: str = config.get("explode_mode", "auto")
        self.explode_paths: list[str] = list(config.get("explode_paths", []))
        self.zip_lists: str = config.get("zip_lists", "auto")
        self.zip_mappings: list[dict] = list(config.get("zip_mappings", []))
        self.choice_mode: str = config.get("choice_mode", "type_value")
        self.rename_style: str = config.get("rename_style", "none")
        self.drop_paths: list[str] = list(config.get("drop_paths", []))
        self.keep_paths: list[str] = list(config.get("keep_paths", []))
        self.max_depth: int = int(config.get("max_depth", 0))
        self.ambiguity_mode: str = config.get("ambiguity_mode", "keep")

        if self.explode_mode not in {"auto", "off", "paths"}:
            raise TransformError("json_flatten: explode_mode must be auto, off, or paths")
        if self.zip_lists not in {"auto", "off", "mappings"}:
            raise TransformError("json_flatten: zip_lists must be auto, off, or mappings")
        if self.choice_mode not in {"keep", "unwrap_value", "type_value"}:
            raise TransformError(
                "json_flatten: choice_mode must be keep, unwrap_value, or type_value"
            )
        if self.rename_style not in {"none", "snake_case"}:
            raise TransformError("json_flatten: rename_style must be none or snake_case")
        if self.ambiguity_mode not in {"keep", "error"}:
            raise TransformError("json_flatten: ambiguity_mode must be keep or error")

    def apply(self, records: list[dict]) -> list[dict]:
        flattened: list[dict] = []
        for record in records:
            flattened.extend(self._expand_dict(record, "", 0))
        if self.rename_style == "snake_case":
            return [{_snake_case(key): value for key, value in record.items()} for record in flattened]
        return flattened

    def _handle_ambiguity(self, path: str, keys: list[str]):
        if self.ambiguity_mode == "error":
            raise TransformError(
                f"json_flatten: ambiguous auto-expansion plan at {path or '<root>'}: {keys}"
            )

    def _zip_mappings_for_level(self, path: str, values: Mapping[str, object]) -> list[dict]:
        mappings: list[dict] = []

        if self.zip_lists == "mappings":
            for mapping in self.zip_mappings:
                labels = mapping["labels"]
                pairs = mapping["values"]
                if "." in labels or "." in pairs:
                    continue
                if labels in values and pairs in values:
                    mappings.append(mapping)
            return mappings

        if self.zip_lists != "auto":
            return mappings

        scalar_lists = [
            key for key, value in values.items()
            if isinstance(value, list) and value and all(not isinstance(item, (dict, list)) for item in value)
        ]
        other_lists = [
            key for key, value in values.items()
            if isinstance(value, list) and value and key not in scalar_lists
        ]

        candidates: list[dict] = []
        for scalar_key in scalar_lists:
            for other_key in other_lists:
                if len(values[scalar_key]) == len(values[other_key]):
                    candidates.append({"labels": scalar_key, "values": other_key})

        if len(candidates) > 1:
            self._handle_ambiguity(path, [f"{c['labels']}~{c['values']}" for c in candidates])
            return []
        return candidates

    def _auto_explode_keys(self, path: str, values: Mapping[str, object], handled: set[str]) -> set[str]:
        if self.explode_mode == "off":
            return set()

        if self.explode_mode == "paths":
            return {
                key for key in values
                if key not in handled and _path_matches(f"{path}.{key}" if path else key, self.explode_paths)
            }

        keys = {
            key for key, value in values.items()
            if key not in handled and isinstance(value, list) and value and any(isinstance(item, dict) for item in value)
        }
        if len(keys) > 1:
            self._handle_ambiguity(path, sorted(keys))
            return set()
        return keys

    def _choice_value(self, value):
        if not _is_choice_dict(value):
            return value
        if self.choice_mode == "keep":
            return {
                "type": self._prepare_nested(value["type"]),
                "value": self._prepare_nested(value["value"]),
            }
        if self.choice_mode == "unwrap_value":
            return self._prepare_nested(value["value"])
        return {
            "type": self._prepare_nested(value["type"]),
            "value": self._prepare_nested(value["value"]),
        }

    def _prepare_nested(self, value):
        if _is_choice_dict(value):
            return self._choice_value(value)
        if isinstance(value, dict):
            return {key: self._prepare_nested(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._prepare_nested(item) for item in value]
        return value

    def _expand_value(self, key: str, value, path: str, depth: int) -> list[dict]:
        if _is_choice_dict(value) and self.choice_mode == "keep":
            return [{key: self._prepare_nested(value)}]
        value = self._choice_value(value)
        if isinstance(value, dict):
            if self.max_depth and depth >= self.max_depth:
                return [{key: self._prepare_nested(value)}]
            return self._expand_dict(value, path, depth + 1)
        if isinstance(value, list):
            return [{key: [self._prepare_nested(item) for item in value]}]
        return [{key: value}]

    def _expand_list_item(self, key: str, item, path: str, depth: int) -> list[dict]:
        if _is_choice_dict(item) and self.choice_mode == "keep":
            return [{key: self._prepare_nested(item)}]
        item = self._choice_value(item)
        if isinstance(item, dict):
            if self.max_depth and depth >= self.max_depth:
                return [{key: self._prepare_nested(item)}]
            return self._expand_dict(item, path, depth + 1)
        return [{key: item}]

    def _zip_pair_records(
        self,
        labels_key: str,
        values_key: str,
        labels_value: list,
        values_value: list,
        mapping: dict,
        path: str,
        depth: int,
    ) -> list[dict]:
        if len(labels_value) != len(values_value):
            raise TransformError(
                f"json_flatten: zip fields {labels_key!r} and {values_key!r} have different lengths"
            )

        records: list[dict] = []
        labels_out = mapping.get("labels_output") or labels_key
        values_out = mapping.get("values_output") or values_key
        labels_path = f"{path}.{labels_key}" if path else labels_key
        values_path = f"{path}.{values_key}" if path else values_key

        for label_item, value_item in zip(labels_value, values_value, strict=True):
            pair_records = self._expand_list_item(labels_out, label_item, labels_path, depth)
            pair_records = _merge_records(
                pair_records,
                self._expand_list_item(values_out, value_item, values_path, depth),
            )
            records.extend(pair_records)
        return records

    def _expand_dict(self, data: Mapping[str, object], path: str, depth: int) -> list[dict]:
        values = {
            key: value for key, value in data.items()
            if _path_allowed(f"{path}.{key}" if path else key, self.keep_paths)
            and not _path_matches(f"{path}.{key}" if path else key, self.drop_paths)
        }

        records: list[dict] = [{}]
        handled: set[str] = set()

        for mapping in self._zip_mappings_for_level(path, values):
            labels_key = mapping["labels"]
            values_key = mapping["values"]
            if labels_key not in values or values_key not in values:
                continue
            records = _merge_records(
                records,
                self._zip_pair_records(
                    labels_key,
                    values_key,
                    values[labels_key],
                    values[values_key],
                    mapping,
                    path,
                    depth,
                ),
            )
            handled.update({labels_key, values_key})

        explode_keys = self._auto_explode_keys(path, values, handled)

        for key, value in values.items():
            if key in handled:
                continue

            child_path = f"{path}.{key}" if path else key
            if key in explode_keys and isinstance(value, list):
                item_records: list[dict] = []
                for item in value:
                    item_records.extend(self._expand_list_item(key, item, child_path, depth))
                records = _merge_records(records, item_records or [{key: []}])
                continue

            records = _merge_records(records, self._expand_value(key, value, child_path, depth))

        return records
