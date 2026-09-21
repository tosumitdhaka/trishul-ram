import { api } from '../api.js'
import { router } from '../router.js'
import { downloadBlob, getSavedPollIntervalMs, renderTableState, setOfflineBanner, toast } from '../utils.js'
import { renderRunsTable } from './runs_table.js'

const RUN_LIST_LIMIT = 200
const RUN_EXPORT_LIMIT = 1000

let _runs = []
let _hasMore = false
let _autoRefresh = true
let _pollTimer = null
let _focusRunId = null
let _focusedOnce = false

export async function init() {
  _runs = []
  _hasMore = false
  _autoRefresh = true
  updateAutoRefreshBtn()
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }

  const onFilterChange = () => {
    // Changing filters invalidates a deep-linked run focus.
    if (_focusRunId) {
      _focusRunId = null
      router.replaceRoute('runs')
    }
    syncRouteFilters()
    void loadFiltered().catch(e => toast(e.message, 'error'))
  }
  document.getElementById('runs-pipeline')?.addEventListener('change', onFilterChange)
  document.getElementById('runs-status')?.addEventListener('change',   onFilterChange)
  document.getElementById('runs-from')?.addEventListener('change',     onFilterChange)
  document.getElementById('runs-export-btn')?.addEventListener('click', exportCsv)
  document.getElementById('runs-refresh-btn')?.addEventListener('click', refreshRuns)
  document.getElementById('runs-autorefresh-btn')?.addEventListener('click', toggleAutoRefresh)
  document.getElementById('runs-more-btn')?.addEventListener('click', () => { void loadMore() })

  _pollTimer = setInterval(() => {
    if (!document.getElementById('runs-table')) { clearInterval(_pollTimer); _pollTimer = null; return }
    if (!_autoRefresh) return
    loadFiltered().catch(() => setOfflineBanner(true))
  }, getSavedPollIntervalMs())

  await loadInitial()
}

async function loadInitial() {
  renderTableState(document.getElementById('runs-body'), 'loading')
  try {
    const pipelines = await api.pipelines.list()
    populatePipelineSelect(pipelines)
    applyRouteFilters()
    await loadFiltered()
  } catch (e) {
    renderTableState(document.getElementById('runs-body'), 'error', e.message, {
      onRetry: () => { void loadInitial() },
    })
  }
}

function buildRunParams(limit = RUN_LIST_LIMIT, offset = 0) {
  const pipeline = document.getElementById('runs-pipeline')?.value || ''
  const status   = document.getElementById('runs-status')?.value   || ''
  const from     = document.getElementById('runs-from')?.value     || ''
  const params   = { limit, offset }
  if (pipeline) params.pipeline = pipeline
  if (status)   params.status   = status
  if (from)     params.from_dt  = new Date(`${from}T00:00:00`).toISOString()
  return params
}

async function loadFiltered() {
  const runs = await api.runs.list(buildRunParams())
  _runs = runs
  _hasMore = runs.length >= RUN_LIST_LIMIT
  renderRuns(_runs)
  setOfflineBanner(false)
  updateCount()
}

// Appends the next page. Browsing deeper than the first page pauses
// auto-refresh — a poll would otherwise replace the table with the latest
// page and discard what was loaded here.
async function loadMore() {
  const btn = document.getElementById('runs-more-btn')
  if (btn) btn.disabled = true
  try {
    const more = await api.runs.list(buildRunParams(RUN_LIST_LIMIT, _runs.length))
    _runs = _runs.concat(more)
    _hasMore = more.length >= RUN_LIST_LIMIT
    renderRuns(_runs)
    updateCount()
    if (_autoRefresh) {
      _autoRefresh = false
  _focusRunId = null
  _focusedOnce = false
  updateAutoRefreshBtn()
    }
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    if (btn) btn.disabled = false
  }
}

function toggleAutoRefresh() {
  _autoRefresh = !_autoRefresh
  if (_autoRefresh) {
    // Resuming reloads the latest page — the expanded history is replaced.
    loadFiltered().catch(() => setOfflineBanner(true))
  }
  updateAutoRefreshBtn()
}

function updateAutoRefreshBtn() {
  const btn = document.getElementById('runs-autorefresh-btn')
  const icon = document.getElementById('runs-autorefresh-icon')
  if (btn) {
    btn.title = _autoRefresh
      ? `Auto-refresh is on — updates every ${Math.round(getSavedPollIntervalMs() / 1000)}s. Click to pause.`
      : 'Auto-refresh is paused — click to resume and load the latest runs.'
    btn.setAttribute('aria-label', btn.title)
    btn.setAttribute('aria-pressed', String(_autoRefresh))
  }
  if (icon) icon.className = _autoRefresh ? 'bi bi-pause-circle-fill' : 'bi bi-play-circle-fill'
}

// Deep link state: #runs/:runId?pipeline=x&status=failed&from=2026-09-01.
// Filters initialize from the route; every change is written back with
// replaceState so the URL stays shareable without spamming Back.
function applyRouteFilters() {
  const { params, query } = router.route()
  _focusRunId = params[0] ? String(params[0]) : null
  const pipeline = document.getElementById('runs-pipeline')
  const status = document.getElementById('runs-status')
  const from = document.getElementById('runs-from')
  if (pipeline && query.pipeline) pipeline.value = query.pipeline
  if (status && query.status) status.value = query.status
  if (from && query.from && !Number.isNaN(new Date(query.from).getTime())) from.value = query.from
}

function syncRouteFilters() {
  router.setSearchParams({
    pipeline: document.getElementById('runs-pipeline')?.value || '',
    status:   document.getElementById('runs-status')?.value   || '',
    from:     document.getElementById('runs-from')?.value     || '',
  })
}

async function exportCsv() {
  try {
    const blob = await api.runs.exportCsv(buildRunParams(RUN_EXPORT_LIMIT))
    downloadBlob(`tram-runs-${Date.now()}.csv`, blob)
    const exported = await countCsvRows(blob)
    if (exported >= RUN_EXPORT_LIMIT) {
      toast('CSV export hit the 1,000-row limit — older runs were left out. Narrow the filters to export everything.', 'warning')
    }
  } catch (e) {
    toast(`CSV export error: ${e.message}`, 'error')
  }
}

// Count data rows in exported CSV text (minus the header). Quoted fields may
// contain newlines, so a plain line count would overestimate.
async function countCsvRows(blob) {
  try {
    const text = await blob.text()
    if (!text) return 0
    let inQuotes = false
    let rows = 0
    for (let i = 0; i < text.length; i++) {
      const ch = text[i]
      if (ch === '"') inQuotes = !inQuotes
      else if (ch === '\n' && !inQuotes) rows += 1
    }
    return Math.max(0, rows - 1)
  } catch (_) {
    return 0
  }
}

async function refreshRuns() {
  const icon = document.getElementById('runs-refresh-icon')
  if (icon) icon.className = 'bi bi-arrow-clockwise spin'
  try {
    await loadFiltered()
  } catch (e) {
    toast(e.message, 'error')
  } finally {
    if (icon) icon.className = 'bi bi-arrow-clockwise'
  }
}

function populatePipelineSelect(pipelines) {
  const sel = document.getElementById('runs-pipeline')
  if (!sel) return
  sel.innerHTML = '<option value="">All pipelines</option>'
  pipelines.forEach(p => {
    const opt = document.createElement('option')
    opt.value = p.name
    opt.textContent = p.name
    sel.appendChild(opt)
  })
}

function renderRuns(runs) {
  const tbody = document.getElementById('runs-body')
  renderRunsTable({
    tbody,
    runs,
    rowIdPrefix: 'runs',
    emptyMessage: 'No runs found',
  })
  if (_focusRunId) _highlightFocusedRun()
}

// Deep link (#runs/:runId) — scroll to the run and keep it highlighted
// across polls until the operator changes filters.
function _highlightFocusedRun() {
  const row = document.querySelector(`tr[data-run-id="${CSS.escape(_focusRunId)}"]`)
  if (!row) return
  row.classList.add('run-row-focused')
  if (!_focusedOnce) {
    row.scrollIntoView({ block: 'center', behavior: 'smooth' })
    _focusedOnce = true
  }
}

function updateCount() {
  const pill = document.getElementById('runs-count')
  if (!pill) return
  const moreWrap = document.getElementById('runs-more-wrap')
  if (moreWrap) moreWrap.classList.toggle('d-none', !_hasMore)
  if (!_runs.length) {
    pill.textContent = ''
    return
  }
  pill.textContent = _hasMore
    ? `showing latest ${_runs.length} — more available`
    : `${_runs.length} run${_runs.length === 1 ? '' : 's'}`
}

function set(id, val) {
  const el = document.getElementById(id)
  if (el) el.textContent = val
}
