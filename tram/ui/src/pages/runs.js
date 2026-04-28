import { api } from '../api.js'
import { relTime, fmtDur, fmtNum, statusBadge, esc, toast } from '../utils.js'

const RUN_LIST_LIMIT = 200
const RUN_EXPORT_LIMIT = 1000

export async function init() {
  try {
    const pipelines = await api.pipelines.list()
    populatePipelineSelect(pipelines)
    applyPresetFilters()
    await loadFiltered()
  } catch (e) {
    toast(`Runs error: ${e.message}`, 'error')
  }

  document.getElementById('runs-pipeline')?.addEventListener('change', loadFiltered)
  document.getElementById('runs-status')?.addEventListener('change',   loadFiltered)
  document.getElementById('runs-from')?.addEventListener('change',     loadFiltered)

  window._runsExport  = exportCsv
  window._runsRefresh = async () => {
    const btn = document.querySelector('[onclick="window._runsRefresh?.()"] i')
    if (btn) btn.className = 'bi bi-arrow-clockwise spin'
    try { await loadFiltered() } catch (e) { toast(e.message, 'error') }
    finally { if (btn) btn.className = 'bi bi-arrow-clockwise' }
  }
}

function buildRunParams(limit = RUN_LIST_LIMIT) {
  const pipeline = document.getElementById('runs-pipeline')?.value || ''
  const status   = document.getElementById('runs-status')?.value   || ''
  const from     = document.getElementById('runs-from')?.value     || ''
  const params   = { limit }
  if (pipeline) params.pipeline = pipeline
  if (status)   params.status   = status
  if (from)     params.from_dt  = new Date(`${from}T00:00:00`).toISOString()
  return params
}

async function loadFiltered() {
  const params = buildRunParams()
  try {
    const runs = await api.runs.list(params)
    renderRuns(runs)
    set('runs-count', runs.length)
  } catch (e) {
    toast(e.message, 'error')
  }
}

function applyPresetFilters() {
  const preset = window._runsFilters || null
  window._runsFilters = null
  if (!preset) return
  const pipeline = document.getElementById('runs-pipeline')
  const status = document.getElementById('runs-status')
  const from = document.getElementById('runs-from')
  if (pipeline && preset.pipeline) pipeline.value = preset.pipeline
  if (status && preset.status) status.value = preset.status
  if (from && preset.from_dt) {
    const dt = new Date(preset.from_dt)
    if (!Number.isNaN(dt.getTime())) from.value = dt.toISOString().slice(0, 10)
  }
}

async function exportCsv() {
  try {
    const blob = await api.runs.exportCsv(buildRunParams(RUN_EXPORT_LIMIT))
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `tram-runs-${Date.now()}.csv`
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) {
    toast(`CSV export error: ${e.message}`, 'error')
  }
}

function populatePipelineSelect(pipelines) {
  const sel = document.getElementById('runs-pipeline')
  if (!sel) return
  pipelines.forEach(p => {
    const opt = document.createElement('option')
    opt.value = p.name
    opt.textContent = p.name
    sel.appendChild(opt)
  })
}

function renderRuns(runs) {
  const tbody = document.getElementById('runs-body')
  if (!tbody) return
  if (!runs.length) {
    tbody.innerHTML = '<tr><td colspan="12" class="text-secondary text-center py-4">No runs found</td></tr>'
    return
  }
  const rows = []
  runs.forEach((r, i) => {
    const failureReason = topLevelFailureReason(r)
    const summary = issueSummary(r, failureReason)
    const tooltip = issueTooltip(r, failureReason)
    const hasDetail = Boolean(failureReason) || r.records_skipped > 0 || r.dlq_count > 0
    const toggle = hasDetail
      ? `<button class="btn-flat" style="padding:0 4px" onclick="window._runsToggleLog(${i})"><i class="bi bi-chevron-right" id="runs-chev-${i}" style="font-size:10px"></i></button>`
      : ''
    rows.push(`<tr id="runs-row-${i}">
      <td class="mono" style="font-size:11px">${esc(String(r.run_id || r.id || '').slice(0,8))}</td>
      <td class="fw-semibold">${esc(r.pipeline)}</td>
      <td class="text-secondary">${esc(r.node || '—')}</td>
      <td class="text-secondary">${r.started_at ? relTime(r.started_at) : '—'}</td>
      <td class="text-secondary">${fmtDur(r.started_at, r.finished_at)}</td>
      <td class="num-in">${fmtNum(r.records_in)}</td>
      <td class="num-out">${fmtNum(r.records_out)}</td>
      <td class="text-secondary">${fmtNum(r.records_skipped)}</td>
      <td class="text-secondary">${fmtNum(r.dlq_count)}</td>
      <td>${statusBadge(r.status)}</td>
      <td class="text-secondary" style="font-size:11px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(tooltip)}">${esc(summary)}</td>
      <td style="width:20px" class="text-end">${toggle}</td>
    </tr>`)
  })
  tbody.innerHTML = rows.join('')

  window._runsToggleLog = (i) => {
    const existingLog = document.getElementById(`runs-log-${i}`)
    const chev = document.getElementById(`runs-chev-${i}`)
    if (existingLog) {
      existingLog.remove()
      if (chev) { chev.classList.remove('bi-chevron-down'); chev.classList.add('bi-chevron-right') }
      return
    }
    const r = runs[i]
    const failureReason = topLevelFailureReason(r)
    const details = []
    if (failureReason) {
      details.push(`<div style="color:#f85149;padding:1px 0"><i class="bi bi-x-circle me-1" style="font-size:10px"></i>${esc(failureReason)}</div>`)
    }
    if (r.records_skipped > 0) {
      details.push(`<div class="text-secondary" style="padding:1px 0"><i class="bi bi-skip-forward me-1" style="font-size:10px"></i>${fmtNum(r.records_skipped)} record(s) skipped</div>`)
    }
    if (r.dlq_count > 0) {
      details.push(`<div class="text-secondary" style="padding:1px 0"><i class="bi bi-inbox me-1" style="font-size:10px"></i>${fmtNum(r.dlq_count)} record(s) sent to DLQ</div>`)
    }
    const content = details.join('') || '<div class="text-secondary">No error details available</div>'
    const logRow = document.createElement('tr')
    logRow.id = `runs-log-${i}`
    logRow.className = 'error-detail-row'
    logRow.innerHTML = `<td colspan="12" class="font-monospace" style="font-size:11px;padding:6px 8px 8px;background:#161b22">${content}</td>`
    document.getElementById(`runs-row-${i}`)?.after(logRow)
    if (chev) { chev.classList.remove('bi-chevron-right'); chev.classList.add('bi-chevron-down') }
  }
}

function topLevelFailureReason(r) {
  const status = String(r.status || '').toLowerCase()
  const failed = status === 'failed' || status === 'aborted' || status === 'error'
  if (!failed) return ''
  if (r.error) return r.error
  const fallback = Array.from(new Set((r.errors || []).filter(Boolean)))[0]
  return fallback || ''
}

function issueSummary(r, failureReason) {
  const parts = []
  if (r.records_skipped > 0) parts.push(`${fmtNum(r.records_skipped)} skipped`)
  if (r.dlq_count > 0) parts.push(`${fmtNum(r.dlq_count)} DLQ`)
  if (failureReason) parts.push(failureReason)
  return parts.join(' · ') || '—'
}

function issueTooltip(r, failureReason) {
  const previews = []
  if (r.records_skipped > 0) previews.push(`${fmtNum(r.records_skipped)} record(s) skipped`)
  if (r.dlq_count > 0) previews.push(`${fmtNum(r.dlq_count)} record(s) sent to DLQ`)
  if (failureReason) previews.push(failureReason)
  return previews.join(' | ') || 'No issues captured'
}

function set(id, val) {
  const el = document.getElementById(id)
  if (el) el.textContent = val
}
