"""AI context builder — generates compact connector/transform schema reference from Pydantic models."""

from __future__ import annotations

from fastapi import APIRouter

from tram.api.config_schema import SCHEMA_FIELDS, SCHEMA_LINES, schema_version
from tram.registry.registry import list_plugins

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
    fields = SCHEMA_FIELDS[category].get(type_name, [])
    required = [field["name"] for field in fields if field["required"]]
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
    import tram.connectors  # noqa: F401
    import tram.serializers  # noqa: F401
    import tram.transforms  # noqa: F401

    # Client-provided plugin lists cannot hide or invent runtime capabilities.
    plugins = list_plugins()
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

EXPRESSION SYNTAX (used in add_field.fields, filter.condition, sink.condition):
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
    epoch_ms()          → Unix ms integer        e.g. 1744105425123
- Literal strings in add_field need expression quotes: fields: {site: "'tokyo'"}.
  A bare expression tokyo looks up a record variable; it is not a string constant.

COMMON FIELDS BY CATEGORY:
- Source and transform type is required; filter.condition is a required expression.
- Sinks may use condition (expression), transforms (list of transform objects), and
  serializer_out (serializer object). They inherit top-level serializer_out when omitted.

SEMANTIC RULES:
- SNMP tables: source type snmp_poll, operation: walk, yield_rows: true.
  classify: true replaces flat values with _metrics and _labels dicts; row index is _index.
  Read classified data via paths such as _metrics.ifInOctets and _labels.ifDescr.
  metric_patterns and label_patterns are shell-style globs (e.g. '*Octets*'), not regexes;
  metric patterns win. Use resolve_oids: true for symbolic field names.
- gNMI subscription_mode: once ends after one snapshot; pair with schedule type interval
  for repeated snapshots. poll repeats snapshots internally using poll_interval_seconds;
  poll and stream are continuous sources and need schedule type stream.
- aggregate.operations maps output names to 'op:field' or {op: sum, field: rx}.
  Ops: sum, avg, min, max, count, first, last. count counts non-null values of its field,
  not all rows. To count rows, add a constant field first and count that field.
- select_from_list.select is a list: each entry sets exactly one of match (exact key/value
  match against an item) or first_item: true, plus output: {item.path: output_field}.
  Example: select: [{match: {name: eth0}, output: {stats.rx: rx_bytes}}].
- project.fields maps output_field to a dotted source path (supports list indices), or
  {source: path, default: value, required: true}, or {source_any: [path1, path2]}.
  Defaults apply to missing paths; an existing null is preserved. Only projected fields remain.
- File sink filename_template supports record tokens {field.path}, e.g.
  '{field.device.name}_{timestamp}.json'; do not use a bare {device} record token.""")


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


def build_task_guidance(request_text: str) -> str:
    """Return bounded, relevant runtime examples for the end of the system prompt."""
    text = request_text.lower()
    sections = [
        "FINAL TASK GUIDANCE — adapt example names and paths to the user's actual intent. "
        "Before returning YAML, verify matching versus first-item selection, missing versus null "
        "defaults, the field counted for record totals, and MIB readiness for symbolic SNMP fields."
    ]
    if any(word in text for word in (
        "select", "list", "nested", "project", "default", "null", "missing",
    )):
        sections.append("""Nested selection and defaults:
select_from_list.match picks the FIRST MATCHING item. first_item: true picks the first item
without testing conditions; use match when the user asks for a particular name or category.
output maps item source paths to output names. A missing match or missing projected path yields
None. project.default handles absent paths only: it does NOT replace an existing None.
When the user wants zero for missing/null values, coalesce before projecting (0 remains 0).
Adapt this example's list path, match keys, and output names:
```yaml
transforms:
  - type: select_from_list
    field: interfaces
    select:
      - match: {name: eth0}
        output: {stats.rx: rx}
  - type: coalesce_fields
    fields:
      rx: {sources: [rx], default: 0, empty_values: [null, '']}
  - type: project
    fields: {rx: rx}
```""")
    if any(word in text for word in ("aggregate", "group", "sum", "count", "total")):
        sections.append("""Aggregation and record counts:
operations maps output names to 'op:field' or {op: ..., field: ...}. count counts NON-NULL
values in that source field. count:bytes is not a record count when bytes is null or absent.
If the user requests number of records, add a constant marker and count it. IMPORTANT: the
executor applies TOP-LEVEL transforms separately to each record. Put aggregate inside
sinks[].transforms to group the whole parsed source chunk; top-level aggregate produces
one aggregate per input record. For a local JSON array file, omit record_chunk_size so the
whole array is one chunk. This aggregates each chunk/file, NOT across all files in a run.
For multi-chunk windows use window_aggregate only when timestamp window semantics are wanted.
Adapt group keys, sink, and output names below to the requested output schema:
```yaml
transforms:
  - type: add_field
    fields: {_row_present: '1'}
sinks:
  - type: local
    path: /output
    serializer_out: {type: ndjson}
    transforms:
      - type: aggregate
        group_by: [device]
        operations:
          bytes_total: 'sum:bytes'
          records: 'count:_row_present'
```""")
    if "snmp" in text or any(word in text for word in ("iftype", "ifdescr", "ifoctets")):
        sections.append("""SNMP symbolic IF-MIB tables:
For symbolic IF-MIB categorization, INCLUDE source.resolve_oids: true, source.mib_dirs with
the real assets directory, and source.mib_modules: [IF-MIB, IANAifType-MIB]. These explicit
source settings are mandatory for this generated configuration; do not omit them even when
the host may auto-discover /mibs. Do not emit symbolic category expressions without them.
Symbolic fields and enum labels require available MIB bundles and successful runtime loading.
Explicit mib_dirs/modules make that intent clear; the runtime can also auto-use /mibs or
TRAM_MIB_DIR when present. For the trishul SNMP stack, modules alone do not supply JSON IR
bundles: use the directory containing the actual assets (the deployed example uses /mibs).
For IF-MIB table rows use operation: walk, yield_rows: true, index_depth: 1. classify: true
produces _metrics, _labels, and _index, not the original flat fields. ifType belongs in labels
for interface-type categorization. Load both IF-MIB and IANAifType-MIB assets for symbolic
interface type names. Enum labels include the number, e.g. "ethernetCsmacd (6)"; test the name
within that string rather than equality to a bare name. label_patterns: [ifType] forces labels
but overrides enum rendering with raw numeric text (e.g. "6"); use it only when intentionally
matching numeric values. Confirm those MIBs are available on the
runtime host; otherwise do not assume resolved names or silently label every row 'unknown'.
Adapt the device, OIDs, and metric/label patterns to the actual requested table:
```yaml
source:
  type: snmp_poll
  host: device.example
  oids: ['IF-MIB::ifTable']
  operation: walk
  resolve_oids: true
  mib_dirs: [/mibs]
  mib_modules: [IF-MIB, IANAifType-MIB]
  yield_rows: true
  index_depth: 1
  classify: true
```""")
    return "\n\n".join(sections)
