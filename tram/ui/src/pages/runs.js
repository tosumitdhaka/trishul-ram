import { api } from '../api.js'
import { downloadBlob, toast } from '../utils.js'
import { renderRunsTable } from './runs_table.js'

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
  document.getElementById('runs-export-btn')?.addEventListener('click', exportCsv)
  document.getElementById('runs-refresh-btn')?.addEventListener('click', refreshRuns)
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
    downloadBlob(`tram-runs-${Date.now()}.csv`, blob)
  } catch (e) {
    toast(`CSV export error: ${e.message}`, 'error')
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
    toggleHandlerName: '_runsToggleLog',
    emptyMessage: 'No runs found',
  })
}

function set(id, val) {
  const el = document.getElementById(id)
  if (el) el.textContent = val
}
