import { api } from '../api.js'
import { esc, toast } from '../utils.js'

const CATEGORY_META = {
  sources: { icon: 'box-arrow-in-right', title: 'Sources' },
  sinks: { icon: 'box-arrow-right', title: 'Sinks' },
  serializers: { icon: 'braces', title: 'Serializers' },
  transforms: { icon: 'shuffle', title: 'Transforms' },
}
const _openState = {}
let _pluginDetails = null

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

export async function init() {
  window._pluginsFilter = _render
  const body = document.getElementById('plugins-body')
  if (!body) return

  try {
    const plugins = await api.plugins()
    _pluginDetails = _normalizeDetails(plugins)
    _render()
  } catch (e) {
    body.innerHTML = document.getElementById('plugins-error-template')?.innerHTML || ''
    toast(`Plugins error: ${e.message}`, 'error')
  }
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
        <button class="btn-flat w-100 text-start d-flex align-items-center gap-2" onclick="window._pluginsToggle('${key}')">
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

  window._pluginsToggle = (key) => {
    _openState[key] = !(_openState[key] ?? false)
    _render()
  }
  window._pluginsToggleRow = (key, name) => {
    const stateKey = `${key}:${name}`
    _openState[stateKey] = !(_openState[stateKey] ?? false)
    _render()
  }
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
      <table class="table table-sm mb-0" style="font-size:12px">
        <thead>
          <tr>
            <th style="width:180px">Plugin</th>
            <th style="width:320px">Summary</th>
            <th>Config Highlights</th>
            <th class="text-end" style="width:90px"></th>
          </tr>
        </thead>
        <tbody>
          ${items.map(item => _renderRow(item, key)).join('')}
        </tbody>
      </table>
    </div>`
}

function _renderRow(item, key) {
  const stateKey = `${key}:${item.name}`
  const open = _openState[stateKey] ?? false
  const encodedKey = JSON.stringify(key)
  const encodedName = JSON.stringify(item.name)
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
        <button class="btn-flat" title="${toggleLabel}" aria-label="${toggleLabel}" onclick='window._pluginsToggleRow(${encodedKey}, ${encodedName})'>
          <i class="bi bi-chevron-${open ? 'down' : 'right'}"></i>
        </button>
      </td>
    </tr>
    ${open ? `
      <tr>
        <td colspan="4" style="background:rgba(255,255,255,0.02)">
          <div class="py-2">
            ${item.description ? `<div class="mb-2 text-secondary">${esc(item.description)}</div>` : '<div class="mb-2 text-secondary">No additional description available.</div>'}
            <div class="d-flex flex-wrap gap-3" style="font-size:12px">
              <div><span class="text-secondary">Class:</span> <span class="mono">${esc(item.class_name || 'n/a')}</span></div>
              <div><span class="text-secondary">Schema fields:</span> ${item.field_count}</div>
            </div>
            <div class="mt-3">
              <div class="text-secondary text-uppercase mb-2" style="font-size:10px;letter-spacing:.08em">Available Fields</div>
              ${_renderFieldDetails(item.fields || [])}
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

function _renderFieldDetails(fields) {
  if (!fields.length) {
    return '<div class="text-secondary">No schema-backed fields available.</div>'
  }
  return `
    <div class="table-responsive">
      <table class="table table-sm mb-0" style="font-size:12px">
        <thead>
          <tr>
            <th style="width:180px">Field</th>
            <th style="width:140px">Type</th>
            <th style="width:100px">Required</th>
            <th>Default</th>
          </tr>
        </thead>
        <tbody>
          ${fields.map(field => `
            <tr>
              <td class="mono">${esc(field.name)}</td>
              <td>${esc(field.type || 'n/a')}</td>
              <td><span class="${field.required ? 'text-light fw-semibold' : 'text-secondary'}">${field.required ? 'yes' : 'no'}</span></td>
              <td>${_renderDefault(field.default)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`
}

function _renderDefault(value) {
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
