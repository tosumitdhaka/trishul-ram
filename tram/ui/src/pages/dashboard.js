import { api } from '../api.js'
import {
  bindDataActions,
  downloadText,
  fmtBytes,
  fmtDur,
  fmtNum,
  getSavedPollIntervalMs,
  statusBadge,
  esc,
  toast,
  pipelineStartFeedback,
} from '../utils.js'
import { monitorTriggeredRun, runOutcomeToast } from '../run_monitor.js'

const DEFAULT_STATS_PARAMS = { period: '1h', granularity: '5m' }
const DEFAULT_CHART_METRIC = 'bytes_processed'

let _pollTimer  = null
let _statsParams = loadStatsParams()
let _statsCache = null
let _pollMs = getSavedPollIntervalMs()
let _chartMetric = loadChartMetric()
const _pipelineMeta = new Map()
let _runMonitorToken = 0

export async function init() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }
  _hideSparklineTooltip()
  _wireControls()
  _wireActions()
  _pollMs = getSavedPollIntervalMs()

  await _refresh()

  _pollTimer = setInterval(async () => {
    if (!document.getElementById('dash-sparkline')) {
      _hideSparklineTooltip()
      clearInterval(_pollTimer)
      _pollTimer = null
      return
    }
    await _refresh()
  }, _pollMs)
}

async function _refresh() {
  try {
    const [stats, runs, pipelines] = await Promise.all([
      api.stats.get(_statsParams),
      api.runs.list({ limit: 10 }),
      api.pipelines.list().catch(() => []),
    ])
    _statsCache = stats
    _cachePipelineMeta(pipelines)
    _renderStatCards(stats)
    _renderSparkline(stats)
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
    const [stats, pipelines] = await Promise.all([
      api.stats.get(_statsParams),
      api.pipelines.list().catch(() => []),
    ])
    _statsCache = stats
    _cachePipelineMeta(pipelines)
    _renderStatCards(stats)
    _renderSparkline(stats)
    _renderPipelines(stats.per_pipeline || [])
    _setLiveDot(true)
  } catch (_) {
    _setLiveDot(false)
  }
}

function _cachePipelineMeta(pipelines = []) {
  pipelines.forEach((pipeline) => {
    if (!pipeline?.name) return
    _pipelineMeta.set(pipeline.name, {
      schedule_type: pipeline.schedule_type || '',
    })
  })
}

// ── Stat cards ───────────────────────────────────────────────────────────────

function _renderStatCards(s) {
  const active = (s.pipelines_running || 0) + (s.pipelines_scheduled || 0)
  const avgDuration = s.avg_duration_last_hour_s != null ? `avg ${s.avg_duration_last_hour_s}s` : null
  _set('stat-total',       s.pipelines_total ?? '—')
  _set('stat-total-sub',   `${s.pipelines_running ?? 0} running · ${s.pipelines_scheduled ?? 0} scheduled`)
  _set('stat-running',     active)
  _set('stat-running-sub', `${s.pipelines_error ?? 0} error · ${s.runs_today ?? 0} runs today`)
  _set('stat-records-in',      fmtNum(s.records_in_last_15m))
  _set('stat-records-in-sub',  'input volume')
  _set('stat-records-out',     fmtNum(s.records_out_last_15m))
  _set('stat-records-out-sub', 'output volume')
  _set('stat-bytes-in',        fmtBytes(s.bytes_in_last_15m))
  _set('stat-bytes-in-sub',    'input payload')
  _set('stat-bytes-out',       fmtBytes(s.bytes_out_last_15m))
  _set('stat-bytes-out-sub',   'output payload')

  const errEl = document.getElementById('stat-errors')
  if (errEl) {
    const hasErrors = (s.errors_last_15m || 0) > 0
    errEl.textContent  = s.errors_last_15m ?? '—'
    errEl.classList.toggle('stat-val-danger', hasErrors)
    errEl.classList.toggle('stat-val-success', !hasErrors)
  }
  _set('stat-errors-sub',      avgDuration ? `runs last hour: ${s.runs_last_hour ?? 0} · ${avgDuration}` : `runs last hour: ${s.runs_last_hour ?? 0}`)
  _set('dash-pipeline-count',  `${s.pipelines_total ?? 0} pipelines`)
  _set('dash-pipeline-window', 'last hour')
}

// ── Sparkline (Canvas API, no dependencies) ──────────────────────────────────

function _renderSparkline(stats) {
  const windowInfo = stats?.window || _statsParams
  const chart = stats?.chart || {}
  const buckets = chart.points || stats?.sparkline || []
  const preferredMetric = _chartMetric || chart.metric || DEFAULT_CHART_METRIC
  const metric = buckets.some((bucket) => Object.prototype.hasOwnProperty.call(bucket || {}, preferredMetric))
    ? preferredMetric
    : (chart.metric || 'records_out')
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
  _set(
    'spark-label',
    `${_chartMetricLabel(metric)}/${_granularityLabel(windowInfo.granularity)} — ${_periodLabel(windowInfo.period)}`,
  )

  if (!buckets.length) {
    _set('spark-meta', `${_chartMetricTotal(metric, 0)} total · ${windowInfo.bucket_count ?? 0} buckets`)
    _hideSparklineTooltip()
    return
  }

  const vals   = buckets.map((bucket) => Number(bucket?.[metric] || 0))
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

  const computedTotal = vals.reduce((a, b) => a + b, 0)
  const total = metric === (chart.metric || preferredMetric) ? (chart.total ?? computedTotal) : computedTotal
  _set('spark-meta', `${_chartMetricTotal(metric, total)} total · ${n} buckets`)
  _bindSparklineTooltip(canvas, { buckets, gap, barW, metric })
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
    const scheduleType = p.schedule_type || _pipelineMeta.get(p.name)?.schedule_type || ''
    const primaryBtn = isRunning
      ? `<button class="btn-flat-danger" type="button" title="Stop" data-action="stop" data-name="${esc(p.name)}"><i class="bi bi-stop-fill"></i></button>`
      : scheduleType === 'manual'
        ? `<button class="btn-flat-primary" type="button" title="Run now" aria-label="Run now" data-action="run" data-name="${esc(p.name)}"><i class="bi bi-play-fill"></i></button>`
        : `<button class="btn-flat-primary" type="button" title="Start" data-action="start" data-name="${esc(p.name)}"><i class="bi bi-play-fill"></i></button>`
    const dlBtn    = `<button class="btn-flat" type="button" title="Download YAML" data-action="download" data-name="${esc(p.name)}"><i class="bi bi-download"></i></button>`
    const errCls = p.errors > 0 ? ' dashboard-error-cell has-errors' : ' dashboard-error-cell'
    return `<tr class="dashboard-row-link" data-pipeline-name="${esc(p.name)}">
      <td class="fw-semibold">${esc(p.name)}</td>
      <td>${statusBadge(p.status)}</td>
      <td class="text-secondary dashboard-metric-cell">${fmtNum(p.records_out)}</td>
      <td class="text-secondary dashboard-metric-cell">${p.runs_last_hour}</td>
      <td class="text-secondary dashboard-metric-cell${errCls}">${p.errors > 0 ? p.errors + ' err' : ''}</td>
      <td class="text-end">
        <div class="d-flex gap-1 justify-content-end">${primaryBtn}${dlBtn}</div>
      </td>
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
    <td class="mono-sm">${esc(r.pipeline)}</td>
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
  dot.classList.toggle('is-live', ok)
  dot.title = ok ? `Live — updates every ${Math.round(_pollMs / 1000)}s` : 'Polling paused'
  const status = document.getElementById('dashboard-status-text')
  if (status) {
    status.textContent = ok
      ? `Overview refreshes every ${Math.round(_pollMs / 1000)}s`
      : 'Dashboard polling paused'
  }
}

async function stopPipeline(name) {
  try { await api.pipelines.stop(name); toast(`Stopped ${name}`); await _refresh() }
  catch (e) { toast(e.message, 'error') }
}

async function startPipeline(name) {
  try {
    const result = await api.pipelines.start(name)
    const feedback = pipelineStartFeedback(name, result)
    toast(feedback.message, feedback.type)
    setTimeout(_refresh, 800)
  }
  catch (e) { toast(e.message, 'error') }
}

async function runPipeline(name) {
  try {
    const result = await api.pipelines.run(name)
    setTimeout(_refresh, 400)
    if (result?.run_id) {
      const token = ++_runMonitorToken
      void monitorTriggeredRun(result.run_id, {
        isActive: () => token === _runMonitorToken && Boolean(document.getElementById('dash-pipelines-body')),
      }).then(async (run) => {
        if (token !== _runMonitorToken || !document.getElementById('dash-pipelines-body')) return
        await _refresh()
        const feedback = runOutcomeToast(run, { name })
        if (feedback) {
          toast(feedback.message, feedback.type)
        }
      }).catch((err) => {
        toast(`Run monitor error: ${err.message}`, 'error')
      })
    }
  }
  catch (e) { toast(e.message, 'error') }
}

function openDetail(name) {
  window._detailPipeline = name
  navigate('detail')
}

async function downloadPipelineYaml(name) {
  try {
    const p = await api.pipelines.get(name)
    const yaml = p.yaml || p.raw || ''
    if (!yaml) { toast('No YAML available', 'error'); return }
    downloadText(`${name}.yaml`, yaml, 'text/yaml;charset=utf-8')
  } catch (e) { toast(e.message, 'error') }
}

function _set(id, val) {
  const el = document.getElementById(id)
  if (el) el.textContent = val
}

function _wireControls() {
  const metricEl = document.getElementById('dash-chart-metric')
  const periodEl = document.getElementById('dash-period')
  const granularityEl = document.getElementById('dash-granularity')
  if (!metricEl || !periodEl || !granularityEl) return

  metricEl.value = _chartMetric
  periodEl.value = _statsParams.period
  granularityEl.value = _statsParams.granularity

  metricEl.onchange = async () => {
    _chartMetric = metricEl.value
    saveChartMetric()
    if (_statsCache) {
      _renderSparkline(_statsCache)
      return
    }
    await _refreshStats()
  }
  periodEl.onchange = async () => {
    _statsParams = { ..._statsParams, period: periodEl.value }
    saveStatsParams()
    await _refreshStats()
  }
  granularityEl.onchange = async () => {
    _statsParams = { ..._statsParams, granularity: granularityEl.value }
    saveStatsParams()
    await _refreshStats()
  }
}

function _wireActions() {
  const pipelineBody = document.getElementById('dash-pipelines-body')
  if (pipelineBody) {
    bindDataActions(pipelineBody, {
      stop: async (button, event) => {
        event.stopPropagation()
        await stopPipeline(button.dataset.name)
      },
      start: async (button, event) => {
        event.stopPropagation()
        await startPipeline(button.dataset.name)
      },
      run: async (button, event) => {
        event.stopPropagation()
        await runPipeline(button.dataset.name)
      },
      download: async (button, event) => {
        event.stopPropagation()
        await downloadPipelineYaml(button.dataset.name)
      },
    })
    if (pipelineBody._tramRowClickListener) {
      pipelineBody.removeEventListener('click', pipelineBody._tramRowClickListener)
    }
    const rowClickListener = (event) => {
      if (event.target.closest('[data-action]')) return
      const row = event.target.closest('tr[data-pipeline-name]')
      if (!row || !pipelineBody.contains(row)) return
      openDetail(row.dataset.pipelineName)
    }
    pipelineBody.addEventListener('click', rowClickListener)
    pipelineBody._tramRowClickListener = rowClickListener
  }

  document.getElementById('dash-refresh-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('dash-refresh-btn')
    const icon = document.getElementById('dash-refresh-icon')
    if (btn) btn.disabled = true
    if (icon) icon.className = 'bi bi-arrow-clockwise spin'
    try {
      await _refresh()
    } finally {
      if (btn) btn.disabled = false
      if (icon) icon.className = 'bi bi-arrow-clockwise'
    }
  })
  document.getElementById('dash-manage-btn')?.addEventListener('click', () => navigate('pipelines'))
  document.getElementById('dash-new-btn')?.addEventListener('click', () => navigate('editor'))
  document.getElementById('dash-view-runs-btn')?.addEventListener('click', () => navigate('runs'))
}

function loadStatsParams() {
  const period = localStorage.getItem('tram_dash_period') || DEFAULT_STATS_PARAMS.period
  const granularity = localStorage.getItem('tram_dash_granularity') || DEFAULT_STATS_PARAMS.granularity
  return {
    period: ['1h', '6h', '24h'].includes(period) ? period : DEFAULT_STATS_PARAMS.period,
    granularity: ['5m', '15m', '1h'].includes(granularity) ? granularity : DEFAULT_STATS_PARAMS.granularity,
  }
}

function saveStatsParams() {
  localStorage.setItem('tram_dash_period', _statsParams.period)
  localStorage.setItem('tram_dash_granularity', _statsParams.granularity)
}

function loadChartMetric() {
  const metric = localStorage.getItem('tram_dash_chart_metric') || DEFAULT_CHART_METRIC
  return ['bytes_processed', 'records_out'].includes(metric) ? metric : DEFAULT_CHART_METRIC
}

function saveChartMetric() {
  localStorage.setItem('tram_dash_chart_metric', _chartMetric)
}

function _periodLabel(period) {
  return {
    '1h': 'last hour',
    '6h': 'last 6 hours',
    '24h': 'last 24 hours',
  }[period] || 'selected window'
}

function _granularityLabel(granularity) {
  return {
    '5m': '5m',
    '15m': '15m',
    '1h': '1h',
  }[granularity] || granularity || 'bucket'
}

function _chartMetricLabel(metric) {
  return metric === 'bytes_processed' ? 'Bytes processed' : 'Records out'
}

function _chartMetricTotal(metric, total) {
  return metric === 'bytes_processed' ? fmtBytes(total) : fmtNum(total)
}

function _bindSparklineTooltip(canvas, state) {
  canvas._tramSparklineState = state
  if (canvas._tramSparklineTooltipBound) return

  const handleMove = (event) => {
    const current = canvas._tramSparklineState
    if (!current?.buckets?.length) {
      _hideSparklineTooltip()
      return
    }
    const rect = canvas.getBoundingClientRect()
    const offsetX = event.clientX - rect.left
    if (offsetX < 0 || offsetX > rect.width) {
      _hideSparklineTooltip()
      return
    }
    const step = current.barW + current.gap
    if (step <= 0) {
      _hideSparklineTooltip()
      return
    }
    const index = Math.max(0, Math.min(current.buckets.length - 1, Math.floor(offsetX / step)))
    const bucket = current.buckets[index]
    if (!bucket) {
      _hideSparklineTooltip()
      return
    }
    _showSparklineTooltip(bucket, event.clientX, event.clientY)
  }

  const hideTooltip = () => _hideSparklineTooltip()

  canvas.addEventListener('mousemove', handleMove)
  canvas.addEventListener('mouseleave', hideTooltip)
  canvas.addEventListener('blur', hideTooltip)
  canvas._tramSparklineTooltipBound = true
}

function _showSparklineTooltip(bucket, clientX, clientY) {
  const tooltip = _getSparklineTooltip()
  tooltip.innerHTML = `
    <div class="dashboard-chart-tooltip-range">${esc(_formatBucketRange(bucket.bucket_start, bucket.bucket_end))}</div>
    <div class="dashboard-chart-tooltip-row">
      <span class="dashboard-chart-tooltip-key">Bytes processed</span>
      <span class="dashboard-chart-tooltip-val">${esc(fmtBytes(bucket.bytes_processed || 0))}</span>
    </div>
    <div class="dashboard-chart-tooltip-row">
      <span class="dashboard-chart-tooltip-key">Records out</span>
      <span class="dashboard-chart-tooltip-val">${esc(fmtNum(bucket.records_out || 0))}</span>
    </div>`
  tooltip.hidden = false

  const width = tooltip.offsetWidth
  const height = tooltip.offsetHeight
  let left = clientX + 14
  let top = clientY - height - 12

  if (left + width > window.innerWidth - 8) {
    left = window.innerWidth - width - 8
  }
  if (top < 8) {
    top = clientY + 16
  }
  tooltip.style.left = `${Math.max(8, left)}px`
  tooltip.style.top = `${Math.max(8, top)}px`
}

function _hideSparklineTooltip() {
  const tooltip = document.getElementById('dash-spark-tooltip')
  if (!tooltip) return
  tooltip.hidden = true
}

function _getSparklineTooltip() {
  let tooltip = document.getElementById('dash-spark-tooltip')
  if (tooltip) return tooltip
  tooltip = document.createElement('div')
  tooltip.id = 'dash-spark-tooltip'
  tooltip.className = 'dashboard-chart-tooltip'
  tooltip.hidden = true
  document.body.appendChild(tooltip)
  return tooltip
}

function _formatBucketRange(startIso, endIso) {
  const start = new Date(startIso)
  const end = new Date(endIso)
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return 'Selected bucket'

  const startLabel = start.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
  const endLabel = end.toLocaleString([], start.toDateString() === end.toDateString()
    ? { hour: 'numeric', minute: '2-digit' }
    : { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
  return `${startLabel} - ${endLabel}`
}
