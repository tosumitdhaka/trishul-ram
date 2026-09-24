// Wizard YAML quoting + poll page-leave guard (L7 release-review fixes):
//   1. Numeric/boolean-looking strings in text/secret/select kinds must be
//      QUOTED in the review YAML (`password: "12345"`, `name: "123"`), while
//      genuine integer fields stay unquoted (`port: 42`).
//   2. Navigating away during init's schema load must not start the 60s
//      schema poll (verified by counting /api/config/schema fetches — the
//      poll interval is stubbed to 3s via window.__TRAM_TEST_SCHEMA_POLL_MS__
//      so the guard can be proven in seconds, not a 61.5s wait).
//   3. `#wizard` redirects to `#create`.
// The static server has no backend — API calls are stubbed at the network
// level from tests/browser/fixtures, shaped like the kind cluster's schema.
import { chromium } from '../lib/playwright.mjs'
import { installFixtures, json } from '../lib/stub.mjs'
import { check, failureCount } from '../lib/check.mjs'

const BASE = process.env.TRAM_BROWSER_BASE || 'http://127.0.0.1:8899'

let schemaFetches = 0
let holdSchema = false
let releaseSchema = null
let schemaGate = null
const makeGate = () => { schemaGate = new Promise((res) => { releaseSchema = res }) }

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const page = await browser.newPage()
const pageErrors = []
const consoleErrors = []
page.on('pageerror', (e) => pageErrors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })

await installFixtures(page, {
  onRoute: async ({ route, pathname, fixtures }) => {
    if (pathname === '/api/__test/hold-on') { holdSchema = true; await route.fulfill(json({ ok: true })); return true }
    if (pathname === '/api/__test/release') {
      holdSchema = false
      if (releaseSchema) { const r = releaseSchema; releaseSchema = null; r() }
      await route.fulfill(json({ ok: true }))
      return true
    }
    if (pathname === '/api/config/schema') {
      schemaFetches += 1
      if (holdSchema) { if (!schemaGate) makeGate(); await schemaGate }
      await route.fulfill(json({ ...fixtures.schema, schema_version: fixtures.schema.schema_version }))
      return true
    }
    return false
  },
})

// Stub the schema poll to 3s so the page-leave guard below is proven in
// seconds (addInitScript runs before the first document load and the key
// persists on the window for the whole SPA session).
await page.addInitScript(() => { window.__TRAM_TEST_SCHEMA_POLL_MS__ = 3000 })

const holdOn = () => page.evaluate(() => fetch('/api/__test/hold-on').then((r) => r.json()))
const release = () => page.evaluate(() => fetch('/api/__test/release').then((r) => r.json()))

// ── Phase A: review YAML quoting ─────────────────────────────────────────────
await page.goto(`${BASE}/#create`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(600)

// Step 1: numeric pipeline name + numeric-looking description.
await page.fill('#wiz-name', '123')
await page.fill('#wiz-desc', '12345')
await page.click('#wiz-next-btn')
await page.waitForTimeout(300)

// Step 2: kafka source; required fields + secret/text/integer optional fields.
await page.selectOption('#wiz-src-type', 'kafka')
await page.waitForTimeout(200)
await page.fill('#wiz-src-fields [data-field-name="brokers"]', 'host:9092')
await page.fill('#wiz-src-fields [data-field-name="topic"]', 'true')
await page.click('#wiz-src-fields [data-advanced-toggle]')
await page.fill('#wiz-src-fields [data-field-name="password"]', '12345')
await page.fill('#wiz-src-fields [data-field-name="port"]', '42')
await page.click('#wiz-next-btn')
await page.waitForTimeout(300)

// Step 3: one kafka sink so step 3 collects.
await page.click('#wiz-add-sink-btn')
await page.waitForTimeout(200)
await page.selectOption('#wiz-sinks-list .wiz-s-type', 'kafka')
await page.waitForTimeout(200)
await page.fill('#wiz-sinks-list [data-field-name="topic"]', 'sink-topic')
await page.click('#wiz-next-btn')
await page.waitForTimeout(300)

// Step 4 → 5 review.
await page.click('#wiz-next-btn')
await page.waitForTimeout(400)
const reviewYaml = await page.evaluate(() => document.getElementById('wiz-yaml-preview')?.value || '')
console.log('REVIEW_YAML:\n' + reviewYaml + '\n-----')
const lines = reviewYaml.split('\n')
check('name quoted (numeric pipeline name)', lines[0] === 'name: "123"', `got ${JSON.stringify(lines[0])}`)
check('description quoted (numeric-looking text)', lines.includes('description: "12345"'))
check('secret password quoted', lines.includes('  password: "12345"'))
check('text field boolean-looking value quoted', lines.includes('  topic: "true"'))
check('integer field stays unquoted', lines.includes('  port: 42'))
check('list item still quoted', lines.includes('    - "host:9092"'))

// ── Phase A2: legacy #wizard alias redirects to #create ──────────────────────
await page.evaluate(() => { window.location.hash = '#wizard?return=detail' })
await page.waitForTimeout(600)
const hashAfterWizard = await page.evaluate(() => window.location.hash)
const wizardLandsOnCreate = await page.evaluate(() => Boolean(document.getElementById('wiz-step-1')))
check('legacy #wizard redirects to #create', hashAfterWizard.startsWith('#create') && wizardLandsOnCreate, `hash=${hashAfterWizard}`)

// ── Phase B: no stray schema poll after leaving mid-init ─────────────────────
await holdOn()
await page.evaluate(() => { window.location.hash = '#pipelines' })
await page.waitForTimeout(500)
await page.evaluate(() => { window.location.hash = '#create' })
await page.waitForTimeout(500) // init() awaits the held schema fetch
await page.evaluate(() => { window.location.hash = '#pipelines' }) // page-leave fires while init still pending
await page.waitForTimeout(500)
await release()
await page.waitForTimeout(1200) // init resumes; _startSchemaPoll would run here if unguarded
const fetchesAfterRelease = schemaFetches
await page.waitForTimeout(3500) // one stubbed 3s poll interval + margin
const fetchesAfterPollWindow = schemaFetches
check('no stray schema poll after page-leave during init', fetchesAfterPollWindow === fetchesAfterRelease, `fetches after release=${fetchesAfterRelease}, after 3s=${fetchesAfterPollWindow}`)

console.log('SCHEMA_FETCHES_AFTER_RELEASE:', fetchesAfterRelease)
console.log('SCHEMA_FETCHES_AFTER_3S:', fetchesAfterPollWindow)
console.log('PAGE ERRORS:', pageErrors.length ? pageErrors : 'NONE')
console.log('CONSOLE ERRORS:', consoleErrors.length ? consoleErrors : 'NONE')

const ok = failureCount() === 0 && pageErrors.length === 0 && consoleErrors.length === 0
console.log(ok ? 'ALL CHECKS PASSED' : 'CHECKS FAILED')
await browser.close()
process.exit(ok ? 0 : 1)