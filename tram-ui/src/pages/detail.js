import { api } from '../api.js'
import { relTime, fmtDur, fmtNum, statusBadge, schedBadge, esc, toast } from '../utils.js'

let _name = null

export async function init() {
  _name = window._detailPipeline
  if (!_name) { navigate('pipelines'); return }

  // Update topbar subtitle
  const sub = document.getElementById('tb-sub')
  if (sub) sub.textContent = _name

  try {
    const [pipeline, runs] = await Promise.all([
      api.pipelines.get(_name),
      api.runs.list({ pipeline: _name, limit: 50 }),
    ])
    renderCards(pipeline)
    renderRuns(runs)
    wireActions(pipeline)
  } catch (e) {
    toast(`Detail error: ${e.message}`, 'error')
  }

  // Tab switching
  document.querySelectorAll('.nav-tabs .nav-link').forEach((tab, i) => {
    tab.addEventListener('click', e => {
      e.preventDefault()
      document.querySelectorAll('.nav-tabs .nav-link').forEach(t => t.classList.remove('active'))
      tab.classList.add('active')
      if (i === 1) loadVersions()
      if (i === 2) loadConfig()
    })
  })

  // Run filter
  document.getElementById('detail-runs-status')?.addEventListener('change', reloadRuns)
  document.getElementById('detail-runs-from')?.addEventListener('change',   reloadRuns)
}

function renderCards(p) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val }
  set('detail-source',    p.source?.type || '—')
  const sinks = Array.isArray(p.sinks) ? p.sinks.map(s => s.type || s).join(', ') : '—'
  set('detail-sinks',     sinks)
  set('detail-schedule',  p.schedule_type === 'interval' && p.interval_seconds
    ? `every ${fmtInterval(p.interval_seconds)}`
    : p.schedule_type || '—')
  set('detail-serializers', p.serializer || p.serializer_in || '—')
  const xforms = Array.isArray(p.transforms) ? p.transforms.map(t => t.type || t).join(', ') : '—'
  set('detail-transforms', xforms)
  set('detail-error',      p.error_policy || p.dlq ? 'DLQ enabled' : 'default')
}

function renderRuns(runs) {
  const tbody = document.getElementById('detail-runs-body')
  if (!tbody) return
  if (!runs.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="text-secondary text-center py-4">No runs yet</td></tr>'
    return
  }
  tbody.innerHTML = runs.map(r => `<tr>
    <td class="mono" style="font-size:11px">${esc(String(r.run_id || r.id || '').slice(0,8))}</td>
    <td class="text-secondary">${esc(r.node || '—')}</td>
    <td class="text-secondary">${r.started_at ? relTime(r.started_at) : '—'}</td>
    <td class="text-secondary">${fmtDur(r.started_at, r.finished_at)}</td>
    <td class="num-in">${fmtNum(r.records_in)}</td>
    <td class="num-out">${fmtNum(r.records_out)}</td>
    <td class="text-secondary">${fmtNum(r.records_skipped)}</td>
    <td class="text-secondary">${fmtNum(r.dlq_count)}</td>
    <td>${statusBadge(r.status)}</td>
    <td class="text-secondary" style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.error||'')}">${esc(r.error || '')}</td>
  </tr>`).join('')
}

function wireActions(pipeline) {
  const btn = document.getElementById('detail-run-btn')
  if (!btn) return
  const isRunning = pipeline.status === 'running'
  const isStream  = pipeline.schedule_type === 'stream'
  if (isRunning) {
    btn.innerHTML = '<i class="bi bi-stop-fill me-1"></i>Stop'
    btn.className = 'btn btn-sm btn-danger'
    btn.onclick = () => window._detailStop?.()
  } else {
    btn.innerHTML = `<i class="bi bi-play-fill me-1"></i>${isStream ? 'Start' : 'Run Now'}`
    btn.className = 'btn btn-sm btn-primary'
    btn.onclick = () => window._detailRun?.()
  }

  window._detailRun = async () => {
    try {
      if (isStream) { await api.pipelines.start(_name); toast(`Started ${_name}`) }
      else          { await api.pipelines.run(_name);   toast(`Triggered ${_name}`) }
      setTimeout(() => init(), 800)
    } catch (e) { toast(e.message, 'error') }
  }

  window._detailStop = async () => {
    try { await api.pipelines.stop(_name); toast(`Stopped ${_name}`); setTimeout(() => init(), 800) }
    catch (e) { toast(e.message, 'error') }
  }

  window._detailDownload = async () => {
    try {
      const p = await api.pipelines.get(_name)
      const yaml = p.yaml || p.raw || JSON.stringify(p, null, 2)
      const a = document.createElement('a')
      a.href = 'data:text/yaml;charset=utf-8,' + encodeURIComponent(yaml)
      a.download = `${_name}.yaml`
      a.click()
    } catch (e) { toast(e.message, 'error') }
  }
}

async function reloadRuns() {
  const status = document.getElementById('detail-runs-status')?.value || ''
  const from   = document.getElementById('detail-runs-from')?.value   || ''
  const params = { pipeline: _name, limit: 100 }
  if (status) params.status = status
  if (from)   params.from   = from
  try {
    const runs = await api.runs.list(params)
    renderRuns(runs)
  } catch (e) { toast(e.message, 'error') }
}

async function loadVersions() {
  const tbody = document.getElementById('detail-runs-body')
  if (!tbody) return
  tbody.innerHTML = '<tr><td colspan="10" class="text-secondary text-center py-4">Loading versions…</td></tr>'
  try {
    const versions = await api.pipelines.versions(_name)
    if (!versions?.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="text-secondary text-center py-4">No versions saved</td></tr>'
      return
    }
    tbody.innerHTML = versions.map(v => `<tr>
      <td class="mono">v${v.version}</td>
      <td class="text-secondary" colspan="7">${v.created_at ? relTime(v.created_at) : '—'}</td>
      <td>${v.active ? statusBadge('running') : ''}</td>
      <td><button class="btn-flat" onclick="window._detailRollback(${v.version})">Rollback</button></td>
    </tr>`).join('')

    window._detailRollback = async (ver) => {
      if (!confirm(`Rollback to version ${ver}?`)) return
      try { await api.pipelines.rollback(_name, ver); toast('Rolled back'); init() }
      catch (e) { toast(e.message, 'error') }
    }
  } catch (e) { toast(e.message, 'error') }
}

async function loadConfig() {
  const tbody = document.getElementById('detail-runs-body')
  if (!tbody) return
  try {
    const p = await api.pipelines.get(_name)
    const yaml = p.yaml || p.raw || JSON.stringify(p, null, 2)
    tbody.parentElement.innerHTML = `<pre class="p-3 rounded" style="background:#161b22;font-size:12px;color:#e6edf3;overflow:auto;max-height:480px">${esc(yaml)}</pre>`
  } catch (e) { toast(e.message, 'error') }
}

function fmtInterval(s) {
  if (!s) return '?'
  if (s < 60)   return `${s}s`
  if (s < 3600) return `${s / 60}m`
  return `${s / 3600}h`
}
