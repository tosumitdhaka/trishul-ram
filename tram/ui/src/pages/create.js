// ── Guided pipeline creation (#create) ────────────────────────────────────────
//
// The structured path for operators who don't write YAML: five steps
// (basics → source → sinks → schedule → review), every connector field
// rendered from /api/config/schema descriptors — no hardcoded connector
// knowledge. The final step hands off to the editor for the advanced
// sections, or saves directly after a dry-run.
//
// Stale-schema guard: the schema payload carries a schema_version content
// hash. While the wizard is open it is re-checked on a slow poll and before
// every submit; a hash mismatch (daemon upgraded underneath this tab) blocks
// saving and forces a form reload instead of submitting against a stale
// schema.

import { api } from '../api.js'
import { router } from '../router.js'
import { bindDataActions, esc, setStatusMessage, toast } from '../utils.js'

const WIZARD_PREFILL_KEY = 'tram_wizard_prefill'
const SCHEMA_POLL_MS = 60000

// Test-only override honored at the poll read site: the browser smoke suite
// (tests/browser/checks/yaml-quote.mjs) sets window.__TRAM_TEST_SCHEMA_POLL_MS__
// to observe the stale-schema poll in seconds instead of a real 60s wait.
// The key is absent outside tests, so production behavior is byte-identical
// to the SCHEMA_POLL_MS default.
function _schemaPollMs() {
  const testMs = Number(window.__TRAM_TEST_SCHEMA_POLL_MS__)
  return Number.isInteger(testMs) && testMs > 0 ? testMs : SCHEMA_POLL_MS
}

let _step = 1
let _plugins = {}
let _schema = { sources: {}, sinks: {}, serializers: {}, transforms: {} }
let _schemaVersion = null
let _schemaStale = false
let _schemaTimer = null
let _pageLeft = false
let _state = _defaultState()
let _returnTo = 'pipelines'
let _templatesLoaded = false

function _defaultState() {
  return {
    name: '',
    description: '',
    scheduleType: 'interval',
    intervalSeconds: 300,
    cronExpr: '',
    onError: 'continue',
    serializer: 'json',
    serializerOut: '',
    source: { type: '', fields: {}, extraYaml: '' },
    transforms: [],
    sinks: [],
  }
}

export async function init() {
  _step = 1
  _plugins = {}
  _schema = { sources: {}, sinks: {}, serializers: {}, transforms: {} }
  _schemaVersion = null
  _schemaStale = false
  _state = _defaultState()
  _templatesLoaded = false
  _pageLeft = false

  const { query } = router.route()
  _returnTo = ['dashboard', 'pipelines', 'detail'].includes(query.return) ? query.return : 'pipelines'

  _wireActions()
  _showStep(1)
  _hydrateInfo()

  await Promise.all([_loadPluginsAndSchema(), _checkAI()])
  _populateSelections()
  _renderSourceFields()
  _renderSinksList()
  _startSchemaPoll()

  if (query.template) await _seedFromTemplate(query.template)
  else await _populateTemplateSelect()
}

// ── Data loading ───────────────────────────────────────────────────────────────

async function _loadPluginsAndSchema() {
  try {
    const [plugins, schema] = await Promise.all([
      api.plugins().catch(() => ({ sources: [], sinks: [], serializers: [], transforms: [] })),
      api.configSchema.get().catch(() => null),
    ])
    _plugins = plugins || { sources: [], sinks: [], serializers: [], transforms: [] }
    if (schema) {
      _schema = schema
      _schemaVersion = schema.schema_version || null
    }
  } catch (_) {
    _plugins = { sources: [], sinks: [], serializers: [], transforms: [] }
  }
}

async function _populateTemplateSelect() {
  const sel = document.getElementById('wiz-template')
  if (!sel || _templatesLoaded) return
  try {
    const templates = await api.templates.list()
    if (!Array.isArray(templates) || !templates.length) return
    templates.forEach((t) => {
      const opt = document.createElement('option')
      opt.value = t.name
      opt.textContent = `${t.name}${t.description ? ` — ${t.description}` : ''}`
      sel.appendChild(opt)
    })
    _templatesLoaded = true
  } catch { /* template pre-seed is optional */ }
}

async function _seedFromTemplate(name) {
  try {
    const templates = await api.templates.list()
    const tpl = (templates || []).find(t => t.name === name)
    if (!tpl?.yaml) {
      toast(`Template '${name}' not found — starting blank`, 'warning')
      await _populateTemplateSelect()
      return
    }
    const seeded = _extractTemplateBasics(tpl.yaml)
    _state = { ..._defaultState(), ...seeded }
    _hydrateInfo()
    // Re-populate the type/serializer selects — init filled them before the
    // template state existed.
    _populateSelections()
    if (_state.source.type) _renderSourceFields()
    _renderSinksList()
    toast(`Template '${name}' loaded — review each step before saving`)
  } catch (e) {
    toast(`Template load failed: ${e.message}`, 'warning')
  }
}

// Conservative extractor: only the flat scalars the wizard itself renders.
// Transforms, workers, nested connector config and alerts stay in the
// template YAML — those continue in the editor.
function _extractTemplateBasics(yaml) {
  const out = { source: { type: '', fields: {}, extraYaml: '' }, sinks: [] }
  const lines = String(yaml || '').split('\n')
  const scalar = (raw) => {
    const v = String(raw).trim()
    if (v.startsWith('"') && v.endsWith('"')) return v.slice(1, -1)
    if (v.startsWith("'") && v.endsWith("'")) return v.slice(1, -1)
    return v
  }

  let section = null
  let sink = null
  for (const line of lines) {
    if (!line.trim() || line.trim().startsWith('#')) continue
    const indent = line.length - line.trimStart().length
    const m = line.match(/^(\s*)([A-Za-z0-9_]+):\s*(.*)$/)
    if (!m) continue
    const [, , key, restRaw] = m
    const rest = restRaw.split('#')[0].trim()

    if (indent === 0) {
      section = key
      if (key === 'sinks') out.sinks = []
      if (key === 'source') out.source = { type: '', fields: {}, extraYaml: '' }
      sink = null
      if (key === 'name' && rest) out.name = scalar(rest)
      if (key === 'description' && rest) out.description = scalar(rest)
      if (key === 'on_error' && rest) out.onError = scalar(rest)
      continue
    }

    if (section === 'schedule' && indent === 2) {
      if (key === 'type' && rest) out.scheduleType = scalar(rest)
      if (key === 'interval_seconds' && rest) out.intervalSeconds = Number(scalar(rest)) || 300
      if (key === 'cron' && rest) out.cronExpr = scalar(rest)
    } else if (section === 'source' && indent === 2) {
      if (key === 'type' && rest) { out.source.type = scalar(rest); continue }
      if (rest) out.source.fields[key] = scalar(rest)
    } else if (section === 'sinks') {
      if (line.trim().startsWith('- ')) {
        const item = line.trim().slice(2)
        const tm = item.match(/^type:\s*(.*)$/)
        sink = { type: tm ? scalar(tm[1]) : '', fields: {}, serializer_out: '', condition: '', extraYaml: '' }
        out.sinks.push(sink)
        continue
      }
      if (!sink) continue
      if (key === 'type' && rest) sink.type = scalar(rest)
      else if (key === 'condition' && rest) sink.condition = scalar(rest)
      else if (key === 'serializer_out') continue // nested — skipped conservatively
      else if (rest) sink.fields[key] = scalar(rest)
    } else if (section === 'serializer_in' && indent === 2 && key === 'type' && rest) {
      out.serializer = scalar(rest)
    } else if (section === 'serializer_out' && indent === 2 && key === 'type' && rest) {
      out.serializerOut = scalar(rest)
    }
  }
  if (!out.sinks.length) out.sinks = [{ type: '', fields: {}, serializer_out: '', condition: '', extraYaml: '' }]
  return out
}

// ── Stale-schema guard ────────────────────────────────────────────────────────

function _startSchemaPoll() {
  if (_pageLeft) return // init may resume after the operator already left (page-leave fired during the load awaits)
  if (_schemaTimer) clearInterval(_schemaTimer)
  if (_schemaVersion === null) return // backend without schema_version — nothing to compare
  _schemaTimer = setInterval(() => { void _checkSchemaFreshness() }, _schemaPollMs())
}

function _stopSchemaPoll() {
  if (_schemaTimer) { clearInterval(_schemaTimer); _schemaTimer = null }
}

async function _checkSchemaFreshness() {
  if (_schemaStale) return true
  try {
    const fresh = await api.configSchema.get()
    const freshVersion = fresh?.schema_version || null
    if (_schemaVersion !== null && freshVersion !== null && freshVersion !== _schemaVersion) {
      _schemaStale = true
      _showStaleBanner()
      return false
    }
  } catch { /* daemon unreachable — the offline banner covers this */ }
  return true
}

function _showStaleBanner() {
  document.getElementById('wiz-stale-banner')?.classList.remove('d-none')
  document.getElementById('wiz-next-btn')?.setAttribute('disabled', '')
  document.getElementById('wiz-save-btn')?.setAttribute('disabled', '')
}

function _hideStaleBanner() {
  document.getElementById('wiz-stale-banner')?.classList.add('d-none')
  document.getElementById('wiz-next-btn')?.removeAttribute('disabled')
  document.getElementById('wiz-save-btn')?.removeAttribute('disabled')
}

async function _reloadFromFreshSchema() {
  try {
    const fresh = await api.configSchema.get()
    _schema = fresh || _schema
    _schemaVersion = fresh?.schema_version || null
    _schemaStale = false
    _hideStaleBanner()
    _populateSelections()
    _renderSourceFields()
    _renderSinksList()
    toast('Form reloaded from the current connector schema — please review your fields')
  } catch (e) {
    toast(`Reload failed: ${e.message}`, 'error')
  }
}

// Wire cleanup: the router's page-leave event clears the version poll when
// the operator navigates away (same pattern as the editor's draft flush).
window.addEventListener('tram:page-leave', (e) => {
  if (e.detail?.from !== 'create') return
  _pageLeft = true
  _stopSchemaPoll()
})

// ── AI assist ────────────────────────────────────────────────────────────────

async function _checkAI() {
  try {
    const status = await api.ai.status()
    const modelEl = document.getElementById('wiz-ai-model')
    const uncfgEl = document.getElementById('wiz-ai-unconfigured')
    const genBtn = document.getElementById('wiz-ai-gen-btn')

    if (status.enabled) {
      if (modelEl) modelEl.textContent = `${status.provider} / ${status.model}`
      if (uncfgEl) uncfgEl.classList.add('d-none')
      if (genBtn) genBtn.disabled = false
    } else {
      if (modelEl) modelEl.textContent = ''
      if (uncfgEl) uncfgEl.classList.remove('d-none')
      if (genBtn) genBtn.disabled = true
    }
  } catch (_) {}
}

async function _aiGenerate() {
  const prompt = (document.getElementById('wiz-ai-prompt')?.value || '').trim()
  if (!prompt) { toast('Enter a description first', 'error'); return }
  const btn = document.getElementById('wiz-ai-gen-btn')
  if (btn) btn.disabled = true
  setStatusMessage('wiz-ai-status', 'Generating…', 'info')
  try {
    const result = await api.ai.suggest({ mode: 'generate', prompt, plugins: _plugins })
    if (!result.yaml) throw new Error('No YAML returned')
    _showStep(5)
    const textarea = document.getElementById('wiz-yaml-preview')
    if (textarea) textarea.value = result.yaml
    setStatusMessage('wiz-ai-status', '', 'muted')
    toast('YAML generated — review and save', 'info')
  } catch (e) {
    toast(`AI error: ${e.message}`, 'error')
    setStatusMessage('wiz-ai-status', '', 'muted')
  } finally {
    if (btn) btn.disabled = false
  }
}

// ── Wiring ────────────────────────────────────────────────────────────────────

function _wireActions() {
  document.getElementById('wiz-back-btn')?.addEventListener('click', _goBack)
  document.getElementById('wiz-next-btn')?.addEventListener('click', _goNext)
  document.getElementById('wiz-save-btn')?.addEventListener('click', () => { void _save() })
  document.getElementById('wiz-cancel-btn')?.addEventListener('click', _cancel)
  document.getElementById('wiz-sched-type')?.addEventListener('change', _schedChange)
  document.getElementById('wiz-src-type')?.addEventListener('change', _renderSourceFields)
  document.getElementById('wiz-test-src-btn')?.addEventListener('click', () => { void _testSrc() })
  document.getElementById('wiz-add-sink-btn')?.addEventListener('click', _addSink)
  document.getElementById('wiz-dry-run-btn')?.addEventListener('click', () => { void _dryRun() })
  document.getElementById('wiz-ai-gen-btn')?.addEventListener('click', () => { void _aiGenerate() })
  document.getElementById('wiz-open-editor-btn')?.addEventListener('click', _openEditor)
  document.getElementById('wiz-open-blank-editor-link')?.addEventListener('click', _openBlankEditor)
  document.getElementById('wiz-reload-schema-btn')?.addEventListener('click', () => { void _reloadFromFreshSchema() })
  document.getElementById('wiz-template')?.addEventListener('change', (event) => {
    if (event.target.value) { void _seedFromTemplate(event.target.value) }
  })
  document.getElementById('wiz-ai-settings-link')?.addEventListener('click', (event) => {
    event.preventDefault()
    router.navigate('settings')
  })

  // Optional-field disclosure: delegated on the wrap (fields re-render on
  // connector type changes; the wrap itself is fresh per page entry).
  document.querySelector('.wizard-wrap')?.addEventListener('click', (event) => {
    const toggle = event.target.closest('[data-advanced-toggle]')
    if (!toggle) return
    const fields = toggle.parentElement?.querySelector('[data-advanced-fields]')
    if (!fields) return
    const open = fields.classList.toggle('open')
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false')
    toggle.querySelector('span').textContent = open
      ? toggle.querySelector('span').textContent.replace('Show', 'Hide')
      : toggle.querySelector('span').textContent.replace('Hide', 'Show')
  })

  const sinksList = document.getElementById('wiz-sinks-list')
  bindDataActions(sinksList, {
    'delete-sink': (button) => {
      _deleteSink(parseInt(button.dataset.index || '', 10))
    },
    'test-sink': (button) => {
      void _testSink(parseInt(button.dataset.index || '', 10))
    },
  })
  if (sinksList?._wizardChangeListener) {
    sinksList.removeEventListener('change', sinksList._wizardChangeListener)
  }
  const changeListener = (event) => {
    const typeSelect = event.target.closest('.wiz-s-type')
    if (!typeSelect || !sinksList?.contains(typeSelect)) return
    const index = parseInt(typeSelect.closest('.wizard-sink-card')?.dataset.index || '', 10)
    if (!Number.isFinite(index)) return
    _handleSinkTypeChange(index, typeSelect.value)
  }
  sinksList?.addEventListener('change', changeListener)
  if (sinksList) sinksList._wizardChangeListener = changeListener
}

function _cancel() {
  if (_returnTo === 'dashboard') router.navigate('dashboard')
  else router.navigate('pipelines')
}

function _showStep(n) {
  _step = n
  for (let i = 1; i <= 5; i++) {
    document.getElementById(`wiz-step-${i}`)?.classList.toggle('d-none', i !== n)
  }
  document.querySelectorAll('#wiz-steps .wiz-step').forEach((el) => {
    const step = parseInt(el.dataset.step || '0', 10)
    el.classList.toggle('active', step === n)
    el.classList.toggle('done', step < n)
    el.classList.toggle('upcoming', step > n)
  })
  document.getElementById('wiz-cancel-btn')?.classList.toggle('d-none', n !== 1)
  document.getElementById('wiz-back-btn')?.classList.toggle('d-none', n === 1)
  document.getElementById('wiz-next-btn')?.classList.toggle('d-none', n === 5)
  document.getElementById('wiz-save-btn')?.classList.toggle('d-none', n !== 5)

  if (n === 2) _renderSourceFields()
  if (n === 3) _renderSinksList()
  if (n === 5) _buildReviewYaml()
}

function _goBack() {
  if (_step > 1) _showStep(_step - 1)
}

async function _goNext() {
  if (!(await _checkSchemaFreshness())) return
  if (!_collectStep(_step)) return
  _showStep(_step + 1)
}

function _hydrateInfo() {
  _setInput('wiz-name', _state.name)
  _setInput('wiz-desc', _state.description)
  _setInput('wiz-sched-type', _state.scheduleType)
  _setInput('wiz-on-error', _state.onError)
  _setInput('wiz-cron-expr', _state.cronExpr)
  const intervalVal = Math.max(1, Math.round((_state.intervalSeconds || 300) / 60))
  _setInput('wiz-interval-val', intervalVal)
  _setInput('wiz-interval-unit', '60')
  _schedChange()
}

function _populateSelections() {
  _populateSelect('wiz-src-type', _availableTypeOptions('sources'), _state.source.type, true)
  _populateSelect('wiz-serializer', _availableTypeOptions('serializers'), _state.serializer, false)
  _populateSelect('wiz-serializer-out', _availableTypeOptions('serializers'), _state.serializerOut, true)
}

function _availableTypeOptions(category) {
  const pluginItems = Array.isArray(_plugins[category]) ? _plugins[category] : []
  const schemaItems = Object.keys(_schema[category] || {})
  if (!pluginItems.length) return schemaItems.sort()
  return pluginItems.filter(item => schemaItems.includes(item)).sort()
}

function _populateSelect(id, options, selected, withBlank) {
  const el = document.getElementById(id)
  if (!el) return
  const current = selected || ''
  const blank = withBlank ? '<option value="">— select —</option>' : ''
  el.innerHTML = blank + options.map((option) => (
    `<option value="${esc(option)}"${option === current ? ' selected' : ''}>${esc(option)}</option>`
  )).join('')
  if (!withBlank && options.length && !current) {
    el.value = options.includes('json') ? 'json' : options[0]
  } else if (current) {
    el.value = current
  }
}

function _schedChange() {
  const type = document.getElementById('wiz-sched-type')?.value || 'interval'
  document.getElementById('wiz-interval-row')?.classList.toggle('d-none', type !== 'interval')
  document.getElementById('wiz-cron-row')?.classList.toggle('d-none', type !== 'cron')
}

function _collectStep(n, quiet = false) {
  if (n === 1) {
    const name = (document.getElementById('wiz-name')?.value || '').trim()
    if (!name) { if (!quiet) toast('Pipeline name is required', 'error'); return false }
    if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
      if (!quiet) toast('Name must be alphanumeric with hyphens/underscores', 'error')
      return false
    }
    _state.name = name
    _state.description = (document.getElementById('wiz-desc')?.value || '').trim()
    return true
  }

  if (n === 2) {
    const type = document.getElementById('wiz-src-type')?.value || ''
    if (!type) { if (!quiet) toast('Select a source type', 'error'); return false }
    const model = _schema.sources[type]
    const collected = _collectModelFields('wiz-src-fields', model)
    _state.source = {
      type,
      fields: collected.fields,
      extraYaml: collected.extraYaml,
    }
    _state.serializer = document.getElementById('wiz-serializer')?.value || 'json'
    if (!_markMissingRequired('wiz-src-fields', model, quiet)) return false
    return true
  }

  if (n === 3) {
    _state.serializerOut = document.getElementById('wiz-serializer-out')?.value || ''
    return _collectSinks(quiet)
  }

  if (n === 4) {
    _state.scheduleType = document.getElementById('wiz-sched-type')?.value || 'interval'
    _state.onError = document.getElementById('wiz-on-error')?.value || 'continue'
    _state.cronExpr = (document.getElementById('wiz-cron-expr')?.value || '').trim()
    const val = parseInt(document.getElementById('wiz-interval-val')?.value || '5', 10) || 5
    const unit = parseInt(document.getElementById('wiz-interval-unit')?.value || '60', 10) || 60
    _state.intervalSeconds = val * unit
    if (_state.scheduleType === 'cron' && !_state.cronExpr) {
      if (!quiet) toast('Enter a cron expression (or pick a different schedule type)', 'error')
      return false
    }
    return true
  }

  return true
}

// ── Schema-driven field rendering ────────────────────────────────────────────

function _renderSourceFields() {
  const container = document.getElementById('wiz-src-fields')
  if (!container) return
  const type = document.getElementById('wiz-src-type')?.value || _state.source.type
  if (!type) {
    container.innerHTML = ''
    return
  }
  const model = _schema.sources[type]
  const sourceState = _state.source.type === type ? _state.source : { type, fields: {}, extraYaml: '' }
  container.innerHTML = _renderModelFieldsHtml('source', type, model, sourceState)
}

function _renderModelFieldsHtml(category, type, model, statePart) {
  const fields = Array.isArray(model?.fields) ? model.fields : []
  if (!model || !fields.length) {
    const fallback = model
      ? 'No simple fields available here. Use the advanced YAML patch below or continue in the editor.'
      : `Schema unavailable for ${esc(type)}. Use the YAML editor for this connector.`
    return `
      <div class="wizard-schema-note">${fallback}</div>
      ${_extraYamlBlock(type, statePart)}`
  }

  const supported = fields.filter(field => field.kind !== 'complex')
  const omitted = fields.filter(field => field.kind === 'complex')
  const required = supported.filter(field => field.required)
  const optional = supported.filter(field => !field.required)
  const sections = []

  if (required.length) {
    sections.push(`
      <div class="col-12">
        <div class="wizard-field-group-title">Required</div>
        <div class="row g-2">${required.map(field => _fieldHtml(category, type, field, statePart.fields?.[field.name])).join('')}</div>
      </div>`)
  }

  if (optional.length) {
    sections.push(`
      <div class="col-12 mt-2">
        <button class="wizard-advanced-toggle" type="button" data-advanced-toggle="true">
          <i class="bi bi-chevron-right"></i><span>Show ${optional.length} optional field${optional.length === 1 ? '' : 's'}</span>
        </button>
        <div class="wizard-advanced-fields" data-advanced-fields="true">
          <div class="row g-2">${optional.map(field => _fieldHtml(category, type, field, statePart.fields?.[field.name])).join('')}</div>
        </div>
      </div>`)
  }

  const fieldsHtml = supported.length
    ? `<div class="row g-2">${sections.join('')}</div>`
    : '<div class="wizard-schema-note">No simple fields available here. Use the advanced YAML patch below or continue in the editor.</div>'

  const omittedHtml = omitted.length
    ? `<div class="wizard-omitted-note">
         Advanced fields omitted from the wizard: ${omitted.map(field => esc(field.name)).join(', ')}.
         Use the YAML patch below or continue in the editor.
       </div>`
    : ''

  return `
    ${fieldsHtml}
    ${omittedHtml}
    ${_extraYamlBlock(type, statePart)}`
}

function _extraYamlBlock(type, statePart) {
  return `
    <div class="wizard-extra-yaml-block mt-3">
      <label class="form-label wizard-field-label">Advanced YAML Patch <span class="wizard-optional">(optional)</span></label>
      <textarea class="form-control form-control-sm font-monospace wizard-code-input"
        data-extra-yaml="true" rows="4"
        placeholder="Add nested or unsupported fields here, relative to this block. Example:&#10;subscriptions:&#10;  - path: /interfaces/interface/state">${esc(statePart.extraYaml || '')}</textarea>
      <div class="form-text wizard-field-hint">This block is inserted under the current ${esc(type)} section with indentation preserved.</div>
    </div>`
}

function _fieldHtml(category, type, field, value) {
  const id = `${category}-${type}-${field.name}`
  const label = `<label class="form-label wizard-field-label mb-1" for="${id}">${esc(_labelize(field.name))}${field.required ? ' <span class="wizard-required">*</span>' : ''}</label>`
  const defaultText = field.default !== null && field.default !== undefined && field.default !== '' ? ` · default ${String(field.default)}` : ''
  const hint = `<div class="form-text wizard-field-hint">${esc(field.type)}${esc(defaultText)}</div>`
  const stringValue = _valueForInput(field, value)
  const widthClass = _fieldWidthClass(field)

  if (field.kind === 'select') {
    return `<div class="${widthClass}">${label}
      <select class="form-select form-select-sm" id="${id}" data-field-name="${field.name}" data-field-kind="${field.kind}">
        ${field.choices.map(choice => `<option value="${esc(choice)}"${choice === stringValue ? ' selected' : ''}>${esc(choice)}</option>`).join('')}
      </select>${hint}</div>`
  }

  if (field.kind === 'boolean') {
    return `<div class="${widthClass}">${label}
      <select class="form-select form-select-sm" id="${id}" data-field-name="${field.name}" data-field-kind="${field.kind}">
        <option value="true"${stringValue === 'true' ? ' selected' : ''}>true</option>
        <option value="false"${stringValue !== 'true' ? ' selected' : ''}>false</option>
      </select>${hint}</div>`
  }

  if (field.kind === 'list' || field.kind === 'map' || field.multiline) {
    const placeholder = field.kind === 'map'
      ? 'key: value'
      : field.kind === 'list'
        ? 'one item per line or comma-separated'
        : ''
    return `<div class="col-12">${label}
      <textarea class="form-control form-control-sm font-monospace wizard-code-input" id="${id}" data-field-name="${field.name}" data-field-kind="${field.kind}" rows="3"
        placeholder="${placeholder}">${esc(stringValue)}</textarea>${hint}</div>`
  }

  const inputType = field.secret ? 'password' : field.kind === 'integer' || field.kind === 'number' ? 'number' : 'text'
  const autoComplete = field.secret ? ' autocomplete="new-password"' : ''
  return `<div class="${widthClass}">${label}
    <input type="${inputType}"${autoComplete} class="form-control form-control-sm" id="${id}" data-field-name="${field.name}" data-field-kind="${field.kind}" value="${esc(stringValue)}">${hint}</div>`
}

function _labelize(name) {
  return String(name || '')
    .split('_')
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

function _fieldWidthClass(field) {
  if (field.secret) return 'col-12 col-md-6'
  if (field.kind === 'number' || field.kind === 'integer' || field.kind === 'boolean' || field.kind === 'select') {
    return 'col-12 col-md-4'
  }
  if (field.name.endsWith('_file') || field.name.endsWith('_path') || field.name.includes('template')) {
    return 'col-12 col-md-8'
  }
  return 'col-12 col-md-6'
}

function _valueForInput(field, value) {
  if (value === null || value === undefined) {
    if (field.default === null || field.default === undefined) return field.kind === 'boolean' ? 'false' : ''
    if (Array.isArray(field.default)) return field.default.join('\n')
    if (typeof field.default === 'object') return Object.entries(field.default).map(([key, itemValue]) => `${key}: ${itemValue}`).join('\n')
    return String(field.default)
  }
  if (Array.isArray(value)) return value.join('\n')
  if (typeof value === 'object') return Object.entries(value).map(([key, itemValue]) => `${key}: ${itemValue}`).join('\n')
  return String(value)
}

function _collectModelFields(containerId, model) {
  const container = document.getElementById(containerId)
  const fields = Array.isArray(model?.fields) ? model.fields : []
  if (!container || !model) return { fields: {}, extraYaml: '' }

  const collected = {}
  fields.filter(field => field.kind !== 'complex').forEach((field) => {
    const el = container.querySelector(`[data-field-name="${field.name}"]`)
    if (!el) return
    const parsed = _parseFieldValue(field, el.value)
    if (parsed !== undefined && parsed !== null && !(Array.isArray(parsed) && !parsed.length)) {
      if (!(typeof parsed === 'string' && parsed === '')) {
        collected[field.name] = parsed
      }
    }
  })
  const extraYaml = (container.querySelector('[data-extra-yaml="true"]')?.value || '').trim()
  return { fields: collected, extraYaml }
}

// Inline validation: required fields missing at collection time get the
// Bootstrap invalid style and a message naming them. `quiet` suppresses the
// toast/focus side effects — the review rebuild must not fire step-advance
// validation for form state the AI path never fills.
function _markMissingRequired(containerId, model, quiet = false) {
  const container = document.getElementById(containerId)
  const fields = Array.isArray(model?.fields) ? model.fields : []
  if (!container) return true
  const missing = []
  fields.filter(field => field.required && field.kind !== 'complex').forEach((field) => {
    const el = container.querySelector(`[data-field-name="${field.name}"]`)
    el?.classList.remove('is-invalid')
    if (el && !String(el.value || '').trim()) {
      el.classList.add('is-invalid')
      missing.push(_labelize(field.name))
    }
  })
  if (missing.length) {
    if (!quiet) {
      toast(`Required field${missing.length === 1 ? '' : 's'} missing: ${missing.join(', ')}`, 'error')
      container.querySelector('.is-invalid')?.focus()
    }
    return false
  }
  return true
}

function _parseFieldValue(field, raw) {
  const value = String(raw || '').trim()
  if (!value) return undefined
  if (field.kind === 'boolean') return value === 'true'
  if (field.kind === 'integer') return Number.parseInt(value, 10)
  if (field.kind === 'number') return Number(value)
  if (field.kind === 'list') {
    return value.split('\n').flatMap(line => line.split(',')).map(item => item.trim()).filter(Boolean)
  }
  if (field.kind === 'map') {
    const result = {}
    value.split('\n').forEach((line) => {
      const idx = line.indexOf(':')
      if (idx < 0) return
      const key = line.slice(0, idx).trim()
      const itemValue = line.slice(idx + 1).trim()
      if (key) result[key] = itemValue
    })
    return Object.keys(result).length ? result : undefined
  }
  return value
}

// ── Connection tests ──────────────────────────────────────────────────────────

async function _testSrc() {
  const type = document.getElementById('wiz-src-type')?.value || ''
  if (!type) { toast('Select a source type first', 'error'); return }
  const resultEl = document.getElementById('wiz-src-test-result')
  setStatusMessage(resultEl, 'Testing…', 'info')
  try {
    const model = _schema.sources[type]
    const { fields } = _collectModelFields('wiz-src-fields', model)
    const result = await api.connectors.test(type, fields)
    setStatusMessage(
      resultEl,
      result.ok
        ? `✓ ${result.detail || 'OK'}${result.latency_ms != null ? ` (${result.latency_ms}ms)` : ''}`
        : `✗ ${result.error || 'failed'}`,
      result.ok ? 'success' : 'error',
    )
  } catch (e) {
    setStatusMessage(resultEl, `✗ ${e.message}`, 'error')
  }
}

// ── Sinks ─────────────────────────────────────────────────────────────────────

async function _addSink() {
  _collectSinks()
  _state.sinks.push({ type: '', fields: {}, serializer_out: '', condition: '', extraYaml: '' })
  _renderSinksList()
}

function _collectSinks(quiet = false) {
  const list = document.getElementById('wiz-sinks-list')
  if (!list) return true
  const updated = []
  list.querySelectorAll('.wizard-sink-card').forEach((card, index) => {
    const type = card.querySelector('.wiz-s-type')?.value || ''
    const model = _schema.sinks[type]
    const collected = _collectModelFields(`wiz-sk-${index}`, model)
    updated.push({
      type,
      fields: collected.fields,
      serializer_out: card.querySelector('.wiz-s-ser')?.value || '',
      condition: (card.querySelector('.wiz-s-cond')?.value || '').trim(),
      extraYaml: collected.extraYaml,
    })
  })
  _state.sinks = updated
  if (!_state.sinks.some(sink => sink.type)) {
    if (!quiet) toast('Add at least one sink', 'error')
    return false
  }
  return true
}

function _renderSinksList() {
  const list = document.getElementById('wiz-sinks-list')
  if (!list) return
  if (!_state.sinks.length) {
    list.innerHTML = '<div class="wizard-empty-note">No sinks yet — add one to continue.</div>'
    return
  }

  const sinkTypes = _availableTypeOptions('sinks')
  const serializerTypes = _availableTypeOptions('serializers')
  list.innerHTML = _state.sinks.map((sink, index) => {
    const model = sink.type ? _schema.sinks[sink.type] : null
    return `
      <div class="wizard-sink-card mb-3" data-index="${index}">
        <div class="wizard-sink-toolbar">
          <select class="form-select form-select-sm wiz-s-type wizard-sink-type" aria-label="Sink ${index + 1} type">
            <option value="">— select —</option>
            ${sinkTypes.map(type => `<option value="${esc(type)}"${sink.type === type ? ' selected' : ''}>${esc(type)}</option>`).join('')}
          </select>
          <select class="form-select form-select-sm wiz-s-ser wizard-sink-serializer" aria-label="Sink ${index + 1} serializer">
            <option value="">default ser.</option>
            ${serializerTypes.map(type => `<option value="${esc(type)}"${sink.serializer_out === type ? ' selected' : ''}>${esc(type)}</option>`).join('')}
          </select>
          <button class="btn btn-sm btn-outline-secondary" type="button" data-action="test-sink" data-index="${index}">
            <i class="bi bi-plug"></i> Test
          </button>
          <span id="wiz-sk-test-${index}" class="wizard-inline-status" aria-live="polite"></span>
          <button class="btn-flat-danger ms-auto" type="button" data-action="delete-sink" data-index="${index}" aria-label="Remove sink ${index + 1}">
            <i class="bi bi-trash"></i>
          </button>
        </div>
        <div id="wiz-sk-${index}">
          ${sink.type ? _renderModelFieldsHtml('sink', sink.type, model, sink) : '<div class="wizard-schema-note">Select a sink type to continue.</div>'}
        </div>
        <div class="mt-2">
          <label class="form-label wizard-field-label mb-1">Condition <span class="wizard-optional">(optional)</span></label>
          <input type="text" class="form-control form-control-sm wiz-s-cond font-monospace wizard-sink-condition" value="${esc(sink.condition || '')}"
            placeholder="status == 'ok'">
        </div>
      </div>`
  }).join('')
}

function _deleteSink(index) {
  if (!Number.isFinite(index)) return
  _collectSinks()
  _state.sinks.splice(index, 1)
  _renderSinksList()
}

function _handleSinkTypeChange(index, type) {
  if (!Number.isFinite(index)) return
  _collectSinks()
  _state.sinks[index] = { type, fields: {}, serializer_out: '', condition: '', extraYaml: '' }
  _renderSinksList()
}

async function _testSink(index) {
  if (!Number.isFinite(index)) return
  _collectSinks()
  const sink = _state.sinks[index]
  if (!sink?.type) return
  const resultEl = document.getElementById(`wiz-sk-test-${index}`)
  setStatusMessage(resultEl, 'Testing…', 'info')
  try {
    const result = await api.connectors.test(sink.type, sink.fields)
    setStatusMessage(
      resultEl,
      result.ok
        ? `✓${result.latency_ms != null ? ` ${result.latency_ms}ms` : ''}`
        : `✗ ${result.error || 'fail'}`,
      result.ok ? 'success' : 'error',
    )
  } catch (e) {
    setStatusMessage(resultEl, `✗ ${e.message}`, 'error')
  }
}

// ── YAML generation ───────────────────────────────────────────────────────────

function buildYaml(state) {
  const lines = []
  // Pipeline name is a text value — always quote so a numeric-looking name
  // ("123") stays a YAML string instead of an int that Pydantic rejects.
  lines.push(`name: ${_yamlScalar(state.name, true)}`)
  if (state.description) lines.push(`description: ${_yamlScalar(state.description, true)}`)
  lines.push('schedule:')
  lines.push(`  type: ${state.scheduleType}`)
  if (state.scheduleType === 'interval') lines.push(`  interval_seconds: ${state.intervalSeconds}`)
  if (state.scheduleType === 'cron' && state.cronExpr) lines.push(`  cron: ${_yamlScalar(state.cronExpr, true)}`)
  if (state.onError && state.onError !== 'continue') lines.push(`on_error: ${state.onError}`)

  lines.push('source:')
  lines.push(`  type: ${state.source.type}`)
  _emitFields(lines, state.source.fields, 2, _schema.sources[state.source.type])
  _appendExtraYaml(lines, state.source.extraYaml, 2)

  if (state.serializer) {
    lines.push('serializer_in:')
    lines.push(`  type: ${state.serializer}`)
  }

  if (state.serializerOut) {
    lines.push('serializer_out:')
    lines.push(`  type: ${state.serializerOut}`)
  }

  if (state.transforms?.length) {
    lines.push('transforms:')
    state.transforms.forEach((transform) => {
      lines.push(`  - type: ${transform.type}`)
      _emitFields(lines, transform.fields || {}, 4, _schema.transforms[transform.type])
    })
  }

  if (state.sinks.length) {
    lines.push('sinks:')
    state.sinks.filter(sink => sink.type).forEach((sink) => {
      lines.push(`  - type: ${sink.type}`)
      _emitFields(lines, sink.fields, 4, _schema.sinks[sink.type])
      if (sink.serializer_out) {
        lines.push('    serializer_out:')
        lines.push(`      type: ${sink.serializer_out}`)
      }
      if (sink.condition) lines.push(`    condition: ${_yamlScalar(sink.condition, true)}`)
      _appendExtraYaml(lines, sink.extraYaml, 4)
    })
  }

  return lines.join('\n')
}

function _emitFields(lines, fields, indent, model) {
  const descriptors = {}
  ;(Array.isArray(model?.fields) ? model.fields : []).forEach((field) => { descriptors[field.name] = field })
  Object.entries(fields || {}).forEach(([key, value]) => {
    _emitField(lines, key, value, indent, descriptors[key])
  })
}

function _emitField(lines, key, value, indent, field) {
  if (value === undefined || value === null || value === '') return
  const pad = ' '.repeat(indent)
  // Only schema kinds that genuinely carry numbers/bools emit scalars
  // unquoted; every other kind (text/secret/select/list/map — and values
  // without a descriptor) is quoted so `password: 12345` never becomes a
  // YAML int. The string is already parsed per-kind on collection, so this
  // only controls quoting, never the value.
  const forceQuote = !field || (field.kind !== 'integer' && field.kind !== 'number' && field.kind !== 'boolean')
  if (Array.isArray(value)) {
    if (!value.length) return
    lines.push(`${pad}${key}:`)
    value.forEach((item) => {
      if (typeof item === 'object' && item !== null) {
        lines.push(`${pad}  -`)
        Object.entries(item).forEach(([childKey, childValue]) => _emitField(lines, childKey, childValue, indent + 4))
      } else {
        lines.push(`${pad}  - ${_yamlScalar(item, forceQuote)}`)
      }
    })
    return
  }
  if (typeof value === 'object') {
    if (!Object.keys(value).length) return
    lines.push(`${pad}${key}:`)
    Object.entries(value).forEach(([childKey, childValue]) => _emitField(lines, childKey, childValue, indent + 2, { kind: 'string' }))
    return
  }
  lines.push(`${pad}${key}: ${_yamlScalar(value, forceQuote)}`)
}

function _appendExtraYaml(lines, extraYaml, indent) {
  const text = (extraYaml || '').trim()
  if (!text) return
  const pad = ' '.repeat(indent)
  text.split('\n').forEach((line) => {
    lines.push(line.trim() ? `${pad}${line}` : '')
  })
}

function _yamlScalar(value, forceQuote = false) {
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') return String(value)
  const str = String(value)
  if (!forceQuote && /^-?\d+(\.\d+)?$/.test(str)) return str
  if (!forceQuote && /^(true|false|null)$/i.test(str)) return str.toLowerCase()
  return `"${str.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

// Pipeline name from saved YAML (quoted or bare) — the source of truth for
// post-save navigation. After AI assist _state.name can be empty or stale,
// but the YAML that was actually created always carries the real name.
function _yamlPipelineName(yaml) {
  const m = String(yaml || '').match(/^\s*name:\s*(.+)$/m)
  if (!m) return ''
  const raw = m[1].split('#')[0].trim()
  if ((raw.startsWith('"') && raw.endsWith('"')) || (raw.startsWith("'") && raw.endsWith("'"))) return raw.slice(1, -1)
  return raw
}

function _buildReviewYaml() {
  const textarea = document.getElementById('wiz-yaml-preview')
  if (!textarea) return
  // Quiet collects: this runs on every entry to the Review step, including
  // the AI-assist bypass, where the form steps were never filled — step
  // validation toasts must not fire for a path that never went through them.
  _collectStep(2, true)
  _collectStep(3, true)
  _collectStep(4, true)
  if (!_state.name) _collectStep(1, true)
  textarea.value = buildYaml(_state)
}

// ── Review: dry-run, save, editor hand-off ───────────────────────────────────

async function _dryRun() {
  const yaml = document.getElementById('wiz-yaml-preview')?.value?.trim()
  if (!yaml) return
  const resultEl = document.getElementById('wiz-dryrun-result')
  if (resultEl) {
    resultEl.innerHTML = _renderStatusPanel('Dry Run', '<div class="editor-status-line muted">Running…</div>', true)
  }
  try {
    const json = await api.pipelines.dryRun(yaml)
    if (!resultEl) return
    const ok = json.status === 'ok' || json.valid
    const issues = json.errors || json.issues || []
    const lines = [
      `<div class="editor-status-line ${ok ? 'success' : 'error'}">${ok ? '✓ Dry run passed' : '✗ Dry run failed'}</div>`,
      ...issues.map(issue => `<div class="editor-status-line error">${esc(issue)}</div>`),
      ...(json.warnings || []).map(warning => `<div class="editor-status-line warning">${esc(warning)}</div>`),
    ]
    resultEl.innerHTML = _renderStatusPanel('Dry Run', lines.join(''), true)
    if (!ok) _flagIssueFields(issues)
  } catch (e) {
    if (resultEl) {
      resultEl.innerHTML = _renderStatusPanel('Dry Run', `<div class="editor-status-line error">${esc(e.message)}</div>`, true)
    }
  }
}

// Map dry-run issues back to the offending wizard field where the issue text
// names it — jumps to the step that owns the field and marks the input.
function _flagIssueFields(issues) {
  const texts = (issues || []).map(i => String(i || ''))
  if (!texts.length) return
  const names = new Set()
  const markIn = (containerId, model) => {
    const container = document.getElementById(containerId)
    if (!container) return false
    const fields = Array.isArray(model?.fields) ? model.fields : []
    let hit = false
    fields.forEach((field) => {
      const el = container.querySelector(`[data-field-name="${field.name}"]`)
      el?.classList.remove('is-invalid')
      if (!el) return
      if (texts.some(t => t.includes(field.name))) {
        el.classList.add('is-invalid')
        names.add(_labelize(field.name))
        hit = true
      }
    })
    return hit
  }
  const srcModel = _schema.sources[_state.source.type]
  const srcHit = _state.source.type ? markIn('wiz-src-fields', srcModel) : false
  let sinkHit = false
  _state.sinks.forEach((sink, index) => {
    if (sink.type && markIn(`wiz-sk-${index}`, _schema.sinks[sink.type])) sinkHit = true
  })
  if (srcHit || sinkHit) {
    _showStep(2)
    _remarkInvalid(texts)
    toast(`Issue${names.size === 1 ? '' : 's'} point at field${names.size === 1 ? '' : 's'}: ${Array.from(names).join(', ')} — marked on the wiring step`, 'error')
  }
}

function _remarkInvalid(texts) {
  document.querySelectorAll('#wiz-step-2 [data-field-name], #wiz-step-3 [data-field-name]').forEach((el) => {
    const name = el.getAttribute('data-field-name')
    if (texts.some(t => t.includes(name))) el.classList.add('is-invalid')
  })
}

async function _save() {
  if (!(await _checkSchemaFreshness())) return
  const yaml = document.getElementById('wiz-yaml-preview')?.value?.trim()
  if (!yaml) { toast('No YAML to save', 'error'); return }
  const btn = document.getElementById('wiz-save-btn')
  if (btn) btn.disabled = true
  try {
    const created = await api.pipelines.create(yaml)
    // The create response carries the pipeline exactly as the backend
    // persisted it — authoritative over regex extraction (env-substituted
    // `name: ${PIPE_NAME}` and nested first-`name:` keys both land a 404
    // detail page otherwise). After AI assist the form state may be empty
    // or carry a different name than the AI output, so _state.name is the
    // last resort, never the primary source.
    const createdName = created?.name || _yamlPipelineName(yaml) || _state.name
    toast(`Pipeline '${createdName}' created`)
    router.navigate(`detail/${encodeURIComponent(createdName)}`)
  } catch (e) {
    toast(e.message, 'error')
    if (e.message) _remarkInvalid([e.message])
  } finally {
    if (btn) btn.disabled = false
  }
}

// Continue in the editor: the unsaved YAML travels via sessionStorage (never
// a window global), keyed to an explicit ?from=wizard route flag so it can't
// leak into ordinary editor entries. After AI assist the Review textarea
// holds the AI output (the form state was never filled) — hand that off
// verbatim instead of rebuilding an empty skeleton from _state.
function _openEditor() {
  const preview = (document.getElementById('wiz-yaml-preview')?.value || '').trim()
  let yaml = preview
  if (!yaml) {
    _collectStep(2, true)
    _collectStep(3, true)
    _collectStep(4, true)
    if (!_state.name) _collectStep(1, true)
    yaml = buildYaml(_state)
  }
  try { sessionStorage.setItem(WIZARD_PREFILL_KEY, yaml) } catch { /* storage full/blocked — editor starts blank */ }
  router.navigate('editor?from=wizard&return=pipelines')
}

function _openBlankEditor(event) {
  event.preventDefault()
  router.navigate('editor?return=pipelines')
}

function _setInput(id, value) {
  const el = document.getElementById(id)
  if (el && value !== undefined && value !== null) el.value = value
}

function _renderStatusPanel(title, content, scrollable = false) {
  return `
    <div class="editor-status-panel">
      <div class="editor-status-panel-header">${title}</div>
      <div class="editor-status-panel-body${scrollable ? ' editor-status-panel-body-scroll' : ''}">
        ${content}
      </div>
    </div>`
}
