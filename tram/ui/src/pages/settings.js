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

  // Show password change card if user is logged in
  const authUser = localStorage.getItem('tram_auth_user')
  if (authUser) {
    const col = document.getElementById('pwd-col')
    if (col) col.style.display = ''
  }

  window._settingsChangePwd = async () => {
    const current = document.getElementById('pwd-current')?.value || ''
    const newPwd  = document.getElementById('pwd-new')?.value     || ''
    const confirm = document.getElementById('pwd-confirm')?.value  || ''
    const s = document.getElementById('pwd-status')
    if (!current) { if (s) { s.textContent = '✗ Enter current password'; s.style.color = '#f85149' }; return }
    if (newPwd.length < 6) { if (s) { s.textContent = '✗ New password must be at least 6 characters'; s.style.color = '#f85149' }; return }
    if (newPwd !== confirm) { if (s) { s.textContent = '✗ Passwords do not match'; s.style.color = '#f85149' }; return }
    try {
      if (s) { s.textContent = 'Saving…'; s.style.color = '#8b949e' }
      await api.auth.changePassword(current, newPwd)
      if (s) { s.textContent = '✓ Password changed'; s.style.color = '#3fb950' }
      document.getElementById('pwd-current').value = ''
      document.getElementById('pwd-new').value     = ''
      document.getElementById('pwd-confirm').value  = ''
    } catch (e) {
      if (s) { s.textContent = `✗ ${e.message}`; s.style.color = '#f85149' }
    }
  }

  // Load AI config
  try {
    const aiCfg = await api.ai.getConfig()
    const provEl = document.getElementById('ai-provider')
    const modelEl = document.getElementById('ai-model')
    const baseEl  = document.getElementById('ai-base-url')
    const hintEl  = document.getElementById('ai-key-hint')
    const badge   = document.getElementById('ai-status-badge')
    if (provEl) provEl.value = aiCfg.provider || 'anthropic'
    if (modelEl) modelEl.value = aiCfg.model || ''
    if (baseEl)  baseEl.value  = aiCfg.base_url || ''
    if (hintEl)  hintEl.textContent = aiCfg.api_key_set
      ? `Current key: ${aiCfg.api_key_hint} (${aiCfg.source})`
      : 'No API key configured'
    if (badge) {
      badge.textContent = aiCfg.api_key_set ? '● enabled' : '○ disabled'
      badge.style.color = aiCfg.api_key_set ? '#3fb950' : '#8b949e'
    }
  } catch { /* AI not available */ }

  window._settingsSaveAI = async () => {
    const s = document.getElementById('ai-cfg-status')
    const payload = {
      provider: document.getElementById('ai-provider')?.value || '',
      api_key:  document.getElementById('ai-api-key')?.value  || '',
      model:    document.getElementById('ai-model')?.value    || '',
      base_url: document.getElementById('ai-base-url')?.value || '',
    }
    try {
      if (s) { s.textContent = 'Saving…'; s.style.color = '#8b949e' }
      await api.ai.saveConfig(payload)
      // Clear the password field after save
      const keyEl = document.getElementById('ai-api-key')
      if (keyEl) keyEl.value = ''
      // Refresh hint
      const aiCfg = await api.ai.getConfig()
      const hintEl = document.getElementById('ai-key-hint')
      const badge  = document.getElementById('ai-status-badge')
      if (hintEl) hintEl.textContent = aiCfg.api_key_set
        ? `Current key: ${aiCfg.api_key_hint} (${aiCfg.source})`
        : 'No API key configured'
      if (badge) {
        badge.textContent = aiCfg.api_key_set ? '● enabled' : '○ disabled'
        badge.style.color = aiCfg.api_key_set ? '#3fb950' : '#8b949e'
      }
      if (s) { s.textContent = '✓ Saved'; s.style.color = '#3fb950' }
    } catch (e) {
      if (s) { s.textContent = `✗ ${e.message}`; s.style.color = '#f85149' }
    }
  }

  window._settingsTestAI = async () => {
    const s = document.getElementById('ai-cfg-status')
    try {
      if (s) { s.textContent = 'Testing…'; s.style.color = '#8b949e' }
      const r = await api.ai.test()
      if (s) { s.textContent = `✓ Connected · ${r.provider} / ${r.model}`; s.style.color = '#3fb950' }
    } catch (e) {
      if (s) { s.textContent = `✗ ${e.message}`; s.style.color = '#f85149' }
    }
  }
}
