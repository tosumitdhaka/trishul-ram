// ── Shared UI helpers ────────────────────────────────────────────────────────
import * as bootstrap from 'bootstrap'

export function relTime(iso) {
  if (!iso) return '—'
  const s = (Date.now() - new Date(iso)) / 1000
  if (s < 5)     return 'just now'
  if (s < 60)    return `${Math.round(s)}s ago`
  if (s < 3600)  return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

export function fmtDur(startedAt, finishedAt) {
  if (!startedAt || !finishedAt) return '—'
  const s = (new Date(finishedAt) - new Date(startedAt)) / 1000
  if (s < 60)   return `${s.toFixed(1)}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
}

export function fmtNum(n) {
  if (n === null || n === undefined) return '—'
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`
  return Number(n).toLocaleString()
}

export function fmtBytes(value) {
  if (value === null || value === undefined) return '—'
  const bytes = Number(value)
  if (!Number.isFinite(bytes)) return '—'
  const abs = Math.abs(bytes)
  if (abs < 1024) return `${fmtNum(Math.round(bytes))} B`

  const units = ['KB', 'MB', 'GB', 'TB', 'PB']
  let scaled = abs
  let unitIndex = -1
  while (scaled >= 1024 && unitIndex < units.length - 1) {
    scaled /= 1024
    unitIndex += 1
  }

  const sign = bytes < 0 ? '-' : ''
  const precision = scaled >= 10 ? 0 : 1
  return `${sign}${scaled.toFixed(precision)} ${units[unitIndex]}`
}

export function fmtRate(value) {
  return `${fmtNum(Math.round(value || 0))}/s`
}

export function fmtBytesRate(value) {
  return `${fmtBytes(value || 0)}/s`
}

export function statusBadge(status) {
  const cls = {
    running:   'badge-running has-dot running',
    scheduled: 'badge-scheduled has-dot scheduled',
    stopped:   'badge-stopped has-dot stopped',
    degraded:  'badge-partial has-dot scheduled',
    stale:     'badge-partial has-dot scheduled',
    reconciling:'badge-paused has-dot paused',
    queued:    'badge-queued has-dot queued',
    error:     'badge-error has-dot error',
    success:   'badge-success has-dot success',
    failed:    'badge-failed has-dot failed',
    aborted:   'badge-failed has-dot failed',
    disabled:  'badge-disabled',
  }[status] || 'badge-stopped'
  return `<span class="tram-badge ${cls}">${status ?? '—'}</span>`
}

export function schedBadge(p) {
  // p is a pipeline object
  const type = p.schedule_type || p.status
  const cls = {
    stream:   'badge-stream has-dot stream',
    interval: 'badge-interval',
    cron:     'badge-cron',
    manual:   'badge-manual',
  }[type] || 'badge-interval'
  const label = type === 'interval' && p.interval_seconds
    ? `every ${fmtInterval(p.interval_seconds)}`
    : type || '—'
  return `<span class="tram-badge ${cls}">${label}</span>`
}

function fmtInterval(s) {
  if (!s) return '?'
  if (s < 60)   return `${s}s`
  if (s < 3600) return `${s / 60}m`
  return `${s / 3600}h`
}

export function esc(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')
}

export function bindDataActions(root, handlers = {}) {
  if (!root) return
  if (root._tramActionListener) {
    root.removeEventListener('click', root._tramActionListener)
  }
  const listener = (event) => {
    const target = event.target.closest('[data-action]')
    if (!target || !root.contains(target)) return
    const handler = handlers[target.dataset.action]
    if (!handler) return
    handler(target, event)
  }
  root.addEventListener('click', listener)
  root._tramActionListener = listener
}

export function downloadBlob(filename, blob) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function downloadText(filename, text, mime = 'text/plain;charset=utf-8') {
  downloadBlob(filename, new Blob([text], { type: mime }))
}

export function getSavedPollIntervalMs(defaultSeconds = 10) {
  const raw = parseInt(localStorage.getItem('tram_poll_interval') || String(defaultSeconds), 10)
  const seconds = Number.isFinite(raw) && raw > 0 ? raw : defaultSeconds
  return seconds * 1000
}

const STATUS_TONES = ['muted', 'info', 'success', 'warning', 'error']

export function setStatusMessage(target, message = '', tone = 'muted') {
  const el = typeof target === 'string' ? document.getElementById(target) : target
  if (!el) return
  el.textContent = message
  el.classList.add('ui-status')
  STATUS_TONES.forEach(value => el.classList.remove(`ui-status-${value}`))
  el.classList.add(`ui-status-${STATUS_TONES.includes(tone) ? tone : 'muted'}`)
}

// ── Toasts ───────────────────────────────────────────────────────────────────
// Single stacking container (aria-live) with a dismiss control per toast.
// Repeating the same message inside the dedupe window refreshes the existing
// toast and bumps its ×N counter instead of stacking a copy — a daemon outage
// must not produce an unreadable toast pile.

const TOAST_DEDUPE_MS = 8000
const TOAST_TTL_MS = 4000
const _activeToasts = new Map()

function toastContainer() {
  let el = document.getElementById('tram-toast-stack')
  if (!el) {
    el = document.createElement('div')
    el.id = 'tram-toast-stack'
    el.setAttribute('role', 'status')
    el.setAttribute('aria-live', 'polite')
    document.body.appendChild(el)
  }
  return el
}

function removeToast(key, el) {
  const entry = _activeToasts.get(key)
  if (entry) {
    clearTimeout(entry.hideTimer)
    _activeToasts.delete(key)
  }
  el.classList.remove('is-visible')
  el.classList.add('is-leaving')
  setTimeout(() => el.remove(), 300)
}

export function toast(msg, type = 'success') {
  const container = toastContainer()
  const key = `${type}||${msg}`
  const existing = _activeToasts.get(key)

  if (existing && container.contains(existing.el)) {
    existing.count += 1
    existing.el.querySelector('.tram-toast-count').textContent =
      existing.count > 1 ? `×${existing.count}` : ''
    clearTimeout(existing.hideTimer)
    existing.hideTimer = setTimeout(() => removeToast(key, existing.el), TOAST_TTL_MS)
    return
  }

  const el = document.createElement('div')
  el.className = `tram-toast tram-toast-${type || 'success'}`
  const message = document.createElement('span')
  message.className = 'tram-toast-msg'
  message.textContent = msg
  const count = document.createElement('span')
  count.className = 'tram-toast-count'
  const dismiss = document.createElement('button')
  dismiss.className = 'tram-toast-dismiss'
  dismiss.type = 'button'
  dismiss.setAttribute('aria-label', 'Dismiss notification')
  dismiss.textContent = '✕'
  dismiss.addEventListener('click', () => removeToast(key, el))
  el.append(message, count, dismiss)
  container.appendChild(el)
  requestAnimationFrame(() => el.classList.add('is-visible'))

  const entry = { el, count: 1, hideTimer: null }
  entry.hideTimer = setTimeout(() => removeToast(key, el), TOAST_TTL_MS)
  _activeToasts.set(key, entry)
}

// ── Offline banner ───────────────────────────────────────────────────────────
// Poll-driven failures degrade to one inline banner per page (the page keeps
// showing its last known data) instead of re-toasting every poll cycle. The
// banner lives inside #content, so navigating away clears it naturally.

export function setOfflineBanner(shown, message = 'Daemon unreachable — showing last known data') {
  if (!shown) {
    document.getElementById('tram-offline-banner')?.classList.add('d-none')
    return
  }
  let el = document.getElementById('tram-offline-banner')
  if (!el) {
    el = document.createElement('div')
    el.id = 'tram-offline-banner'
    el.className = 'tram-offline-banner'
    el.setAttribute('role', 'status')
    document.getElementById('content')?.prepend(el)
  }
  el.innerHTML = `<i class="bi bi-exclamation-triangle"></i><span>${esc(message)}</span>`
  el.classList.remove('d-none')
}

// ── Table loading / empty / error states ─────────────────────────────────────
// Replaces the "Loading…" skeleton that used to stay up forever when the
// initial page fetch failed. Error state renders the message plus an optional
// retry control wired to the page's own loader.

export function renderTableState(tbody, state = 'loading', message = '', { onRetry } = {}) {
  if (!tbody) return
  const table = tbody.closest('table')
  const columns = table ? table.querySelectorAll('thead th').length : 1
  const text = message
    || { loading: 'Loading…', empty: 'No data yet', error: 'Could not load data' }[state]
    || 'Loading…'
  let inner = esc(text)
  if (state === 'error') {
    inner = `
      <div class="table-state-error"><i class="bi bi-exclamation-triangle me-1"></i>${esc(text)}</div>
      ${onRetry ? '<div class="mt-2"><button class="btn btn-sm btn-outline-secondary" type="button"><i class="bi bi-arrow-clockwise me-1"></i>Retry</button></div>' : ''}`
  }
  tbody.innerHTML = `<tr><td colspan="${columns}" class="text-secondary text-center py-4">${inner}</td></tr>`
  if (state === 'error' && onRetry) {
    tbody.querySelector('button')?.addEventListener('click', () => {
      renderTableState(tbody, 'loading')
      onRetry()
    })
  }
}

// ── Styled confirmation dialog ──────────────────────────────────────────────
// One shared Bootstrap modal for consequential actions (stop/reload/rollback/
// delete), replacing the mix of native confirm() and no confirmation at all.

let _confirmModalEl = null

function buildConfirmModal() {
  const el = document.createElement('div')
  el.className = 'modal fade'
  el.id = 'tram-confirm-modal'
  el.tabIndex = -1
  el.innerHTML = `
    <div class="modal-dialog modal-dialog-centered modal-sm">
      <div class="modal-content detail-modal-shell">
        <div class="modal-header detail-modal-header">
          <h6 class="modal-title detail-modal-title" id="tram-confirm-title"></h6>
          <button type="button" class="btn-close btn-close-theme" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <p class="tram-confirm-body" id="tram-confirm-body"></p>
        </div>
        <div class="modal-footer detail-modal-footer">
          <button type="button" class="btn btn-sm btn-secondary" data-bs-dismiss="modal" id="tram-confirm-cancel"></button>
          <button type="button" class="btn btn-sm btn-primary" id="tram-confirm-ok"></button>
        </div>
      </div>
    </div>`
  document.body.appendChild(el)
  return el
}

export function confirmAction({
  title = 'Are you sure?',
  body = '',
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
} = {}) {
  return new Promise((resolve) => {
    const el = _confirmModalEl ?? (_confirmModalEl = buildConfirmModal())
    const titleEl  = el.querySelector('#tram-confirm-title')
    const bodyEl   = el.querySelector('#tram-confirm-body')
    const okBtn    = el.querySelector('#tram-confirm-ok')
    const cancelBtn = el.querySelector('#tram-confirm-cancel')
    titleEl.textContent = title
    bodyEl.textContent = body
    okBtn.textContent = confirmLabel
    cancelBtn.textContent = cancelLabel
    okBtn.className = danger ? 'btn btn-sm btn-danger' : 'btn btn-sm btn-primary'

    const modal = bootstrap.Modal.getOrCreateInstance(el)
    let settled = false
    const onOk = () => { settled = true; resolve(true); modal.hide() }
    const onHidden = () => {
      el.removeEventListener('hidden.bs.modal', onHidden)
      okBtn.removeEventListener('click', onOk)
      if (!settled) resolve(false)
    }
    el.addEventListener('hidden.bs.modal', onHidden)
    okBtn.addEventListener('click', onOk)
    el.addEventListener('shown.bs.modal', () => okBtn.focus(), { once: true })
    modal.show()
  })
}

export function pipelineStartFeedback(name, result = {}) {
  const status = result?.status || 'started'
  const detail = result?.detail
  if (status === 'disabled') {
    return { message: detail || `Pipeline '${name}' is disabled in YAML.`, type: 'error' }
  }
  if (status === 'manual') {
    return { message: detail || `Pipeline '${name}' uses a manual schedule. Use Run Now instead.`, type: 'info' }
  }
  if (status === 'already_running') {
    return { message: detail || `Pipeline '${name}' is already active.`, type: 'info' }
  }
  return { message: detail || `Started ${name}`, type: 'success' }
}
