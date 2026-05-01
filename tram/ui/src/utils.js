// ── Shared UI helpers ────────────────────────────────────────────────────────

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
    error:     'badge-error has-dot error',
    success:   'badge-success has-dot success',
    failed:    'badge-failed has-dot failed',
    aborted:   'badge-failed has-dot failed',
    partial:   'badge-partial has-dot',
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

export function toast(msg, type = 'success') {
  const el = document.createElement('div')
  el.className = `tram-toast tram-toast-${type || 'success'}`
  el.textContent = msg
  document.body.appendChild(el)
  requestAnimationFrame(() => el.classList.add('is-visible'))
  setTimeout(() => {
    el.classList.remove('is-visible')
    el.classList.add('is-leaving')
    setTimeout(() => el.remove(), 300)
  }, 4000)
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
