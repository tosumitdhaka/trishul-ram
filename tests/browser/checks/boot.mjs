// Boot smoke: every main page route boots without console/page errors,
// without failed network requests, and the shell renders the released
// version. This is the regression net for the v1.4.3 class of browser-only
// release blockers: a missing import crashing the SPA at boot (frozen shell)
// or a confirm modal that never resolves.
//
// Run under node >= 20. The static server + fixture stubs come from run.mjs
// (or set TRAM_BROWSER_BASE to point at any tram/ui/dist server).
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '../lib/playwright.mjs'
import { installFixtures } from '../lib/stub.mjs'
import { check, failureCount } from '../lib/check.mjs'

const BASE = process.env.TRAM_BROWSER_BASE || 'http://127.0.0.1:8899'

// The version under gate comes from tram/ui/package.json, not a hardcoded
// string — a stale fixtures/meta.json then fails the boot check instead of
// drifting silently alongside the assertion (the v1.4.3→1.4.6 drift class).
const here = dirname(fileURLToPath(import.meta.url))
const UI_PACKAGE = JSON.parse(readFileSync(join(here, '..', '..', '..', 'tram', 'ui', 'package.json'), 'utf8'))
const EXPECTED_VERSION = UI_PACKAGE.version
const META_FIXTURE = JSON.parse(readFileSync(join(here, '..', 'fixtures', 'meta.json'), 'utf8'))

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const page = await browser.newPage()
const pageErrors = []
const consoleErrors = []
const failedRequests = []
page.on('pageerror', (e) => pageErrors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })
page.on('requestfailed', (req) => {
  failedRequests.push({ url: req.url(), failure: req.failure()?.errorText ?? 'unknown' })
})

await installFixtures(page)

const errorsSoFar = () =>
  pageErrors.length + consoleErrors.length +
  failedRequests.filter((f) => !/net::ERR_ABORTED/.test(f.failure)).length

// ── Boot: shell + version ────────────────────────────────────────────────────
await page.goto(`${BASE}/#dashboard`, { waitUntil: 'networkidle', timeout: 15000 })

// The brand version is populated by the health poller from /api/meta —
// wait deterministically instead of sleeping.
await page.waitForFunction(
  () => (document.getElementById('brand-ver')?.textContent || '').trim().length > 0,
  { timeout: 10000 }
)
const brandVersion = await page.evaluate(() => document.getElementById("brand-ver")?.textContent.trim())
const hcVersion = await page.evaluate(() => document.getElementById('hc-version')?.textContent.trim())
check('shell visible (auth-disabled boot path)', await page.evaluate(() => document.getElementById('app-shell')?.hidden === false))
check('fixture meta.json version matches tram/ui/package.json', META_FIXTURE.version === EXPECTED_VERSION, `fixture=${META_FIXTURE.version}, package.json=${EXPECTED_VERSION}`)
check('brand version renders', brandVersion === `v${EXPECTED_VERSION}`, `got ${JSON.stringify(brandVersion)} (fixture meta.json, expected v${EXPECTED_VERSION})`)
check('health card shows the same version', hcVersion === EXPECTED_VERSION, `got ${JSON.stringify(hcVersion)} (expected ${EXPECTED_VERSION})`)
check('no errors at boot', errorsSoFar() === 0)

// ── Every main page route ────────────────────────────────────────────────────
// { route, expected topbar title } — router.js meta titles.
const ROUTES = [
  ['#dashboard', 'Dashboard'],
  ['#pipelines', 'Pipelines'],
  ['#runs', 'Run History'],
  ['#editor', 'Pipeline Editor'],
  ['#create', 'New Pipeline'],
  ['#schemas', 'Schemas'],
  ['#mibs', 'MIB Modules'],
  ['#cluster', 'Cluster'],
  ['#plugins', 'Plugins'],
  ['#settings', 'Settings'],
  ['#detail/sftp-pm-to-kafka', 'Pipeline Detail: sftp-pm-to-kafka'],
  ['#runs/479d6f4d-fa80-4e79-976c-6a6c1b84925c', 'Run Detail: 479d6f4d'],
]

for (const [route, expectedTitle] of ROUTES) {
  const errsBefore = errorsSoFar()
  await page.evaluate((r) => { window.location.hash = r }, route)
  await page.waitForFunction(
    (title) => document.getElementById('tb-title')?.textContent === title,
    expectedTitle,
    { timeout: 10000 }
  )
  // Let the lazy page init settle, then verify no new errors appeared.
  await page.waitForTimeout(500)
  const errsAfter = errorsSoFar()
  check(`#${route.replace(/^#/, '').replace(/\/.*/, '')} boots clean (${expectedTitle})`, errsAfter === errsBefore)
}

// ── Final verdict ────────────────────────────────────────────────────────────
const realFailures = failedRequests.filter((f) => !/net::ERR_ABORTED/.test(f.failure))
console.log('PAGE ERRORS:', pageErrors.length ? pageErrors : 'NONE')
console.log('CONSOLE ERRORS:', consoleErrors.length ? consoleErrors : 'NONE')
console.log('FAILED REQUESTS:', realFailures.length ? JSON.stringify(realFailures) : 'NONE')
if (failedRequests.some((f) => /net::ERR_ABORTED/.test(f.failure))) {
  console.log('  (aborted requests from hash navigation ignored — expected noise)')
}

const ok = failureCount() === 0 && pageErrors.length === 0 && consoleErrors.length === 0 && realFailures.length === 0
console.log(ok ? 'ALL CHECKS PASSED' : 'CHECKS FAILED')
await browser.close()
process.exit(ok ? 0 : 1)