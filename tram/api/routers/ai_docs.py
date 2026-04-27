"""AI context builder — generates compact connector/transform schema reference from Pydantic models."""

from __future__ import annotations

from tram.api.config_schema import SCHEMA_LINES

from fastapi import APIRouter

router = APIRouter()

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
    lines = SCHEMA_LINES[category].get(type_name, [])
    if not lines:
        return f"{type_name}:  # (no schema available)"
    return f"{type_name}:\n" + "\n".join(lines)


def _one_liner(category: str, type_name: str) -> str:
    """Return a compact one-line summary showing only required fields."""
    lines = SCHEMA_LINES[category].get(type_name, [])
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
