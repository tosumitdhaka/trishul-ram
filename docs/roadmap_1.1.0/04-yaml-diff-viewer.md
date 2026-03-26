# Feature 4 — YAML Diff Viewer (Versions Tab)

## Goal

When viewing pipeline version history, show a diff between any two versions — or
between a historical version and the currently active one — so operators can see
exactly what changed before rolling back.

## Current State

- `GET /api/pipelines/{name}/versions` → `[{version, created_at, is_active}, ...]`
- `GET /api/pipelines/{name}/versions/{v}` → raw YAML text
- Versions tab in `detail.js` lists versions with a "Restore" button — no diff

## Design

No new backend API endpoints. Both YAML texts are fetched via the existing version
endpoint and the diff is computed client-side.

### Interaction

1. User opens Versions tab on a pipeline detail page
2. Each version row gains a "Diff" button
3. Clicking "Diff" on a historical version opens a modal:
   - Left pane: selected historical version (labelled "v3 — 2026-03-20 14:02")
   - Right pane: currently active version (labelled "active — 2026-03-26 05:19")
   - Optional: "Compare two versions" dropdown to swap either side
4. Lines are coloured:
   - Added (in right, not in left): green background `#1a3328`
   - Removed (in left, not in right): red background `#3d1a1a`
   - Unchanged: default text, dimmed opacity

### Diff Algorithm

Myers diff on `yaml.split('\n')` arrays, implemented inline in `detail.js`.
~60 lines of plain JS, no external library.

```javascript
// detail.js
function myersDiff(a, b) {
  // Returns array of {type: 'equal'|'insert'|'delete', line: string}
  ...
}

function renderDiff(oldYaml, newYaml) {
  const hunks = myersDiff(oldYaml.split('\n'), newYaml.split('\n'))
  return hunks.map(h => {
    const cls = h.type === 'insert' ? 'diff-add'
               : h.type === 'delete' ? 'diff-del' : 'diff-eq'
    return `<div class="diff-line ${cls}">${esc(h.line)}</div>`
  }).join('')
}
```

### Modal Layout

```
┌─────────────────────────────────────────────────────────────────┐
│ Diff: sample-health                    [v3 ▼] vs [active ▼]  ✕ │
├────────────────────────────┬────────────────────────────────────┤
│ v3 — 2026-03-20 14:02      │ active — 2026-03-26 05:19          │
│ pipeline:                  │ pipeline:                          │
│   name: sample-health      │   name: sample-health              │
│ - interval_seconds: 30     │ + interval_seconds: 60             │
│   source:                  │   source:                          │
│     type: local            │     type: local                    │
└────────────────────────────┴────────────────────────────────────┘
```

Side-by-side on wide screens; unified (single column) on narrow screens.

## Files Changed

| File | Change |
|------|--------|
| `tram-ui/src/pages/detail.js` | Add `myersDiff`, `renderDiff`, diff modal, Diff button in versions table |
| `tram-ui/src/pages/detail.html` | Add diff modal markup |
