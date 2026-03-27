import { api } from '../api.js'
import { relTime, fmtNum, statusBadge, schedBadge, esc, toast } from '../utils.js'

let _all = []

export async function init() {
  // Load templates when modal opens; reset to list view on each open
  document.getElementById('pl-templates-modal')?.addEventListener('show.bs.modal', () => {
    document.getElementById('pl-tpl-list-view').style.display = ''
    document.getElementById('pl-tpl-yaml-view').style.display = 'none'
    _openTemplates()
  })
  window._plReload  = async () => {
    try { await api.pipelines.reload(); toast('Pipelines reloaded'); _all = await api.pipelines.list(); renderTable(filtered()) }
    catch (e) { toast(e.message, 'error') }
  }
  window._plStart   = async (name) => { try { await api.pipelines.start(name);  toast(`Started ${name}`);  refresh() } catch (e) { toast(e.message, 'error') } }
  window._plStop    = async (name) => { try { await api.pipelines.stop(name);   toast(`Stopped ${name}`);  refresh() } catch (e) { toast(e.message, 'error') } }
  window._plRun     = async (name) => { try { await api.pipelines.run(name);    toast(`Triggered ${name}`); setTimeout(refresh, 800) } catch (e) { toast(e.message, 'error') } }
  window._plEdit    = (name) => { window._editorPipeline = name; navigate('editor') }
  window._plDelete  = async (name) => {
    if (!confirm(`Delete pipeline "${name}"?`)) return
    try { await api.pipelines.delete(name); toast(`Deleted ${name}`); _all = _all.filter(p => p.name !== name); renderTable(filtered()) }
    catch (e) { toast(e.message, 'error') }
  }
  window._plDownload = async (name) => {
    try {
      const p = await api.pipelines.get(name)
      const yaml = p.yaml || p.raw || JSON.stringify(p, null, 2)
      const a = document.createElement('a')
      a.href = 'data:text/yaml;charset=utf-8,' + encodeURIComponent(yaml)
      a.download = `${name}.yaml`
      a.click()
    } catch (e) { toast(e.message, 'error') }
  }

  try {
    _all = await api.pipelines.list()
    renderTable(_all)
  } catch (e) {
    toast(`Pipelines error: ${e.message}`, 'error')
  }

  window._plFilter  = () => renderTable(filtered())

  window._plImportFile = async (input) => {
    const file = input.files[0]
    if (!file) return
    input.value = ''
    const yaml = await file.text()
    const name = _extractName(yaml)
    const exists = _all.some(p => p.name === name)
    if (!exists) {
      try { await api.pipelines.create(yaml); toast(`Imported ${name}`); await refresh() }
      catch (e) { toast(e.message, 'error') }
      return
    }
    // Conflict — show modal
    document.getElementById('pl-import-name').textContent = name
    const modal = new bootstrap.Modal(document.getElementById('pl-import-modal'))
    document.getElementById('pl-import-replace').onclick = async () => {
      modal.hide()
      try { await api.pipelines.update(name, yaml); toast(`Replaced ${name} (new version saved)`); await refresh() }
      catch (e) { toast(e.message, 'error') }
    }
    document.getElementById('pl-import-rename').onclick = async () => {
      const newName = document.getElementById('pl-import-newname').value.trim()
      if (!newName) { toast('Enter a new name', 'error'); return }
      modal.hide()
      const patched = _patchName(yaml, newName)
      try { await api.pipelines.create(patched); toast(`Imported as ${newName}`); await refresh() }
      catch (e) { toast(e.message, 'error') }
    }
    modal.show()
  }
}

// ── Template modal ───────────────────────────────────────────────────────────

let _templates = []

async function _openTemplates() {
  const tbody = document.getElementById('pl-tpl-body')
  if (tbody) tbody.innerHTML = '<tr><td colspan="5" class="text-secondary text-center py-4">Loading…</td></tr>'
  try {
    const raw = await api.templates.list()
    // Validate: must have source_type; dedup by name
    const seen = new Set()
    _templates = raw.filter(t => {
      if (!t.source_type) return false
      if (seen.has(t.name)) return false
      seen.add(t.name)
      return true
    })
    _renderTemplates(_templates)
  } catch (e) {
    toast(e.message, 'error')
  }

  window._tplSearch = () => {
    const q = (document.getElementById('pl-tpl-search')?.value || '').toLowerCase()
    _renderTemplates(q
      ? _templates.filter(t => t.name.toLowerCase().includes(q) || (t.description || '').toLowerCase().includes(q))
      : _templates)
  }
}

function _renderTemplates(tpls) {
  const tbody = document.getElementById('pl-tpl-body')
  const count = document.getElementById('pl-tpl-count')
  if (!tbody) return
  if (count) count.textContent = `${tpls.length} template${tpls.length !== 1 ? 's' : ''}`
  if (!tpls.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-secondary text-center py-4">No templates match</td></tr>'
    return
  }
  const schedCls = { stream: 'badge-stream', interval: 'badge-interval', cron: 'badge-cron', manual: 'badge-manual' }
  tbody.innerHTML = tpls.map(t => {
    const flow = t.sink_types?.length
      ? `${esc(t.source_type)} → ${t.sink_types.map(esc).join(', ')}`
      : esc(t.source_type)
    const cls  = schedCls[t.schedule_type] || 'badge-interval'
    return `<tr>
      <td class="fw-semibold">${esc(t.name)}</td>
      <td class="mono" style="font-size:12px">${flow}</td>
      <td><span class="tram-badge ${cls}">${esc(t.schedule_type)}</span></td>
      <td class="text-secondary" style="font-size:12px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${esc(t.description || '')}">${esc(t.description || '—')}</td>
      <td class="text-end" style="white-space:nowrap">
        <button class="btn-flat" title="View YAML" onclick="window._tplViewYaml('${esc(t.id)}')">
          <i class="bi bi-eye"></i>
        </button>
        <button class="btn btn-sm btn-primary" style="font-size:11px;padding:2px 10px"
                onclick="window._tplDeploy('${esc(t.id)}')">
          <i class="bi bi-rocket-takeoff me-1"></i>Deploy
        </button>
      </td>
    </tr>`
  }).join('')

  window._tplViewYaml = (id) => {
    const t = _templates.find(x => x.id === id)
    if (!t) return
    document.getElementById('pl-tpl-yaml-name').textContent = t.name
    document.getElementById('pl-tpl-yaml-body').textContent = t.yaml || ''
    window._tplDeployFromView = () => _doTplDeploy(t)
    document.getElementById('pl-tpl-list-view').style.display = 'none'
    document.getElementById('pl-tpl-yaml-view').style.display = ''
  }

  window._tplBackToList = () => {
    document.getElementById('pl-tpl-list-view').style.display = ''
    document.getElementById('pl-tpl-yaml-view').style.display = 'none'
  }

  window._tplDeploy = (id) => {
    const t = _templates.find(x => x.id === id)
    if (t) _doTplDeploy(t)
  }
}

function _doTplDeploy(t) {
  // Clean up Bootstrap modal state before navigating (backdrop stays on body otherwise)
  document.querySelectorAll('.modal-backdrop').forEach(el => el.remove())
  document.body.classList.remove('modal-open')
  document.body.style.removeProperty('overflow')
  document.body.style.removeProperty('padding-right')
  window._editorYaml      = t.yaml
  window._editorPipeline  = null
  navigate('editor')
  toast(`Template "${t.name}" loaded — edit name and connection details, then save`)
}

async function refresh() {
  _all = await api.pipelines.list()
  renderTable(filtered())
}

function filtered() {
  const q   = (document.getElementById('pl-search')?.value  || '').toLowerCase()
  const st  = document.getElementById('pl-status')?.value  || ''
  const ty  = document.getElementById('pl-type')?.value    || ''
  return _all.filter(p =>
    (!q  || p.name.toLowerCase().includes(q)) &&
    (!st || p.status === st) &&
    (!ty || p.schedule_type === ty)
  )
}

function _extractName(yaml) {
  const m = yaml.match(/^\s*name:\s*(\S+)/m)
  return m ? m[1] : 'unknown'
}

function _patchName(yaml, newName) {
  return yaml.replace(/^(\s*name:\s*)\S+/m, `$1${newName}`)
}

function renderTable(pipelines) {
  const tbody = document.getElementById('pl-body')
  if (!tbody) return
  if (!pipelines.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-secondary text-center py-4">No pipelines found</td></tr>'
    return
  }
  tbody.innerHTML = pipelines.map(p => {
    const isRunning = p.status === 'running'
    const isStream  = p.schedule_type === 'stream'
    const actionBtn = isRunning
      ? `<button class="btn-flat-danger" title="Stop"     onclick="window._plStop('${esc(p.name)}')"><i class="bi bi-stop-fill"></i></button>`
      : isStream
        ? `<button class="btn-flat-primary" title="Start"  onclick="window._plStart('${esc(p.name)}')"><i class="bi bi-play-fill"></i></button>`
        : `<button class="btn-flat-primary" title="Run now" onclick="window._plRun('${esc(p.name)}')"><i class="bi bi-play-fill"></i></button>`
    const sinks = Array.isArray(p.sinks) ? p.sinks.map(s => esc(s.type || s)).join(', ') : '—'
    return `<tr onclick="navigate('detail');window._detailPipeline='${esc(p.name)}'" style="cursor:pointer">
      <td class="fw-semibold">${esc(p.name)}</td>
      <td class="text-secondary">${esc(p.source?.type || '—')}</td>
      <td class="text-secondary">${sinks}</td>
      <td>${schedBadge(p)}</td>
      <td>${statusBadge(p.status)}</td>
      <td class="text-secondary">${p.last_run ? relTime(p.last_run) : '—'}</td>
      <td>${p.last_run_status ? statusBadge(p.last_run_status) : '—'}</td>
      <td class="text-end" onclick="event.stopPropagation()">
        ${actionBtn}
        <button class="btn-flat" title="Edit"     onclick="window._plEdit('${esc(p.name)}')"><i class="bi bi-pencil"></i></button>
        <button class="btn-flat" title="Export YAML" onclick="window._plDownload('${esc(p.name)}')"><i class="bi bi-download"></i></button>
        <button class="btn-flat-danger" title="Delete" onclick="window._plDelete('${esc(p.name)}')"><i class="bi bi-trash"></i></button>
      </td>
    </tr>`
  }).join('')
}
