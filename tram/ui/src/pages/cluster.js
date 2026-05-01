import { api } from '../api.js'
import {
  bindDataActions,
  esc,
  fmtBytes,
  fmtBytesRate,
  fmtNum,
  fmtRate,
  getSavedPollIntervalMs,
  statusBadge,
  toast,
} from '../utils.js'

const _openWorkers = {}
let _pollTimer = null
let _refreshInFlight = null
let _workers = []

export async function init() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }
  wireActions()
  await refresh()

  const pollMs = getSavedPollIntervalMs()
  _pollTimer = setInterval(() => {
    if (!document.getElementById('cluster-streams')) {
      clearInterval(_pollTimer)
      _pollTimer = null
      return
    }
    void refresh({ silent: true })
  }, pollMs)
}

function wireActions() {
  document.getElementById('cluster-refresh-btn')?.addEventListener('click', async () => {
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
  })
  bindDataActions(document.getElementById('cluster-streams'), {
    'open-placement': (row) => {
      window._detailPipeline = row.dataset.pipelineName
      navigate('detail')
    },
  })
  bindDataActions(document.getElementById('cluster-nodes'), {
    'toggle-worker': (button) => {
      const key = button.dataset.workerKey
      _openWorkers[key] = !(_openWorkers[key] ?? false)
      renderWorkers(_workers)
    },
  })
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
  const dbEngineEl = document.getElementById('cluster-daemon-db-engine')
  const dbMetaEl = document.getElementById('cluster-daemon-db-meta')
  const dbCardEl = document.getElementById('cluster-daemon-db-card')
  const uptimeEl = document.getElementById('cluster-daemon-uptime')

  if (!ready) {
    if (modeEl) modeEl.textContent = 'unavailable'
    if (modeSubEl) modeSubEl.textContent = 'Runtime status unavailable'
    if (dbEngineEl) dbEngineEl.textContent = 'unavailable'
    if (dbMetaEl) dbMetaEl.textContent = 'Runtime status unavailable'
    if (uptimeEl) uptimeEl.textContent = '—'
    dbEngineEl?.classList.remove('is-ok', 'is-error')
    dbCardEl?.classList.remove('is-ok', 'is-error')
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

  if (dbEngineEl) {
    dbEngineEl.textContent = ready.db_engine || 'SQLite'
    dbEngineEl.classList.toggle('is-ok', ready.db === 'ok')
    dbEngineEl.classList.toggle('is-error', ready.db !== 'ok')
  }
  if (dbMetaEl) {
    dbMetaEl.textContent = ready.db === 'ok'
      ? (ready.db_path ? `Connected · ${ready.db_path}` : 'Connected')
      : (ready.detail || ready.db || 'Unavailable')
  }
  if (dbCardEl) {
    dbCardEl.classList.toggle('is-ok', ready.db === 'ok')
    dbCardEl.classList.toggle('is-error', ready.db !== 'ok')
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
  _workers = workers

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
    txt.textContent = `Standalone mode · local daemon runtime · ${streams.length} active pipeline${streams.length !== 1 ? 's' : ''}`
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
  _set('cluster-workers-value', mode === 'manager' ? `${onlineWorkers}/${workers.length || 0} online` : 'Local runtime')
  _set('cluster-workers-sub', mode === 'manager'
    ? `${activeRuns} active runs · ${streams.length} active pipelines`
    : `${stats?.runs_last_hour ?? 0} completed runs in the last hour`)
  _set('cluster-streams-value', fmtNum(streams.length))
  _set(
    'cluster-streams-sub',
    `${streamTotals.activeSlots} active slot${streamTotals.activeSlots !== 1 ? 's' : ''} · ${fmtNum(activeRuns)} active run${activeRuns !== 1 ? 's' : ''}`,
  )
  _set('cluster-running-value', fmtNum(mode === 'manager' ? liveRunningPipelines : (stats?.pipelines_running ?? liveRunningPipelines)))
  _set('cluster-running-sub', `${fmtNum(stats?.pipelines_scheduled ?? 0)} scheduled · ${fmtNum(activeRuns)} active runs`)
  _set('cluster-input-value', fmtRate(streamTotals.recordsInPerSec))
  _set('cluster-input-sub', `${fmtBytesRate(streamTotals.bytesInPerSec)} · ${fmtNum(streamTotals.recordsIn)} records`)
  _set('cluster-output-value', fmtRate(streamTotals.recordsOutPerSec))
  _set('cluster-output-sub', `${fmtBytesRate(streamTotals.bytesOutPerSec)} · ${fmtNum(streamTotals.recordsOut)} records`)
  _set('cluster-errors-value', fmtNum(stats?.pipelines_error ?? streamTotals.errors ?? 0))
  _set(
    'cluster-errors-sub',
    `${fmtNum(stats?.errors_last_15m ?? 0)} in last 15m · ${fmtNum(streamTotals.errors)} live pipeline errors`,
  )
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
            <th>Active Streams</th>
            <th>Records Processed</th>
            <th>Bytes Processed</th>
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
        <div class="cluster-body-note">Standalone mode has no worker pool. Active pipelines still appear above while they are running.</div>
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
        <div class="cluster-node-meta-label">Records Out/s</div>
        <div class="cluster-node-meta-value">${fmtRate(streamTotals.recordsOutPerSec)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Bytes Out/s</div>
        <div class="cluster-node-meta-value">${fmtBytesRate(streamTotals.bytesOutPerSec)}</div>
      </div>
    </div>
    <div class="cluster-body-note">
      Batch and manual pipelines do not stay on this page after completion. Use pipeline detail for terminal run results and active stream placement for live workloads.
    </div>
  </div>`
}

function renderStreams(streams, mode) {
  const count = document.getElementById('cluster-stream-count')
  const container = document.getElementById('cluster-streams')
  if (!container) return

  if (count) {
    count.textContent = `${streams.length} active pipeline${streams.length !== 1 ? 's' : ''}`
  }

  if (!streams.length) {
    container.innerHTML = `<div class="text-secondary text-center py-3">${
      mode === 'manager'
        ? 'No active pipelines at the moment.'
        : 'No active pipelines in the local runtime.'
    }</div>`
    return
  }

  container.innerHTML = `
    <div class="table-wrap table-wrap-subtle">
      <table class="table mb-0">
        <thead>
          <tr>
            <th>Pipeline</th>
            <th>Status</th>
            <th>Slots</th>
            <th>In/s</th>
            <th>Out/s</th>
            <th>Bytes In/s</th>
            <th>Bytes Out/s</th>
            <th>Errors</th>
          </tr>
        </thead>
        <tbody>
          ${streams.map(stream => {
            const slots = Array.isArray(stream.slots) ? stream.slots : []
            const healthy = slots.filter(slot => !slot.stats?.stale).length
            const stale = slots.filter(slot => slot.stats?.stale).length
            return `<tr class="table-row-link" data-action="open-placement" data-pipeline-name="${esc(stream.pipeline_name)}">
              <td class="fw-semibold">${esc(stream.pipeline_name)}</td>
              <td>${statusBadge(stream.status)}</td>
              <td class="text-secondary">${healthy}/${stream.slot_count}${stale ? ` · ${stale} stale` : ''}</td>
              <td class="text-secondary">${fmtRate(stream.records_in_per_sec || 0)}</td>
              <td class="text-secondary">${fmtRate(stream.records_out_per_sec || 0)}</td>
              <td class="text-secondary">${fmtBytesRate(stream.bytes_in_per_sec || 0)}</td>
              <td class="text-secondary">${fmtBytesRate(stream.bytes_out_per_sec || 0)}</td>
              <td class="text-secondary">${fmtNum(stream.error_count || 0)}</td>
            </tr>`
          }).join('')}
        </tbody>
      </table>
    </div>`
}

function renderWorkerRow(worker) {
  const assigned = worker.assigned_pipelines || []
  const running = new Set(worker.running_pipelines || [])
  const load = summarizeWorkerLoad(worker)
  const nodeLabel = shortNodeLabel(worker.url)
  const rowKey = worker.url || nodeLabel
  const open = _openWorkers[rowKey] ?? false
  const toggleLabel = open ? 'Collapse worker details' : 'Expand worker details'
  const stateBadge = `<span class="badge ${worker.ok ? 'bg-success' : 'bg-danger'}">${worker.ok ? 'online' : 'offline'}</span>`
  return `
    <tr>
      <td>
        <div class="fw-semibold">${esc(nodeLabel)}</div>
        <div class="cluster-worker-url">${esc(worker.url || '—')}</div>
      </td>
      <td>${stateBadge}</td>
      <td class="text-secondary">${fmtNum(worker.active_runs ?? 0)}</td>
      <td class="text-secondary">${fmtNum(worker.active_streams ?? load.activeStreams)}</td>
      <td class="text-secondary">${fmtNum(load.recordsProcessed)}</td>
      <td class="text-secondary">${fmtBytes(load.bytesProcessed)}</td>
      <td class="text-secondary">${fmtNum(running.size)}</td>
      <td class="text-secondary">${fmtNum(assigned.length)}</td>
      <td class="text-end">
        <button class="btn-flat"
                type="button"
                title="${toggleLabel}"
                aria-label="${toggleLabel}"
                data-action="toggle-worker"
                data-worker-key="${esc(rowKey)}">
          <i class="bi bi-chevron-${open ? 'down' : 'right'}"></i>
        </button>
      </td>
    </tr>
    ${open ? `<tr class="cluster-worker-detail-row">
      <td colspan="9">
        ${renderWorkerExpanded(worker, assigned, running, load)}
      </td>
    </tr>` : ''}`
}

function renderWorkerExpanded(worker, assigned, running, load) {
  const runningList = [...running]
  const runningHtml = runningList.length
    ? runningList.map(name => `<span class="type-pill cluster-pill-running">${esc(name)} ▶</span>`).join('')
    : '<span class="text-secondary">None</span>'
  const dispatchedHtml = assigned.length
    ? assigned.map(name => `<span class="type-pill cluster-pill-neutral">${esc(name)}</span>`).join('')
    : '<span class="text-secondary">None</span>'

  return `<div class="cluster-worker-expanded">
    <div class="cluster-worker-expanded-head">
      <div class="cluster-worker-expanded-head-url">
        <div class="cluster-node-meta-label">Worker URL</div>
        <div class="mono cluster-worker-url">${esc(worker.url || '—')}</div>
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
        <div class="cluster-node-meta-label">Active Streams</div>
        <div>${fmtNum(worker.active_streams ?? load.activeStreams)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Live Errors</div>
        <div>${fmtNum(load.errors)}</div>
      </div>
    </div>
    <div class="cluster-worker-expanded-stats mt-3">
      <div>
        <div class="cluster-node-meta-label">Records Processed</div>
        <div>${fmtNum(load.recordsProcessed)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Bytes Processed</div>
        <div>${fmtBytes(load.bytesProcessed)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Records In/s</div>
        <div>${fmtRate(load.recordsInPerSec)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Records Out/s</div>
        <div>${fmtRate(load.recordsOutPerSec)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Bytes In/s</div>
        <div>${fmtBytesRate(load.bytesInPerSec)}</div>
      </div>
      <div>
        <div class="cluster-node-meta-label">Bytes Out/s</div>
        <div>${fmtBytesRate(load.bytesOutPerSec)}</div>
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
    <div class="cluster-body-note mt-3">
      Rates aggregate the worker's current active batch and stream workloads. Dispatched = current manager placement set, not completed run history.
    </div>
  </div>`
}

function summarizeStreams(streams) {
  return streams.reduce((summary, stream) => {
    summary.activeSlots += stream.active_slots || 0
    summary.recordsIn += stream.records_in || 0
    summary.recordsOut += stream.records_out || 0
    summary.bytesIn += stream.bytes_in || 0
    summary.bytesOut += stream.bytes_out || 0
    summary.recordsInPerSec += stream.records_in_per_sec || 0
    summary.recordsOutPerSec += stream.records_out_per_sec || 0
    summary.bytesInPerSec += stream.bytes_in_per_sec || 0
    summary.bytesOutPerSec += stream.bytes_out_per_sec || 0
    summary.errors += stream.error_count || 0
    return summary
  }, {
    activeSlots: 0,
    recordsIn: 0,
    recordsOut: 0,
    bytesIn: 0,
    bytesOut: 0,
    recordsInPerSec: 0,
    recordsOutPerSec: 0,
    bytesInPerSec: 0,
    bytesOutPerSec: 0,
    errors: 0,
  })
}

function summarizeWorkerLoad(worker) {
  const running = Array.isArray(worker?.running) ? worker.running : []
  const streams = Array.isArray(worker?.streams) ? worker.streams : []
  return [...running, ...streams].reduce((summary, item) => {
    const stats = item?.stats || {}
    const uptimeSeconds = Number(item?.uptime_seconds || 0)
    summary.activeStreams = streams.length
    summary.recordsProcessed += Number(stats.records_in || 0) + Number(stats.records_out || 0)
    summary.bytesProcessed += Number(stats.bytes_in || 0) + Number(stats.bytes_out || 0)
    summary.recordsInPerSec += rateFromLiveItem(stats.records_in, uptimeSeconds)
    summary.recordsOutPerSec += rateFromLiveItem(stats.records_out, uptimeSeconds)
    summary.bytesInPerSec += rateFromLiveItem(stats.bytes_in, uptimeSeconds)
    summary.bytesOutPerSec += rateFromLiveItem(stats.bytes_out, uptimeSeconds)
    summary.errors += Number(stats.error_count || 0)
    return summary
  }, {
    activeStreams: streams.length,
    recordsProcessed: 0,
    bytesProcessed: 0,
    recordsInPerSec: 0,
    recordsOutPerSec: 0,
    bytesInPerSec: 0,
    bytesOutPerSec: 0,
    errors: 0,
  })
}

function shortNodeLabel(url) {
  if (!url) return 'Worker'
  try {
    return new URL(url).hostname.split('.')[0]
  } catch (_) {
    return url
  }
}

function rateFromLiveItem(total, uptimeSeconds) {
  const uptime = Number(uptimeSeconds || 0)
  if (!Number.isFinite(uptime) || uptime <= 0) return 0
  return Number(total || 0) / uptime
}

function _set(id, value) {
  const el = document.getElementById(id)
  if (el) el.textContent = value
}
