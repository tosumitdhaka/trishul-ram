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
let _aiUndoSnapshot = null  // pre-AI text for one-level undo of the last AI write
let _baselineYaml = null   // value considered "saved" — unsaved changes are relative to this
let _draftSaveTimer = null

const DRAFT_STORAGE_KEY = 'tram_editor_draft'
const DRAFT_SAVE_DEBOUNCE_MS = 500

// Warn before the tab is closed or reloaded with unsaved edits. Hash
// navigation is covered by the draft recovery flow instead (the router
// replaces the page unconditionally).
window.addEventListener('beforeunload', (event) => {
  if (!document.getElementById('editor-textarea')) return
  if (!_hasUnsavedChanges()) return
  event.preventDefault()
  event.returnValue = ''
})

function _hasUnsavedChanges() {
  return Boolean(_textarea) && _textarea.value !== (_baselineYaml ?? '')
}

function _scheduleDraftSave() {
  clearTimeout(_draftSaveTimer)
  _draftSaveTimer = setTimeout(_saveDraftNow, DRAFT_SAVE_DEBOUNCE_MS)
}

function _saveDraftNow() {
  if (!_hasUnsavedChanges()) {
    _clearDraft()
    return
  }
  try {
    localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify({
      name: _editName ?? null,
      yaml: _textarea.value,
      savedAt: Date.now(),
    }))
  } catch (_) { /* storage full/blocked — the beforeunload guard still applies */ }
}

function _clearDraft() {
  try { localStorage.removeItem(DRAFT_STORAGE_KEY) } catch (_) { /* ignore */ }
}

function _readDraft() {
  try {
    const draft = JSON.parse(localStorage.getItem(DRAFT_STORAGE_KEY) || 'null')
    return (draft && typeof draft.yaml === 'string') ? draft : null
  } catch (_) {
    return null
  }
}

function _maybeOfferDraftRestore() {
  const bar = document.getElementById('editor-draft-bar')
  if (!bar || !_textarea) return
  const draft = _readDraft()
  // Only offer a draft that belongs to this editor context and differs from
  // what is on screen (a draft identical to the loaded YAML is stale).
  if (!draft || draft.name !== (_editName ?? null) || draft.yaml === _textarea.value) return
  const time = new Date(draft.savedAt || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const text = document.getElementById('editor-draft-text')
  if (text) text.textContent = `Unsaved draft from ${time} found`
  bar.classList.remove('d-none')
}

function _hideDraftBar() {
  document.getElementById('editor-draft-bar')?.classList.add('d-none')
}

function _restoreDraft() {
  const draft = _readDraft()
  if (!draft) { _hideDraftBar(); return }
  _textarea.value = draft.yaml
  // The restored draft replaces the AI output — retire the AI undo snapshot
  // so "Undo AI change" cannot yank the draft out from under the operator.
  _discardAiUndo()
  _hideDraftBar()
  _saveDraftNow()
  toast('Draft restored')
}

function _discardDraft() {
  _clearDraft()
  _hideDraftBar()
}

function _leaveEditor(pipelineName = null) {
  _aiUndoSnapshot = null
  clearTimeout(_draftSaveTimer)
  // Keep the draft on cancel/navigation (it is the recovery copy); on a
  // successful save _editorSave clears it explicitly first.
  _saveDraftNow()
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
  _aiUndoSnapshot = null
  _baselineYaml = null
  clearTimeout(_draftSaveTimer)
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

  // ── Reference pills from the live plugin registry ─────────────────────────
  void _renderPluginReference()

  // Baseline for unsaved-change detection: the loaded YAML in edit mode, the
  // template/preloaded YAML in new mode.
  _baselineYaml = _textarea?.value ?? ''

  // Offer recovery of an unsaved draft from a previous session.
  _maybeOfferDraftRestore()

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

  // ── Typing retires the AI undo affordance and schedules a draft save ───────
  ta?.addEventListener('input', _onEditorInput)
}

function _onEditorInput() {
  _discardAiUndo()
  _hideDraftBar()
  _scheduleDraftSave()
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
  document.getElementById('editor-ai-undo-btn')?.addEventListener('click', _undoAiChange)
  document.getElementById('editor-open-settings-link')?.addEventListener('click', (event) => {
    event.preventDefault()
    navigate('settings')
  })
  document.getElementById('editor-draft-restore')?.addEventListener('click', _restoreDraft)
  document.getElementById('editor-draft-discard')?.addEventListener('click', _discardDraft)
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

// Fill the sidebar reference pills from the live registry so they never drift
// from what the daemon actually has registered (the Plugins page shows the
// same source of truth in full detail).
async function _renderPluginReference() {
  try {
    const plugins = await _getPlugins()
    const groups = {
      'ref-sources':      plugins.sources,
      'ref-sinks':        plugins.sinks,
      'ref-serializers':  plugins.serializers,
      'ref-transforms':   plugins.transforms,
    }
    Object.entries(groups).forEach(([id, names]) => {
      const el = document.getElementById(id)
      if (!el || !Array.isArray(names) || !names.length) return
      el.innerHTML = names.map(name => `<span class="ref-pill">${esc(name)}</span>`).join('')
    })
  } catch (_) { /* reference stays empty; full details live on the Plugins page */ }
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
    _applyAiYaml(r)
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
    _applyAiYaml(r)
    setStatusMessage('editor-ai-status', '', 'muted')
    toast('Pipeline modified — review the diff and save')
  } catch (e) {
    toast(`AI error: ${e.message}`, 'error')
    setStatusMessage('editor-ai-status', '', 'muted')
  } finally {
    if (btn) btn.disabled = false
  }
}

// ── AI output application: snapshot, write, diff, validate, undo ──────────────
function _applyAiYaml(result) {
  if (_textarea) {
    _aiUndoSnapshot = _textarea.value
    _textarea.value = result.yaml
  } else {
    _aiUndoSnapshot = null
  }
  _showAiUndo()
  _showInlineDiff()
  _renderAiValidation(result)
  // The AI output is unsaved work — persist it as the recovery draft too.
  _saveDraftNow()
}

function _undoAiChange() {
  if (_aiUndoSnapshot === null) return
  if (_textarea) _textarea.value = _aiUndoSnapshot
  _aiUndoSnapshot = null
  _hideAiUndo()
  _clearAiValidation()
  // Refresh the diff only when the operator had it open, so it matches the
  // restored text instead of showing the AI output.
  if (document.getElementById('editor-inline-diff')) _showInlineDiff()
  _saveDraftNow()
  toast('AI change undone')
}

function _discardAiUndo() {
  _aiUndoSnapshot = null
  _hideAiUndo()
}

function _showAiUndo() {
  document.getElementById('editor-ai-undo')?.classList.remove('d-none')
}

function _hideAiUndo() {
  document.getElementById('editor-ai-undo')?.classList.add('d-none')
}

// Force-open (or refresh) the inline saved-vs-current diff. Unlike the
// toolbar button, this never toggles: an AI write always leaves the diff open.
function _showInlineDiff() {
  document.getElementById('editor-inline-diff')?.remove()
  _editorDiffSaved()
}

// ── AI validation result (A3 response shape: {yaml, valid, issues}) ─────────
function _renderAiValidation(result = {}) {
  const el = document.getElementById('editor-ai-validation')
  if (!el) return
  // Older backends return {yaml} only — nothing to render.
  if (result.valid === undefined) { _clearAiValidation(); return }
  const issues = Array.isArray(result.issues) ? result.issues : []
  if (result.valid) {
    el.classList.remove('d-none')
    el.innerHTML = '<div class="editor-ai-validation-ok"><i class="bi bi-check-circle me-1"></i>AI YAML passed validation</div>'
    return
  }
  el.classList.remove('d-none')
  el.innerHTML = `
    <div class="p-2 rounded editor-ai-warning">
      <div><i class="bi bi-exclamation-triangle me-1"></i>AI YAML failed validation — fix before saving:</div>
      ${issues.length ? `<ul class="editor-ai-validation-list mb-0 mt-1">${issues.map(i => `<li>${esc(i)}</li>`).join('')}</ul>` : ''}
    </div>`
}

function _clearAiValidation() {
  const el = document.getElementById('editor-ai-validation')
  if (el) { el.classList.add('d-none'); el.innerHTML = '' }
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
    _baselineYaml = _textarea.value
    _clearDraft()
    _leaveEditor(_editName)
    return
  }
  try {
    if (_editName) {
      await api.pipelines.update(_editName, yaml)
      _originalYaml = yaml
      toast(`Saved ${_editName}`)
    } else {
      await api.pipelines.create(yaml)
      toast('Pipeline created')
    }
    // The work is persisted — the recovery draft is no longer needed.
    _baselineYaml = _textarea.value
    _clearDraft()
    _leaveEditor(_editName)
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
    _applyAiYaml(r)
    setStatusMessage(el, '', 'muted')
    toast('YAML fixed — review the changes')
  } catch (e) {
    setStatusMessage(el, `Could not fix: ${e.message}`, 'error')
  }
}
