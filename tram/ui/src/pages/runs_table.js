import { relTime, fmtDur, fmtNum, statusBadge, esc } from '../utils.js'

// Expanded run-issue rows, keyed by run id per tbody. Keying by id (not row
// index) keeps expansions open across poll re-renders and aligned even when
// new runs shift the row order.
const _expanded = new WeakMap()

function _expandedSet(tbody) {
  if (!_expanded.has(tbody)) _expanded.set(tbody, new Set())
  return _expanded.get(tbody)
}

export function renderRunsTable({
  tbody,
  runs,
  rowIdPrefix = 'runs',
  emptyMessage = 'No runs found',
  colspan = 12,
}) {
  if (!tbody) return
  const expanded = _expandedSet(tbody)
  // Drop expansions for runs that no longer exist in this page of data.
  const visibleIds = new Set(runs.map(r => String(r.run_id || r.id || '')))
  for (const id of expanded) {
    if (!visibleIds.has(id)) expanded.delete(id)
  }
  if (!runs.length) {
    expanded.clear()
    tbody.innerHTML = `<tr><td colspan="${colspan}" class="text-secondary text-center py-4">${esc(emptyMessage)}</td></tr>`
    tbody.onclick = null
    return
  }

  const rows = []
  runs.forEach((r) => {
    const runId = String(r.run_id || r.id || '')
    const failureReason = topLevelFailureReason(r)
    const reasonGroups = groupedIssueReasons(r, failureReason)
    const summary = issueSummary(r, failureReason, reasonGroups)
    const tooltip = issueTooltip(r, failureReason, reasonGroups)
    const hasDetail = Boolean(failureReason) || Boolean(reasonGroups.length) || r.records_skipped > 0 || r.dlq_count > 0
    const toggle = hasDetail
      ? `<button class="btn-flat runs-expand-btn" type="button" data-run-toggle="${esc(runId)}" aria-label="Toggle run details"><i class="bi ${expanded.has(runId) ? 'bi-chevron-down' : 'bi-chevron-right'} runs-chevron"></i></button>`
      : ''
    rows.push(`<tr data-run-id="${esc(runId)}">
      <td class="mono-sm"><a class="table-row-name-link" href="#runs/${encodeURIComponent(runId)}" title="Open run detail">${esc(runId.slice(0, 8))}</a></td>
      <td class="fw-semibold">${esc(r.pipeline)}</td>
      <td class="text-secondary">${esc(r.node || '—')}</td>
      <td class="text-secondary">${r.started_at ? relTime(r.started_at) : '—'}</td>
      <td class="text-secondary">${fmtDur(r.started_at, r.finished_at)}</td>
      <td class="num-in">${fmtNum(r.records_in)}</td>
      <td class="num-out">${fmtNum(r.records_out)}</td>
      <td class="text-secondary">${fmtNum(r.records_skipped)}</td>
      <td class="text-secondary">${fmtNum(r.dlq_count)}</td>
      <td>${statusBadge(r.status)}</td>
      <td class="text-secondary runs-issue-cell" title="${esc(tooltip)}">${esc(summary)}</td>
      <td class="text-end runs-toggle-cell">${toggle}</td>
    </tr>`)
  })
  tbody.innerHTML = rows.join('')

  // Re-insert detail rows for expansions that survived the re-render.
  runs.forEach((r) => {
    const runId = String(r.run_id || r.id || '')
    if (!expanded.has(runId)) return
    const row = tbody.querySelector(`tr[data-run-id="${cssq(runId)}"]`)
    if (row) row.after(_detailRow(r, colspan))
  })

  tbody.onclick = (event) => {
    const toggleButton = event.target.closest('[data-run-toggle]')
    if (!toggleButton || !tbody.contains(toggleButton)) return
    const runId = toggleButton.dataset.runToggle
    const run = runs.find(r => String(r.run_id || r.id || '') === runId)
    if (!run) return
    const row = tbody.querySelector(`tr[data-run-id="${cssq(runId)}"]`)
    const existingLog = row?.nextElementSibling?.dataset?.runDetail === runId ? row.nextElementSibling : null
    const chev = toggleButton.querySelector('.runs-chevron')
    if (existingLog) {
      existingLog.remove()
      expanded.delete(runId)
      if (chev) {
        chev.classList.remove('bi-chevron-down')
        chev.classList.add('bi-chevron-right')
      }
      return
    }
    row?.after(_detailRow(run, colspan))
    expanded.add(runId)
    if (chev) {
      chev.classList.remove('bi-chevron-right')
      chev.classList.add('bi-chevron-down')
    }
  }
}

function cssq(value) {
  return (window.CSS && CSS.escape) ? CSS.escape(String(value)) : String(value)
}

function _detailRow(r, colspan) {
  const runId = String(r.run_id || r.id || '')
  const logRow = document.createElement('tr')
  logRow.className = 'error-detail-row'
  logRow.dataset.runDetail = runId
  logRow.innerHTML = `<td colspan="${colspan}" class="run-issues-cell">${runDetailHtml(r)}</td>`
  return logRow
}

// The shared issue-detail renderer — used by the expandable row here and by
// the #runs/:id detail page (L4).
export function runDetailHtml(r) {
  const failureReason = topLevelFailureReason(r)
  const reasonGroups = groupedIssueReasons(r, failureReason)
  const details = []
  if (failureReason) {
    details.push(`
      <div class="run-issue-block">
        <div class="run-issue-heading run-issue-heading-danger">
          <i class="bi bi-x-circle"></i>
          <span>Pipeline failure</span>
        </div>
        <div class="run-issue-text">${esc(failureReason)}</div>
      </div>`)
  }
  if (r.records_skipped > 0 || reasonGroups.length) {
    const reasonHeading = r.records_skipped > 0
      ? `${fmtNum(r.records_skipped)} record(s) skipped`
      : 'Recorded reasons'
    details.push(`
      <div class="run-issue-block">
        <div class="run-issue-heading">
          <i class="bi bi-skip-forward"></i>
          <span>${reasonHeading}</span>
        </div>
        ${reasonGroups.length
          ? `<div class="run-issue-list">${reasonGroups.map(([reason, count]) => `
              <div class="run-issue-item">
                <span class="run-issue-count">${count > 1 ? `${fmtNum(count)}x` : '1x'}</span>
                <span class="run-issue-text">${esc(reason)}</span>
              </div>`).join('')}</div>`
          : `<div class="run-issue-text text-secondary">No skip reason captured</div>`}
      </div>`)
  }
  if (r.dlq_count > 0) {
    details.push(`
      <div class="run-issue-block">
        <div class="run-issue-heading">
          <i class="bi bi-inbox"></i>
          <span>${fmtNum(r.dlq_count)} record(s) sent to DLQ</span>
        </div>
      </div>`)
  }
  return details.join('') || '<div class="text-secondary">No error details available</div>'
}

function topLevelFailureReason(r) {
  const status = String(r.status || '').toLowerCase()
  const failed = status === 'failed' || status === 'aborted' || status === 'error'
  if (!failed) return ''
  if (r.error) return r.error
  const fallback = Array.from(new Set((r.errors || []).filter(Boolean)))[0]
  return fallback || ''
}

function issueSummary(r, failureReason, reasonGroups) {
  const parts = []
  if (r.records_skipped > 0) parts.push(`${fmtNum(r.records_skipped)} skipped`)
  if (r.dlq_count > 0) parts.push(`${fmtNum(r.dlq_count)} DLQ`)
  if (failureReason) parts.push(failureReason)
  if (!parts.length && reasonGroups.length) return `${reasonGroups.length} issue reason${reasonGroups.length === 1 ? '' : 's'}`
  return parts.join(' · ') || '—'
}

function issueTooltip(r, failureReason, reasonGroups) {
  const previews = []
  if (r.records_skipped > 0) previews.push(`${fmtNum(r.records_skipped)} record(s) skipped`)
  if (r.dlq_count > 0) previews.push(`${fmtNum(r.dlq_count)} record(s) sent to DLQ`)
  if (failureReason) previews.push(failureReason)
  if (!failureReason && reasonGroups.length) {
    previews.push(...reasonGroups.slice(0, 3).map(([reason, count]) => count > 1 ? `${fmtNum(count)}x ${reason}` : reason))
  }
  return previews.join(' | ') || 'No issues captured'
}

function groupedIssueReasons(r, failureReason) {
  const counts = new Map()
  for (const raw of Array.isArray(r.errors) ? r.errors : []) {
    const msg = String(raw || '').trim()
    if (!msg) continue
    if (failureReason && msg === failureReason) continue
    counts.set(msg, (counts.get(msg) || 0) + 1)
  }
  return Array.from(counts.entries()).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
}
