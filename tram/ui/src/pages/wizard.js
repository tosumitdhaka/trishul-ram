import { api } from '../api.js'
import { esc, toast } from '../utils.js'

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

  _wireNav()
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

function _wireNav() {
  window._wizBack = _goBack
  window._wizNext = _goNext
  window._wizSave = _save
  window._wizSchedChange = _schedChange
  window._wizRenderSrcFields = _renderSourceFields
  window._wizTestSrc = _testSrc
  window._wizAddSink = _addSink
  window._wizDryRun = _dryRun
  window._wizAiGenerate = _aiGenerate
  window._wizOpenEditor = _openEditor
}

function _showStep(n) {
  _step = n
  for (let i = 1; i <= 3; i++) {
    document.getElementById(`wiz-step-${i}`)?.classList.toggle('d-none', i !== n)
  }
  document.querySelectorAll('#wiz-steps .wiz-step').forEach(el => {
    const s = parseInt(el.dataset.step || '0', 10)
    el.classList.toggle('active', s === n)
    el.classList.toggle('done', s < n)
    el.classList.toggle('upcoming', s > n)
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
  el.innerHTML = blank + options.map(option => (
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
  if (!model) {
    return `<div class="col-12 text-secondary" style="font-size:12px">Schema unavailable for ${esc(type)}. Use the YAML editor for this connector.</div>`
  }
  const supported = model.fields.filter(field => field.kind !== 'complex')
  const omitted = model.fields.filter(field => field.kind === 'complex')
  const required = supported.filter(field => field.required)
  const optional = supported.filter(field => !field.required)
  const sections = []

  if (required.length) {
    sections.push(`
      <div class="col-12">
        <div class="text-secondary text-uppercase mb-2" style="font-size:10px;letter-spacing:.08em">Required</div>
        <div class="row g-2">${required.map(field => _fieldHtml(category, type, field, statePart.fields?.[field.name])).join('')}</div>
      </div>`)
  }

  if (optional.length) {
    sections.push(`
      <div class="col-12 mt-2">
        <div class="text-secondary text-uppercase mb-2" style="font-size:10px;letter-spacing:.08em">Optional</div>
        <div class="row g-2">${optional.map(field => _fieldHtml(category, type, field, statePart.fields?.[field.name])).join('')}</div>
      </div>`)
  }

  const fieldsHtml = supported.length
    ? `<div class="row g-2">${sections.join('')}</div>`
    : '<div class="text-secondary" style="font-size:12px">No simple fields available here. Use advanced YAML below or continue in the editor.</div>'

  const omittedHtml = omitted.length
    ? `<div class="mt-2 text-secondary" style="font-size:11px">
         Advanced fields omitted from the wizard: ${omitted.map(field => esc(field.name)).join(', ')}.
         Use the YAML patch below or continue in the editor.
       </div>`
    : ''

  return `
    ${fieldsHtml}
    ${omittedHtml}
    <div class="col-12 mt-3">
      <label class="form-label text-secondary" style="font-size:12px">Advanced YAML Patch <span style="opacity:.6">(optional)</span></label>
      <textarea class="form-control form-control-sm font-monospace" id="${category}-${type}-extra-${Math.random().toString(36).slice(2)}"
        data-extra-yaml="true" rows="4" style="background:#0d1117;color:#e6edf3;border-color:#30363d;resize:vertical"
        placeholder="Add nested or unsupported fields here, relative to this block. Example:\nsubscriptions:\n  - path: /interfaces/interface/state">${esc(statePart.extraYaml || '')}</textarea>
      <div class="form-text" style="font-size:10px;color:#6e7681">This block is inserted under the current ${esc(type)} section with indentation preserved.</div>
    </div>`
}

function _fieldHtml(category, type, field, value) {
  const id = `${category}-${type}-${field.name}`
  const label = `<label class="form-label text-secondary mb-1" style="font-size:12px">${esc(_labelize(field.name))}${field.required ? ' <span style="color:#f85149">*</span>' : ''}</label>`
  const hint = `<div class="form-text" style="font-size:10px;color:#6e7681">${esc(field.type)}${field.default !== null && field.default !== undefined && field.default !== '' ? ` · default ${String(field.default)}` : ''}</div>`
  const stringValue = _valueForInput(field, value)
  const widthClass = _fieldWidthClass(field)
  if (field.kind === 'select') {
    return `<div class="${widthClass}">${label}
      <select class="form-select form-select-sm" id="${id}" data-field-name="${field.name}" data-field-kind="${field.kind}" style="background:#161b22;color:#e6edf3;border-color:#30363d">
        ${field.choices.map(choice => `<option value="${esc(choice)}"${choice === stringValue ? ' selected' : ''}>${esc(choice)}</option>`).join('')}
      </select>${hint}</div>`
  }
  if (field.kind === 'boolean') {
    return `<div class="${widthClass}">${label}
      <select class="form-select form-select-sm" id="${id}" data-field-name="${field.name}" data-field-kind="${field.kind}" style="background:#161b22;color:#e6edf3;border-color:#30363d">
        <option value="true"${stringValue === 'true' ? ' selected' : ''}>true</option>
        <option value="false"${stringValue === 'false' ? ' selected' : ''}>false</option>
      </select>${hint}</div>`
  }
  if (field.kind === 'list' || field.kind === 'map' || field.multiline) {
    return `<div class="col-12">${label}
      <textarea class="form-control form-control-sm font-monospace" id="${id}" data-field-name="${field.name}" data-field-kind="${field.kind}" rows="3" style="background:#0d1117;color:#e6edf3;border-color:#30363d;resize:vertical"
        placeholder="${field.kind === 'map' ? 'key: value' : field.kind === 'list' ? 'one item per line or comma-separated' : ''}">${esc(stringValue)}</textarea>${hint}</div>`
  }
  const inputType = field.secret ? 'password' : field.kind === 'integer' || field.kind === 'number' ? 'number' : 'text'
  return `<div class="${widthClass}">${label}
    <input type="${inputType}" class="form-control form-control-sm" id="${id}" data-field-name="${field.name}" data-field-kind="${field.kind}" value="${esc(stringValue)}"
      style="background:#161b22;color:#e6edf3;border-color:#30363d">${hint}</div>`
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
    if (typeof field.default === 'object') return Object.entries(field.default).map(([k, v]) => `${k}: ${v}`).join('\n')
    return String(field.default)
  }
  if (Array.isArray(value)) return value.join('\n')
  if (typeof value === 'object') return Object.entries(value).map(([k, v]) => `${k}: ${v}`).join('\n')
  return String(value)
}

function _collectModelFields(containerId, model) {
  const container = document.getElementById(containerId)
  if (!container || !model) return { fields: {}, extraYaml: '' }
  const fields = {}
  model.fields.filter(field => field.kind !== 'complex').forEach(field => {
    const el = container.querySelector(`[data-field-name="${field.name}"]`)
    if (!el) return
    const parsed = _parseFieldValue(field, el.value)
    if (parsed !== undefined && parsed !== null && !(Array.isArray(parsed) && !parsed.length)) {
      if (!(typeof parsed === 'string' && parsed === '')) {
        fields[field.name] = parsed
      }
    }
  })
  const extraYaml = (container.querySelector('[data-extra-yaml="true"]')?.value || '').trim()
  return { fields, extraYaml }
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
    value.split('\n').forEach(line => {
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
  if (resultEl) resultEl.textContent = 'Testing…'
  try {
    const model = _schema.sources[type]
    const { fields } = _collectModelFields('wiz-src-fields', model)
    const result = await api.connectors.test(type, fields)
    if (resultEl) {
      resultEl.style.color = result.ok ? '#3fb950' : '#f85149'
      resultEl.textContent = result.ok
        ? `✓ ${result.detail || 'OK'}${result.latency_ms != null ? ` (${result.latency_ms}ms)` : ''}`
        : `✗ ${result.error || 'failed'}`
    }
  } catch (e) {
    if (resultEl) {
      resultEl.style.color = '#f85149'
      resultEl.textContent = `✗ ${e.message}`
    }
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
    list.innerHTML = '<div class="text-secondary text-center py-3" style="font-size:13px">No sinks yet. Add one or continue in the editor for advanced pipelines.</div>'
    return
  }

  const sinkTypes = _availableTypeOptions('sinks')
  const serializerTypes = _availableTypeOptions('serializers')
  list.innerHTML = _state.sinks.map((sink, index) => {
    const model = sink.type ? _schema.sinks[sink.type] : null
    return `
      <div class="wiz-sink-card mb-3 p-3 rounded" style="background:#161b22;border:1px solid #30363d">
        <div class="d-flex gap-2 align-items-center mb-2 flex-wrap">
          <select class="form-select form-select-sm wiz-s-type" style="width:180px;background:#0d1117;color:#e6edf3;border-color:#30363d"
                  onchange="window._wizChangeSinkType?.(${index}, this.value)">
            <option value="">— select —</option>
            ${sinkTypes.map(type => `<option value="${esc(type)}"${sink.type === type ? ' selected' : ''}>${esc(type)}</option>`).join('')}
          </select>
          <select class="form-select form-select-sm wiz-s-ser" style="width:140px;background:#0d1117;color:#e6edf3;border-color:#30363d">
            <option value="">default ser.</option>
            ${serializerTypes.map(type => `<option value="${esc(type)}"${sink.serializer_out === type ? ' selected' : ''}>${esc(type)}</option>`).join('')}
          </select>
          <button class="btn btn-sm btn-outline-secondary" onclick="window._wizTestSink?.(${index})"><i class="bi bi-plug"></i> Test</button>
          <span id="wiz-sk-test-${index}" style="font-size:11px"></span>
          <button class="btn-flat-danger ms-auto" onclick="window._wizDeleteSink?.(${index})"><i class="bi bi-trash"></i></button>
        </div>
        <div id="wiz-sk-${index}">
          ${sink.type ? _renderModelFieldsHtml('sink', sink.type, model, sink) : '<div class="text-secondary" style="font-size:12px">Select a sink type to continue.</div>'}
        </div>
        <div class="mt-2">
          <label class="form-label text-secondary mb-1" style="font-size:12px">Condition <span style="opacity:.6">(optional)</span></label>
          <input type="text" class="form-control form-control-sm wiz-s-cond font-monospace" value="${esc(sink.condition || '')}"
            placeholder="status == 'ok'" style="background:#0d1117;color:#e6edf3;border-color:#30363d;font-size:11px">
        </div>
      </div>`
  }).join('')

  window._wizDeleteSink = (index) => {
    _collectSinks()
    _state.sinks.splice(index, 1)
    _renderSinksList()
  }
  window._wizChangeSinkType = (index, type) => {
    _collectSinks()
    _state.sinks[index] = { type, fields: {}, serializer_out: '', condition: '', extraYaml: '' }
    _renderSinksList()
  }
  window._wizTestSink = async (index) => {
    _collectSinks()
    const sink = _state.sinks[index]
    if (!sink?.type) return
    const resEl = document.getElementById(`wiz-sk-test-${index}`)
    if (resEl) resEl.textContent = 'Testing…'
    try {
      const result = await api.connectors.test(sink.type, sink.fields)
      if (resEl) {
        resEl.style.color = result.ok ? '#3fb950' : '#f85149'
        resEl.textContent = result.ok ? `✓${result.latency_ms != null ? ` ${result.latency_ms}ms` : ''}` : `✗ ${result.error || 'fail'}`
      }
    } catch (e) {
      if (resEl) {
        resEl.style.color = '#f85149'
        resEl.textContent = `✗ ${e.message}`
      }
    }
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
    state.transforms.forEach(transform => {
      lines.push(`  - type: ${transform.type}`)
      _emitFields(lines, transform.fields || {}, 4)
    })
  }

  if (state.sinks.length) {
    lines.push('sinks:')
    state.sinks.filter(sink => sink.type).forEach(sink => {
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
    value.forEach(item => {
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
  text.split('\n').forEach(line => {
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
  const ta = document.getElementById('wiz-yaml-preview')
  if (!ta) return
  _collectStep(2)
  ta.value = buildYaml(_state)
}

async function _dryRun() {
  const yaml = document.getElementById('wiz-yaml-preview')?.value?.trim()
  if (!yaml) return
  const resultEl = document.getElementById('wiz-dryrun-result')
  if (resultEl) resultEl.innerHTML = '<span class="text-secondary" style="font-size:12px">Running…</span>'
  try {
    const json = await api.pipelines.dryRun(yaml)
    if (!resultEl) return
    const ok = json.status === 'ok' || json.valid
    const issues = json.errors || json.issues || []
    let html = `<div style="font-size:12px;padding:10px;border-radius:4px;background:${ok ? '#1a3328' : '#3d1a1a'};border:1px solid ${ok ? '#3fb950' : '#f85149'}">`
    html += `<div style="color:${ok ? '#3fb950' : '#f85149'};margin-bottom:4px">${ok ? '✓ Dry run passed' : '✗ Dry run failed'}</div>`
    if (issues.length) html += issues.map(issue => `<div style="color:#f85149">${esc(issue)}</div>`).join('')
    if (json.warnings?.length) html += json.warnings.map(warning => `<div style="color:#e3b341">${esc(warning)}</div>`).join('')
    html += '</div>'
    resultEl.innerHTML = html
  } catch (e) {
    if (resultEl) resultEl.innerHTML = `<div style="color:#f85149;font-size:12px">${esc(e.message)}</div>`
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
  const status = document.getElementById('wiz-ai-status')
  if (btn) btn.disabled = true
  if (status) status.textContent = 'Generating…'
  try {
    const result = await api.ai.suggest({ mode: 'generate', prompt, plugins: _plugins })
    if (!result.yaml) throw new Error('No YAML returned')
    _showStep(3)
    const ta = document.getElementById('wiz-yaml-preview')
    if (ta) ta.value = result.yaml
    if (status) status.textContent = ''
    toast('YAML generated — review and save')
  } catch (e) {
    toast(`AI error: ${e.message}`, 'error')
    if (status) status.textContent = ''
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

function _setInput(id, value) {
  const el = document.getElementById(id)
  if (el && value !== undefined && value !== null) el.value = value
}
