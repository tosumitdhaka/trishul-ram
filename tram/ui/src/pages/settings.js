// Settings page — load saved config + daemon status
import { api, getConfig, saveConfig } from '../api.js'
import { setStatusMessage } from '../utils.js'

export async function init() {
  const { baseUrl, apiKey } = getConfig()
  const url = document.getElementById('cfg-url')
  const key = document.getElementById('cfg-key')
  const poll = document.getElementById('cfg-poll')
  if (url) url.value = baseUrl
  if (key) key.value = apiKey
  if (poll) poll.value = localStorage.getItem('tram_poll_interval') || '10'

  document.getElementById('cfg-reset-btn')?.addEventListener('click', () => {
    localStorage.removeItem('tram_base_url')
    if (url) url.value = window.location.origin
    setStatusMessage('cfg-status', 'Reset to default', 'muted')
  })

  document.getElementById('cfg-save-btn')?.addEventListener('click', () => {
    saveConfig(url?.value?.trim() || window.location.origin, key?.value?.trim() || '')
    localStorage.setItem('tram_poll_interval', poll?.value || '10')
    setStatusMessage('cfg-status', 'Saved', 'success')
  })

  document.getElementById('cfg-test-btn')?.addEventListener('click', async () => {
    try {
      setStatusMessage('cfg-status', 'Testing…', 'muted')
      await api.ready()
      const meta = await api.meta()
      setStatusMessage('cfg-status', `Connected · v${meta.version}`, 'success')
    } catch (e) {
      setStatusMessage('cfg-status', e.message, 'error')
    }
  })

  // Show password change card if user is logged in
  const authUser = localStorage.getItem('tram_auth_user')
  if (authUser) {
    const col = document.getElementById('pwd-col')
    if (col) col.classList.remove('d-none')
  }

  document.getElementById('pwd-change-btn')?.addEventListener('click', async () => {
    const current = document.getElementById('pwd-current')?.value || ''
    const newPwd  = document.getElementById('pwd-new')?.value     || ''
    const confirm = document.getElementById('pwd-confirm')?.value  || ''
    if (!current) { setStatusMessage('pwd-status', 'Enter current password', 'error'); return }
    if (newPwd.length < 6) { setStatusMessage('pwd-status', 'New password must be at least 6 characters', 'error'); return }
    if (newPwd !== confirm) { setStatusMessage('pwd-status', 'Passwords do not match', 'error'); return }
    try {
      setStatusMessage('pwd-status', 'Saving…', 'muted')
      await api.auth.changePassword(current, newPwd)
      setStatusMessage('pwd-status', 'Password changed', 'success')
      document.getElementById('pwd-current').value = ''
      document.getElementById('pwd-new').value     = ''
      document.getElementById('pwd-confirm').value  = ''
    } catch (e) {
      setStatusMessage('pwd-status', e.message, 'error')
    }
  })

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
    updateAiBadge(badge, aiCfg.api_key_set)
  } catch { /* AI not available */ }

  document.getElementById('ai-save-btn')?.addEventListener('click', async () => {
    const payload = {
      provider: document.getElementById('ai-provider')?.value || '',
      api_key:  document.getElementById('ai-api-key')?.value  || '',
      model:    document.getElementById('ai-model')?.value    || '',
      base_url: document.getElementById('ai-base-url')?.value || '',
    }
    try {
      setStatusMessage('ai-cfg-status', 'Saving…', 'muted')
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
      updateAiBadge(badge, aiCfg.api_key_set)
      setStatusMessage('ai-cfg-status', 'Saved', 'success')
    } catch (e) {
      setStatusMessage('ai-cfg-status', e.message, 'error')
    }
  })

  document.getElementById('ai-test-btn')?.addEventListener('click', async () => {
    try {
      setStatusMessage('ai-cfg-status', 'Testing…', 'muted')
      const r = await api.ai.test()
      setStatusMessage('ai-cfg-status', `Connected · ${r.provider} / ${r.model}`, 'success')
    } catch (e) {
      setStatusMessage('ai-cfg-status', e.message, 'error')
    }
  })
}

function updateAiBadge(badge, enabled) {
  if (!badge) return
  setStatusMessage(badge, enabled ? '● enabled' : '○ disabled', enabled ? 'success' : 'muted')
}
