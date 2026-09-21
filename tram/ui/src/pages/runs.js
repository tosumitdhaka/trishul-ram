import { api } from '../api.js'
import { router } from '../router.js'
import { createPageController } from '../page.js'
import { downloadBlob, getSavedPollIntervalMs, toast } from '../utils.js'
import { renderRunsTable } from './runs_table.js'

const RUN_LIST_LIMIT = 200
const RUN_EXPORT_LIMIT = 1000

let _runs = []
let _total = null    // matching runs for the current filters (null = unknown, no persistence)
let _hasMore = false
let _autoRefresh = true
let _booted = false

const controller = createPageController({
  page: 'runs',
  fetch: async () => {
    if (!_booted) {
      // One-time page setup: the pipeline filter dropdown + deep-link filters.
      const pipelines = await api.pipelines.list()
      populatePipelineSelect(pipelines)
      applyRouteFilters()
      _booted = true
    }
    const [runs, count] = await Promise.all([
      api.runs.list(buildRunParams()),
      api.runs.count(buildCountParams()).catch(() => ({ total: null })),
    ])
    return { runs, total: count?.total ?? null }
  },
  render: ({ runs, total }) => {
    _runs = runs
    _total = total
    _hasMore = computeHasMore(runs)
    renderRuns(_runs)
    updateCount()
  },
  pollMs: () => getSavedPollIntervalMs(),
  // QW6 pause/resume: the timer keeps ticking but skips fetches while paused.
  pollEnabled: () => _autoRefresh,
  tableBody: () => document.getElementById('runs-body'),
})

export async function init() {
  _runs = []
  _total = null
  _hasMore = false
  _autoRefresh = true
  _booted = false
  updateAutoRefreshBtn()

  const onFilterChange = () => {
    syncRouteFilters()
    void controller.refresh()
  }
  document.getElementById('runs-pipeline')?.addEventListener('change', onFilterChange)
  document.getElementById('runs-status')?.addEventListener('change',   onFilterChange)
  document.getElementById('runs-from')?.addEventListener('change',     onFilterChange)
  document.getElementById('runs-export-btn')?.addEventListener('click', exportCsv)
  document.getElementById('runs-refresh-btn')?.addEventListener('click', refreshRuns)
  document.getElementById('runs-autorefresh-btn')?.addEventListener('click', toggleAutoRefresh)
  document.getElementById('runs-more-btn')?.addEventListener('click', () => { void loadMore() })

  await controller.mount()
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

// Filter params for the count endpoint — same filters, no pagination.
function buildCountParams() {
  const params = {}
  const pipeline = document.getElementById('runs-pipeline')?.value || ''
  const status   = document.getElementById('runs-status')?.value   || ''
  const from     = document.getElementById('runs-from')?.value     || ''
  if (pipeline) params.pipeline = pipeline
  if (status)   params.status   = status
  if (from)     params.from_dt  = new Date(`${from}T00:00:00`).toISOString()
  return params
}

// With a known total the cut-off is exact; without persistence fall back to
// the page-full heuristic.
function computeHasMore(runs) {
  if (_total !== null) return runs.length < _total
  return runs.length >= RUN_LIST_LIMIT
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
    _hasMore = computeHasMore(_runs)
    renderRuns(_runs)
    updateCount()
    if (_autoRefresh) {
      _autoRefresh = false
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
    void controller.pollNow()
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

// Deep link state: #runs?pipeline=x&status=failed&from=2026-09-01.
// Filters initialize from the route; every change is written back with
// replaceState so the URL stays shareable without spamming Back.
function applyRouteFilters() {
  const { query } = router.route()
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
    await controller.refresh()
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
  if (_total !== null) {
    pill.textContent = _hasMore
      ? `showing ${_runs.length} of ${_total}`
      : `${_total} run${_total === 1 ? '' : 's'}`
    return
  }
  // No persistence → no count endpoint; fall back to the page-full heuristic.
  pill.textContent = _hasMore
    ? `showing latest ${_runs.length} — more available`
    : `${_runs.length} run${_runs.length === 1 ? '' : 's'}`
}

function set(id, val) {
  const el = document.getElementById(id)
  if (el) el.textContent = val
}
