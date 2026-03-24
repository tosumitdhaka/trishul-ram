import { api } from '../api.js'
import { esc, toast } from '../utils.js'

const TEMPLATE = `name: my-pipeline
source:
  type: local
  path: /data/input
  file_pattern: "*.json"
serializer: json
transforms:
  - type: rename
    fields:
      old_name: new_name
sinks:
  - type: local
    path: /data/output
`

export async function init() {
  const ta       = document.getElementById('editor-textarea')
  const filename = document.getElementById('editor-filename')
  const editName = window._editorPipeline

  if (editName) {
    // Edit mode — load existing YAML
    if (filename) filename.textContent = `${editName}.yaml`
    try {
      const p = await api.pipelines.get(editName)
      const yaml = p.yaml || p.raw || JSON.stringify(p, null, 2)
      if (ta) ta.value = yaml
    } catch (e) {
      toast(`Could not load pipeline: ${e.message}`, 'error')
      if (ta) ta.value = TEMPLATE
    }
  } else {
    // New mode
    if (filename) filename.textContent = 'new-pipeline.yaml'
    if (ta) ta.value = TEMPLATE
  }

  window._editorSave = async () => {
    const yaml = ta?.value?.trim()
    if (!yaml) { toast('Nothing to save', 'error'); return }
    try {
      if (editName) {
        await api.pipelines.update(editName, yaml)
        toast(`Saved ${editName}`)
      } else {
        await api.pipelines.create(yaml)
        toast('Pipeline created')
      }
      window._editorPipeline = null
      navigate('pipelines')
    } catch (e) { toast(e.message, 'error') }
  }

  window._editorDryRun = async () => {
    const yaml = ta?.value?.trim()
    if (!yaml) { toast('Nothing to dry-run', 'error'); return }
    const btn = document.querySelector('[onclick="window._editorDryRun?.()"]')
    const orig = btn?.innerHTML
    if (btn) btn.innerHTML = '<i class="bi bi-hourglass" style="font-size:15px"></i> Running…'
    try {
      // dry-run: create a temp pipeline via the API's dry-run endpoint if available
      // fallback: POST to /api/pipelines/dry-run
      const { baseUrl, apiKey } = (await import('../api.js')).getConfig()
      const headers = { 'Content-Type': 'application/yaml' }
      if (apiKey) headers['X-API-Key'] = apiKey
      const res = await fetch(`${baseUrl}/api/pipelines/dry-run`, {
        method: 'POST', headers, body: yaml,
      })
      const json = res.ok ? await res.json() : null
      if (!res.ok) throw new Error(json?.detail || res.statusText)
      showDryRunResult(json)
    } catch (e) {
      toast(`Dry run: ${e.message}`, 'error')
    } finally {
      if (btn && orig) btn.innerHTML = orig
    }
  }

  // Tab key inserts spaces instead of blurring
  ta?.addEventListener('keydown', e => {
    if (e.key === 'Tab') {
      e.preventDefault()
      const start = ta.selectionStart
      const end   = ta.selectionEnd
      ta.value = ta.value.slice(0, start) + '  ' + ta.value.slice(end)
      ta.selectionStart = ta.selectionEnd = start + 2
    }
  })
}

function showDryRunResult(result) {
  const existing = document.getElementById('dry-run-result')
  if (existing) existing.remove()

  const div = document.createElement('div')
  div.id = 'dry-run-result'
  div.style.cssText = 'margin-top:12px;padding:12px;border-radius:6px;font-size:12px;font-family:monospace;background:#161b22;border:1px solid #30363d;color:#e6edf3;max-height:200px;overflow:auto'
  const ok = result.status === 'ok' || result.valid
  div.innerHTML = `<div style="color:${ok ? '#3fb950' : '#f85149'};margin-bottom:6px">${ok ? '✓ Dry run passed' : '✗ Dry run failed'}</div>`
  if (result.records_out !== undefined) {
    div.innerHTML += `<div>Records out: ${result.records_out}</div>`
  }
  if (result.errors?.length) {
    div.innerHTML += result.errors.map(e => `<div style="color:#f85149">${esc(e)}</div>`).join('')
  }
  if (result.warnings?.length) {
    div.innerHTML += result.warnings.map(w => `<div style="color:#e3b341">${esc(w)}</div>`).join('')
  }
  document.querySelector('.editor-wrap')?.appendChild(div)
}
