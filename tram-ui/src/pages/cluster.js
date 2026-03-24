import { api } from '../api.js'
import { relTime, esc, toast } from '../utils.js'

export async function init() {
  try {
    const status = await api.daemon.status()
    renderCluster(status)
  } catch (e) {
    const txt = document.getElementById('cluster-status-text')
    if (txt) txt.textContent = 'Cluster info unavailable — daemon offline'
    toast(`Cluster error: ${e.message}`, 'error')
  }
}

function renderCluster(status) {
  const txt   = document.getElementById('cluster-status-text')
  const nodes = status?.nodes || []
  const mode  = status?.cluster_mode || 'standalone'

  if (txt) txt.textContent = mode === 'standalone'
    ? 'Cluster disabled — running in standalone mode'
    : `Cluster active · ${nodes.length} node${nodes.length !== 1 ? 's' : ''}`

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
    return `<div class="accordion-item" style="border:none;border-bottom:1px solid #30363d">
      <h2 class="accordion-header">
        <button class="accordion-button collapsed" type="button"
                data-bs-toggle="collapse" data-bs-target="#cn-${i}"
                style="background:#0d1117;color:#e6edf3;font-size:13px">
          ${dot}${esc(node.node_id || node.id || `Node ${i + 1}`)}
          <span class="ms-2 text-secondary" style="font-size:12px">${esc(node.address || '')}</span>
          <span class="ms-auto me-2 text-secondary" style="font-size:12px">${node.last_seen ? relTime(node.last_seen) : ''}</span>
        </button>
      </h2>
      <div id="cn-${i}" class="accordion-collapse collapse">
        <div class="accordion-body" style="background:#161b22;font-size:13px">
          <div class="row g-2 mb-2">
            <div class="col-4"><span class="text-secondary">Status</span><div>${esc(node.status || '—')}</div></div>
            <div class="col-4"><span class="text-secondary">Pipelines</span><div>${node.pipeline_count ?? '—'}</div></div>
            <div class="col-4"><span class="text-secondary">Joined</span><div>${node.joined_at ? relTime(node.joined_at) : '—'}</div></div>
          </div>
          ${pipelines ? `<div class="mt-2"><span class="text-secondary d-block mb-1">Assigned pipelines</span>${pipelines}</div>` : ''}
        </div>
      </div>
    </div>`
  }).join('')
}
