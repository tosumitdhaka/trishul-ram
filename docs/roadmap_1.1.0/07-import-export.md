# Feature 7 — Pipeline Import / Export

## Goal

Download a pipeline's YAML from the UI, and re-upload it to the same or another
TRAM instance. Enables config backup, cross-environment promotion (dev → staging → prod),
and a lightweight GitOps workflow without requiring Helm `--set-file` flags.

## Export

### User Flow
1. Open pipeline detail page
2. Click "Export YAML" button in the header actions
3. Browser downloads `{pipeline-name}.yaml`

### Implementation

No new API endpoint. The detail endpoint `GET /api/pipelines/{name}` already returns
`yaml` in the response body (from `to_detail_dict()`).

```javascript
// detail.js
function exportYaml(name, yaml) {
  const blob = new Blob([yaml], { type: 'text/yaml' })
  const a    = document.createElement('a')
  a.href     = URL.createObjectURL(blob)
  a.download = `${name}.yaml`
  a.click()
  URL.revokeObjectURL(a.href)
}
```

## Import

### User Flow
1. Open Pipelines list page
2. Click "Import" button (next to existing "New Pipeline" button)
3. File picker opens — user selects a `.yaml` file (or drag-and-drop onto button)
4. If no pipeline with that name exists:
   - Calls `POST /api/pipelines` with file content
   - On success: toast "Imported sample-health" + list refreshes
5. If a pipeline with the same name already exists:
   - Prompt modal:
     ```
     Pipeline "sample-health" already exists.
     [ Replace (saves new version) ]
     [ Import as new name:  _______________ ]
     [ Cancel ]
     ```
   - Replace → `PUT /api/pipelines/{name}` with file content
   - Rename → user types new name, YAML is patched client-side, then `POST /api/pipelines`

### Implementation

No new API endpoints. Reuses:
- `POST /api/pipelines` — create (already exists)
- `PUT /api/pipelines/{name}` — update / save new version (already exists)

```javascript
// pipelines.js
async function importYaml(file) {
  const yaml = await file.text()
  const name = extractName(yaml)   // parse "name: xxx" from YAML text

  if (await pipelineExists(name)) {
    showImportConflictModal(name, yaml)
  } else {
    await api.pipelines.create(yaml)
    toast(`Imported ${name}`)
    await loadPipelines()
  }
}

function patchName(yaml, newName) {
  return yaml.replace(/^(\s*name:\s*)\S+/m, `$1${newName}`)
}
```

## Bulk Import

Stretch goal (can be a follow-up): support selecting multiple YAML files at once.
Each file imported sequentially; a summary toast shows "Imported 3 pipelines, 1 failed".

## Files Changed

| File | Change |
|------|--------|
| `tram-ui/src/pages/detail.js` | Add "Export YAML" button and handler |
| `tram-ui/src/pages/detail.html` | Export button in header |
| `tram-ui/src/pages/pipelines.js` | Add "Import" button, file input, conflict modal |
| `tram-ui/src/pages/pipelines.html` | Import button markup |
