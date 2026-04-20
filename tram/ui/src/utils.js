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

export function toast(msg, type = 'success') {
  const el = document.createElement('div')
  el.style.cssText = `position:fixed;bottom:24px;right:24px;z-index:9999;padding:10px 16px;border-radius:6px;font-size:13px;color:#e6edf3;background:${type === 'error' ? '#3d1a1a' : '#1a3328'};border:1px solid ${type === 'error' ? '#f85149' : '#3fb950'};box-shadow:0 4px 12px rgba(0,0,0,.4);transition:opacity .3s`
  el.textContent = msg
  document.body.appendChild(el)
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300) }, 2500)
}
