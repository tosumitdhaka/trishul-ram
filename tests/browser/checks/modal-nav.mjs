// Modal + navigation smoke (v1.4.8 modal-cleanup fixes): a shown Bootstrap
// modal keeps its backdrop, body scroll-lock (`modal-open` + inline
// overflow/padding) and focus trap OUTSIDE #content, so a route change while
// one is open (browser Back, deep links, #detail/a → #detail/b) used to
// strand an undismissable overlay on the incoming page. The router now runs
// closeAllModals() before every render swap; these checks prove:
//   1. Back with the templates modal open lands on a clean, interactive page.
//   2. #pipelines/templates deep link opens the modal and dismiss rewrites
//      the hash back to #pipelines (and only while still on pipelines).
//   3. A version modal open during a same-page route change leaves no
//      backdrop/scroll-lock behind.
//   4. Back while a Stop confirm is open resets the shared dialog (not
//      destroys it) — the next confirm still opens.
//   5. The brand version renders the real released version, not the `v—`
//      placeholder shipped in index.html.
// Static server + fixture stubs, no backend.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '../lib/playwright.mjs'
import { installFixtures, json } from '../lib/stub.mjs'
import { check, failureCount } from '../lib/check.mjs'

const BASE = process.env.TRAM_BROWSER_BASE || 'http://127.0.0.1:8899'

// The version YAML endpoint is exercised by the detail page's version modal;
// the stock stub returns [] for both, so shape one version here.
const VERSIONED_PIPELINE = 'sftp-pm-to-kafka'
const VERSIONS_FIXTURE = [{
  version: 1,
  is_active: true,
  created_at: '2026-09-22T06:16:06.581287+00:00',
}]
const VERSION_YAML = 'name: sftp-pm-to-kafka\ndescription: Poll SFTP for PM files and publish to Kafka\n'

// The version under gate comes from fixtures/meta.json (the health poller
// writes `v${version}` into #brand-ver), same source boot.mjs asserts.
const here = dirname(fileURLToPath(import.meta.url))
const META_FIXTURE = JSON.parse(readFileSync(join(here, '..', 'fixtures', 'meta.json'), 'utf8'))

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const page = await browser.newPage()
const pageErrors = []
const consoleErrors = []
page.on('pageerror', (e) => pageErrors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })

await installFixtures(page, {
  onRoute: async ({ route, pathname }) => {
    if (pathname === `/api/pipelines/${VERSIONED_PIPELINE}/versions`) {
      await route.fulfill(json(VERSIONS_FIXTURE))
      return true
    }
    if (pathname === `/api/pipelines/${VERSIONED_PIPELINE}/versions/1`) {
      await route.fulfill({ status: 200, contentType: 'text/plain', body: VERSION_YAML })
      return true
    }
    return false
  },
})

// Reads the modal/scroll-lock residue that a botched cleanup would leave on
// the incoming page. `shown` counts every modal still carrying .show;
// `confirmDisplay` is the shared dialog's effective display ('none' = reset
// for its next use, not destroyed).
const residue = () => page.evaluate(() => {
  const confirmEl = document.getElementById('tram-confirm-modal')
  return {
    backdrops: document.querySelectorAll('.modal-backdrop').length,
    modalOpen: document.body.classList.contains('modal-open'),
    overflow: document.body.style.overflow,
    shown: document.querySelectorAll('.modal.show').length,
    // null until the first confirmAction lazily builds the shared dialog.
    confirmDisplay: confirmEl ? getComputedStyle(confirmEl).display : null,
  }
})

// ── 1. Back with the templates modal open ─────────────────────────────────────
await page.goto(`${BASE}/#pipelines`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForFunction(() => document.querySelectorAll('#pl-body tr[data-pipeline-name]').length > 0, { timeout: 10000 })
await page.evaluate(() => { window.location.hash = '#pipelines/templates' })
await page.waitForFunction(() => document.getElementById('pl-templates-modal')?.classList.contains('show'), { timeout: 10000 })
await page.evaluate(() => history.back())
await page.waitForFunction(() => window.location.hash === '#pipelines' && document.querySelectorAll('#pl-body tr[data-pipeline-name]').length > 0, { timeout: 10000 })

const backState = await residue()
check('Back with modal open: no .modal-backdrop remains', backState.backdrops === 0, JSON.stringify(backState))
check('Back with modal open: body lost modal-open', !backState.modalOpen)
check('Back with modal open: body overflow cleared', backState.overflow === '', `got ${JSON.stringify(backState.overflow)}`)
check('Back with modal open: no .modal.show remains', backState.shown === 0)
await page.click('#pl-search')
check('Back with modal open: search accepts focus (page interactive)', await page.evaluate(() => document.activeElement === document.getElementById('pl-search')))

// ── 2. #pipelines/templates deep link ─────────────────────────────────────────
await page.goto(`${BASE}/#pipelines/templates`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForFunction(() => document.getElementById('pl-templates-modal')?.classList.contains('show'), { timeout: 10000 })
check('deep link opens the templates modal on fresh load', await page.evaluate(() => document.getElementById('pl-templates-modal')?.classList.contains('show')))
check('topbar title stays "Pipelines" while the modal is open', await page.evaluate(() => document.getElementById('tb-title')?.textContent === 'Pipelines'))
await page.click('#pl-tpl-list-header .btn-close')
await page.waitForFunction(() => window.location.hash === '#pipelines', { timeout: 10000 })
check('dismissing the deep-linked modal rewrites the hash to #pipelines', await page.evaluate(() => window.location.hash) === '#pipelines')

// ── 3. Same-page route change with the version modal open ─────────────────────
await page.goto(`${BASE}/#detail/${VERSIONED_PIPELINE}?tab=versions`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForFunction(() => document.querySelector('#detail-versions-body [data-action="view-version"]') !== null, { timeout: 10000 })
await page.click('#detail-versions-body [data-action="view-version"]')
await page.waitForFunction(() => document.getElementById('detail-version-modal')?.classList.contains('show'), { timeout: 10000 })
await page.evaluate(() => { window.location.hash = '#detail/snmp-to-kafka' })
await page.waitForFunction(() => document.getElementById('tb-title')?.textContent === 'Pipeline Detail: snmp-to-kafka', { timeout: 10000 })

const routeChangeState = await residue()
check('route change with modal open: no .modal-backdrop remains', routeChangeState.backdrops === 0, JSON.stringify(routeChangeState))
check('route change with modal open: body lost modal-open', !routeChangeState.modalOpen)
check('route change with modal open: body overflow cleared', routeChangeState.overflow === '', `got ${JSON.stringify(routeChangeState.overflow)}`)
check('route change with modal open: no .modal.show remains', routeChangeState.shown === 0)

// ── 4. Confirm dialog across navigation ───────────────────────────────────────
// Both fixture pipelines are running, so the table renders Stop buttons —
// clicking one opens the shared confirmAction dialog.
await page.goto(`${BASE}/#dashboard`, { waitUntil: 'networkidle', timeout: 15000 })
await page.evaluate(() => { window.location.hash = '#pipelines' })
await page.waitForFunction(() => document.querySelector('[data-action="stop"]') !== null, { timeout: 10000 })
await page.click('[data-action="stop"]')
await page.waitForFunction(() => document.getElementById('tram-confirm-modal')?.classList.contains('show'), { timeout: 10000 })
check('Stop opens the shared confirm dialog', await page.evaluate(() => document.getElementById('tram-confirm-modal')?.classList.contains('show')))

await page.evaluate(() => history.back())
await page.waitForFunction(() => document.getElementById('tb-title')?.textContent === 'Dashboard', { timeout: 10000 })

const confirmBackState = await residue()
check('Back with confirm open: no .modal-backdrop remains', confirmBackState.backdrops === 0, JSON.stringify(confirmBackState))
check('Back with confirm open: body lost modal-open', !confirmBackState.modalOpen)
check('Back with confirm open: body overflow cleared', confirmBackState.overflow === '', `got ${JSON.stringify(confirmBackState.overflow)}`)
check('Back with confirm open: no .modal.show remains', confirmBackState.shown === 0)
check('Back with confirm open: dialog reset to hidden (not destroyed)', confirmBackState.confirmDisplay === 'none', `got ${JSON.stringify(confirmBackState.confirmDisplay)}`)

// The shared dialog lives at body level — prove it still opens after the reset.
await page.evaluate(() => { window.location.hash = '#pipelines' })
await page.waitForFunction(() => document.querySelector('[data-action="stop"]') !== null, { timeout: 10000 })
await page.click('[data-action="stop"]')
const reopened = await page.waitForFunction(() => document.getElementById('tram-confirm-modal')?.classList.contains('show'), { timeout: 10000 }).then(() => true)
check('confirm dialog opens again after Back (element was reset, not destroyed)', reopened)
// Dismiss cleanly. The .show class lands before the fade transition finishes,
// so wait for focus to be trapped inside the dialog first — Escape only
// reaches Bootstrap's keydown handler once it bubbles through the modal.
await page.waitForFunction(() => document.getElementById('tram-confirm-modal')?.contains(document.activeElement), { timeout: 10000 })
await page.keyboard.press('Escape')
await page.waitForFunction(() => !document.getElementById('tram-confirm-modal')?.classList.contains('show'), { timeout: 10000 })

// ── 5. Brand literal ──────────────────────────────────────────────────────────
await page.goto(`${BASE}/#dashboard`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForFunction(() => (document.getElementById('brand-ver')?.textContent || '').trim() !== 'v—', { timeout: 10000 })
const brandVersion = await page.evaluate(() => document.getElementById('brand-ver')?.textContent.trim())
check('brand literal: brand-ver renders the released version, not the v— placeholder', brandVersion === `v${META_FIXTURE.version}`, `got ${JSON.stringify(brandVersion)} (fixture meta.json v${META_FIXTURE.version})`)

console.log('PAGE ERRORS:', pageErrors.length ? pageErrors : 'NONE')
console.log('CONSOLE ERRORS:', consoleErrors.length ? consoleErrors : 'NONE')

const ok = failureCount() === 0 && pageErrors.length === 0 && consoleErrors.length === 0
console.log(ok ? 'ALL CHECKS PASSED' : 'CHECKS FAILED')
await browser.close()
process.exit(ok ? 0 : 1)