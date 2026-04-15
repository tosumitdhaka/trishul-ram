import { api } from '../api.js'
import { esc, toast } from '../utils.js'

let _all = []

export async function init() {
  try {
    _all = await api.templates.list()
    _buildFilters(_all)
    _render(_all)
  } catch (e) {
    toast(`Templates error: ${e.message}`, 'error')
    document.getElementById('tpl-grid').innerHTML =
      '<div class="col-12 text-secondary text-center py-4">Failed to load templates</div>'
  }

  window._tplFilter  = () => _render(_filtered())
  window._tplPreview = (id) => _preview(id)
  window._tplDeploy  = (id) => _deploy(id)
}

function _filtered() {
  const q  = (document.getElementById('tpl-search')?.value  || '').toLowerCase()
  const src = document.getElementById('tpl-source')?.value   || ''
  const snk = document.getElementById('tpl-sink')?.value     || ''
  const sch = document.getElementById('tpl-schedule')?.value || ''
  return _all.filter(t =>
    (!q   || t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)) &&
    (!src || t.source_type === src) &&
    (!snk || t.sink_types.includes(snk)) &&
    (!sch || t.schedule_type === sch)
  )
}

function _buildFilters(templates) {
  const sources = [...new Set(templates.map(t => t.source_type).filter(Boolean))].sort()
  const sinks   = [...new Set(templates.flatMap(t => t.sink_types).filter(Boolean))].sort()

  const srcEl = document.getElementById('tpl-source')
  const snkEl = document.getElementById('tpl-sink')
  if (srcEl) sources.forEach(s => { const o = document.createElement('option'); o.value = s; o.textContent = s; srcEl.appendChild(o) })
  if (snkEl) sinks.forEach(s =>   { const o = document.createElement('option'); o.value = s; o.textContent = s; snkEl.appendChild(o) })
}

function _render(templates) {
  const grid  = document.getElementById('tpl-grid')
  const count = document.getElementById('tpl-count')
  if (!grid) return
  if (count) count.textContent = `${templates.length} template${templates.length !== 1 ? 's' : ''}`
  if (!templates.length) {
    grid.innerHTML = '<div class="col-12 text-secondary text-center py-4">No templates match your filters</div>'
    return
  }
  grid.innerHTML = templates.map(t => {
    const schedCls = { stream: 'badge-stream', interval: 'badge-interval', cron: 'badge-cron', manual: 'badge-manual' }[t.schedule_type] || 'badge-interval'
    const tags = t.tags.map(tag => `<span class="type-pill">${esc(tag)}</span>`).join(' ')
    const flow = t.sink_types.length
      ? `${esc(t.source_type)} <i class="bi bi-arrow-right" style="font-size:11px"></i> ${t.sink_types.map(esc).join(', ')}`
      : esc(t.source_type)
    return `
    <div class="col-md-6 col-lg-4">
      <div class="detail-card h-100 d-flex flex-column" style="gap:10px">
        <div class="d-flex align-items-start justify-content-between gap-2">
          <div class="fw-semibold" style="font-size:14px">${esc(t.name)}</div>
          <span class="tram-badge ${schedCls}" style="white-space:nowrap">${esc(t.schedule_type)}</span>
        </div>
        <div class="text-secondary" style="font-size:12px;flex:1">${esc(t.description || '—')}</div>
        <div style="font-size:12px;color:#8b949e">${flow}</div>
        <div class="d-flex flex-wrap gap-1">${tags}</div>
        <div class="d-flex gap-2 mt-1">
          <button class="btn btn-sm btn-primary flex-fill" onclick="window._tplDeploy('${esc(t.id)}')">
            <i class="bi bi-rocket-takeoff me-1"></i>Deploy
          </button>
          <button class="btn btn-sm btn-secondary" onclick="window._tplPreview('${esc(t.id)}')">
            <i class="bi bi-eye"></i>
          </button>
        </div>
      </div>
    </div>`
  }).join('')
}

function _preview(id) {
  const t = _all.find(t => t.id === id)
  if (!t) return
  document.getElementById('tpl-preview-title').textContent = t.name
  document.getElementById('tpl-preview-body').textContent  = t.yaml
  const deployBtn = document.getElementById('tpl-preview-deploy')
  deployBtn.onclick = () => { bootstrap.Modal.getInstance(document.getElementById('tpl-preview-modal'))?.hide(); _deploy(id) }
  new bootstrap.Modal(document.getElementById('tpl-preview-modal')).show()
}

function _deploy(id) {
  const t = _all.find(t => t.id === id)
  if (!t) return
  // Pre-fill editor with template YAML and navigate to editor
  window._editorYaml = t.yaml
  window._editorPipeline = null
  navigate('editor')
  toast(`Template "${t.name}" loaded — edit name and connection details, then save`)
}
