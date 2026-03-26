# Feature 1 — Pipeline Setup Wizard

## Goal

A guided multi-step form for creating a new pipeline without writing YAML manually.
Replaces the blank code editor as the default entry point for new pipelines.
The editor remains available for advanced users and bulk edits.

## User Flow

```
Step 1: Basic info
  - Pipeline name (validated: unique, slug-safe)
  - Description (optional)
  - Schedule type: stream | interval | cron | manual
  - If interval: interval value + unit (seconds / minutes / hours)
  - If cron: cron expression input with human-readable preview

Step 2: Source
  - Dropdown: pick source type (populated from GET /api/plugins)
  - Dynamic form fields for the chosen type (host, port, path, topic, etc.)
  - [Test Connection] button → POST /api/connectors/test (Feature 5)

Step 3: Transforms (optional)
  - "Add transform" button → pick type → fill fields
  - Ordered list; drag-to-reorder or up/down arrows
  - Can add zero or more transforms

Step 4: Sinks
  - "Add sink" button → pick type → fill fields
  - Per-sink: optional serializer_out override dropdown
  - Per-sink: optional condition expression (fan-out routing)
  - [Test Connection] button per sink → POST /api/connectors/test

Step 5: Review
  - Rendered YAML preview (syntax-highlighted, read-only)
  - [Dry Run] button → POST /api/pipelines/dry-run
  - Dry-run result shown inline: pass (green) or fail with error message
  - [Save & Register] → POST /api/pipelines
  - On success: redirect to pipeline detail page
```

## UI

New files:
- `tram-ui/src/pages/wizard.html`
- `tram-ui/src/pages/wizard.js`

Nav: "New Pipeline" button on the Pipelines list page opens wizard (replaces direct
link to editor). A small "Advanced: open in editor" link is shown on step 1.

Step indicator bar at top showing steps 1–5. Back/Next buttons. Keyboard navigable.

## YAML Assembly

The wizard assembles a valid `PipelineConfig` YAML client-side from form state.
No server-side rendering needed.

```javascript
// wizard.js
function buildYaml(state) {
  const lines = [
    `pipeline:`,
    `  name: ${state.name}`,
    state.description ? `  description: "${state.description}"` : null,
    `  schedule:`,
    `    type: ${state.scheduleType}`,
    state.intervalSeconds ? `    interval_seconds: ${state.intervalSeconds}` : null,
    state.cronExpr ? `    cron_expr: "${state.cronExpr}"` : null,
    `  source:`,
    `    type: ${state.source.type}`,
    ...Object.entries(state.source.fields).map(([k,v]) => `    ${k}: ${v}`),
    // transforms, sinks ...
  ].filter(Boolean)
  return lines.join('\n')
}
```

Uses `GET /api/plugins` response to know which connector types are available.
Field metadata (required fields, defaults) is defined in a static `FIELD_SCHEMA`
map in `wizard.js` for the most common connectors; unknown connectors fall back
to a freeform key-value editor.

## AI Assist (optional)

When `TRAM_AI_API_KEY` is set, the wizard gains two AI-powered features.
If the env var is absent the wizard is fully functional without them — the AI
entry points are simply hidden.

### 1. Natural language → YAML ("Describe your pipeline")

A text area on Step 1 (above the form fields):

```
Describe what you want (optional):
┌──────────────────────────────────────────────────────────┐
│ Poll SFTP every 5 minutes, parse CSV, rename columns,    │
│ push to Kafka topic pm-raw                               │
└──────────────────────────────────────────────────────────┘
[Generate with AI]
```

Clicking "Generate with AI":
1. Calls `POST /api/ai/suggest` with the user's prompt
2. Backend calls Claude API with TRAM schema context + prompt
3. Returns generated YAML
4. Wizard parses the YAML and pre-fills all form fields across steps 1–4
5. User reviews each step normally, edits anything needed, then dry-runs on step 5

The natural language entry is a shortcut — it does not replace the step-by-step
form. After generation the user still walks through each step to verify and refine.

### 2. Dry-run error explanation ("Explain this error")

When a dry-run on Step 5 fails, an "Explain" button appears next to the error:

```
Dry run failed
  source type 'sftp' missing required field: host      [Explain]
```

Clicking "Explain":
1. Calls `POST /api/ai/suggest` with `mode: "explain"`, the error text, and the current YAML
2. Returns a plain-English explanation and suggested fix
3. Shown inline below the error, e.g.:
   > "The SFTP source requires a `host` field specifying the server hostname or IP.
   > Go back to Step 2 and fill in the Host field."

---

## AI Backend

### New endpoint

```
POST /api/ai/suggest
Content-Type: application/json

// Mode 1 — generate YAML from description
{"mode": "generate", "prompt": "Poll SFTP every 5 min, parse CSV, push to Kafka pm-raw", "plugins": {...}}

// Mode 2 — explain dry-run error
{"mode": "explain", "error": "missing required field: host", "yaml": "pipeline:\n  ..."}
```

Response:
```json
{"yaml": "pipeline:\n  name: ..."}        // mode: generate
{"explanation": "The SFTP source ..."}    // mode: explain
```

Returns HTTP 503 if `TRAM_AI_API_KEY` is not set.

### Implementation

New file: `tram/api/routers/ai.py`

```python
import anthropic

GENERATE_SYSTEM = """
You are a TRAM pipeline configuration assistant.
TRAM pipelines are defined in YAML. Given a user description, output ONLY valid
TRAM pipeline YAML — no prose, no markdown fences.

Available source types: {source_types}
Available sink types:   {sink_types}
Available transforms:   {transform_types}
Available serializers:  {serializer_types}

Schema reference:
{schema_excerpt}

Example pipeline:
{example}
"""

@router.post("/api/ai/suggest")
async def ai_suggest(request: Request) -> dict:
    api_key = os.getenv("TRAM_AI_API_KEY")
    if not api_key:
        raise HTTPException(503, "AI assist not configured (TRAM_AI_API_KEY not set)")

    body = await request.json()
    client = anthropic.Anthropic(api_key=api_key)

    if body["mode"] == "generate":
        plugins = body.get("plugins", {})
        system = GENERATE_SYSTEM.format(
            source_types=", ".join(plugins.get("sources", [])),
            sink_types=", ".join(plugins.get("sinks", [])),
            transform_types=", ".join(plugins.get("transforms", [])),
            serializer_types=", ".join(plugins.get("serializers", [])),
            schema_excerpt=SCHEMA_EXCERPT,   # static string, key fields only
            example=EXAMPLE_YAML,
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": body["prompt"]}],
        )
        return {"yaml": msg.content[0].text.strip()}

    elif body["mode"] == "explain":
        prompt = f"YAML:\n{body['yaml']}\n\nError: {body['error']}\n\nExplain the error and how to fix it in 2-3 sentences."
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return {"explanation": msg.content[0].text.strip()}
```

Model: `claude-haiku-4-5-20251001` — fast, low cost, sufficient for structured YAML generation.

### System prompt strategy

The generate system prompt includes:
- Dynamic plugin type lists from `GET /api/plugins` (injected at request time)
- A static schema excerpt covering the most important fields (`name`, `schedule`,
  `source`, `sinks`, `transforms`) — not the full Pydantic model
- One complete example pipeline YAML as a few-shot reference

This keeps the context window small and generation fast (~1–2 s).

---

## Backend

Wizard POSTs to:
- `POST /api/pipelines/dry-run` — validation on step 5
- `POST /api/pipelines` — save on final step
- `GET /api/plugins` — connector type lists (already exists)
- `POST /api/connectors/test` — connection test on steps 2 and 4 (Feature 5)
- `POST /api/ai/suggest` — AI generation and error explanation (optional)

## Files Changed

| File | Change |
|------|--------|
| `tram-ui/src/pages/wizard.html` | New |
| `tram-ui/src/pages/wizard.js` | New |
| `tram-ui/index.html` | Add nav link "New Pipeline" → wizard |
| `tram-ui/src/pages/pipelines.js` | Change "New" button to open wizard |
| `tram/api/routers/ai.py` | New — `/api/ai/suggest` endpoint |
| `tram/api/app.py` | Register ai router |
| `tram-ui/src/api.js` | Add `api.ai.suggest(mode, payload)` |
