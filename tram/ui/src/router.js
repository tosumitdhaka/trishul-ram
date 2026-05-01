// ── Hash-based page router ───────────────────────────────────────────────────
import dashboardHtml from './pages/dashboard.html?raw'
import pipelinesHtml from './pages/pipelines.html?raw'
import detailHtml    from './pages/detail.html?raw'
import editorHtml    from './pages/editor.html?raw'
import runsHtml      from './pages/runs.html?raw'
import schemasHtml   from './pages/schemas.html?raw'
import mibsHtml      from './pages/mibs.html?raw'
import clusterHtml   from './pages/cluster.html?raw'
import pluginsHtml   from './pages/plugins.html?raw'
import settingsHtml  from './pages/settings.html?raw'

const pages = {
  dashboard: dashboardHtml,
  pipelines: pipelinesHtml,
  detail:    detailHtml,
  editor:    editorHtml,
  runs:      runsHtml,
  schemas:   schemasHtml,
  mibs:      mibsHtml,
  cluster:   clusterHtml,
  plugins:   pluginsHtml,
  settings:  settingsHtml,
}

const meta = {
  dashboard: { title: 'Dashboard',         sub: 'Overview' },
  pipelines: { title: 'Pipelines',         sub: '' },
  detail:    { title: 'Pipeline Detail',   sub: '' },
  editor:    { title: 'Pipeline Editor',   sub: '' },
  runs:      { title: 'Run History',       sub: '' },
  schemas:   { title: 'Schemas',           sub: '' },
  mibs:      { title: 'MIB Modules',       sub: '' },
  cluster:   { title: 'Cluster',           sub: 'Runtime and worker status' },
  plugins:   { title: 'Plugins',           sub: '' },
  settings:  { title: 'Settings',          sub: 'Connection & daemon configuration' },
}

// Page init hooks
const inits = {
  dashboard: () => import('./pages/dashboard.js').then(m => m.init?.()),
  pipelines: () => import('./pages/pipelines.js').then(m => m.init?.()),
  detail:    () => import('./pages/detail.js').then(m => m.init?.()),
  editor:    () => import('./pages/editor.js').then(m => m.init?.()),
  runs:      () => import('./pages/runs.js').then(m => m.init?.()),
  schemas:   () => import('./pages/schemas.js').then(m => m.init?.()),
  mibs:      () => import('./pages/mibs.js').then(m => m.init?.()),
  cluster:   () => import('./pages/cluster.js').then(m => m.init?.()),
  plugins:   () => import('./pages/plugins.js').then(m => m.init?.()),
  settings:  () => import('./pages/settings.js').then(m => m.init?.()),
}

function resolveRoute(name) {
  const requested = String(name || 'dashboard').trim() || 'dashboard'

  if (requested === 'templates') {
    window._openPipelinesTemplatesModal = true
    return { page: 'pipelines', replace: true }
  }

  if (requested === 'wizard') {
    return { page: 'pipelines', replace: true }
  }

  if (!pages[requested]) {
    return { page: 'dashboard', replace: requested !== 'dashboard' }
  }

  return { page: requested, replace: false }
}

export const router = {
  current: null,

  _render(page) {
    if (!pages[page]) page = 'dashboard'

    if (window._tramAuthPending) {
      return
    }

    // Render HTML
    document.getElementById('content').innerHTML = pages[page]

    // Update topbar
    const m = meta[page] || {}
    document.getElementById('tb-title').textContent = m.title || page
    document.getElementById('tb-sub').textContent   = m.sub   || ''

    // Update sidebar active link
    document.querySelectorAll('#sidebar .nav-link').forEach(a => {
      a.classList.toggle('active', a.dataset.page === page ||
        (page === 'detail' && a.dataset.page === 'pipelines') ||
        (page === 'editor' && a.dataset.page === 'pipelines'))
    })
    this.current = page

    // Run page-specific init (lazy, best-effort)
    inits[page]?.().catch(() => {})
  },

  navigate(name, options = {}) {
    const { page, replace: routeReplace } = resolveRoute(name)
    const replace = Boolean(options.replace || routeReplace)
    const targetHash = `#${page}`

    if (window.location.hash !== targetHash) {
      if (replace) {
        history.replaceState(null, '', targetHash)
      } else if (!options.fromHashChange) {
        window.location.hash = page
        return
      }
    }

    if (window._tramAuthPending) {
      this.current = page
      return
    }

    this._render(page)
  },

  init() {
    // Handle hash navigation
    window.addEventListener('hashchange', () => {
      const page = window.location.hash.slice(1) || 'dashboard'
      this.navigate(page, { fromHashChange: true })
    })

    // Initial page from hash or default
    const initial = window.location.hash.slice(1) || 'dashboard'
    this.navigate(initial, { fromHashChange: true })
  },
}
