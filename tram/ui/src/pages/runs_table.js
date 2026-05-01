import { relTime, fmtDur, fmtNum, statusBadge, esc } from '../utils.js'

export function renderRunsTable({
  tbody,
  runs,
  rowIdPrefix = 'runs',
  toggleHandlerName = '_runsToggleLog',
  emptyMessage = 'No runs found',
  colspan = 12,
}) {
  if (!tbody) return
  if (!runs.length) {
    tbody.innerHTML = `<tr><td colspan="${colspan}" class="text-secondary text-center py-4">${esc(emptyMessage)}</td></tr>`
    tbody.onclick = null
    return
  }

  const rows = []
  runs.forEach((r, i) => {
    const failureReason = topLevelFailureReason(r)
    const reasonGroups = groupedIssueReasons(r, failureReason)
    const summary = issueSummary(r, failureReason, reasonGroups)
    const tooltip = issueTooltip(r, failureReason, reasonGroups)
    const hasDetail = Boolean(failureReason) || Boolean(reasonGroups.length) || r.records_skipped > 0 || r.dlq_count > 0
    const toggle = hasDetail
      ? `<button class="btn-flat runs-expand-btn" type="button" data-run-toggle="${i}"><i class="bi bi-chevron-right runs-chevron" id="${rowIdPrefix}-chev-${i}"></i></button>`
      : ''
    rows.push(`<tr id="${rowIdPrefix}-row-${i}">
      <td class="mono-sm">${esc(String(r.run_id || r.id || '').slice(0, 8))}</td>
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

  tbody.onclick = (event) => {
    const toggleButton = event.target.closest('[data-run-toggle]')
    if (!toggleButton || !tbody.contains(toggleButton)) return
    const i = parseInt(toggleButton.dataset.runToggle || '', 10)
    if (!Number.isFinite(i)) return
    const existingLog = document.getElementById(`${rowIdPrefix}-log-${i}`)
    const chev = document.getElementById(`${rowIdPrefix}-chev-${i}`)
    if (existingLog) {
      existingLog.remove()
      if (chev) {
        chev.classList.remove('bi-chevron-down')
        chev.classList.add('bi-chevron-right')
      }
      return
    }

    const r = runs[i]
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
    const content = details.join('') || '<div class="text-secondary">No error details available</div>'
    const logRow = document.createElement('tr')
    logRow.id = `${rowIdPrefix}-log-${i}`
    logRow.className = 'error-detail-row'
    logRow.innerHTML = `<td colspan="${colspan}" class="run-issues-cell">${content}</td>`
    document.getElementById(`${rowIdPrefix}-row-${i}`)?.after(logRow)
    if (chev) {
      chev.classList.remove('bi-chevron-right')
      chev.classList.add('bi-chevron-down')
    }
  }
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
