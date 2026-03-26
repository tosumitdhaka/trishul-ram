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

## Backend

No new API endpoints. Wizard POSTs to:
- `POST /api/pipelines/dry-run` — validation on step 5
- `POST /api/pipelines` — save on final step
- `GET /api/plugins` — connector type lists (already exists)
- `POST /api/connectors/test` — connection test on steps 2 and 4 (Feature 5)

## Files Changed

| File | Change |
|------|--------|
| `tram-ui/src/pages/wizard.html` | New |
| `tram-ui/src/pages/wizard.js` | New |
| `tram-ui/index.html` | Add nav link "New Pipeline" → wizard |
| `tram-ui/src/pages/pipelines.js` | Change "New" button to open wizard |
