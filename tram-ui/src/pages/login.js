import { getConfig } from '../api.js'

export async function init() {
  const userInput = document.getElementById('login-user')
  const passInput = document.getElementById('login-pass')
  const errEl     = document.getElementById('login-error')
  const btn       = document.getElementById('login-btn')

  // Submit on Enter key in either field
  ;[userInput, passInput].forEach(el => {
    el?.addEventListener('keydown', e => { if (e.key === 'Enter') window._loginSubmit?.() })
  })

  window._loginSubmit = async () => {
    const username = userInput?.value?.trim()
    const password = passInput?.value || ''
    if (!username) { showErr('Enter a username'); return }

    btn.disabled = true
    btn.textContent = 'Signing in…'
    if (errEl) errEl.style.display = 'none'

    try {
      const { baseUrl } = getConfig()
      const res = await fetch(`${baseUrl}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Login failed')
      localStorage.setItem('tram_auth_token', data.token)
      localStorage.setItem('tram_auth_user', data.username)
      // Show the main shell and navigate to dashboard
      document.getElementById('app-shell').style.display = ''
      document.getElementById('login-overlay').style.display = 'none'
      window.navigate('dashboard')
    } catch (e) {
      showErr(e.message)
    } finally {
      btn.disabled = false
      btn.textContent = 'Sign in'
    }
  }

  function showErr(msg) {
    if (errEl) { errEl.textContent = msg; errEl.style.display = '' }
  }
}
