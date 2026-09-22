// Fixture-driven network stubbing. Installs a single page.route for every
// /api/** request and fulfills it from the checked-in JSON fixtures
// (tests/browser/fixtures), so the built SPA boots against deterministic
// payloads shaped like the live cluster — no backend, no network.
//
// Checks may pass `onRoute` to intercept specific endpoints before the
// default handling (e.g. rotating the schema_version for the stale-schema
// guard, or shaping a dry-run failure). Return a truthy value to signal the
// override handled the request.
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
export const FIXTURES_DIR = join(here, '..', 'fixtures')

const _cache = new Map()
export async function fixture(name) {
  if (!_cache.has(name)) {
    _cache.set(name, JSON.parse(await readFile(join(FIXTURES_DIR, `${name}.json`), 'utf8')))
  }
  return _cache.get(name)
}

export const json = (data, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(data),
})

// Endpoint pathname → fixture file name (exact match, checked first).
const FIXTURE_BY_PATH = {
  '/api/auth/me': 'auth_me',
  '/api/health': 'health',
  '/api/meta': 'meta',
  '/api/ready': 'ready',
  '/api/plugins': 'plugins',
  '/api/config/schema': 'schema',
  '/api/templates': 'templates',
  '/api/ai/status': 'ai_status',
  '/api/ai/config': 'ai_config',
  '/api/runs/count': 'runs_count',
  '/api/daemon/status': 'daemon_status',
  '/api/stats': 'stats',
  '/api/pipelines': 'pipelines',
  '/api/cluster/nodes': 'cluster_nodes',
  '/api/cluster/streams': 'cluster_streams',
  '/api/schemas': 'schemas',
  '/api/mibs': 'mibs',
  '/api/runs': 'runs',
}

function decode(s) {
  try {
    return decodeURIComponent(s)
  } catch {
    return s
  }
}

async function fulfillFromFixtures(route, pathname, fixtures) {
  const exact = FIXTURE_BY_PATH[pathname]
  if (exact) {
    return route.fulfill(json(fixtures[exact]))
  }

  // /api/pipelines/{name} and friends
  if (pathname.startsWith('/api/pipelines/')) {
    const rest = pathname.slice('/api/pipelines/'.length)
    const [name = '', ...sub] = rest.split('/')
    if (sub.length === 0) {
      const pipeline = fixtures.pipelines.find((p) => p.name === decode(name))
      return route.fulfill(pipeline ? json(pipeline) : json({ detail: 'pipeline not found' }, 404))
    }
    if (sub[0] === 'placement') return route.fulfill(json({ detail: 'placement not found' }, 404))
    if (sub[0] === 'versions') return route.fulfill(json([]))
    if (sub[0] === 'alerts') return route.fulfill(json([]))
    if (sub[0] === 'dry-run') return route.fulfill(json({ valid: true, issues: [] }))
    return route.fulfill(json({ ok: true }))
  }

  // /api/runs/{run_id} (the /api/runs/count exact match above wins)
  if (pathname.startsWith('/api/runs/')) {
    const run = fixtures.runs.find((r) => r.run_id === decode(pathname.slice('/api/runs/'.length)))
    return route.fulfill(run ? json(run) : json({ detail: 'run not found' }, 404))
  }

  // Anything else the SPA calls that the checks do not drive: empty object
  // (the ad-hoc suite used the same fallback and every page tolerated it).
  return route.fulfill(json({}))
}

// Install the fixture stub on a page. `onRoute({ route, pathname, url,
// fixtures })` runs first; return truthy to short-circuit the default.
export async function installFixtures(page, { onRoute } = {}) {
  const fixtures = {}
  for (const name of new Set(Object.values(FIXTURE_BY_PATH))) {
    fixtures[name] = await fixture(name)
  }

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const pathname = url.pathname
    if (onRoute) {
      // Contract: onRoute awaits route.fulfill(...) itself and returns
      // `true` if it handled the request, or `false` to fall through to
      // the default fixture handling.
      const handled = await onRoute({ route, pathname, url, fixtures })
      if (handled) return
    }
    await fulfillFromFixtures(route, pathname, fixtures)
  })

  return fixtures
}