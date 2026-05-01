import { api } from '../api.js'
import { bindDataActions, esc, setStatusMessage, toast } from '../utils.js'
import {
  renderCodeOnlyDiffLine,
  renderDiffStats,
  renderSideBySideYamlDiff,
} from '../yaml_diff.js'

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
let _editorPlugins = null
let _editName = null
let _textarea = null
let _aiEnabled = false
let _lastDryRunErrors = []

function _leaveEditor(pipelineName = null) {
  const returnTo = window._editorReturn
  window._editorReturn = null
  window._editorYaml = null
  window._editorPipeline = null
  if (returnTo === 'detail' && pipelineName) {
    window._detailPipeline = pipelineName
    navigate('detail')
    return
  }
  if (returnTo && returnTo !== 'detail') {
    navigate(returnTo)
    return
  }
  navigate('pipelines')
}

export async function init() {
  const ta       = document.getElementById('editor-textarea')
  const titleEl  = document.getElementById('editor-title')
  const editName = window._editorPipeline
  const isEdit   = Boolean(editName)
  _textarea = ta
  _editName = editName
  _originalYaml = null
  _aiEnabled = false
  _lastDryRunErrors = []
  _bindEditorActions()

  // ── Mode-specific UI setup ─────────────────────────────────────────────────
  if (isEdit) {
    if (titleEl) titleEl.textContent = editName
    const saveLabel = document.getElementById('editor-save-label')
    if (saveLabel) saveLabel.textContent = 'Save'
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
    document.getElementById('editor-diff-btn')?.setAttribute('hidden', '')
    const preloaded = window._editorYaml
    window._editorYaml = null
    if (titleEl) titleEl.textContent = 'New Pipeline'
    if (preloaded) {
      if (ta) ta.value = preloaded
    } else {
      if (ta) ta.value = TEMPLATE
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

function _bindEditorActions() {
  const wrap = document.querySelector('.editor-wrap')
  bindDataActions(wrap, {
    'close-diff': () => {
      document.getElementById('editor-inline-diff')?.remove()
    },
    'ai-explain': () => { void _editorAiExplain() },
    'ai-fix': () => { void _editorAiFix() },
  })
  document.getElementById('editor-copy-btn')?.addEventListener('click', () => { void _editorCopy() })
  document.getElementById('editor-diff-btn')?.addEventListener('click', _editorDiffSaved)
  document.getElementById('editor-cancel-btn')?.addEventListener('click', () => _leaveEditor(_editName))
  document.getElementById('editor-test-btn')?.addEventListener('click', () => { void _editorTestConnectors() })
  document.getElementById('editor-dry-run-btn')?.addEventListener('click', () => { void _editorDryRun() })
  document.getElementById('editor-save-btn')?.addEventListener('click', () => { void _editorSave() })
  document.getElementById('editor-ai-gen-btn')?.addEventListener('click', () => { void _editorAiGenerate() })
  document.getElementById('editor-ai-mod-btn')?.addEventListener('click', () => { void _editorAiModify() })
  document.getElementById('editor-open-settings-link')?.addEventListener('click', (event) => {
    event.preventDefault()
    navigate('settings')
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
    _aiEnabled = Boolean(status.enabled)
  } catch (_) {}
}

async function _getPlugins() {
  if (_editorPlugins) return _editorPlugins
  try { _editorPlugins = await api.plugins() } catch (_) { _editorPlugins = {} }
  return _editorPlugins
}

// ── AI: Generate (new pipeline) ──────────────────────────────────────────────
async function _editorAiGenerate() {
  const prompt = document.getElementById('editor-ai-prompt')?.value.trim()
  if (!prompt) { toast('Enter a description first', 'error'); return }
  const btn = document.getElementById('editor-ai-gen-btn')
  if (btn) btn.disabled = true
  setStatusMessage('editor-ai-status', 'Generating…', 'info')
  try {
    const plugins = await _getPlugins()
    const r = await api.ai.suggest({ mode: 'generate', prompt, plugins })
    if (!r.yaml) throw new Error('No YAML returned')
    if (_textarea) _textarea.value = r.yaml
    setStatusMessage('editor-ai-status', '', 'muted')
    toast('YAML generated — review and save')
  } catch (e) {
    toast(`AI error: ${e.message}`, 'error')
    setStatusMessage('editor-ai-status', '', 'muted')
  } finally {
    if (btn) btn.disabled = false
  }
}

// ── AI: Modify (existing pipeline) ───────────────────────────────────────────
async function _editorAiModify() {
  const instruction = document.getElementById('editor-ai-instruction')?.value.trim()
  if (!instruction) { toast('Enter an instruction first', 'error'); return }
  const btn = document.getElementById('editor-ai-mod-btn')
  const yaml = _textarea?.value?.trim()
  if (!yaml) { toast('Editor is empty', 'error'); return }
  if (btn) btn.disabled = true
  setStatusMessage('editor-ai-status', 'Modifying…', 'info')
  try {
    const plugins = await _getPlugins()
    const r = await api.ai.suggest({ mode: 'modify', yaml, instruction, plugins })
    if (!r.yaml) throw new Error('No YAML returned')
    if (_textarea) _textarea.value = r.yaml
    setStatusMessage('editor-ai-status', '', 'muted')
    // Refresh inline diff if already open, otherwise open it
    document.getElementById('editor-inline-diff')?.remove()
    _editorDiffSaved()
    toast('Pipeline modified — review the diff and save')
  } catch (e) {
    toast(`AI error: ${e.message}`, 'error')
    setStatusMessage('editor-ai-status', '', 'muted')
  } finally {
    if (btn) btn.disabled = false
  }
}

function _renderEditorDiff(oldYaml, newYaml) {
  const leftPane  = document.getElementById('inline-diff-left')
  const rightPane = document.getElementById('inline-diff-right')
  const stats = document.getElementById('inline-diff-stats')
  renderSideBySideYamlDiff(oldYaml, newYaml, {
    leftPane,
    rightPane,
    statsEl: stats,
    renderLine: (_lineNo, line, type) => renderCodeOnlyDiffLine(line, type, 'editor-inline-diff'),
    renderStats: (adds, dels) => renderDiffStats(adds, dels, {
      muted: 'editor-inline-diff-stat-muted',
      insert: 'editor-inline-diff-stat-insert',
      delete: 'editor-inline-diff-stat-delete',
    }),
    emptyLine: '<div class="editor-inline-diff-empty">— empty —</div>',
  })
}

// ── Connector test result ─────────────────────────────────────────────────────
function showConnectorTestResult(result) {
  const existing = document.getElementById('connector-test-result')
  if (existing) existing.remove()
  const div = document.createElement('div')
  div.id = 'connector-test-result'

  const renderOne = (label, r) => {
    const ok = r?.ok
    const icon  = ok ? '✓' : '✗'
    const msg   = ok ? (r.detail || 'OK') : (r.error || 'failed')
    const lat   = r?.latency_ms != null ? ` (${r.latency_ms}ms)` : ''
    return `<div class="editor-status-line ${ok ? 'success' : 'error'}">${icon} ${esc(label)} — ${esc(msg)}${lat}</div>`
  }

  let html = ''
  if (result.source) html += renderOne(`source (${result.source.type})`, result.source)
  for (const s of (result.sinks || [])) html += renderOne(`sink (${s.type})`, s)
  if (result.error) html += `<div class="editor-status-line error">${esc(result.error)}</div>`
  div.className = 'editor-status-panel'
  div.innerHTML = `
    <div class="editor-status-panel-header">Connector Test</div>
    <div class="editor-status-panel-body">
      ${html || '<div class="editor-status-line muted">No connectors found in YAML</div>'}
    </div>`
  document.querySelector('.editor-wrap')?.appendChild(div)
}

// ── Dry run result ────────────────────────────────────────────────────────────
function showDryRunResult(result) {
  const existing = document.getElementById('dry-run-result')
  if (existing) existing.remove()

  const div = document.createElement('div')
  div.id = 'dry-run-result'
  const ok = result.status === 'ok' || result.valid
  const issues = result.errors || result.issues || []
  _lastDryRunErrors = issues
  div.className = 'editor-status-panel'
  div.innerHTML = `
    <div class="editor-status-panel-header">Dry Run</div>
    <div class="editor-status-panel-body editor-status-panel-body-scroll">
      <div class="editor-status-line ${ok ? 'success' : 'error'}">${ok ? '✓ Dry run passed' : '✗ Dry run failed'}</div>
    </div>`
  const body = div.querySelector('.editor-status-panel-body')
  if (result.records_out !== undefined) {
    body.innerHTML += `<div class="editor-status-line">Records out: ${result.records_out}</div>`
  }
  if (issues.length) {
    body.innerHTML += issues.map(e => `<div class="editor-status-line error">${esc(e)}</div>`).join('')
    if (_aiEnabled) {
      body.innerHTML += `<div class="d-flex gap-2 mt-2">
        <button class="btn btn-sm btn-outline-secondary" type="button" data-action="ai-explain">
          <i class="bi bi-stars me-1"></i>Explain
        </button>
        <button class="btn btn-sm btn-outline-secondary" type="button" data-action="ai-fix">
          <i class="bi bi-wrench me-1"></i>AI Fix
        </button>
      </div>
      <div id="editor-ai-explain-result" class="mt-2 editor-inline-status"></div>`
    }
  }
  if (result.warnings?.length) {
    body.innerHTML += result.warnings.map(w => `<div class="editor-status-line warning">${esc(w)}</div>`).join('')
  }
  document.querySelector('.editor-wrap')?.appendChild(div)
}

async function _editorSave() {
  const yaml = _textarea?.value?.trim()
  if (!yaml) { toast('Nothing to save', 'error'); return }
  if (_editName && _originalYaml && yaml === _originalYaml.trim()) {
    toast('No changes — pipeline is already up to date')
    _leaveEditor(_editName)
    return
  }
  try {
    if (_editName) {
      await api.pipelines.update(_editName, yaml)
      _originalYaml = yaml
      toast(`Saved ${_editName}`)
      _leaveEditor(_editName)
    } else {
      await api.pipelines.create(yaml)
      toast('Pipeline created')
      _leaveEditor()
    }
  } catch (e) {
    toast(e.message, 'error')
  }
}

async function _editorCopy() {
  const yaml = _textarea?.value
  if (!yaml) return
  const btn = document.getElementById('editor-copy-btn')
  try {
    await navigator.clipboard.writeText(yaml)
    if (btn) {
      const orig = btn.innerHTML
      btn.innerHTML = '<i class="bi bi-clipboard-check"></i>'
      setTimeout(() => { btn.innerHTML = orig }, 1500)
    }
  } catch (_) {
    toast('Copy failed — use Ctrl+A / Ctrl+C', 'error')
  }
}

function _editorDiffSaved() {
  const existing = document.getElementById('editor-inline-diff')
  if (existing) { existing.remove(); return }

  const wrap = document.createElement('div')
  wrap.id = 'editor-inline-diff'
  wrap.className = 'editor-inline-diff'
  wrap.innerHTML = `
    <div class="editor-inline-diff-bar">
      <i class="bi bi-file-diff"></i>
      <span class="editor-inline-diff-title">Changes vs saved</span>
      <span id="inline-diff-stats" class="ms-2 editor-inline-diff-stats"></span>
      <button class="editor-inline-diff-close" type="button" data-action="close-diff">✕</button>
    </div>
    <div class="editor-inline-diff-panels">
      <div class="editor-inline-diff-pane">
        <div class="editor-inline-diff-pane-header">Saved</div>
        <div id="inline-diff-left" class="editor-inline-diff-pane-body"></div>
      </div>
      <div class="editor-inline-diff-pane">
        <div class="editor-inline-diff-pane-header">Current</div>
        <div id="inline-diff-right" class="editor-inline-diff-pane-body"></div>
      </div>
    </div>`
  document.querySelector('.editor-wrap')?.appendChild(wrap)
  _renderEditorDiff(_originalYaml || '', _textarea?.value || '')
}

async function _editorDryRun() {
  const yaml = _textarea?.value?.trim()
  if (!yaml) { toast('Nothing to dry-run', 'error'); return }
  const btn = document.getElementById('editor-dry-run-btn')
  const orig = btn?.innerHTML
  if (btn) btn.innerHTML = '<i class="bi bi-hourglass"></i><span>Running…</span>'
  try {
    showDryRunResult(await api.pipelines.dryRun(yaml))
  } catch (e) {
    showDryRunResult({
      valid: false,
      issues: [e.message || 'Dry run request failed'],
    })
    toast(`Dry run: ${e.message}`, 'error')
  } finally {
    if (btn && orig) btn.innerHTML = orig
  }
}

async function _editorTestConnectors() {
  const yaml = _textarea?.value?.trim()
  if (!yaml) { toast('Nothing to test', 'error'); return }
  const btn = document.getElementById('editor-test-btn')
  const orig = btn?.innerHTML
  if (btn) btn.innerHTML = '<i class="bi bi-hourglass"></i><span>Testing…</span>'
  try {
    const result = await api.connectors.testPipeline(yaml)
    showConnectorTestResult(result)
  } catch (e) {
    toast(`Test error: ${e.message}`, 'error')
  } finally {
    if (btn && orig) btn.innerHTML = orig
  }
}

async function _editorAiExplain() {
  const el = document.getElementById('editor-ai-explain-result')
  if (!_lastDryRunErrors.length || !el) return
  setStatusMessage(el, 'Explaining…', 'info')
  try {
    const r = await api.ai.suggest({ mode: 'explain', error: _lastDryRunErrors[0], yaml: _textarea?.value })
    el.innerHTML = `<em>${esc(r.explanation || '')}</em>`
  } catch (e) {
    setStatusMessage(el, `Could not explain: ${e.message}`, 'error')
  }
}

async function _editorAiFix() {
  const el = document.getElementById('editor-ai-explain-result')
  if (!_lastDryRunErrors.length || !el) return
  setStatusMessage(el, 'Fixing…', 'info')
  try {
    const plugins = await _getPlugins()
    const r = await api.ai.suggest({ mode: 'fix', error: _lastDryRunErrors[0], yaml: _textarea?.value, plugins })
    if (!r.yaml) throw new Error('No YAML returned')
    if (_textarea) _textarea.value = r.yaml
    setStatusMessage(el, '', 'muted')
    toast('YAML fixed — review the changes')
  } catch (e) {
    setStatusMessage(el, `Could not fix: ${e.message}`, 'error')
  }
}
