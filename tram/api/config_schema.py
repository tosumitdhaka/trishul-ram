"""Shared backend-generated config schema metadata for UI and AI helpers."""

from __future__ import annotations

import re
import typing
from typing import get_args, get_origin

from pydantic_core import PydanticUndefined

# Fields that appear on every model and carry no connector-specific meaning
_SKIP_FIELDS = {"type", "condition", "transforms", "serializer_out"}

# Fields that are internal plumbing injected by the executor at runtime
_INTERNAL_PREFIX = "_"


def _type_name(annotation) -> str:
    """Return a compact human-readable type string for a Pydantic field annotation."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    union_type = getattr(typing, "UnionType", None)

    if origin is typing.Union or (
        union_type is not None and isinstance(annotation, union_type)
    ):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_name(non_none[0]) + " | None"
        return " | ".join(_type_name(a) for a in non_none)

    if origin is list:
        inner = _type_name(args[0]) if args else "any"
        return f"list[{inner}]"

    if origin is dict:
        k = _type_name(args[0]) if args else "str"
        v = _type_name(args[1]) if len(args) > 1 else "any"
        return f"dict[{k}, {v}]"

    if hasattr(annotation, "__name__"):
        name = annotation.__name__
        return {"str": "str", "int": "int", "float": "float", "bool": "bool"}.get(name, name)

    if origin is typing.Literal:
        return str(args[0]) if len(args) == 1 else "|".join(str(a) for a in args)

    return str(annotation)


def _model_to_schema_lines(model_cls) -> list[str]:
    lines = []
    try:
        fields = model_cls.model_fields
    except AttributeError:
        return lines

    for name, field_info in fields.items():
        if name in _SKIP_FIELDS or name.startswith(_INTERNAL_PREFIX):
            continue

        required = field_info.is_required()
        default = field_info.default
        annotation = field_info.annotation

        type_str = _type_name(annotation) if annotation is not None else "any"
        type_str = re.sub(r"SerializerConfig \| None", "SerializerConfig", type_str)

        if required:
            lines.append(f"  {name}: {type_str}  # required")
        else:
            if default is None:
                lines.append(f"  {name}: {type_str}  # optional")
            elif isinstance(default, bool):
                lines.append(f"  {name}: {type_str} = {str(default).lower()}")
            elif isinstance(default, (int, float)):
                lines.append(f"  {name}: {type_str} = {default}")
            elif isinstance(default, str) and default:
                lines.append(f"  {name}: {type_str} = \"{default}\"")
            elif isinstance(default, list) and not default:
                pass
            elif isinstance(default, str) and not default:
                pass
            else:
                lines.append(f"  {name}: {type_str}")

    return lines


def _unwrap_optional(annotation):
    origin = get_origin(annotation)
    args = get_args(annotation)
    import types as _types

    if origin is typing.Union or (
        isinstance(annotation, _types.UnionType)
    ):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and len(non_none) != len(args):
            return non_none[0], True
    return annotation, False


def _field_kind(annotation) -> tuple[str, list[str] | None]:
    inner, _optional = _unwrap_optional(annotation)
    origin = get_origin(inner)
    args = get_args(inner)

    if origin is typing.Literal:
        return "select", [str(a) for a in args]
    if inner is bool:
        return "boolean", None
    if inner is int:
        return "integer", None
    if inner is float:
        return "number", None
    if inner is str:
        return "text", None
    if origin is list:
        item = args[0] if args else str
        item_inner, _ = _unwrap_optional(item)
        if item_inner in (str, int, float, bool):
            return "list", None
        return "complex", None
    if origin is dict:
        return "map", None
    return "complex", None


def _serialize_default(value):
    if value is None or value is PydanticUndefined:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        return value
    return str(value)


def _model_to_field_descriptors(model_cls) -> list[dict]:
    fields = []
    try:
        model_fields = model_cls.model_fields
    except AttributeError:
        return fields

    for name, field_info in model_fields.items():
        if name in _SKIP_FIELDS or name.startswith(_INTERNAL_PREFIX):
            continue
        annotation = field_info.annotation
        kind, choices = _field_kind(annotation)
        fields.append(
            {
                "name": name,
                "type": _type_name(annotation) if annotation is not None else "any",
                "kind": kind,
                "choices": choices or [],
                "required": field_info.is_required(),
                "default": _serialize_default(field_info.default),
                "secret": any(token in name for token in ("password", "token", "secret")),
                "multiline": name in {"query", "body"} or name.endswith("_template"),
            }
        )
    return fields


SCHEMA_LINES: dict[str, dict[str, list[str]]] = {
    "source": {},
    "sink": {},
    "serializer": {},
    "transform": {},
}
SCHEMA_FIELDS: dict[str, dict[str, list[dict]]] = {
    "source": {},
    "sink": {},
    "serializer": {},
    "transform": {},
}


def _iter_union_models(union_type) -> list:
    """Unwrap Annotated[A | B | C, ...] into a model list."""
    import types as _types

    args = get_args(union_type)
    if not args:
        return []
    inner = args[0]
    if isinstance(inner, _types.UnionType) or get_origin(inner) is typing.Union:
        return list(get_args(inner))
    return [inner]


def _build_schema_cache() -> None:
    try:
        from tram.models.pipeline import (  # noqa: PLC0415
            SerializerConfig,
            SinkConfig,
            SourceConfig,
            TransformConfig,
        )
    except ImportError:
        return

    for category, union_type in (
        ("source", SourceConfig),
        ("sink", SinkConfig),
        ("serializer", SerializerConfig),
        ("transform", TransformConfig),
    ):
        for model_cls in _iter_union_models(union_type):
            try:
                ann = model_cls.model_fields["type"].annotation
                type_args = get_args(ann)
                type_name = type_args[0] if type_args else None
                if not type_name:
                    default = model_cls.model_fields["type"].default
                    type_name = default if isinstance(default, str) else None
                if type_name:
                    SCHEMA_LINES[category][type_name] = _model_to_schema_lines(model_cls)
                    SCHEMA_FIELDS[category][type_name] = _model_to_field_descriptors(model_cls)
            except Exception:
                pass


def build_config_schema_payload() -> dict:
    """Return backend-generated config schema metadata for UI-driven forms."""

    def wrap(category: str) -> dict[str, dict]:
        return {
            type_name: {"fields": fields}
            for type_name, fields in SCHEMA_FIELDS[category].items()
        }

    return {
        "sources": wrap("source"),
        "sinks": wrap("sink"),
        "serializers": wrap("serializer"),
        "transforms": wrap("transform"),
    }


_build_schema_cache()
