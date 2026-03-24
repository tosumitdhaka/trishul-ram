import { api } from '../api.js'
import { relTime, fmtDur, fmtNum, statusBadge, schedBadge, esc, toast } from '../utils.js'

export async function init() {
  try {
    const [pipelines, runs] = await Promise.all([
      api.pipelines.list(),
      api.runs.list({ limit: 10 }),
    ])
    renderStats(pipelines, runs)
    renderPipelines(pipelines)
    renderRuns(runs)
  } catch (e) {
    toast(`Dashboard error: ${e.message}`, 'error')
  }
}

function renderStats(pipelines, runs) {
  const total    = pipelines.length
  const running  = pipelines.filter(p => p.status === 'running').length
  const streams  = pipelines.filter(p => p.schedule_type === 'stream' && p.status === 'running').length
  const errors   = pipelines.filter(p => p.status === 'error').length
  const failed   = runs.filter(r => r.status === 'failed').length
  const recOut   = runs.reduce((s, r) => s + (r.records_out || 0), 0)
  const enabled  = pipelines.filter(p => p.enabled).length
  const disabled = total - enabled

  set('stat-total',        total)
  set('stat-total-sub',    `${enabled} enabled · ${disabled} disabled`)
  set('stat-running',      running)
  set('stat-running-sub',  `${streams} stream · ${running - streams} interval`)
  set('stat-errors',       errors + failed)
  set('stat-errors-sub',   `${errors} pipeline errors · ${failed} run failures`)
  set('stat-records',      fmtNum(recOut))
  set('stat-records-sub',  `from last ${runs.length} runs`)
  set('dash-pipeline-count', `${total} pipelines`)
}

function renderPipelines(pipelines) {
  const tbody = document.getElementById('dash-pipelines-body')
  if (!tbody) return
  if (!pipelines.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-secondary text-center py-3">No pipelines registered</td></tr>'
    return
  }
  tbody.innerHTML = pipelines.map(p => {
    const isRunning = p.status === 'running'
    const isStream  = p.schedule_type === 'stream'
    const actionBtn = isRunning
      ? `<button class="btn-flat-danger" title="Stop" onclick="event.stopPropagation();_dashStop('${esc(p.name)}')"><i class="bi bi-stop-fill"></i></button>`
      : `<button class="btn-flat-primary" title="${isStream ? 'Start' : 'Run now'}" onclick="event.stopPropagation();_dashRun('${esc(p.name)}','${isStream}')"><i class="bi bi-play-fill"></i></button>`
    return `<tr onclick="navigate('detail');window._detailPipeline='${esc(p.name)}'">
      <td class="fw-semibold">${esc(p.name)}</td>
      <td>${schedBadge(p)}</td>
      <td>${statusBadge(p.status)}</td>
      <td class="text-secondary">${p.last_run ? relTime(p.last_run) : '—'}</td>
      <td>${actionBtn}</td>
    </tr>`
  }).join('')
}

function renderRuns(runs) {
  const tbody = document.getElementById('dash-runs-body')
  if (!tbody) return
  if (!runs.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-secondary text-center py-3">No runs yet</td></tr>'
    return
  }
  tbody.innerHTML = runs.map(r => `
    <tr>
      <td class="mono">${esc(r.pipeline)}</td>
      <td>${statusBadge(r.status)}</td>
      <td class="num-in">${fmtNum(r.records_in)}</td>
      <td class="num-out">${fmtNum(r.records_out)}</td>
      <td class="text-secondary">${fmtDur(r.started_at, r.finished_at)}</td>
    </tr>`).join('')
}

// Action handlers
window._dashStop = async (name) => {
  try { await api.pipelines.stop(name); toast(`Stopped ${name}`); init() }
  catch (e) { toast(e.message, 'error') }
}
window._dashRun = async (name, isStream) => {
  try {
    if (isStream === 'true') { await api.pipelines.start(name); toast(`Started ${name}`) }
    else { await api.pipelines.run(name); toast(`Triggered ${name}`) }
    setTimeout(init, 800)
  } catch (e) { toast(e.message, 'error') }
}

function set(id, val) {
  const el = document.getElementById(id)
  if (el) el.textContent = val
}
