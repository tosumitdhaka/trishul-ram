import 'bootstrap/dist/css/bootstrap.min.css'
import 'bootstrap-icons/font/bootstrap-icons.css'
import 'bootstrap/dist/js/bootstrap.bundle.min.js'
import './style.css'

import { router } from './router.js'
import { startHealthPoller } from './health.js'

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

// Boot
applyTheme(localStorage.getItem('tram_theme') || 'dark')
router.init()
startHealthPoller()
