import { api } from '../api.js'
import { relTime, fmtDur, fmtNum, statusBadge, schedBadge, esc, toast } from '../utils.js'

const POLL_INTERVAL = 10_000  // 10 s

let _pollTimer  = null
let _runsCache  = null

export async function init() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }

  await _refresh()

  _pollTimer = setInterval(async () => {
    if (!document.getElementById('dash-sparkline')) { clearInterval(_pollTimer); _pollTimer = null; return }
    await _refreshStats()
  }, POLL_INTERVAL)
}

async function _refresh() {
  try {
    const [stats, runs] = await Promise.all([
      api.stats.get(),
      api.runs.list({ limit: 10 }),
    ])
    _runsCache = runs
    _renderStatCards(stats)
    _renderSparkline(stats.sparkline || [])
    _renderPipelines(stats.per_pipeline || [])
    _renderRuns(runs)
    _setLiveDot(true)
  } catch (e) {
    toast(`Dashboard error: ${e.message}`, 'error')
    _setLiveDot(false)
  }
}

async function _refreshStats() {
  try {
    const stats = await api.stats.get()
    _renderStatCards(stats)
    _renderSparkline(stats.sparkline || [])
    _renderPipelines(stats.per_pipeline || [])
    _setLiveDot(true)
  } catch (_) {
    _setLiveDot(false)
  }
}

// ── Stat cards ───────────────────────────────────────────────────────────────

function _renderStatCards(s) {
  const active = (s.pipelines_running || 0) + (s.pipelines_scheduled || 0)
  _set('stat-total',       s.pipelines_total ?? '—')
  _set('stat-total-sub',   `${s.pipelines_running ?? 0} running · ${s.pipelines_scheduled ?? 0} scheduled`)
  _set('stat-running',     active)
  _set('stat-running-sub', `${s.pipelines_error ?? 0} error · ${s.runs_today ?? 0} runs today`)
  _set('stat-records',     fmtNum(s.records_out_last_15m))
  _set('stat-records-sub', `${fmtNum(s.records_in_last_15m)} in · avg ${s.avg_duration_last_hour_s != null ? s.avg_duration_last_hour_s + 's' : '—'}`)

  const errEl = document.getElementById('stat-errors')
  if (errEl) {
    errEl.textContent  = s.errors_last_15m ?? '—'
    errEl.style.color  = (s.errors_last_15m || 0) > 0 ? '#f85149' : '#3fb950'
  }
  _set('stat-errors-sub',      `runs last hour: ${s.runs_last_hour ?? 0}`)
  _set('dash-pipeline-count',  `${s.pipelines_total ?? 0} pipelines`)
}

// ── Sparkline (Canvas API, no dependencies) ──────────────────────────────────

function _renderSparkline(buckets) {
  const canvas = document.getElementById('dash-sparkline')
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  const dpr = window.devicePixelRatio || 1
  const W   = canvas.parentElement?.clientWidth - 32 || 600
  const H   = 48

  canvas.width  = W * dpr
  canvas.height = H * dpr
  canvas.style.width  = W + 'px'
  canvas.style.height = H + 'px'
  ctx.scale(dpr, dpr)
  ctx.clearRect(0, 0, W, H)

  if (!buckets.length) return

  const vals   = buckets.map(b => b.records_out || 0)
  const maxVal = Math.max(...vals, 1)
  const n      = vals.length
  const gap    = 3
  const barW   = Math.max(4, (W - gap * (n - 1)) / n)

  vals.forEach((v, i) => {
    const barH  = Math.max(2, (v / maxVal) * (H - 4))
    const x     = i * (barW + gap)
    const y     = H - barH
    const alpha = 0.35 + 0.65 * (i / Math.max(n - 1, 1))
    ctx.fillStyle = v > 0 ? `rgba(63,185,80,${alpha})` : 'rgba(110,118,129,0.25)'
    ctx.beginPath()
    ctx.roundRect?.(x, y, barW, barH, 2) ?? ctx.rect(x, y, barW, barH)
    ctx.fill()
  })

  const total = vals.reduce((a, b) => a + b, 0)
  _set('spark-meta', total > 0 ? `${fmtNum(total)} records in last hour` : 'no records in last hour')
}

// ── Pipeline table ────────────────────────────────────────────────────────────

function _renderPipelines(perPipeline) {
  const tbody = document.getElementById('dash-pipelines-body')
  if (!tbody) return
  if (!perPipeline.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="text-secondary text-center py-3">No pipelines registered</td></tr>'
    return
  }
  tbody.innerHTML = perPipeline.map(p => {
    const isRunning = p.status === 'running' || p.status === 'scheduled'
    const stopBtn  = `<button class="btn-flat-danger" title="Stop" ${isRunning ? '' : 'disabled style="opacity:.35"'}
      onclick="event.stopPropagation();window._dashStop('${esc(p.name)}')"><i class="bi bi-stop-fill"></i></button>`
    const startBtn = `<button class="btn-flat-primary" title="Start" ${isRunning ? 'disabled style="opacity:.35"' : ''}
      onclick="event.stopPropagation();window._dashStart('${esc(p.name)}')"><i class="bi bi-play-fill"></i></button>`
    const dlBtn    = `<button class="btn-flat" title="Download YAML" onclick="event.stopPropagation();window._dashDownload('${esc(p.name)}')"><i class="bi bi-download"></i></button>`
    const errStyle = p.errors > 0 ? 'color:#f85149' : ''
    return `<tr style="cursor:pointer" onclick="navigate('detail');window._detailPipeline='${esc(p.name)}'">
      <td class="fw-semibold">${esc(p.name)}</td>
      <td>${statusBadge(p.status)}</td>
      <td class="text-secondary" style="font-size:12px">${fmtNum(p.records_out)}</td>
      <td class="text-secondary" style="font-size:12px">${p.runs_last_hour}</td>
      <td class="text-secondary" style="font-size:12px;${errStyle}">${p.errors > 0 ? p.errors + ' err' : ''}</td>
      <td class="text-end d-flex gap-1 justify-content-end">${startBtn}${stopBtn}${dlBtn}</td>
    </tr>`
  }).join('')

  // Update thead to match columns
  const thead = tbody.closest('table')?.querySelector('thead tr')
  if (thead) thead.innerHTML = '<th>Name</th><th>Status</th><th>Out/hr</th><th>Runs/hr</th><th>Errors</th><th></th>'
}

// ── Runs table ────────────────────────────────────────────────────────────────

function _renderRuns(runs) {
  const tbody = document.getElementById('dash-runs-body')
  if (!tbody) return
  if (!runs.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-secondary text-center py-3">No runs yet</td></tr>'
    return
  }
  tbody.innerHTML = runs.map(r => `<tr>
    <td class="mono" style="font-size:11px">${esc(r.pipeline)}</td>
    <td>${statusBadge(r.status)}</td>
    <td class="num-in">${fmtNum(r.records_in)}</td>
    <td class="num-out">${fmtNum(r.records_out)}</td>
    <td class="text-secondary">${fmtDur(r.started_at, r.finished_at)}</td>
  </tr>`).join('')
}

// ── Live indicator ────────────────────────────────────────────────────────────

function _setLiveDot(ok) {
  const dot = document.getElementById('live-dot')
  if (!dot) return
  dot.style.background = ok ? '#3fb950' : '#6e7681'
  dot.style.boxShadow  = ok ? '0 0 5px #3fb950' : 'none'
  dot.title = ok ? 'Live — updates every 10s' : 'Polling paused'
}

// ── Action handlers ───────────────────────────────────────────────────────────

window._dashStop = async (name) => {
  try { await api.pipelines.stop(name); toast(`Stopped ${name}`); await _refresh() }
  catch (e) { toast(e.message, 'error') }
}
window._dashStart = async (name) => {
  try { await api.pipelines.start(name); toast(`Started ${name}`); setTimeout(_refresh, 800) }
  catch (e) { toast(e.message, 'error') }
}
window._dashDownload = async (name) => {
  try {
    const p = await api.pipelines.get(name)
    const yaml = p.yaml || p.raw || ''
    if (!yaml) { toast('No YAML available', 'error'); return }
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([yaml], { type: 'text/yaml' }))
    a.download = `${name}.yaml`
    a.click()
  } catch (e) { toast(e.message, 'error') }
}

function _set(id, val) {
  const el = document.getElementById(id)
  if (el) el.textContent = val
}
