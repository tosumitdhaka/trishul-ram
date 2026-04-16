// ── Health poller — updates sidebar footer + topbar health card ──────────────
import { api } from './api.js'

let _timer = null

export function startHealthPoller(intervalMs = 10000) {
  poll()
  _timer = setInterval(poll, intervalMs)
}

export function stopHealthPoller() {
  clearInterval(_timer)
}

async function poll() {
  try {
    const [ready, meta] = await Promise.all([api.ready(), api.meta()])
    setOnline(ready, meta)
  } catch {
    setOffline()
  }
}

function setOnline(ready, meta) {
  const dot  = document.getElementById('health-dot')
  const text = document.getElementById('health-text')
  const port = document.getElementById('health-port')
  const icon = document.getElementById('health-icon')
  if (dot)  { dot.style.background = '#3fb950'; dot.style.boxShadow = '0 0 5px #3fb950' }
  if (text) text.textContent = 'Daemon online'
  if (icon) { icon.style.background = '#3fb950'; icon.style.boxShadow = '0 0 5px #3fb950' }

  try {
    const url = new URL(localStorage.getItem('tram_base_url') || 'http://localhost:8765')
    if (port) port.textContent = `:${url.port || 8765}`
  } catch { /**/ }

  // Populate health card
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val }
  set('hc-status',    ready.status    || '—')
  set('hc-scheduler', ready.scheduler || '—')
  set('hc-db',        ready.db        || '—')
  set('hc-pipelines', ready.pipelines_loaded ?? '—')
  set('hc-version',   meta?.version   || '—')

  // Update brand version
  const bv = document.getElementById('brand-ver')
  if (bv && meta?.version) bv.textContent = `v${meta.version}`
}

function setOffline() {
  const dot  = document.getElementById('health-dot')
  const text = document.getElementById('health-text')
  const icon = document.getElementById('health-icon')
  if (dot)  { dot.style.background = '#f85149'; dot.style.boxShadow = 'none' }
  if (text) text.textContent = 'Daemon offline'
  if (icon) { icon.style.background = '#f85149'; icon.style.boxShadow = 'none' }
  const set = (id) => { const el = document.getElementById(id); if (el) el.textContent = '—' }
  ;['hc-status','hc-scheduler','hc-db','hc-pipelines','hc-version'].forEach(set)
}
