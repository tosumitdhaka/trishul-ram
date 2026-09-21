// ── Hash-based page router with route parameters ──────────────────────────────
//
// Routes carry state so deep links, refresh, and browser Back/Forward work:
//   #dashboard?period=24h&granularity=hour
//   #pipelines
//   #pipelines/templates          (opens the templates modal on load)
//   #detail/:pipeline?tab=runs
//   #editor/:pipeline?return=detail        (edit)
//   #editor?template=tpl&return=pipelines  (new from template)
//   #runs/:runId?pipeline=x&status=failed&from=2026-09-01
//   #schemas #mibs #cluster #plugins #settings
//
// Large payloads (YAML) are never carried in the hash — the editor fetches
// them by name, so a refresh recovers without in-memory handoffs.

import { isAuthPending } from './auth_state.js'
import dashboardHtml from './pages/dashboard.html?raw'
import pipelinesHtml from './pages/pipelines.html?raw'
import detailHtml    from './pages/detail.html?raw'
import editorHtml    from './pages/editor.html?raw'
import runsHtml      from './pages/runs.html?raw'
import schemasHtml   from './pages/schemas.html?raw'
import mibsHtml      from './pages/mibs.html?raw'
import clusterHtml   from './pages/cluster.html?raw'
import pluginsHtml   from './pages/plugins.html?raw'
import settingsHtml   from './pages/settings.html?raw'

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
  runs:      { title: 'Run History',      sub: '' },
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

// ── Route parsing and canonicalization ────────────────────────────────────────

// '#editor/my-pipe?return=detail' → { page: 'editor', params: ['my-pipe'], query: { return: 'detail' } }
export function parseRoute(hash) {
  const raw = String(hash || '').replace(/^#/, '')
  const [pathPart = '', queryPart = ''] = raw.split('?')
  const decode = (s) => { try { return decodeURIComponent(s) } catch { return s } }
  const segments = pathPart.split('/').filter(Boolean).map(decode)
  const query = {}
  new URLSearchParams(queryPart || '').forEach((value, key) => { query[key] = value })
  return { page: segments[0] || 'dashboard', params: segments.slice(1), query }
}

function buildRoute(page, params = [], query = {}) {
  const path = [page, ...(params || []).map(p => encodeURIComponent(p))]
    .filter(Boolean).join('/')
  const qs = new URLSearchParams()
  Object.entries(query || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  })
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return `#${path}${suffix}`
}

// Legacy aliases and unknown routes → canonical replacements.
function resolveRoute(routeString) {
  const parsed = typeof routeString === 'object' && routeString !== null
    ? routeString
    : parseRoute(routeString)

  if (parsed.page === 'templates') return { ...parsed, page: 'pipelines', params: ['templates'], replace: true }
  if (parsed.page === 'wizard')   return { ...parsed, page: 'pipelines', params: [], replace: true }

  if (!pages[parsed.page]) {
    return { page: 'dashboard', params: [], query: parsed.query, replace: true }
  }
  return { ...parsed, replace: false }
}

export const router = {
  current: null,

  // Current parsed route — pages read their state from this in init().
  route() {
    return resolveRoute(window.location.hash)
  },

  _render(page, parsed) {
    if (!pages[page]) page = 'dashboard'

    if (isAuthPending()) return

    // Let the outgoing page flush in-memory state (the editor saves its
    // recovery draft here when the operator leaves via a sidebar link).
    window.dispatchEvent(new CustomEvent('tram:page-leave', { detail: { from: this.current, page } }))

    // Render HTML
    document.getElementById('content').innerHTML = pages[page]

    // Update topbar (detail/editor carry the pipeline name)
    const m = meta[page] || {}
    const nameArg = page === 'detail' || page === 'editor' ? parsed?.params?.[0] : null
    document.getElementById('tb-title').textContent = nameArg
      ? `${m.title}: ${nameArg}`
      : (m.title || page)
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

  navigate(routeString, options = {}) {
    const resolved = resolveRoute(routeString)
    const { page, params, query, replace: routeReplace } = resolved
    const replace = Boolean(options.replace || routeReplace)
    const targetHash = buildRoute(page, params, query)

    if (window.location.hash !== targetHash) {
      if (replace) {
        history.replaceState(null, '', targetHash)
      } else if (!options.fromHashChange) {
        window.location.hash = targetHash
        return
      }
    }

    if (isAuthPending()) {
      this.current = page
      return
    }

    this._render(page, resolved)
  },

  // Update the current route's query without adding a history entry —
  // pages use this to keep filters/tabs shareable without spamming Back.
  setSearchParams(patch) {
    const current = this.route()
    const query = { ...current.query }
    Object.entries(patch || {}).forEach(([k, v]) => {
      if (v === undefined || v === null || v === '') delete query[k]
      else query[k] = String(v)
    })
    history.replaceState(null, '', buildRoute(current.page, current.params, query))
  },

  // Replace the whole route (path and query) without adding a history entry.
  replaceRoute(routeString) {
    const resolved = resolveRoute(routeString)
    history.replaceState(null, '', buildRoute(resolved.page, resolved.params, resolved.query))
  },

  init() {
    // Handle hash navigation (Back/Forward, in-page links, location.hash writes)
    window.addEventListener('hashchange', () => {
      this.navigate(window.location.hash || '#dashboard', { fromHashChange: true })
    })

    // Initial page from hash or default
    this.navigate(window.location.hash || '#dashboard', { fromHashChange: true })
  },
}
