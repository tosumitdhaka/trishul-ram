import 'bootstrap/dist/css/bootstrap.min.css'
import 'bootstrap-icons/font/bootstrap-icons.css'
import 'bootstrap/dist/js/bootstrap.bundle.min.js'
import './style.css'

import { router } from './router.js'
import { startHealthPoller } from './health.js'
import { api } from './api.js'
import loginHtml from './pages/login.html?raw'

// Expose globally so inline onclick handlers in page HTML can call navigate()
window.navigate = (page, params) => router.navigate(page, params)

function applyTheme(theme) {
  document.documentElement.setAttribute('data-bs-theme', theme)
  localStorage.setItem('tram_theme', theme)
  const btn = document.getElementById('theme-btn')
  if (btn) btn.innerHTML = theme === 'dark'
    ? '<i class="bi bi-moon-stars-fill"></i>'
    : '<i class="bi bi-sun-fill"></i>'
}

window.toggleTheme = () => {
  const current = document.documentElement.getAttribute('data-bs-theme') || 'dark'
  applyTheme(current === 'dark' ? 'light' : 'dark')
}

window._logout = () => {
  localStorage.removeItem('tram_auth_token')
  localStorage.removeItem('tram_auth_user')
  document.getElementById('logout-btn')?.style && (document.getElementById('logout-btn').style.display = 'none')
  showLogin()
}

function showLogin() {
  const overlay = document.getElementById('login-overlay')
  const shell   = document.getElementById('app-shell')
  if (overlay) { overlay.style.display = ''; overlay.querySelector('#login-content').innerHTML = loginHtml }
  if (shell) shell.style.display = 'none'
  import('./pages/login.js').then(m => m.init?.())
}

async function checkAuth() {
  // Check if auth is required by calling /api/auth/me
  // If 403 (not configured), auth is disabled — proceed normally
  // If 401, show login; if 200, proceed
  try {
    await api.auth.me()
    // Token valid — show logout button
    document.getElementById('logout-btn').style.display = ''
  } catch (e) {
    if (e.status === 401) { showLogin(); return false }
    // 403 = auth not configured, or other error — proceed without login
  }
  return true
}

// Boot
applyTheme(localStorage.getItem('tram_theme') || 'dark')

checkAuth().then(ok => {
  if (ok) {
    router.init()
    startHealthPoller()
  }
})
