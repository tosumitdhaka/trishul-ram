import { api } from '../api.js'
import { esc, fmtNum, statusBadge, toast } from '../utils.js'

const _openWorkers = {}
let _pollTimer = null
let _refreshInFlight = null

export async function init() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }
  window._clusterRefresh = async () => {
    const btn = document.getElementById('cluster-refresh-btn')
    const icon = document.getElementById('cluster-refresh-icon')
    if (btn) btn.disabled = true
    if (icon) icon.className = 'bi bi-arrow-clockwise spin'
    try {
      await refresh()
    } catch (e) {
      toast(e.message, 'error')
    } finally {
      if (btn) btn.disabled = false
      if (icon) icon.className = 'bi bi-arrow-clockwise'
    }
  }

  await refresh()

  const pollMs = (parseInt(localStorage.getItem('tram_poll_interval') || '10', 10)) * 1000
  _pollTimer = setInterval(() => {
    if (!document.getElementById('cluster-streams')) {
      clearInterval(_pollTimer)
      _pollTimer = null
      return
    }
    void refresh({ silent: true })
  }, pollMs)
}

async function refresh({ silent = false } = {}) {
  if (_refreshInFlight) return _refreshInFlight
  _refreshInFlight = (async () => {
    try {
      const [readyResp, statusResp, streamsResp, statsResp] = await Promise.allSettled([
        api.ready(),
        api.cluster.nodes(),
        api.cluster.streams(),
        api.stats.get({ period: '1h', granularity: '15m' }),
      ])
      if (statusResp.status !== 'fulfilled') throw statusResp.reason
      if (streamsResp.status !== 'fulfilled') throw streamsResp.reason
      const stats = statsResp.status === 'fulfilled' ? (statsResp.value || {}) : {}
      const ready = readyResp.status === 'fulfilled' ? (readyResp.value || {}) : null
      renderDaemonStatus(ready)
      renderCluster(statusResp.value, streamsResp.value?.streams || [], stats)
    } catch (e) {
      const txt = document.getElementById('cluster-status-text')
      if (txt) txt.textContent = 'Cluster info unavailable — daemon offline'
      if (!silent) toast(`Cluster error: ${e.message}`, 'error')
      throw e
    } finally {
      _refreshInFlight = null
    }
  })()
  return _refreshInFlight
}

function renderDaemonStatus(ready) {
  const modeEl = document.getElementById('cluster-daemon-mode')
  const modeSubEl = document.getElementById('cluster-daemon-mode-sub')
  const schedulerEl = document.getElementById('cluster-daemon-scheduler')
  const dbEngineEl = document.getElementById('cluster-daemon-db-engine')
  const dbMetaEl = document.getElementById('cluster-daemon-db-meta')
  const dbCardEl = document.getElementById('cluster-daemon-db-card')
  const uptimeEl = document.getElementById('cluster-daemon-uptime')

  if (!ready) {
    if (modeEl) modeEl.textContent = 'unavailable'
    if (modeSubEl) modeSubEl.textContent = 'Runtime status unavailable'
    if (schedulerEl) schedulerEl.textContent = 'unavailable'
    if (dbEngineEl) dbEngineEl.textContent = 'unavailable'
    if (dbMetaEl) dbMetaEl.textContent = 'Runtime status unavailable'
    if (uptimeEl) uptimeEl.textContent = '—'
    return
  }

  const clusterLabel = String(ready.cluster || '')
  const isManager = clusterLabel.startsWith('manager')
  if (modeEl) modeEl.textContent = isManager ? 'Manager' : 'Standalone'
  if (modeSubEl) {
    modeSubEl.textContent = isManager
      ? (clusterLabel.replace(/^manager\s*·\s*/, '') || 'Worker pool enabled')
      : 'All pipelines execute in the local daemon'
  }

  if (schedulerEl) {
    schedulerEl.innerHTML = `<span class="tram-badge badge-${ready.scheduler === 'running' ? 'running has-dot running' : 'stopped'}">${ready.scheduler || '—'}</span>`
  }
  if (dbEngineEl) {
    dbEngineEl.textContent = ready.db_engine || 'SQLite'
    dbEngineEl.style.color = ready.db === 'ok' ? '#3fb950' : '#f85149'
  }
  if (dbMetaEl) {
    dbMetaEl.textContent = ready.db === 'ok'
      ? (ready.db_path ? `Connected · ${ready.db_path}` : 'Connected')
      : (ready.detail || ready.db || 'Unavailable')
  }
  if (dbCardEl) {
    dbCardEl.style.borderColor = ready.db === 'ok' ? 'rgba(63,185,80,0.35)' : 'rgba(248,81,73,0.45)'
  }
  if (uptimeEl) {
    uptimeEl.textContent = ready.uptime || '—'
  }
}

function renderCluster(status, streams, stats) {
  const txt  = document.getElementById('cluster-status-text')
  const mode = status?.mode || 'standalone'
  const workers = status?.workers || []
  const streamTotals = summarizeStreams(streams)

  renderSummary(mode, workers, streams, streamTotals, stats)
  renderStreams(streams, mode)

  if (mode === 'manager') {
    const online  = workers.filter(w => w.ok).length
    const runs    = workers.reduce((s, w) => s + (w.active_runs || 0), 0)
    if (txt) txt.textContent =
      `Manager mode · ${online}/${workers.length} workers online · ${runs} active run${runs !== 1 ? 's' : ''}`
    renderWorkers(workers)
    return
  }

  if (txt) {
    txt.textContent = `Standalone mode · local daemon runtime · ${streams.length} active stream${streams.length !== 1 ? 's' : ''}`
  }
  renderStandalone(stats, streamTotals)
}

function renderSummary(mode, workers, streams, streamTotals, stats) {
  const onlineWorkers = workers.filter(w => w.ok).length
  const activeRuns = workers.reduce((sum, worker) => sum + (worker.active_runs || 0), 0)
  const liveRunningPipelines = new Set([
    ...workers.flatMap(worker => worker.running_pipelines || []),
    ...streams.map(stream => stream.pipeline_name),
  ]).size
  _set('cluster-workers-value', mode === 'manager' ? `${onlineWorkers}/${workers.length || 0}` : 'Local')
  _set('cluster-workers-sub', mode === 'manager'
    ? `${activeRuns} active runs across workers`
    : `${stats?.runs_last_hour ?? 0} completed runs in the last hour`)
  _set('cluster-streams-value', `${streams.length}`)
  _set('cluster-streams-sub', `${streamTotals.activeSlots} active slot${streamTotals.activeSlots !== 1 ? 's' : ''} · ${liveRunningPipelines} running`)
  _set('cluster-throughput-value', formatRate(streamTotals.recordsOutPerSec))
  _set('cluster-throughput-sub', `${formatBytesRate(streamTotals.bytesOutPerSec)} · ${fmtNum(streamTotals.errors)} total errors`)
  _set('cluster-running-value', fmtNum(mode === 'manager' ? liveRunningPipelines : (stats?.pipelines_running ?? liveRunningPipelines)))
  _set('cluster-running-sub', `${fmtNum(stats?.pipelines_scheduled ?? 0)} scheduled · ${fmtNum(activeRuns)} active runs`)
  _set('cluster-errors-value', fmtNum(stats?.pipelines_error ?? streamTotals.errors ?? 0))
  _set('cluster-errors-sub', `${fmtNum(stats?.errors_last_15m ?? 0)} in last 15m · ${fmtNum(stats?.runs_last_hour ?? 0)} runs last hour`)
}

function renderWorkers(workers) {
  const count = document.getElementById('cluster-node-count')
  const container = document.getElementById('cluster-nodes')
  if (!container) return

  if (count) {
    count.textContent = `${workers.length} worker${workers.length !== 1 ? 's' : ''}`
  }

  if (!workers.length) {
    container.innerHTML = '<div class="text-secondary text-center py-3">Manager mode is enabled, but no workers are currently configured.</div>'
    return
  }

  container.innerHTML = `
    <div class="table-responsive">
      <table class="table mb-0">
        <thead>
          <tr>
            <th>Worker</th>
            <th>Status</th>
            <th>Active Runs</th>
            <th>Running Pipelines</th>
            <th>Dispatched</th>
            <th class="text-end"></th>
          </tr>
        </thead>
        <tbody>
          ${workers.map(worker => renderWorkerRow(worker)).join('')}
        </tbody>
      </table>
    </div>`

  window._clusterToggleWorker = (key) => {
    _openWorkers[key] = !(_openWorkers[key] ?? false)
    renderWorkers(workers)
  }
}

function renderStandalone(stats, streamTotals) {
  const count = document.getElementById('cluster-node-count')
  const container = document.getElementById('cluster-nodes')
  if (!container) return

  if (count) {
    count.textContent = 'local runtime'
  }

  container.innerHTML = `<div class="node-card cluster-node-card">
    <div class="d-flex align-items-center justify-content-between gap-3 mb-3">
      <div>
        <div class="fw-semibold">Local daemon runtime</div>
        <div class="text-secondary" style="font-size:12px">Standalone mode has no worker pool. Streaming pipelines still appear above when active.</div>
      </div>
      <span class="badge bg-secondary">standalone</span>
    </div>
    <div class="cluster-node-meta mb-3">
      <div>
        <div class="cluster-node-meta-label">Registered Pipelines</div>
        <div class="cluster-node-meta-value">${fmtNum(stats?.pipelines_total ?? 0)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Running / Scheduled</div>
        <div class="cluster-node-meta-value">${fmtNum((stats?.pipelines_running ?? 0) + (stats?.pipelines_scheduled ?? 0))}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Runs Last Hour</div>
        <div class="cluster-node-meta-value">${fmtNum(stats?.runs_last_hour ?? 0)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Live Throughput</div>
        <div class="cluster-node-meta-value">${formatRate(streamTotals.recordsOutPerSec)}</div>
      </div>
    </div>
    <div class="text-secondary" style="font-size:12px">
      Batch and manual pipelines do not stay on this page after completion. Use pipeline detail for terminal run results and active stream placement for live workloads.
    </div>
  </div>`
}

function renderStreams(streams, mode) {
  const count = document.getElementById('cluster-stream-count')
  const container = document.getElementById('cluster-streams')
  if (!container) return

  if (count) {
    count.textContent = `${streams.length} stream${streams.length !== 1 ? 's' : ''}`
  }

  if (!streams.length) {
    container.innerHTML = `<div class="text-secondary text-center py-3">${
      mode === 'manager'
        ? 'No active stream placements at the moment.'
        : 'No active stream pipelines in the local runtime.'
    }</div>`
    return
  }

  container.innerHTML = `
    <div class="table-wrap" style="border:none;background:transparent">
      <table class="table mb-0">
        <thead>
          <tr>
            <th>Pipeline</th>
            <th>Status</th>
            <th>Slots</th>
            <th>Out/s</th>
            <th>Bytes/s</th>
            <th>Errors</th>
          </tr>
        </thead>
        <tbody>
          ${streams.map(stream => {
            const encodedName = JSON.stringify(stream.pipeline_name)
            const slots = Array.isArray(stream.slots) ? stream.slots : []
            const healthy = slots.filter(slot => !slot.stats?.stale).length
            const stale = slots.filter(slot => slot.stats?.stale).length
            return `<tr style="cursor:pointer" onclick='window._clusterOpenPlacement(${encodedName})'>
              <td class="fw-semibold">${esc(stream.pipeline_name)}</td>
              <td>${statusBadge(stream.status)}</td>
              <td class="text-secondary">${healthy}/${stream.slot_count}${stale ? ` · ${stale} stale` : ''}</td>
              <td class="text-secondary">${fmtNum(Math.round(stream.records_out_per_sec || 0))}</td>
              <td class="text-secondary">${formatBytesRate(stream.bytes_out_per_sec || 0)}</td>
              <td class="text-secondary">${fmtNum(stream.error_count || 0)}</td>
            </tr>`
          }).join('')}
        </tbody>
      </table>
    </div>`

  window._clusterOpenPlacement = (pipelineName) => {
    window._detailPipeline = pipelineName
    navigate('detail')
  }
}

function renderWorkerRow(worker) {
  const assigned = worker.assigned_pipelines || []
  const running = new Set(worker.running_pipelines || [])
  const nodeLabel = shortNodeLabel(worker.url)
  const rowKey = worker.url || nodeLabel
  const open = _openWorkers[rowKey] ?? false
  const encodedRowKey = JSON.stringify(rowKey)
  const toggleLabel = open ? 'Collapse worker details' : 'Expand worker details'
  const stateBadge = `<span class="badge ${worker.ok ? 'bg-success' : 'bg-danger'}">${worker.ok ? 'online' : 'offline'}</span>`
  return `
    <tr>
      <td>
        <div class="fw-semibold">${esc(nodeLabel)}</div>
        <div class="text-secondary" style="font-size:12px">${esc(worker.url || '—')}</div>
      </td>
      <td>${stateBadge}</td>
      <td class="text-secondary">${fmtNum(worker.active_runs ?? 0)}</td>
      <td class="text-secondary">${fmtNum(running.size)}</td>
      <td class="text-secondary">${fmtNum(assigned.length)}</td>
      <td class="text-end">
        <button class="btn-flat" title="${toggleLabel}" aria-label="${toggleLabel}" onclick='window._clusterToggleWorker(${encodedRowKey})'>
          <i class="bi bi-chevron-${open ? 'down' : 'right'}"></i>
        </button>
      </td>
    </tr>
    ${open ? `<tr class="cluster-worker-detail-row">
      <td colspan="6">
        ${renderWorkerExpanded(worker, assigned, running)}
      </td>
    </tr>` : ''}`
}

function renderWorkerExpanded(worker, assigned, running) {
  const runningList = [...running]
  const runningHtml = runningList.length
    ? runningList.map(name => `<span class="type-pill cluster-pill-running">${esc(name)} ▶</span>`).join('')
    : '<span class="text-secondary">None</span>'
  const dispatchedHtml = assigned.length
    ? assigned.map(name => {
      const active = running.has(name)
      return `<span class="type-pill ${active ? 'cluster-pill-running' : 'cluster-pill-neutral'}">${esc(name)}${active ? ' ▶' : ''}</span>`
    }).join('')
    : '<span class="text-secondary">None</span>'

  return `<div class="cluster-worker-expanded">
    <div class="cluster-worker-expanded-grid">
      <div>
        <div class="cluster-node-meta-label">Worker URL</div>
        <div class="mono text-secondary" style="font-size:12px">${esc(worker.url || '—')}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Health</div>
        <div>${worker.ok ? 'reachable' : 'offline / degraded'}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Active Runs</div>
        <div>${fmtNum(worker.active_runs ?? 0)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Running Pipelines</div>
        <div>${fmtNum(running.size)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Dispatched Pipelines</div>
        <div>${fmtNum(assigned.length)}</div>
      </div>
    </div>
    <div class="mt-3">
      <div class="cluster-node-meta-label mb-2">Running Pipelines</div>
      <div class="d-flex flex-wrap gap-1">${runningHtml}</div>
    </div>
    <div class="mt-3">
      <div class="cluster-node-meta-label mb-2">Dispatched Pipelines</div>
      <div class="d-flex flex-wrap gap-1">${dispatchedHtml}</div>
    </div>
    <div class="mt-3 text-secondary" style="font-size:12px">
      Running = currently active on this worker. Dispatched = current manager placement set, not completed run history.
    </div>
  </div>`
}

function summarizeStreams(streams) {
  return streams.reduce((summary, stream) => {
    summary.activeSlots += stream.active_slots || 0
    summary.recordsOutPerSec += stream.records_out_per_sec || 0
    summary.bytesOutPerSec += stream.bytes_out_per_sec || 0
    summary.errors += stream.error_count || 0
    return summary
  }, { activeSlots: 0, recordsOutPerSec: 0, bytesOutPerSec: 0, errors: 0 })
}

function shortNodeLabel(url) {
  if (!url) return 'Worker'
  try {
    return new URL(url).hostname.split('.')[0]
  } catch (_) {
    return url
  }
}

function formatRate(value) {
  return `${fmtNum(Math.round(value || 0))}/s`
}

function formatBytesRate(value) {
  const bytes = Number(value || 0)
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB/s`
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB/s`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB/s`
  return `${fmtNum(Math.round(bytes))} B/s`
}

function _set(id, value) {
  const el = document.getElementById(id)
  if (el) el.textContent = value
}
