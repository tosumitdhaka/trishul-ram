// Run detail page — #runs/:runId. The deep-linkable, shareable view of a
// single run's outcome, promoting the run-issues expandable row to a full
// page (failure reason, grouped skip reasons, DLQ block) with links back
// to the pipeline and the filtered run list.
import { api } from '../api.js'
import { router } from '../router.js'
import { createPageController } from '../page.js'
import { fmtDur, fmtNum, relTime, statusBadge, esc, toast } from '../utils.js'
import { runDetailHtml } from './runs_table.js'

let _runId = null

const controller = createPageController({
  page: 'runs_detail',
  fetch: () => api.runs.get(_runId),
  render: renderRun,
  onError: (e, mode) => {
    if (e.status === 404) {
      renderNotFound()
      return
    }
    if (mode === 'initial') {
      renderError(e)
      return
    }
    toast(e.message, 'error')
  },
})

export async function init() {
  const { params } = router.route()
  _runId = params[0] || null
  if (!_runId) { router.navigate('runs'); return }

  document.getElementById('run-detail-back-btn').onclick = () => router.navigate('runs')
  document.getElementById('run-detail-refresh-btn').onclick = () => { void refreshRun() }

  await controller.mount()
}

async function refreshRun() {
  const icon = document.getElementById('run-detail-refresh-icon')
  if (icon) icon.className = 'bi bi-arrow-clockwise spin'
  try {
    await controller.refresh()
  } finally {
    if (icon) icon.className = 'bi bi-arrow-clockwise'
  }
}

function renderRun(run) {
  const content = document.getElementById('run-detail-content')
  if (!content) return

  // Back returns to the run list scoped to this run's pipeline.
  document.getElementById('run-detail-back-btn').onclick = () => {
    router.navigate(`runs?pipeline=${encodeURIComponent(run.pipeline || '')}`)
  }

  content.innerHTML = `
    <div class="row g-3 mb-3">
      <div class="col-12 col-md-4">
        <div class="detail-card">
          <div class="detail-label">Run</div>
          <div class="detail-val"><span class="mono-sm">${esc(String(run.run_id || _runId))}</span></div>
          <div class="detail-sub">started ${run.started_at ? `${esc(fmtLocal(run.started_at))} (${relTime(run.started_at)})` : '—'}</div>
        </div>
      </div>
      <div class="col-12 col-md-4">
        <div class="detail-card">
          <div class="detail-label">Pipeline</div>
          <div class="detail-val">
            <a class="table-row-name-link" href="#detail/${encodeURIComponent(run.pipeline || '')}?tab=runs">${esc(run.pipeline || '—')}</a>
          </div>
          <div class="detail-sub">${esc(run.node ? `node ${run.node}` : 'node —')}</div>
        </div>
      </div>
      <div class="col-12 col-md-4">
        <div class="detail-card">
          <div class="detail-label">Outcome</div>
          <div class="detail-val">${statusBadge(run.status)}</div>
          <div class="detail-sub">${esc(fmtDur(run.started_at, run.finished_at))}${run.finished_at ? ` · finished ${relTime(run.finished_at)}` : ''}</div>
        </div>
      </div>
    </div>
    <div class="row g-3 mb-3">
      <div class="col-12 col-md-4">
        <div class="detail-card">
          <div class="detail-label">Records</div>
          <div class="detail-val">
            <span class="num-in">${fmtNum(run.records_in)}</span>
            <i class="bi bi-arrow-right text-secondary"></i>
            <span class="num-out">${fmtNum(run.records_out)}</span>
          </div>
          <div class="detail-sub">${fmtNum(run.records_skipped)} skipped · ${fmtNum(run.dlq_count || 0)} DLQ</div>
        </div>
      </div>
    </div>
    <div class="detail-card">
      <div class="detail-label">Issues</div>
      <div class="run-issues-page-body">${runDetailHtml(run)}</div>
    </div>
  `
}

function renderNotFound() {
  const content = document.getElementById('run-detail-content')
  if (!content) return
  content.innerHTML = `
    <div class="detail-card text-center p-4">
      <div class="mb-2"><i class="bi bi-search"></i></div>
      <div>Run <span class="mono-sm">${esc(String(_runId))}</span> was not found — it may have been trimmed from history.</div>
      <div class="mt-3">
        <button class="btn btn-sm btn-outline-secondary" type="button" id="run-detail-nf-back">
          <i class="bi bi-arrow-left me-1"></i>Back to Run History
        </button>
      </div>
    </div>
  `
  document.getElementById('run-detail-nf-back')?.addEventListener('click', () => router.navigate('runs'))
}

function renderError(e) {
  const content = document.getElementById('run-detail-content')
  if (!content) return
  content.innerHTML = `
    <div class="detail-card text-center p-4">
      <div class="table-state-error mb-2"><i class="bi bi-exclamation-triangle me-1"></i>Could not load run: ${esc(e.message)}</div>
      <button class="btn btn-sm btn-outline-secondary" type="button" id="run-detail-retry">
        <i class="bi bi-arrow-clockwise me-1"></i>Retry
      </button>
    </div>
  `
  document.getElementById('run-detail-retry')?.addEventListener('click', () => { void controller.mount() })
}

// Local timestamp for the header — keeps the shared relTime for the "ago" part.
function fmtLocal(iso) {
  const dt = new Date(iso)
  return Number.isNaN(dt.getTime()) ? String(iso) : dt.toLocaleString()
}
