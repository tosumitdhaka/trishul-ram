import { api } from '../api.js'
import { esc, toast } from '../utils.js'

const TEMPLATE = `name: my-pipeline
source:
  type: local
  path: /data/input
  file_pattern: "*.json"
serializer_in:
  type: json
transforms:
  - type: rename
    fields:
      old_name: new_name
sinks:
  - type: local
    path: /data/output
`

let _originalYaml = null  // YAML as loaded from server (for diff in edit mode)

export async function init() {
  const ta       = document.getElementById('editor-textarea')
  const filename = document.getElementById('editor-filename')
  const editName = window._editorPipeline
  const isEdit   = Boolean(editName)

  // ── Mode-specific UI setup ─────────────────────────────────────────────────
  if (isEdit) {
    if (filename) filename.textContent = `${editName}.yaml`
    const saveLabel = document.getElementById('editor-save-label')
    if (saveLabel) saveLabel.textContent = 'Update Pipeline'
    const diffBtn = document.getElementById('editor-diff-btn')
    if (diffBtn) diffBtn.removeAttribute('hidden')

    try {
      const p = await api.pipelines.get(editName)
      const yaml = p.yaml || p.raw || JSON.stringify(p, null, 2)
      if (ta) ta.value = yaml
      _originalYaml = yaml
    } catch (e) {
      toast(`Could not load pipeline: ${e.message}`, 'error')
      if (ta) ta.value = TEMPLATE
    }
  } else {
    const preloaded = window._editorYaml
    window._editorYaml = null
    if (preloaded) {
      if (ta) ta.value = preloaded
      const nameMatch = preloaded.match(/^\s*name:\s*(\S+)/m)
      if (filename) filename.textContent = nameMatch ? `${nameMatch[1]}.yaml` : 'new-pipeline.yaml'
    } else {
      if (filename) filename.textContent = 'new-pipeline.yaml'
      if (ta) ta.value = TEMPLATE
    }
  }

  // ── Save ───────────────────────────────────────────────────────────────────
  window._editorSave = async () => {
    const yaml = ta?.value?.trim()
    if (!yaml) { toast('Nothing to save', 'error'); return }
    if (editName && _originalYaml && yaml === _originalYaml.trim()) {
      toast('No changes — pipeline is already up to date')
      navigate('pipelines')
      return
    }
    try {
      if (editName) {
        await api.pipelines.update(editName, yaml)
        _originalYaml = yaml
        toast(`Saved ${editName}`)
      } else {
        await api.pipelines.create(yaml)
        toast('Pipeline created')
      }
      window._editorPipeline = null
      navigate('pipelines')
    } catch (e) { toast(e.message, 'error') }
  }

  // ── Copy YAML ──────────────────────────────────────────────────────────────
  window._editorCopy = async () => {
    const yaml = ta?.value
    if (!yaml) return
    try {
      await navigator.clipboard.writeText(yaml)
      const btn = document.querySelector('[onclick="window._editorCopy?.()"]')
      if (btn) {
        const orig = btn.innerHTML
        btn.innerHTML = '<i class="bi bi-clipboard-check" style="font-size:15px"></i>'
        setTimeout(() => { btn.innerHTML = orig }, 1500)
      }
    } catch (_) {
      toast('Copy failed — use Ctrl+A / Ctrl+C', 'error')
    }
  }

  // ── Diff vs saved (inline toggle below editor) ────────────────────────────
  window._editorDiffSaved = () => {
    const existing = document.getElementById('editor-inline-diff')
    if (existing) { existing.remove(); return }   // second click hides it

    const current = ta?.value || ''
    const saved   = _originalYaml || ''

    const wrap = document.createElement('div')
    wrap.id = 'editor-inline-diff'
    wrap.style.cssText = 'margin-top:8px;border:1px solid #30363d;border-radius:6px;overflow:hidden'
    wrap.innerHTML = `
      <div style="display:flex;align-items:center;padding:5px 12px;background:#161b22;border-bottom:1px solid #30363d">
        <i class="bi bi-file-diff me-2" style="color:#8b949e"></i>
        <span style="font-size:12px;color:#e6edf3">Changes vs saved</span>
        <span id="inline-diff-stats" class="ms-2" style="font-size:11px"></span>
        <button style="margin-left:auto;background:none;border:none;color:#8b949e;cursor:pointer;padding:0 4px;font-size:14px" onclick="document.getElementById('editor-inline-diff')?.remove()">✕</button>
      </div>
      <div style="display:flex;height:280px">
        <div style="flex:1;min-width:0;display:flex;flex-direction:column;border-right:1px solid #30363d">
          <div style="padding:3px 12px;font-size:10px;color:#8b949e;background:#161b22;border-bottom:1px solid #21262d">Saved</div>
          <div id="inline-diff-left"  style="flex:1;overflow:auto;padding:8px 12px;background:#161b22;font-size:11px;font-family:monospace;white-space:pre"></div>
        </div>
        <div style="flex:1;min-width:0;display:flex;flex-direction:column">
          <div style="padding:3px 12px;font-size:10px;color:#8b949e;background:#161b22;border-bottom:1px solid #21262d">Current</div>
          <div id="inline-diff-right" style="flex:1;overflow:auto;padding:8px 12px;background:#161b22;font-size:11px;font-family:monospace;white-space:pre"></div>
        </div>
      </div>`
    document.querySelector('.editor-wrap')?.appendChild(wrap)
    _renderEditorDiff(saved, current)
  }

  // ── Dry run ────────────────────────────────────────────────────────────────
  window._editorDryRun = async () => {
    const yaml = ta?.value?.trim()
    if (!yaml) { toast('Nothing to dry-run', 'error'); return }
    const btn = document.querySelector('[onclick="window._editorDryRun?.()"]')
    const orig = btn?.innerHTML
    if (btn) btn.innerHTML = '<i class="bi bi-hourglass" style="font-size:15px"></i> Running…'
    try {
      const { baseUrl, apiKey } = (await import('../api.js')).getConfig()
      const headers = { 'Content-Type': 'application/yaml' }
      if (apiKey) headers['X-API-Key'] = apiKey
      const token = localStorage.getItem('tram_auth_token')
      if (token && !apiKey) headers['Authorization'] = `Bearer ${token}`
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

  // ── Test connectors ────────────────────────────────────────────────────────
  window._editorTestConnectors = async () => {
    const yaml = ta?.value?.trim()
    if (!yaml) { toast('Nothing to test', 'error'); return }
    const btn = document.querySelector('[onclick="window._editorTestConnectors?.()"]')
    const orig = btn?.innerHTML
    if (btn) btn.innerHTML = '<i class="bi bi-hourglass" style="font-size:15px"></i>'
    try {
      const result = await api.connectors.testPipeline(yaml)
      showConnectorTestResult(result)
    } catch (e) {
      toast(`Test error: ${e.message}`, 'error')
    } finally {
      if (btn && orig) btn.innerHTML = orig
    }
  }

  // ── AI assist ──────────────────────────────────────────────────────────────
  await _checkAI(isEdit)

  // ── Tab key inserts spaces ────────────────────────────────────────────────
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

async function _checkAI(isEdit) {
  try {
    const status = await api.ai.status()
    const modelEl   = document.getElementById('editor-ai-model')
    const uncfgEl   = document.getElementById('editor-ai-unconfigured')
    const genBtn    = document.getElementById('editor-ai-gen-btn')
    const modBtn    = document.getElementById('editor-ai-mod-btn')

    if (status.enabled) {
      if (modelEl)  modelEl.textContent = `${status.provider} / ${status.model}`
      if (uncfgEl)  uncfgEl.classList.add('d-none')
      if (genBtn)   genBtn.disabled = false
      if (modBtn)   modBtn.disabled = false
      // Show generate panel for new pipelines, modify panel for existing
      if (isEdit) {
        document.getElementById('editor-ai-generate-panel')?.classList.add('d-none')
        document.getElementById('editor-ai-modify-panel')?.classList.remove('d-none')
      } else {
        document.getElementById('editor-ai-generate-panel')?.classList.remove('d-none')
        document.getElementById('editor-ai-modify-panel')?.classList.add('d-none')
      }
    } else {
      if (modelEl)  modelEl.textContent = ''
      if (uncfgEl)  uncfgEl.classList.remove('d-none')
      if (genBtn)   genBtn.disabled = true
      if (modBtn)   modBtn.disabled = true
    }
    window._editorAiEnabled = status.enabled
  } catch (_) {}
}

let _editorPlugins = null

async function _getPlugins() {
  if (_editorPlugins) return _editorPlugins
  try { _editorPlugins = await api.plugins() } catch (_) { _editorPlugins = {} }
  return _editorPlugins
}

// ── AI: Generate (new pipeline) ──────────────────────────────────────────────
window._editorAiGenerate = async () => {
  const prompt = document.getElementById('editor-ai-prompt')?.value.trim()
  if (!prompt) { toast('Enter a description first', 'error'); return }
  const btn = document.getElementById('editor-ai-gen-btn')
  const statusEl = document.getElementById('editor-ai-status')
  const ta = document.getElementById('editor-textarea')
  if (btn) btn.disabled = true
  if (statusEl) statusEl.textContent = 'Generating…'
  try {
    const plugins = await _getPlugins()
    const r = await api.ai.suggest({ mode: 'generate', prompt, plugins })
    if (!r.yaml) throw new Error('No YAML returned')
    if (ta) ta.value = r.yaml
    if (statusEl) statusEl.textContent = ''
    toast('YAML generated — review and save')
  } catch (e) {
    toast(`AI error: ${e.message}`, 'error')
    if (statusEl) statusEl.textContent = ''
  } finally {
    if (btn) btn.disabled = false
  }
}

// ── AI: Modify (existing pipeline) ───────────────────────────────────────────
window._editorAiModify = async () => {
  const instruction = document.getElementById('editor-ai-instruction')?.value.trim()
  if (!instruction) { toast('Enter an instruction first', 'error'); return }
  const btn = document.getElementById('editor-ai-mod-btn')
  const statusEl = document.getElementById('editor-ai-status')
  const ta = document.getElementById('editor-textarea')
  const yaml = ta?.value?.trim()
  if (!yaml) { toast('Editor is empty', 'error'); return }
  if (btn) btn.disabled = true
  if (statusEl) statusEl.textContent = 'Modifying…'
  try {
    const plugins = await _getPlugins()
    const r = await api.ai.suggest({ mode: 'modify', yaml, instruction, plugins })
    if (!r.yaml) throw new Error('No YAML returned')
    if (ta) ta.value = r.yaml
    if (statusEl) statusEl.textContent = ''
    // Refresh inline diff if already open, otherwise open it
    document.getElementById('editor-inline-diff')?.remove()
    window._editorDiffSaved?.()
    toast('Pipeline modified — review the diff and save')
  } catch (e) {
    toast(`AI error: ${e.message}`, 'error')
    if (statusEl) statusEl.textContent = ''
  } finally {
    if (btn) btn.disabled = false
  }
}

// ── Diff rendering (editor) ───────────────────────────────────────────────────
function _myersDiff(a, b) {
  const m = a.length, n = b.length
  const max = m + n
  const v = new Array(2 * max + 1).fill(0)
  const trace = []
  for (let d = 0; d <= max; d++) {
    trace.push([...v])
    for (let k = -d; k <= d; k += 2) {
      let x = (k === -d || (k !== d && v[k - 1 + max] < v[k + 1 + max]))
        ? v[k + 1 + max] : v[k - 1 + max] + 1
      let y = x - k
      while (x < m && y < n && a[x] === b[y]) { x++; y++ }
      v[k + max] = x
      if (x >= m && y >= n) return _backtrack(trace, a, b, max)
    }
  }
  return _backtrack(trace, a, b, max)
}

function _backtrack(trace, a, b, max) {
  const result = []
  let x = a.length, y = b.length
  for (let d = trace.length - 1; d >= 0; d--) {
    const v = trace[d]
    const k = x - y
    const prevK = (k === -d || (k !== d && v[k - 1 + max] < v[k + 1 + max])) ? k + 1 : k - 1
    const prevX = v[prevK + max]
    const prevY = prevX - prevK
    while (x > prevX && y > prevY) { result.unshift({ type: 'equal', line: a[x - 1] }); x--; y-- }
    if (d > 0) {
      if (x > prevX) { result.unshift({ type: 'delete', line: a[x - 1] }); x-- }
      else            { result.unshift({ type: 'insert', line: b[y - 1] }); y-- }
    }
  }
  return result
}

function _renderEditorDiff(oldYaml, newYaml) {
  const hunks = _myersDiff(oldYaml.split('\n'), newYaml.split('\n'))
  const blank = `<div style="background:#0d1117"> </div>`
  let leftHtml = '', rightHtml = '', adds = 0, dels = 0
  for (const h of hunks) {
    const line = esc(h.line)
    if (h.type === 'equal') {
      const eq = `<div style="color:#8b949e">  ${line}</div>`
      leftHtml  += eq
      rightHtml += eq
    } else if (h.type === 'delete') {
      leftHtml  += `<div style="background:#3d1a1a;color:#ff7b72">- ${line}</div>`
      rightHtml += blank
      dels++
    } else {
      leftHtml  += blank
      rightHtml += `<div style="background:#1a3328;color:#3fb950">+ ${line}</div>`
      adds++
    }
  }
  const leftPane  = document.getElementById('inline-diff-left')
  const rightPane = document.getElementById('inline-diff-right')
  if (leftPane)  leftPane.innerHTML  = leftHtml  || '<div style="opacity:.4">— empty —</div>'
  if (rightPane) rightPane.innerHTML = rightHtml || '<div style="opacity:.4">— empty —</div>'

  if (leftPane && rightPane) {
    leftPane.onscroll  = () => { rightPane.scrollTop = leftPane.scrollTop }
    rightPane.onscroll = () => { leftPane.scrollTop  = rightPane.scrollTop }
  }

  const stats = document.getElementById('inline-diff-stats')
  if (stats) {
    stats.innerHTML = (adds === 0 && dels === 0)
      ? '<span style="color:#8b949e">no changes</span>'
      : `<span style="color:#3fb950">+${adds}</span> <span style="color:#ff7b72">−${dels}</span>`
  }
}

// ── Connector test result ─────────────────────────────────────────────────────
function showConnectorTestResult(result) {
  const existing = document.getElementById('connector-test-result')
  if (existing) existing.remove()
  const div = document.createElement('div')
  div.id = 'connector-test-result'
  div.style.cssText = 'margin-top:12px;padding:12px;border-radius:6px;font-size:12px;font-family:monospace;background:#161b22;border:1px solid #30363d;color:#e6edf3'

  const renderOne = (label, r) => {
    const ok = r?.ok
    const color = ok ? '#3fb950' : '#f85149'
    const icon  = ok ? '✓' : '✗'
    const msg   = ok ? (r.detail || 'OK') : (r.error || 'failed')
    const lat   = r?.latency_ms != null ? ` (${r.latency_ms}ms)` : ''
    return `<div><span style="color:${color}">${icon} ${esc(label)}</span> — ${esc(msg)}${lat}</div>`
  }

  let html = ''
  if (result.source) html += renderOne(`source (${result.source.type})`, result.source)
  for (const s of (result.sinks || [])) html += renderOne(`sink (${s.type})`, s)
  if (result.error) html += `<div style="color:#f85149">${esc(result.error)}</div>`
  div.innerHTML = html || '<div style="color:#6e7681">No connectors found in YAML</div>'
  document.querySelector('.editor-wrap')?.appendChild(div)
}

// ── Dry run result ────────────────────────────────────────────────────────────
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
    if (window._editorAiEnabled) {
      div.innerHTML += `<div class="d-flex gap-2 mt-2">
        <button class="btn btn-sm btn-outline-secondary" onclick="window._editorAiExplain?.()">
          <i class="bi bi-stars me-1"></i>Explain
        </button>
        <button class="btn btn-sm btn-outline-secondary" onclick="window._editorAiFix?.()">
          <i class="bi bi-wrench me-1"></i>AI Fix
        </button>
      </div>
      <div id="editor-ai-explain-result" class="mt-2 text-secondary" style="font-size:11px"></div>`
    }
  }
  if (result.warnings?.length) {
    div.innerHTML += result.warnings.map(w => `<div style="color:#e3b341">${esc(w)}</div>`).join('')
  }
  const ta = document.getElementById('editor-textarea')
  const lastErrors = result.errors || []

  window._editorAiExplain = async () => {
    const el = document.getElementById('editor-ai-explain-result')
    if (el) el.textContent = 'Explaining…'
    try {
      const r = await api.ai.suggest({ mode: 'explain', error: lastErrors[0], yaml: ta?.value })
      if (el) el.innerHTML = `<em>${esc(r.explanation || '')}</em>`
    } catch (e) {
      if (el) el.textContent = `Could not explain: ${e.message}`
    }
  }

  window._editorAiFix = async () => {
    const el = document.getElementById('editor-ai-explain-result')
    if (el) el.textContent = 'Fixing…'
    try {
      const plugins = await _getPlugins()
      const r = await api.ai.suggest({ mode: 'fix', error: lastErrors[0], yaml: ta?.value, plugins })
      if (!r.yaml) throw new Error('No YAML returned')
      if (ta) ta.value = r.yaml
      if (el) el.textContent = ''
      toast('YAML fixed — review the changes')
    } catch (e) {
      if (el) el.textContent = `Could not fix: ${e.message}`
    }
  }

  document.querySelector('.editor-wrap')?.appendChild(div)
}
