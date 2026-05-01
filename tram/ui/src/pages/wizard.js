import { api } from '../api.js'
import { bindDataActions, esc, setStatusMessage, toast } from '../utils.js'

let _step = 1
let _plugins = {}
let _schema = { sources: {}, sinks: {}, serializers: {}, transforms: {} }
let _state = _defaultState()

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
  _state = _defaultState()

  if (window._wizardState) {
    _state = { ..._state, ...window._wizardState }
    window._wizardState = null
  }

  _wireActions()
  _showStep(1)
  _hydrateInfo()

  await Promise.all([_loadPluginsAndSchema(), _checkAI()])
  _populateSelections()
  _renderSourceFields()
  _renderSinksList()
}

async function _loadPluginsAndSchema() {
  try {
    const [plugins, schema] = await Promise.all([
      api.plugins().catch(() => ({ sources: [], sinks: [], serializers: [], transforms: [] })),
      api.configSchema.get().catch(() => ({ sources: {}, sinks: {}, serializers: {}, transforms: {} })),
    ])
    _plugins = plugins || { sources: [], sinks: [], serializers: [], transforms: [] }
    _schema = schema || { sources: {}, sinks: {}, serializers: {}, transforms: {} }
  } catch (_) {
    _plugins = { sources: [], sinks: [], serializers: [], transforms: [] }
    _schema = { sources: {}, sinks: {}, serializers: {}, transforms: {} }
  }
}

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

function _wireActions() {
  document.getElementById('wiz-back-btn')?.addEventListener('click', _goBack)
  document.getElementById('wiz-next-btn')?.addEventListener('click', _goNext)
  document.getElementById('wiz-save-btn')?.addEventListener('click', () => { void _save() })
  document.getElementById('wiz-sched-type')?.addEventListener('change', _schedChange)
  document.getElementById('wiz-src-type')?.addEventListener('change', _renderSourceFields)
  document.getElementById('wiz-test-src-btn')?.addEventListener('click', () => { void _testSrc() })
  document.getElementById('wiz-add-sink-btn')?.addEventListener('click', _addSink)
  document.getElementById('wiz-dry-run-btn')?.addEventListener('click', () => { void _dryRun() })
  document.getElementById('wiz-ai-gen-btn')?.addEventListener('click', () => { void _aiGenerate() })
  document.getElementById('wiz-open-editor-btn')?.addEventListener('click', _openEditor)
  document.getElementById('wiz-open-blank-editor-link')?.addEventListener('click', _openBlankEditor)
  document.getElementById('wiz-ai-settings-link')?.addEventListener('click', (event) => {
    event.preventDefault()
    navigate('settings')
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
    const index = parseInt(typeSelect.closest('.wiz-sink-card')?.dataset.index || '', 10)
    if (!Number.isFinite(index)) return
    _handleSinkTypeChange(index, typeSelect.value)
  }
  sinksList?.addEventListener('change', changeListener)
  if (sinksList) sinksList._wizardChangeListener = changeListener
}

function _showStep(n) {
  _step = n
  for (let i = 1; i <= 3; i++) {
    document.getElementById(`wiz-step-${i}`)?.classList.toggle('d-none', i !== n)
  }
  document.querySelectorAll('#wiz-steps .wiz-step').forEach((el) => {
    const step = parseInt(el.dataset.step || '0', 10)
    el.classList.toggle('active', step === n)
    el.classList.toggle('done', step < n)
    el.classList.toggle('upcoming', step > n)
  })
  document.getElementById('wiz-back-btn')?.classList.toggle('d-none', n === 1)
  document.getElementById('wiz-next-btn')?.classList.toggle('d-none', n === 3)
  document.getElementById('wiz-save-btn')?.classList.toggle('d-none', n !== 3)

  if (n === 2) _renderSinksList()
  if (n === 3) _buildReviewYaml()
}

function _goBack() {
  if (_step > 1) _showStep(_step - 1)
}

function _goNext() {
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

function _collectStep(n) {
  if (n === 1) {
    const name = (document.getElementById('wiz-name')?.value || '').trim()
    if (!name) { toast('Pipeline name is required', 'error'); return false }
    if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
      toast('Name must be alphanumeric with hyphens/underscores', 'error')
      return false
    }
    _state.name = name
    _state.description = (document.getElementById('wiz-desc')?.value || '').trim()
    _state.scheduleType = document.getElementById('wiz-sched-type')?.value || 'interval'
    _state.onError = document.getElementById('wiz-on-error')?.value || 'continue'
    _state.cronExpr = (document.getElementById('wiz-cron-expr')?.value || '').trim()
    const val = parseInt(document.getElementById('wiz-interval-val')?.value || '5', 10) || 5
    const unit = parseInt(document.getElementById('wiz-interval-unit')?.value || '60', 10) || 60
    _state.intervalSeconds = val * unit
    return true
  }

  if (n === 2) {
    const type = document.getElementById('wiz-src-type')?.value || ''
    if (!type) { toast('Select a source type', 'error'); return false }
    const model = _schema.sources[type]
    const collected = _collectModelFields('wiz-src-fields', model)
    _state.source = {
      type,
      fields: collected.fields,
      extraYaml: collected.extraYaml,
    }
    _state.serializer = document.getElementById('wiz-serializer')?.value || 'json'
    _state.serializerOut = document.getElementById('wiz-serializer-out')?.value || ''
    return _collectSinks()
  }

  return true
}

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
      ? 'No simple fields available here. Use advanced YAML below or continue in the editor.'
      : `Schema unavailable for ${esc(type)}. Use the YAML editor for this connector.`
    return `
      <div class="wizard-schema-note">${fallback}</div>
      <div class="wizard-extra-yaml-block mt-3">
        <label class="form-label wizard-field-label">Advanced YAML Patch <span class="wizard-optional">(optional)</span></label>
        <textarea class="form-control form-control-sm font-monospace wizard-code-input"
          data-extra-yaml="true" rows="4"
          placeholder="Add nested or unsupported fields here, relative to this block. Example:\nsubscriptions:\n  - path: /interfaces/interface/state">${esc(statePart.extraYaml || '')}</textarea>
        <div class="form-text wizard-field-hint">This block is inserted under the current ${esc(type)} section with indentation preserved.</div>
      </div>`
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
        <div class="wizard-field-group-title">Optional</div>
        <div class="row g-2">${optional.map(field => _fieldHtml(category, type, field, statePart.fields?.[field.name])).join('')}</div>
      </div>`)
  }

  const fieldsHtml = supported.length
    ? `<div class="row g-2">${sections.join('')}</div>`
    : '<div class="wizard-schema-note">No simple fields available here. Use advanced YAML below or continue in the editor.</div>'

  const omittedHtml = omitted.length
    ? `<div class="wizard-omitted-note">
         Advanced fields omitted from the wizard: ${omitted.map(field => esc(field.name)).join(', ')}.
         Use the YAML patch below or continue in the editor.
       </div>`
    : ''

  return `
    ${fieldsHtml}
    ${omittedHtml}
    <div class="wizard-extra-yaml-block mt-3">
      <label class="form-label wizard-field-label">Advanced YAML Patch <span class="wizard-optional">(optional)</span></label>
      <textarea class="form-control form-control-sm font-monospace wizard-code-input"
        data-extra-yaml="true" rows="4"
        placeholder="Add nested or unsupported fields here, relative to this block. Example:\nsubscriptions:\n  - path: /interfaces/interface/state">${esc(statePart.extraYaml || '')}</textarea>
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
        <option value="false"${stringValue === 'false' ? ' selected' : ''}>false</option>
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
  return `<div class="${widthClass}">${label}
    <input type="${inputType}" class="form-control form-control-sm" id="${id}" data-field-name="${field.name}" data-field-kind="${field.kind}" value="${esc(stringValue)}">${hint}</div>`
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

function _addSink() {
  _collectSinks()
  _state.sinks.push({ type: '', fields: {}, serializer_out: '', condition: '', extraYaml: '' })
  _renderSinksList()
}

function _collectSinks() {
  const list = document.getElementById('wiz-sinks-list')
  if (!list) return true
  const updated = []
  list.querySelectorAll('.wiz-sink-card').forEach((card, index) => {
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
    toast('Add at least one sink', 'error')
    return false
  }
  return true
}

function _renderSinksList() {
  const list = document.getElementById('wiz-sinks-list')
  if (!list) return
  if (!_state.sinks.length) {
    list.innerHTML = '<div class="wizard-empty-note">No sinks yet. Add one or continue in the editor for advanced pipelines.</div>'
    return
  }

  const sinkTypes = _availableTypeOptions('sinks')
  const serializerTypes = _availableTypeOptions('serializers')
  list.innerHTML = _state.sinks.map((sink, index) => {
    const model = sink.type ? _schema.sinks[sink.type] : null
    return `
      <div class="wizard-sink-card mb-3" data-index="${index}">
        <div class="wizard-sink-toolbar">
          <select class="form-select form-select-sm wiz-s-type wizard-sink-type">
            <option value="">— select —</option>
            ${sinkTypes.map(type => `<option value="${esc(type)}"${sink.type === type ? ' selected' : ''}>${esc(type)}</option>`).join('')}
          </select>
          <select class="form-select form-select-sm wiz-s-ser wizard-sink-serializer">
            <option value="">default ser.</option>
            ${serializerTypes.map(type => `<option value="${esc(type)}"${sink.serializer_out === type ? ' selected' : ''}>${esc(type)}</option>`).join('')}
          </select>
          <button class="btn btn-sm btn-outline-secondary" type="button" data-action="test-sink" data-index="${index}">
            <i class="bi bi-plug"></i> Test
          </button>
          <span id="wiz-sk-test-${index}" class="wizard-inline-status" aria-live="polite"></span>
          <button class="btn-flat-danger ms-auto" type="button" data-action="delete-sink" data-index="${index}">
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

function buildYaml(state) {
  const lines = []
  lines.push(`name: ${state.name}`)
  if (state.description) lines.push(`description: ${_yamlScalar(state.description)}`)
  lines.push('schedule:')
  lines.push(`  type: ${state.scheduleType}`)
  if (state.scheduleType === 'interval') lines.push(`  interval_seconds: ${state.intervalSeconds}`)
  if (state.scheduleType === 'cron' && state.cronExpr) lines.push(`  cron: ${_yamlScalar(state.cronExpr)}`)
  if (state.onError && state.onError !== 'continue') lines.push(`on_error: ${state.onError}`)

  lines.push('source:')
  lines.push(`  type: ${state.source.type}`)
  _emitFields(lines, state.source.fields, 2)
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
      _emitFields(lines, transform.fields || {}, 4)
    })
  }

  if (state.sinks.length) {
    lines.push('sinks:')
    state.sinks.filter(sink => sink.type).forEach((sink) => {
      lines.push(`  - type: ${sink.type}`)
      _emitFields(lines, sink.fields, 4)
      if (sink.serializer_out) {
        lines.push('    serializer_out:')
        lines.push(`      type: ${sink.serializer_out}`)
      }
      if (sink.condition) lines.push(`    condition: ${_yamlScalar(sink.condition)}`)
      _appendExtraYaml(lines, sink.extraYaml, 4)
    })
  }

  return lines.join('\n')
}

function _emitFields(lines, fields, indent) {
  Object.entries(fields || {}).forEach(([key, value]) => {
    _emitField(lines, key, value, indent)
  })
}

function _emitField(lines, key, value, indent) {
  if (value === undefined || value === null || value === '') return
  const pad = ' '.repeat(indent)
  if (Array.isArray(value)) {
    if (!value.length) return
    lines.push(`${pad}${key}:`)
    value.forEach((item) => {
      if (typeof item === 'object' && item !== null) {
        lines.push(`${pad}  -`)
        Object.entries(item).forEach(([childKey, childValue]) => _emitField(lines, childKey, childValue, indent + 4))
      } else {
        lines.push(`${pad}  - ${_yamlScalar(item)}`)
      }
    })
    return
  }
  if (typeof value === 'object') {
    if (!Object.keys(value).length) return
    lines.push(`${pad}${key}:`)
    Object.entries(value).forEach(([childKey, childValue]) => _emitField(lines, childKey, childValue, indent + 2))
    return
  }
  lines.push(`${pad}${key}: ${_yamlScalar(value)}`)
}

function _appendExtraYaml(lines, extraYaml, indent) {
  const text = (extraYaml || '').trim()
  if (!text) return
  const pad = ' '.repeat(indent)
  text.split('\n').forEach((line) => {
    lines.push(line.trim() ? `${pad}${line}` : '')
  })
}

function _yamlScalar(value) {
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') return String(value)
  const str = String(value)
  if (/^-?\d+(\.\d+)?$/.test(str)) return str
  if (/^(true|false|null)$/i.test(str)) return str.toLowerCase()
  return `"${str.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`
}

function _buildReviewYaml() {
  const textarea = document.getElementById('wiz-yaml-preview')
  if (!textarea) return
  _collectStep(2)
  textarea.value = buildYaml(_state)
}

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
  } catch (e) {
    if (resultEl) {
      resultEl.innerHTML = _renderStatusPanel('Dry Run', `<div class="editor-status-line error">${esc(e.message)}</div>`, true)
    }
  }
}

async function _save() {
  const yaml = document.getElementById('wiz-yaml-preview')?.value?.trim()
  if (!yaml) { toast('No YAML to save', 'error'); return }
  const btn = document.getElementById('wiz-save-btn')
  if (btn) btn.disabled = true
  try {
    await api.pipelines.create(yaml)
    toast(`Pipeline '${_state.name}' created`)
    window._detailPipeline = _state.name
    navigate('detail')
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    if (btn) btn.disabled = false
  }
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
    _showStep(3)
    const textarea = document.getElementById('wiz-yaml-preview')
    if (textarea) textarea.value = result.yaml
    setStatusMessage('wiz-ai-status', '', 'muted')
    toast('YAML generated — review and save')
  } catch (e) {
    toast(`AI error: ${e.message}`, 'error')
    setStatusMessage('wiz-ai-status', '', 'muted')
  } finally {
    if (btn) btn.disabled = false
  }
}

function _openEditor() {
  if (_step >= 2) _collectStep(2)
  const yaml = buildYaml(_state)
  window._editorPipeline = null
  window._editorYaml = yaml
  navigate('editor')
  toast('Wizard handed off to editor for advanced configuration')
}

function _openBlankEditor(event) {
  event.preventDefault()
  window._editorPipeline = null
  window._editorYaml = null
  navigate('editor')
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
