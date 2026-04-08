"""AI context builder — generates compact connector/transform schema reference from Pydantic models.

Introspects the SourceConfig / SinkConfig / SerializerConfig / TransformConfig union types
at import time and produces per-type field summaries used in AI system prompts.
Always in sync with the code — impossible to drift from the real models.
"""

from __future__ import annotations

import re
import typing
from typing import get_args, get_origin

# ── Field introspection ───────────────────────────────────────────────────────

# Fields that appear on every model and carry no connector-specific meaning
_SKIP_FIELDS = {"type", "condition", "transforms", "serializer_out"}

# Fields that are internal plumbing injected by the executor at runtime
_INTERNAL_PREFIX = "_"


def _type_name(annotation) -> str:
    """Return a compact human-readable type string for a Pydantic field annotation."""
    origin = get_origin(annotation)
    args   = get_args(annotation)

    if origin is typing.Union or (hasattr(typing, "UnionType") and isinstance(annotation, typing.UnionType)):
        # Filter out NoneType for Optional fields
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
        # Shorten verbose names
        return {"str": "str", "int": "int", "float": "float", "bool": "bool"}.get(name, name)

    # Literal["value"] → just return the value
    if origin is typing.Literal:
        return str(args[0]) if len(args) == 1 else "|".join(str(a) for a in args)

    return str(annotation)


def _model_to_schema_lines(model_cls) -> list[str]:
    """Return a list of field description lines for a Pydantic model class."""
    lines = []
    try:
        fields = model_cls.model_fields
    except AttributeError:
        return lines

    for name, field_info in fields.items():
        if name in _SKIP_FIELDS or name.startswith(_INTERNAL_PREFIX):
            continue

        required  = field_info.is_required()
        default   = field_info.default
        annotation = field_info.annotation

        type_str = _type_name(annotation) if annotation is not None else "any"
        # Shorten long nested types
        type_str = re.sub(r"SerializerConfig \| None", "SerializerConfig", type_str)

        if required:
            lines.append(f"  {name}: {type_str}  # required")
        else:
            # Show default only when it's informative
            if default is None:
                lines.append(f"  {name}: {type_str}  # optional")
            elif isinstance(default, bool):
                lines.append(f"  {name}: {type_str} = {str(default).lower()}")
            elif isinstance(default, (int, float)):
                lines.append(f"  {name}: {type_str} = {default}")
            elif isinstance(default, str) and default:
                lines.append(f"  {name}: {type_str} = \"{default}\"")
            elif isinstance(default, list) and not default:
                pass  # skip empty-list defaults — just noise
            elif isinstance(default, str) and not default:
                pass  # skip empty-string defaults
            else:
                lines.append(f"  {name}: {type_str}")

    return lines


# ── Schema cache — built once at import ───────────────────────────────────────

# Maps category → type_name → list[str] of field lines
_SCHEMA: dict[str, dict[str, list[str]]] = {
    "source": {},
    "sink": {},
    "serializer": {},
    "transform": {},
}


def _iter_union_models(union_type) -> list:
    """Unwrap Annotated[A | B | C, ...] → [A, B, C] regardless of union spelling."""
    import types as _types
    args = get_args(union_type)
    if not args:
        return []
    inner = args[0]  # Annotated strips to first arg
    # Python 3.10+ uses types.UnionType for X | Y syntax
    if isinstance(inner, _types.UnionType) or get_origin(inner) is typing.Union:
        return list(get_args(inner))
    return [inner]


def _build_schema_cache() -> None:
    """Walk the union types in pipeline.py and populate _SCHEMA."""
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
        ("source",     SourceConfig),
        ("sink",       SinkConfig),
        ("serializer", SerializerConfig),
        ("transform",  TransformConfig),
    ):
        for model_cls in _iter_union_models(union_type):
            try:
                # Type name lives in the Literal annotation, not the default
                ann = model_cls.model_fields["type"].annotation
                type_args = get_args(ann)
                type_name = type_args[0] if type_args else None
                # Fallback: check default directly
                if not type_name:
                    d = model_cls.model_fields["type"].default
                    type_name = d if isinstance(d, str) else None
                if type_name:
                    _SCHEMA[category][type_name] = _model_to_schema_lines(model_cls)
            except Exception:
                pass


_build_schema_cache()


# ── Context builder ───────────────────────────────────────────────────────────

def _detect_types(prompt: str, available: list[str]) -> list[str]:
    """Return connector type names mentioned (or strongly implied) in the prompt."""
    prompt_lower = prompt.lower()
    detected = []
    for t in available:
        # Direct mention: "sftp", "kafka", "snmp_poll" → also match "snmp"
        stem = t.split("_")[0]   # snmp_poll → snmp, snmp_trap → snmp
        if t in prompt_lower or (len(stem) >= 4 and stem in prompt_lower):
            detected.append(t)
    return detected


def _format_block(category: str, type_name: str) -> str:
    """Return a full YAML-schema block for one connector type."""
    lines = _SCHEMA[category].get(type_name, [])
    if not lines:
        return f"{type_name}:  # (no schema available)"
    return f"{type_name}:\n" + "\n".join(lines)


def _one_liner(category: str, type_name: str) -> str:
    """Return a compact one-line summary showing only required fields."""
    lines = _SCHEMA[category].get(type_name, [])
    required = [ln.strip().split(":")[0] for ln in lines if "required" in ln]
    if required:
        return f"  {type_name}: requires {', '.join(required)}"
    return f"  {type_name}"


def build_ai_context(prompt: str, plugins: dict) -> str:
    """
    Build a compact, prompt-aware schema reference for the AI system prompt.

    - Full schema blocks for connector types mentioned in the user's prompt.
    - One-line summaries for all others.
    - Full schema for all serializers and transforms (they're short).
    """
    sections: list[str] = []

    # ── Critical rules (always included) ─────────────────────────────────────
    sections.append("""\
CRITICAL RULES — violating any of these causes validation errors:
1. serializer_in is REQUIRED at top level and MUST be an object: {type: json}
   NEVER write "serializer: json" — always "serializer_in:\\n  type: json"
2. serializer_out inside a sink MUST be an object: {type: json}  — NEVER a plain string.
3. SFTP sink uses "filename_template" for the output filename — NOT "file_pattern" (source-only field).
4. All list fields (oids, brokers, hosts) must use YAML list syntax, not inline strings.

EXPRESSION SYNTAX (used in add_field.fields, filter_rows.condition, sink.condition):
- Expressions are plain Python evaluated by simpleeval. NO Jinja2 / NO {{...}} wrappers.
- WRONG: timestamp: "{{now()}}"   RIGHT: timestamp: "now()"
- WRONG: condition: "{{rx > 0}}"  RIGHT: condition: "rx > 0"
- Record fields are available as variables: rx_mbps + tx_mbps, str(status), len(name)
- Built-in functions: round abs int float str len min max sum bool sqrt log
- Timestamp functions:
    now()               → UTC ISO-8601 string   e.g. "2026-04-08T10:23:45.123456+00:00"
    now('%Y-%m-%d')     → formatted date string  e.g. "2026-04-08"
    now('%Y-%m-%dT%H:%M:%SZ')  → compact UTC    e.g. "2026-04-08T10:23:45Z"
    epoch()             → Unix timestamp float   e.g. 1744105425.123
    epoch_ms()          → Unix ms integer        e.g. 1744105425123""")


    # ── Sources ───────────────────────────────────────────────────────────────
    available_sources = plugins.get("sources", list(_SCHEMA["source"].keys()))
    detected_sources  = _detect_types(prompt, available_sources)

    source_lines = ["SOURCES:"]
    for t in sorted(available_sources):
        if t in detected_sources:
            source_lines.append(_format_block("source", t))
        else:
            source_lines.append(_one_liner("source", t))
    sections.append("\n".join(source_lines))

    # ── Sinks ─────────────────────────────────────────────────────────────────
    available_sinks  = plugins.get("sinks", list(_SCHEMA["sink"].keys()))
    detected_sinks   = _detect_types(prompt, available_sinks)

    sink_lines = ["SINKS:"]
    for t in sorted(available_sinks):
        if t in detected_sinks:
            sink_lines.append(_format_block("sink", t))
        else:
            sink_lines.append(_one_liner("sink", t))
    sections.append("\n".join(sink_lines))

    # ── Serializers (full, always — they're compact) ──────────────────────────
    available_sers = plugins.get("serializers", list(_SCHEMA["serializer"].keys()))
    ser_lines = ["SERIALIZERS (serializer_in and serializer_out must use these as {type: <name>}):"]
    for t in sorted(available_sers):
        lines = _SCHEMA["serializer"].get(t, [])
        if lines:
            ser_lines.append(f"  {t}:\n" + "\n".join("  " + ln for ln in lines))
        else:
            ser_lines.append(f"  {t}")
    sections.append("\n".join(ser_lines))

    # ── Transforms (full, always — they're compact) ───────────────────────────
    available_transforms = plugins.get("transforms", list(_SCHEMA["transform"].keys()))
    tr_lines = ["TRANSFORMS (each item in the transforms list):"]
    for t in sorted(available_transforms):
        lines = _SCHEMA["transform"].get(t, [])
        if lines:
            tr_lines.append(f"  {t}:\n" + "\n".join("  " + ln for ln in lines))
        else:
            tr_lines.append(f"  {t}")
    sections.append("\n".join(tr_lines))

    return "\n\n".join(sections)
