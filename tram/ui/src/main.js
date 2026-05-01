import 'bootstrap/dist/css/bootstrap.min.css'
import 'bootstrap-icons/font/bootstrap-icons.css'
import 'bootstrap/dist/js/bootstrap.bundle.min.js'
import './style.css'

import { router } from './router.js'
import { startHealthPoller } from './health.js'
import { api } from './api.js'
import loginHtml from './pages/login.html?raw'

window._tramAuthPending = true

// Expose globally so page modules can navigate without importing the router.
window.navigate = (page, params) => router.navigate(page, params)

function applyTheme(theme) {
  document.documentElement.setAttribute('data-bs-theme', theme)
  localStorage.setItem('tram_theme', theme)
  const btn = document.getElementById('theme-btn')
  if (btn) btn.innerHTML = theme === 'dark'
    ? '<i class="bi bi-moon-stars-fill"></i>'
    : '<i class="bi bi-sun-fill"></i>'
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-bs-theme') || 'dark'
  applyTheme(current === 'dark' ? 'light' : 'dark')
}

function logout() {
  localStorage.removeItem('tram_auth_token')
  localStorage.removeItem('tram_auth_user')
  const logoutBtn = document.getElementById('logout-btn')
  if (logoutBtn) logoutBtn.hidden = true
  showLogin()
}

function showLogin() {
  window._tramAuthPending = true
  const overlay = document.getElementById('login-overlay')
  const shell   = document.getElementById('app-shell')
  const logoutBtn = document.getElementById('logout-btn')
  if (overlay) { overlay.hidden = false; overlay.querySelector('#login-content').innerHTML = loginHtml }
  if (shell) shell.hidden = true
  if (logoutBtn) logoutBtn.hidden = true
  import('./pages/login.js').then(m => m.init?.())
}

function resumeRequestedRoute({ showLogout = false } = {}) {
  window._tramAuthPending = false
  const overlay = document.getElementById('login-overlay')
  const shell = document.getElementById('app-shell')
  const logoutBtn = document.getElementById('logout-btn')
  if (overlay) overlay.hidden = true
  if (shell) shell.hidden = false
  if (logoutBtn) logoutBtn.hidden = !showLogout
  window.navigate(window.location.hash.slice(1) || 'dashboard')
}

function wireShellActions() {
  document.getElementById('topbar-settings-btn')?.addEventListener('click', () => {
    window.navigate('settings')
  })
  document.getElementById('theme-btn')?.addEventListener('click', toggleTheme)
  document.getElementById('logout-btn')?.addEventListener('click', logout)
}

async function checkAuth() {
  // Check if auth is required by calling /api/auth/me
  // If 403 (not configured), auth is disabled — proceed normally
  // If 401, show login; if 200, proceed
  try {
    await api.auth.me()
    // Token valid — show logout button
    resumeRequestedRoute({ showLogout: true })
  } catch (e) {
    if (e.status === 401) { showLogin(); return false }
    // 403 = auth not configured, or other error — proceed without login
    resumeRequestedRoute()
  }
  return true
}

// Boot — always init router and health poller so hashchange listener is registered
// even when auth is required (login overlay covers the shell with z-index:9999)
applyTheme(localStorage.getItem('tram_theme') || 'dark')
wireShellActions()
router.init()
startHealthPoller()
checkAuth()
