"""AI context builder — generates compact connector/transform schema reference from Pydantic models."""

from __future__ import annotations

from fastapi import APIRouter

from tram.api.config_schema import SCHEMA_LINES, schema_version

router = APIRouter()

# ── A6: template-grounded generation ─────────────────────────────────────────
# Few-shot grounding from the bundled template library: the model knows field
# *names* but not idiomatic *usage*, and a matching worked template shows real
# condition strings, oid lists, filename templates, etc. Selection and size
# are bounded so the prompt stays within the existing prompt-size discipline.

_TEMPLATE_EXAMPLE_COUNT = 3     # max worked examples embedded in the prompt
_TEMPLATE_EXAMPLE_CHARS = 1200  # per-template YAML cap (truncate beyond this)
_TEMPLATE_EXAMPLE_TOTAL = 3000  # total grounding-section cap


def build_template_examples(
    templates: list[dict],
    prompt: str,
    *,
    max_templates: int = _TEMPLATE_EXAMPLE_COUNT,
    max_template_chars: int = _TEMPLATE_EXAMPLE_CHARS,
    max_total_chars: int = _TEMPLATE_EXAMPLE_TOTAL,
) -> str:
    """Select bundled templates matching *prompt* and format them as worked
    examples for a generate-mode system prompt (A6).

    A template matches when its ``source_type`` or a ``sink_types`` entry
    appears in the prompt; the best matches (most tag hits, then name) are
    kept, capped at *max_templates*. Each template's YAML is capped at
    *max_template_chars* chars and the whole section at *max_total_chars*.

    Returns an empty string when nothing matches (or no templates exist) —
    generation then proceeds ungrounded.
    """
    prompt_lower = prompt.lower()

    def score(tpl: dict) -> int:
        return sum(
            1
            for tag in [tpl.get("source_type", "")] + list(tpl.get("sink_types") or [])
            if isinstance(tag, str) and tag and tag.lower() in prompt_lower
        )

    scored = [(score(tpl), tpl) for tpl in templates if score(tpl) > 0]
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("name") or ""))
    scored = scored[:max_templates]

    blocks: list[str] = []
    total = 0
    for _, tpl in scored:
        name = tpl.get("name") or tpl.get("id") or "template"
        content = (tpl.get("yaml") or "").rstrip()
        if len(content) > max_template_chars:
            content = content[:max_template_chars].rstrip() + "\n# … (truncated)"
        block = f"### Template: {name}\n{content}"
        if blocks and total + len(block) > max_total_chars:
            break
        blocks.append(block)
        total += len(block)
    if not blocks:
        return ""
    return (
        "WORKED TEMPLATE EXAMPLES — use these as idiomatic reference for this "
        "kind of pipeline. Adapt the structure and values; do not copy "
        "verbatim:\n\n"
        + "\n\n".join(blocks)
    )


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

    # ── Schema identity (Issue #24 / Option A) ──────────────────────────────
    # One line identifying which schema this prompt was built against; the
    # same hash lands in the ai_usage audit row so every AI output is
    # attributable to the exact schema knowledge that produced it.
    sections.append(f"TRAM schema v: {schema_version()}")

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
    available_sources = plugins.get("sources", list(SCHEMA_LINES["source"].keys()))
    detected_sources  = _detect_types(prompt, available_sources)

    source_lines = ["SOURCES:"]
    for t in sorted(available_sources):
        if t in detected_sources:
            source_lines.append(_format_block("source", t))
        else:
            source_lines.append(_one_liner("source", t))
    sections.append("\n".join(source_lines))

    # ── Sinks ─────────────────────────────────────────────────────────────────
    available_sinks  = plugins.get("sinks", list(SCHEMA_LINES["sink"].keys()))
    detected_sinks   = _detect_types(prompt, available_sinks)

    sink_lines = ["SINKS:"]
    for t in sorted(available_sinks):
        if t in detected_sinks:
            sink_lines.append(_format_block("sink", t))
        else:
            sink_lines.append(_one_liner("sink", t))
    sections.append("\n".join(sink_lines))

    # ── Serializers (full, always — they're compact) ──────────────────────────
    available_sers = plugins.get("serializers", list(SCHEMA_LINES["serializer"].keys()))
    ser_lines = ["SERIALIZERS (serializer_in and serializer_out must use these as {type: <name>}):"]
    for t in sorted(available_sers):
        lines = SCHEMA_LINES["serializer"].get(t, [])
        if lines:
            ser_lines.append(f"  {t}:\n" + "\n".join("  " + ln for ln in lines))
        else:
            ser_lines.append(f"  {t}")
    sections.append("\n".join(ser_lines))

    # ── Transforms (full, always — they're compact) ───────────────────────────
    available_transforms = plugins.get("transforms", list(SCHEMA_LINES["transform"].keys()))
    tr_lines = ["TRANSFORMS (each item in the transforms list):"]
    for t in sorted(available_transforms):
        lines = SCHEMA_LINES["transform"].get(t, [])
        if lines:
            tr_lines.append(f"  {t}:\n" + "\n".join("  " + ln for ln in lines))
        else:
            tr_lines.append(f"  {t}")
    sections.append("\n".join(tr_lines))

    return "\n\n".join(sections)
