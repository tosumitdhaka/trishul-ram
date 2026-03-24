import { api } from '../api.js'
import { relTime, fmtNum, statusBadge, schedBadge, esc, toast } from '../utils.js'

let _all = []

export async function init() {
  try {
    _all = await api.pipelines.list()
    renderTable(_all)
  } catch (e) {
    toast(`Pipelines error: ${e.message}`, 'error')
  }

  window._plFilter  = () => renderTable(filtered())
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
        <button class="btn-flat" title="Edit"   onclick="window._plEdit('${esc(p.name)}')"><i class="bi bi-pencil"></i></button>
        <button class="btn-flat-danger" title="Delete" onclick="window._plDelete('${esc(p.name)}')"><i class="bi bi-trash"></i></button>
      </td>
    </tr>`
  }).join('')
}
