"""Shared dotted-path helpers for transform implementations."""

from __future__ import annotations

from tram.core.exceptions import TransformError


def get_path(record: dict, path: str) -> tuple[bool, object]:
    """Return (found, value) for a dotted dict path."""
    if "." not in path:
        return (path in record, record.get(path))

    current: object = record
    for token in path.split("."):
        if not isinstance(current, dict):
            return False, None
        if token not in current:
            return False, None
        current = current[token]
    return True, current


def set_path(record: dict, path: str, value: object, create_missing: bool = True) -> None:
    """Set a dotted dict path, optionally creating missing intermediate dicts."""
    if "." not in path:
        record[path] = value
        return

    tokens = path.split(".")
    current = record
    for token in tokens[:-1]:
        if token not in current:
            if not create_missing:
                raise TransformError(f"path '{path}' does not exist")
            current[token] = {}
        next_value = current[token]
        if not isinstance(next_value, dict):
            raise TransformError(
                f"path '{path}' traverses non-dict intermediate '{token}'"
            )
        current = next_value
    current[tokens[-1]] = value


def delete_path(record: dict, path: str) -> bool:
    """Delete a dotted dict path, returning False when it is not found."""
    if "." not in path:
        if path not in record:
            return False
        del record[path]
        return True

    tokens = path.split(".")
    current: object = record
    for token in tokens[:-1]:
        if not isinstance(current, dict):
            return False
        if token not in current:
            return False
        current = current[token]
    if not isinstance(current, dict) or tokens[-1] not in current:
        return False
    del current[tokens[-1]]
    return True


def rename_path(record: dict, old_path: str, new_path: str) -> bool:
    """Rename a dotted dict path, returning False when the source is missing."""
    found, value = get_path(record, old_path)
    if not found:
        return False
    if old_path == new_path:
        return True
    set_path(record, new_path, value, create_missing=True)
    delete_path(record, old_path)
    return True
