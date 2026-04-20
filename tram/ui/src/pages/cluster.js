import { api } from '../api.js'
import { esc, fmtNum, relTime, statusBadge, toast } from '../utils.js'

export async function init() {
  try {
    const [status, streamsResp] = await Promise.all([
      api.cluster.nodes(),
      api.cluster.streams(),
    ])
    renderCluster(status, streamsResp?.streams || [])
  } catch (e) {
    const txt = document.getElementById('cluster-status-text')
    if (txt) txt.textContent = 'Cluster info unavailable — daemon offline'
    toast(`Cluster error: ${e.message}`, 'error')
  }
}

function renderCluster(status, streams) {
  const txt  = document.getElementById('cluster-status-text')
  const mode = status?.mode || 'standalone'
  renderStreams(streams)

  // ── Manager+worker mode (v1.2.0) ──────────────────────────────────────────
  if (mode === 'manager') {
    const workers = status?.workers || []
    const online  = workers.filter(w => w.ok).length
    const runs    = workers.reduce((s, w) => s + (w.active_runs || 0), 0)
    if (txt) txt.textContent =
      `Manager mode · ${workers.length} worker${workers.length !== 1 ? 's' : ''} · ${online} online · ${runs} active run${runs !== 1 ? 's' : ''}`

    const container = document.getElementById('cluster-nodes')
    if (!container) return

    if (!workers.length) {
      container.innerHTML = '<div class="p-4 text-secondary text-center">No workers configured</div>'
      return
    }

    container.innerHTML = workers.map((w, i) => {
      const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${w.ok ? '#3fb950' : '#f85149'};margin-right:6px"></span>`
      // Extract a short node name from the URL hostname (e.g. "worker-0" from the k8s DNS)
      let nodeLabel = w.url
      try {
        const h = new URL(w.url).hostname
        nodeLabel = h.split('.')[0]   // "trishul-ram-worker-0"
      } catch (_) {}
      const addr = w.url

      // Pipeline list per worker
      const assigned = w.assigned_pipelines || []
      const running  = w.running_pipelines  || []
      const assignedHtml = assigned.length
        ? `<div class="mt-2"><span class="text-secondary d-block mb-1" style="font-size:11px">Dispatched pipelines</span>
            <div class="d-flex flex-wrap gap-1">${assigned.map(p => {
              const isRunning = running.includes(p)
              return `<span class="type-pill" style="${isRunning ? 'background:#1a3328;color:#3fb950' : ''}">${esc(p)}${isRunning ? ' ▶' : ''}</span>`
            }).join('')}</div></div>`
        : ''
      const pipelineHtml = assignedHtml

      return `<div class="accordion-item" style="border:none;border-bottom:1px solid var(--border)">
        <h2 class="accordion-header">
          <button class="accordion-button collapsed" type="button"
                  data-bs-toggle="collapse" data-bs-target="#cn-${i}"
                  style="background:var(--bg-page);color:var(--fg);font-size:13px">
            ${dot}${esc(nodeLabel)}
            <span class="ms-2 text-secondary" style="font-size:12px">${esc(addr)}</span>
            <span class="ms-auto me-2 badge ${w.ok ? 'bg-success' : 'bg-danger'}" style="font-size:11px">${w.ok ? 'online' : 'offline'}</span>
            <span class="me-2 badge bg-secondary" style="font-size:11px">${w.active_runs ?? 0} active run${w.active_runs !== 1 ? 's' : ''}</span>
          </button>
        </h2>
        <div id="cn-${i}" class="accordion-collapse collapse">
          <div class="accordion-body" style="background:var(--bg-surface);font-size:13px">
            <div class="row g-2 mb-2">
              <div class="col-4"><span class="text-secondary">Status</span><div>${w.ok ? 'online' : 'offline'}</div></div>
              <div class="col-4"><span class="text-secondary">Active Runs</span><div>${w.active_runs ?? '—'}</div></div>
              <div class="col-4"><span class="text-secondary">Endpoint</span><div class="text-secondary" style="font-size:11px;word-break:break-all">${esc(w.url)}</div></div>
            </div>
            ${pipelineHtml}
          </div>
        </div>
      </div>`
    }).join('')
    return
  }

  // ── Standalone or legacy cluster mode ─────────────────────────────────────
  const nodes   = status?.nodes || []
  const enabled = status?.cluster_enabled

  const totalPipelines = nodes.reduce((sum, n) => sum + (n.pipeline_count || 0), 0)
  if (txt) txt.textContent = (!enabled && mode === 'standalone')
    ? 'Standalone mode — single node, no workers'
    : enabled
      ? `Cluster active · ${nodes.length} node${nodes.length !== 1 ? 's' : ''} · ${totalPipelines} pipeline${totalPipelines !== 1 ? 's' : ''}`
      : 'Cluster disabled — running in standalone mode'

  const container = document.getElementById('cluster-nodes')
  if (!container) return

  if (!nodes.length) {
    container.innerHTML = '<div class="p-4 text-secondary text-center">No cluster nodes found</div>'
    return
  }

  container.innerHTML = nodes.map((node, i) => {
    const isOnline = node.status === 'online' || node.status === 'active'
    const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${isOnline ? '#3fb950' : '#f85149'};margin-right:6px"></span>`
    const pipelines = (node.pipelines || []).map(p => `<span class="type-pill">${esc(p)}</span>`).join(' ')
    return `<div class="accordion-item" style="border:none;border-bottom:1px solid var(--border)">
      <h2 class="accordion-header">
        <button class="accordion-button collapsed" type="button"
                data-bs-toggle="collapse" data-bs-target="#cn-${i}"
                style="background:var(--bg-page);color:var(--fg);font-size:13px">
          ${dot}${esc(node.node_id || node.id || `Node ${i + 1}`)}
          <span class="ms-2 text-secondary" style="font-size:12px">${esc(node.address || '')}</span>
          <span class="mx-auto text-secondary" style="font-size:12px">${node.last_heartbeat ? relTime(node.last_heartbeat) : ''}</span>
          <span class="me-2 badge bg-secondary" style="font-size:11px">${node.pipeline_count ?? 0} pipeline${node.pipeline_count !== 1 ? 's' : ''}</span>
        </button>
      </h2>
      <div id="cn-${i}" class="accordion-collapse collapse">
        <div class="accordion-body" style="background:var(--bg-surface);font-size:13px">
          <div class="row g-2 mb-2">
            <div class="col-4"><span class="text-secondary">Status</span><div>${esc(node.status || '—')}</div></div>
            <div class="col-4"><span class="text-secondary">Pipelines</span><div>${node.pipeline_count ?? '—'}</div></div>
            <div class="col-4"><span class="text-secondary">Joined</span><div>${node.registered_at ? relTime(node.registered_at) : '—'}</div></div>
          </div>
          ${pipelines ? `<div class="mt-2"><span class="text-secondary d-block mb-1">Assigned pipelines</span>${pipelines}</div>` : ''}
        </div>
      </div>
    </div>`
  }).join('')
}

function renderStreams(streams) {
  const count = document.getElementById('cluster-stream-count')
  const container = document.getElementById('cluster-streams')
  if (!container) return

  if (count) {
    count.textContent = `${streams.length} stream${streams.length !== 1 ? 's' : ''}`
  }

  if (!streams.length) {
    container.innerHTML = '<div class="text-secondary text-center py-3">No active streams</div>'
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
            const slots = Array.isArray(stream.slots) ? stream.slots : []
            const healthy = slots.filter(slot => !slot.stats?.stale).length
            const stale = slots.filter(slot => slot.stats?.stale).length
            return `<tr style="cursor:pointer" onclick="window._clusterOpenPlacement('${esc(stream.pipeline_name)}')">
              <td class="fw-semibold">${esc(stream.pipeline_name)}</td>
              <td>${statusBadge(stream.status)}</td>
              <td class="text-secondary">${healthy}/${stream.slot_count}${stale ? ` · ${stale} stale` : ''}</td>
              <td class="text-secondary">${fmtNum(Math.round(stream.records_out_per_sec || 0))}</td>
              <td class="text-secondary">${fmtNum(Math.round(stream.bytes_out_per_sec || 0))}</td>
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
