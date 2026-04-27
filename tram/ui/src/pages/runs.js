import { api } from '../api.js'
import { relTime, fmtDur, fmtNum, statusBadge, esc, toast } from '../utils.js'

export async function init() {
  try {
    const [pipelines, runs] = await Promise.all([
      api.pipelines.list(),
      api.runs.list({ limit: 100 }),
    ])
    populatePipelineSelect(pipelines)
    renderRuns(runs)
    set('runs-count', runs.length)
  } catch (e) {
    toast(`Runs error: ${e.message}`, 'error')
  }

  document.getElementById('runs-pipeline')?.addEventListener('change', loadFiltered)
  document.getElementById('runs-status')?.addEventListener('change',   loadFiltered)
  document.getElementById('runs-from')?.addEventListener('change',     loadFiltered)

  window._runsExport   = exportCsv
  window._runsRefresh = async () => {
    const btn = document.querySelector('[onclick="window._runsRefresh?.()"] i')
    if (btn) btn.className = 'bi bi-arrow-clockwise spin'
    try { await loadFiltered() } catch (e) { toast(e.message, 'error') }
    finally { if (btn) btn.className = 'bi bi-arrow-clockwise' }
  }
}

async function loadFiltered() {
  const pipeline = document.getElementById('runs-pipeline')?.value || ''
  const status   = document.getElementById('runs-status')?.value   || ''
  const from     = document.getElementById('runs-from')?.value     || ''
  const params   = { limit: 200 }
  if (pipeline) params.pipeline = pipeline
  if (status)   params.status   = status
  if (from)     params.from_dt  = new Date(`${from}T00:00:00`).toISOString()
  try {
    const runs = await api.runs.list(params)
    renderRuns(runs)
    set('runs-count', runs.length)
  } catch (e) {
    toast(e.message, 'error')
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
    tbody.innerHTML = '<tr><td colspan="11" class="text-secondary text-center py-4">No runs found</td></tr>'
    return
  }
  const rows = []
  runs.forEach((r, i) => {
    const lines = [...(r.errors || [])]
    if (r.error && !lines.includes(r.error)) lines.unshift(r.error)
    const hasDetail = lines.length > 0 || r.records_skipped > 0 || r.dlq_count > 0
    const toggle = hasDetail
      ? `<button class="btn-flat" style="padding:0 4px" onclick="window._runsToggleLog(${i})"><i class="bi bi-chevron-right" id="runs-chev-${i}" style="font-size:10px"></i></button>`
      : ''
    rows.push(`<tr id="runs-row-${i}">
      <td style="width:20px">${toggle}</td>
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
    const lines = [...(r.errors || [])]
    if (r.error && !lines.includes(r.error)) lines.unshift(r.error)
    const details = []
    if (lines.length) {
      details.push(lines.map(e => `<div style="color:#f85149;padding:1px 0"><i class="bi bi-x-circle me-1" style="font-size:10px"></i>${esc(e)}</div>`).join(''))
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
    logRow.innerHTML = `<td></td><td colspan="10" class="font-monospace" style="font-size:11px;padding:6px 8px 8px;background:#161b22">${content}</td>`
    document.getElementById(`runs-row-${i}`)?.after(logRow)
    if (chev) { chev.classList.remove('bi-chevron-right'); chev.classList.add('bi-chevron-down') }
  }
}

function exportCsv() {
  const rows = [['run_id','pipeline','node','started_at','finished_at','records_in','records_out','records_skipped','dlq_count','status','error']]
  document.querySelectorAll('#runs-body tr:not(.error-detail-row)').forEach(tr => {
    const cells = tr.querySelectorAll('td')
    if (cells.length < 10) return
    rows.push([
      cells[1].textContent.trim(),
      cells[2].textContent.trim(),
      cells[3].textContent.trim(),
      cells[4].textContent.trim(),
      '',
      cells[6].textContent.trim(),
      cells[7].textContent.trim(),
      cells[8].textContent.trim(),
      cells[9].textContent.trim(),
      cells[10].textContent.trim(),
      '',
    ])
  })
  const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n')
  const a = document.createElement('a')
  a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv)
  a.download = `tram-runs-${Date.now()}.csv`
  a.click()
}

function set(id, val) {
  const el = document.getElementById(id)
  if (el) el.textContent = val
}
