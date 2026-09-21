import { api } from '../api.js'
import { createPageController } from '../page.js'
import { bindDataActions, esc, toast } from '../utils.js'

const CATEGORY_META = {
  sources: { icon: 'box-arrow-in-right', title: 'Sources' },
  sinks: { icon: 'box-arrow-right', title: 'Sinks' },
  serializers: { icon: 'braces', title: 'Serializers' },
  transforms: { icon: 'shuffle', title: 'Transforms' },
}
const _openState = {}
let _pluginDetails = null
let _fieldMeta = null      // {sources: {type: {fields: [...]}}} from /api/config/schema — best-effort
const _sampleCache = new Map()

function _normalizeDetails(plugins) {
  const details = plugins?.details || {}
  const normalized = {}
  Object.keys(CATEGORY_META).forEach(key => {
    const existing = Array.isArray(details[key]) ? details[key] : []
    normalized[key] = existing.length
      ? existing
      : (plugins?.[key] || []).map(name => ({
          name,
          class_name: '',
          summary: name,
          description: '',
          required_fields: [],
          common_optional_fields: [],
          fields: [],
          field_count: 0,
        }))
  })
  return normalized
}

const controller = createPageController({
  page: 'plugins',
  fetch: async () => {
    const plugins = await api.plugins()
    // Field metadata is best-effort: the list view works without it.
    let schema = null
    try { schema = await api.configSchema.get() } catch { schema = null }
    return { plugins, schema }
  },
  render: ({ plugins, schema }) => {
    _pluginDetails = _normalizeDetails(plugins)
    _fieldMeta = schema
    _render()
  },
  onError: (e) => {
    const body = document.getElementById('plugins-body')
    if (body) {
      body.innerHTML = document.getElementById('plugins-error-template')?.innerHTML || ''
      const retry = document.getElementById('plugins-error-retry')
      if (retry) retry.onclick = () => { void controller.mount() }
    }
    toast(`Plugins error: ${e.message}`, 'error')
  },
})

export async function init() {
  const body = document.getElementById('plugins-body')
  if (!body) return
  document.getElementById('plugins-search')?.addEventListener('input', _render)
  bindDataActions(body, {
    'toggle-section': (button) => {
      const key = button.dataset.key
      _openState[key] = !(_openState[key] ?? false)
      _render()
    },
    'toggle-row': (button) => {
      const stateKey = `${button.dataset.key}:${button.dataset.name}`
      _openState[stateKey] = !(_openState[stateKey] ?? false)
      _render()
    },
    'copy-sample': (button) => {
      const sample = _sampleCache.get(button.dataset.stateKey)
      if (!sample) return
      void navigator.clipboard.writeText(sample)
        .then(() => toast('Sample YAML copied'))
        .catch(() => toast('Could not copy — select the text manually', 'warning'))
    },
  })

  await controller.mount()
}

function _render() {
  const body = document.getElementById('plugins-body')
  const total = document.getElementById('plugins-total-count')
  if (!body || !_pluginDetails) return

  const query = (document.getElementById('plugins-search')?.value || '').trim().toLowerCase()
  const categories = Object.keys(CATEGORY_META)
  let totalCount = 0

  categories.forEach(key => {
    document.getElementById(`plugins-count-${key}`)?.replaceChildren(document.createTextNode(String(_pluginDetails[key].length)))
  })

  const html = categories.map((key, index) => {
    const meta = CATEGORY_META[key]
    const items = _pluginDetails[key].filter(item => _matches(item, query))
    totalCount += items.length
    const isOpen = _openState[key] ?? (index === 0)

    return `
      <div class="detail-card mb-3">
        <button class="btn-flat plugins-section-toggle w-100 text-start d-flex align-items-center gap-2"
                type="button"
                data-action="toggle-section"
                data-key="${esc(key)}">
          <i class="bi bi-${meta.icon} text-secondary"></i>
          <span class="fw-semibold">${meta.title}</span>
          <span class="count-pill">${items.length}</span>
          <i class="bi bi-chevron-${isOpen ? 'down' : 'right'} ms-auto text-secondary"></i>
        </button>
        <div class="${isOpen ? '' : 'd-none'} mt-3" id="plugins-section-${key}">
          ${items.length ? _renderSectionRows(items, key) : '<div class="text-secondary text-center py-3">No plugins in this category match the filter.</div>'}
        </div>
      </div>`
  }).join('')

  if (total) total.textContent = `${totalCount} visible`
  body.innerHTML = totalCount ? html : (document.getElementById('plugins-empty-template')?.innerHTML || '')
}

function _matches(item, query) {
  if (!query) return true
  const haystack = [
    item.name,
    item.class_name,
    item.summary,
    item.description,
    ...(item.required_fields || []),
    ...(item.common_optional_fields || []),
    ...((item.fields || []).map(field => field.name)),
  ].join(' ').toLowerCase()
  return haystack.includes(query)
}

function _renderSectionRows(items, key) {
  return `
    <div class="table-responsive">
      <table class="table table-sm mb-0 plugins-table">
        <thead>
          <tr>
            <th class="plugins-col-plugin">Plugin</th>
            <th class="plugins-col-summary">Summary</th>
            <th>Config Highlights</th>
            <th class="text-end plugins-col-action"></th>
          </tr>
        </thead>
        <tbody>
          ${items.map(item => _renderRow(item, key)).join('')}
        </tbody>
      </table>
    </div>`
}

// Merge /api/config/schema metadata (choices, secret, multiline) into the
// plugin's field descriptors so rows can show enum values and mask secrets.
function _enrichedFields(key, item) {
  const fields = item.fields || []
  const metaFields = _fieldMeta?.[key]?.[item.name]?.fields
  if (!Array.isArray(metaFields)) return fields
  const byName = new Map(metaFields.map(f => [f.name, f]))
  return fields.map(f => ({ ...f, ...byName.get(f.name) }))
}

// A copy-pasteable YAML fragment built from the plugin's schema fields.
function _yamlFragment(key, item, fields) {
  const indent = (key === 'sinks' || key === 'transforms') ? '    ' : '  '
  const lines = []
  if (key === 'sources') {
    lines.push('source:')
    lines.push(`  type: ${item.name}`)
  } else if (key === 'sinks') {
    lines.push('sinks:')
    lines.push(`  - type: ${item.name}`)
  } else if (key === 'transforms') {
    lines.push('transforms:')
    lines.push(`  - type: ${item.name}`)
  } else {
    lines.push('serializer_out:')
    lines.push(`  type: ${item.name}`)
  }
  for (const f of fields) {
    if (f.secret) { lines.push(`${indent}${f.name}: "••••••"`); continue }
    const d = f.default
    if (d === null || d === undefined || d === '') {
      lines.push(`${indent}${f.name}:  # ${f.required ? 'required' : 'optional'}`)
    } else if (typeof d === 'string') {
      lines.push(`${indent}${f.name}: "${d}"`)
    } else if (typeof d === 'object') {
      lines.push(`${indent}${f.name}: ${JSON.stringify(d)}`)
    } else {
      lines.push(`${indent}${f.name}: ${d}`)
    }
  }
  if (!fields.length) lines.push(`${indent}# no extra fields — type is all it takes`)
  return lines.join('\n')
}

function _renderRow(item, key) {
  const stateKey = `${key}:${item.name}`
  const open = _openState[stateKey] ?? false
  const toggleLabel = open ? 'Collapse plugin details' : 'Expand plugin details'
  return `
    <tr>
      <td class="mono fw-semibold align-middle">${esc(item.name)}</td>
      <td class="align-middle">${esc(item.summary || item.name)}</td>
      <td class="align-middle">
        ${_chipLine('Required', item.required_fields)}
        ${_chipLine('Common optional', item.common_optional_fields)}
      </td>
      <td class="text-end align-middle">
        <button class="btn-flat"
                type="button"
                title="${toggleLabel}"
                aria-label="${toggleLabel}"
                data-action="toggle-row"
                data-key="${esc(key)}"
                data-name="${esc(item.name)}">
          <i class="bi bi-chevron-${open ? 'down' : 'right'}"></i>
        </button>
      </td>
    </tr>
    ${open ? `
      <tr class="plugins-row-detail">
        <td colspan="4">
          <div class="py-2">
            ${item.description ? `<div class="mb-2 text-secondary">${esc(item.description)}</div>` : '<div class="mb-2 text-secondary">No additional description available.</div>'}
            <div class="plugins-detail-meta">
              <div><span class="text-secondary">Class:</span> <span class="mono">${esc(item.class_name || 'n/a')}</span></div>
              <div><span class="text-secondary">Schema fields:</span> ${item.field_count}</div>
            </div>
            <div class="mt-3">
              <div class="plugins-field-heading mb-2">Available Fields</div>
              ${_renderFieldDetails(key, item)}
            </div>
            <div class="mt-3">
              <div class="plugins-field-heading mb-2">Sample Usage</div>
              ${_renderSample(key, item)}
            </div>
          </div>
        </td>
      </tr>` : ''}`
}

function _chipLine(label, values) {
  if (!values?.length) {
    return `<div><span class="text-secondary">${label}:</span> <span class="text-secondary">n/a</span></div>`
  }
  return `<div><span class="text-secondary">${label}:</span> ${values.map(value => `<span class="count-pill me-1">${esc(value)}</span>`).join('')}</div>`
}

function _renderFieldDetails(key, item) {
  const fields = _enrichedFields(key, item)
  if (!fields.length) {
    return '<div class="text-secondary">No schema-backed fields available.</div>'
  }
  return `
    <div class="table-responsive">
      <table class="table table-sm mb-0 plugins-table">
        <thead>
          <tr>
            <th class="plugins-col-plugin">Field</th>
            <th class="plugins-col-type">Type</th>
            <th class="plugins-col-required">Required</th>
            <th>Default</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>
          ${fields.map(field => `
            <tr>
              <td class="mono">${esc(field.name)}</td>
              <td>${esc(field.type || 'n/a')}${field.multiline ? '<span class="plugins-field-note">multiline</span>' : ''}</td>
              <td><span class="${field.required ? 'text-light fw-semibold' : 'text-secondary'}">${field.required ? 'yes' : 'no'}</span></td>
              <td>${_renderDefault(field)}</td>
              <td>${field.choices?.length ? `<span class="plugins-field-choices">one of ${field.choices.map(c => esc(String(c))).join(', ')}</span>` : '<span class="text-secondary">—</span>'}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`
}

function _renderDefault(field) {
  if (field.secret) {
    return '<span class="mono">••••••</span><span class="plugins-field-note">secret</span>'
  }
  const value = field.default
  if (value === null || value === undefined || value === '') {
    return '<span class="text-secondary">n/a</span>'
  }
  if (Array.isArray(value)) {
    return `<span class="mono">${esc(value.join(', '))}</span>`
  }
  if (typeof value === 'object') {
    return `<span class="mono">${esc(JSON.stringify(value))}</span>`
  }
  return `<span class="mono">${esc(String(value))}</span>`
}

function _renderSample(key, item) {
  const stateKey = `${key}:${item.name}`
  const sample = _yamlFragment(key, item, _enrichedFields(key, item))
  _sampleCache.set(stateKey, sample)
  return `
    <div class="plugins-sample">
      <div class="plugins-sample-head">
        <span class="plugins-sample-title">Snippet</span>
        <button class="btn-flat detail-action-inline" type="button"
                title="Copy sample YAML" aria-label="Copy sample YAML for ${esc(item.name)}"
                data-action="copy-sample" data-state-key="${esc(stateKey)}">
          <i class="bi bi-clipboard"></i><span>Copy</span>
        </button>
      </div>
      <pre class="plugins-sample-yaml">${esc(sample)}</pre>
    </div>`
}
