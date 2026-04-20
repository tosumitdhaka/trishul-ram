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
import templatesHtml from './pages/templates.html?raw'
import wizardHtml    from './pages/wizard.html?raw'

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
  templates: templatesHtml,
  wizard:    wizardHtml,
}

const meta = {
  dashboard: { title: 'Dashboard',         sub: 'Overview' },
  pipelines: { title: 'Pipelines',         sub: '' },
  detail:    { title: 'Pipeline Detail',   sub: '' },
  editor:    { title: 'Pipeline Editor',   sub: '' },
  runs:      { title: 'Run History',       sub: '' },
  schemas:   { title: 'Schemas',           sub: '' },
  mibs:      { title: 'MIB Modules',       sub: '' },
  cluster:   { title: 'Workers',           sub: 'Worker pool status' },
  plugins:   { title: 'Plugins',           sub: '' },
  settings:  { title: 'Settings',          sub: 'Connection & daemon configuration' },
  templates: { title: 'Pipeline Templates', sub: 'Start from a pre-built example' },
  wizard:    { title: 'New Pipeline',        sub: 'Setup wizard' },
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
  templates: () => import('./pages/templates.js').then(m => m.init?.()),
  wizard:    () => import('./pages/wizard.js').then(m => m.init?.()),
}

export const router = {
  current: null,

  navigate(name, _params) {
    if (!pages[name]) name = 'dashboard'

    // Render HTML
    document.getElementById('content').innerHTML = pages[name]

    // Update topbar
    const m = meta[name] || {}
    document.getElementById('tb-title').textContent = m.title || name
    document.getElementById('tb-sub').textContent   = m.sub   || ''

    // Update sidebar active link
    document.querySelectorAll('#sidebar .nav-link').forEach(a => {
      a.classList.toggle('active', a.dataset.page === name ||
        (name === 'detail'    && a.dataset.page === 'pipelines') ||
        (name === 'editor'    && a.dataset.page === 'pipelines') ||
        (name === 'wizard'    && a.dataset.page === 'pipelines') ||
        (name === 'templates' && a.dataset.page === 'templates'))
    })

    // Update hash without triggering another hashchange
    if (window.location.hash.slice(1) !== name) {
      history.replaceState(null, '', `#${name}`)
    }

    this.current = name

    // Run page-specific init (lazy, best-effort)
    inits[name]?.().catch(() => {})
  },

  init() {
    // Handle hash navigation
    window.addEventListener('hashchange', () => {
      const page = window.location.hash.slice(1) || 'dashboard'
      this.navigate(page)
    })

    // Initial page from hash or default
    const initial = window.location.hash.slice(1) || 'dashboard'
    this.navigate(initial)
  },
}
