import { api } from '../api.js'
import { setStatusMessage } from '../utils.js'

export async function init() {
  const form = document.getElementById('login-form')
  const userInput = document.getElementById('login-user')
  const passInput = document.getElementById('login-pass')
  const errEl     = document.getElementById('login-error')
  const btn       = document.getElementById('login-btn')

  async function submitLogin() {
    const username = userInput?.value?.trim()
    const password = passInput?.value || ''
    if (!username) { showErr('Enter a username'); return }

    btn.disabled = true
    btn.textContent = 'Signing in…'
    clearErr()

    try {
      const data = await api.auth.login(username, password)
      localStorage.setItem('tram_auth_token', data.token)
      localStorage.setItem('tram_auth_user', data.username)
      // Show the main shell and navigate to dashboard
      window._tramAuthPending = false
      document.getElementById('app-shell').hidden = false
      document.getElementById('login-overlay').hidden = true
      document.getElementById('logout-btn').hidden = false
      window.navigate(window.location.hash.slice(1) || 'dashboard')
    } catch (e) {
      showErr(e.message)
    } finally {
      btn.disabled = false
      btn.textContent = 'Sign in'
    }
  }

  form?.addEventListener('submit', async (event) => {
    event.preventDefault()
    await submitLogin()
  })

  function showErr(msg) {
    if (!errEl) return
    errEl.classList.remove('d-none')
    setStatusMessage(errEl, msg, 'error')
  }

  function clearErr() {
    if (!errEl) return
    errEl.classList.add('d-none')
    setStatusMessage(errEl, '', 'error')
  }
}
