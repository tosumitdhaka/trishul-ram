// Settings page — load saved config + daemon status
import { api, getConfig, saveConfig } from '../api.js'

export async function init() {
  const { baseUrl, apiKey } = getConfig()
  const url = document.getElementById('cfg-url')
  const key = document.getElementById('cfg-key')
  const poll = document.getElementById('cfg-poll')
  if (url) url.value = baseUrl
  if (key) key.value = apiKey
  if (poll) poll.value = localStorage.getItem('tram_poll_interval') || '10'

  window._settingsResetUrl = () => {
    localStorage.removeItem('tram_base_url')
    if (url) url.value = window.location.origin
    const s = document.getElementById('cfg-status')
    if (s) { s.textContent = '✓ Reset to default'; s.style.color = '#8b949e' }
  }

  window._settingsSave = () => {
    saveConfig(url?.value?.trim() || window.location.origin, key?.value?.trim() || '')
    localStorage.setItem('tram_poll_interval', poll?.value || '10')
    const s = document.getElementById('cfg-status')
    if (s) { s.textContent = '✓ Saved'; s.style.color = '#3fb950' }
  }

  window._settingsTest = async () => {
    const s = document.getElementById('cfg-status')
    try {
      if (s) { s.textContent = 'Testing…'; s.style.color = '#8b949e' }
      const r = await api.ready()
      if (s) { s.textContent = `✓ Connected · v${(await api.meta()).version}`; s.style.color = '#3fb950' }
    } catch (e) {
      if (s) { s.textContent = `✗ ${e.message}`; s.style.color = '#f85149' }
    }
  }

  // Load daemon status
  try {
    const [ready, meta] = await Promise.all([api.ready(), api.meta()])
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val ?? '—' }
    const sch = document.getElementById('ds-scheduler')
    if (sch) sch.innerHTML = `<span class="tram-badge badge-${ready.scheduler === 'running' ? 'running has-dot running' : 'stopped'}">${ready.scheduler || '—'}</span>`
    set('ds-db-engine',  ready.db_engine || 'SQLite')
    const dbs = document.getElementById('ds-db-status')
    if (dbs) { dbs.textContent = ready.db || '—'; dbs.style.color = ready.db === 'ok' ? '#3fb950' : '#f85149' }
    set('ds-db-path',    ready.db_path   || '—')
    set('ds-cluster',    ready.cluster   || 'disabled (standalone)')
    set('ds-pipelines',  ready.pipelines_loaded ?? '—')
    set('ds-python',     meta.python_version || '—')
    set('ds-uptime',     ready.uptime    || '—')
  } catch { /* offline */ }
}
