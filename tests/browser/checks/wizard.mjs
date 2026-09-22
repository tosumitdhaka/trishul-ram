// Wizard smoke (L2): mounts #create, renders step 1 without console errors,
// the schema fetch populates the source-type list, step 2 renders
// schema-driven fields, inline required validation, the stale-schema guard
// (schema_version rotation blocks navigation/save and offers a reload),
// template pre-seed, and editor hand-off.
//
// API calls are stubbed at the network level from tests/browser/fixtures
// (shaped like the live cluster's schema descriptors, plus the
// schema_version key the branch serves).
import { chromium } from '../lib/playwright.mjs'
import { installFixtures, json } from '../lib/stub.mjs'
import { check, failureCount } from '../lib/check.mjs'

const BASE = process.env.TRAM_BROWSER_BASE || 'http://127.0.0.1:8899'

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const page = await browser.newPage()
const pageErrors = []
const consoleErrors = []
page.on('pageerror', (e) => pageErrors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })

// The stub latches schema-version rotation only when the check asks for it,
// so the stale-schema guard is exercised deterministically.
let rotate = false
let schemaFetches = 0
await installFixtures(page, {
  onRoute: async ({ route, pathname, fixtures }) => {
    if (pathname === '/api/config/schema') {
      schemaFetches += 1
      const version = rotate ? 'fff999eee888' : fixtures.schema.schema_version
      await route.fulfill(json({ ...fixtures.schema, schema_version: version }))
      return true
    }
    if (pathname === '/api/__test/rotate') {
      rotate = true
      await route.fulfill(json({ ok: true }))
      return true
    }
    return false
  },
})
const flipVersion = () => page.evaluate(() => fetch('/api/__test/rotate').then((r) => r.json()))

await page.goto(`${BASE}/#create`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(800)

const step1Visible = await page.evaluate(() => !document.getElementById('wiz-step-1')?.classList.contains('d-none'))
const stepCount = await page.evaluate(() => document.querySelectorAll('#wiz-steps .wiz-step').length)
const srcOptions = await page.evaluate(() => Array.from(document.querySelectorAll('#wiz-src-type option')).map((o) => o.value))
const aiDisabledNote = await page.evaluate(() => !document.getElementById('wiz-ai-unconfigured')?.classList.contains('d-none'))
check('wizard step 1 renders', step1Visible)
check('wizard has 5 steps', stepCount === 5, `got ${stepCount}`)
check('source types come from the schema fixture', JSON.stringify(srcOptions.filter(Boolean).sort()) === JSON.stringify(['kafka', 'sftp']), `got ${JSON.stringify(srcOptions)}`)
check('AI-unconfigured note shown (fixture says disabled)', aiDisabledNote)

// Step 1 → 2: fill name, Next.
await page.fill('#wiz-name', 'pw-test-pipeline')
await page.click('#wiz-next-btn')
await page.waitForTimeout(300)
const step2Visible = await page.evaluate(() => !document.getElementById('wiz-step-2')?.classList.contains('d-none'))

await page.selectOption('#wiz-src-type', 'kafka')
await page.waitForTimeout(200)
const visibleFields = await page.evaluate(() =>
  Array.from(document.querySelectorAll('#wiz-src-fields [data-field-name]'))
    .filter((el) => el.offsetParent !== null) // disclosure-collapsed fields stay in the DOM but hidden
    .map((el) => el.getAttribute('data-field-name')))
const allFieldNames = await page.evaluate(() =>
  Array.from(document.querySelectorAll('#wiz-src-fields [data-field-name]')).map((el) => el.getAttribute('data-field-name')))
const secretType = await page.evaluate(() => document.querySelector('#wiz-src-fields [data-field-name="api_key"]')?.type)
const advancedCollapsed = await page.evaluate(() => {
  const adv = document.querySelector('#wiz-src-fields [data-advanced-fields]')
  return adv ? !adv.classList.contains('open') : null
})
const serializerOpts = await page.evaluate(() => Array.from(document.querySelectorAll('#wiz-serializer option')).map((o) => o.value))
check('wizard advances to step 2', step2Visible)
check('required fields only revealed by default', JSON.stringify(visibleFields) === JSON.stringify(['brokers', 'topic']), `visible=${JSON.stringify(visibleFields)} all=${JSON.stringify(allFieldNames)}`)
check('optional fields exist but start hidden', JSON.stringify(allFieldNames.sort()) === JSON.stringify(['api_key', 'brokers', 'group_id', 'password', 'port', 'topic']), `got ${JSON.stringify(allFieldNames)}`)
check('secret field renders as password input', secretType === 'password', `got ${JSON.stringify(secretType)}`)
check('optional fields start collapsed behind the disclosure', advancedCollapsed === true)
check('serializer choices from schema fixture', JSON.stringify(serializerOpts.filter(Boolean).sort()) === JSON.stringify(['csv', 'json']), `got ${JSON.stringify(serializerOpts)}`)

// Optional disclosure opens on click.
await page.click('#wiz-src-fields [data-advanced-toggle]')
const advancedOpen = await page.evaluate(() => document.querySelector('#wiz-src-fields [data-advanced-fields]')?.classList.contains('open'))
check('advanced disclosure toggles open', advancedOpen)

// Missing-required inline validation: clear the required topic field, try Next.
await page.fill('#wiz-src-fields [data-field-name="topic"]', '')
await page.click('#wiz-next-btn')
await page.waitForTimeout(300)
const step3Visible = await page.evaluate(() => !document.getElementById('wiz-step-3')?.classList.contains('d-none'))
const invalidMarked = await page.evaluate(() => Boolean(document.querySelector('#wiz-src-fields .is-invalid')))
check('missing required field blocks advance', !step3Visible)
check('missing required field marked .is-invalid', invalidMarked)

// Fill required fields, advance through sinks + schedule to review.
await page.fill('#wiz-src-fields [data-field-name="topic"]', 'test-topic')
await page.fill('#wiz-src-fields [data-field-name="brokers"]', 'localhost:9092')
await page.click('#wiz-next-btn')
await page.waitForTimeout(300)
const step3Now = await page.evaluate(() => !document.getElementById('wiz-step-3')?.classList.contains('d-none'))
await page.click('#wiz-add-sink-btn')
await page.waitForTimeout(200)
await page.selectOption('#wiz-sinks-list .wiz-s-type', 'opensearch')
await page.waitForTimeout(200)
await page.fill('#wiz-sinks-list [data-field-name="hosts"]', 'localhost:9200')
await page.click('#wiz-next-btn')
await page.waitForTimeout(300)
const step4Now = await page.evaluate(() => !document.getElementById('wiz-step-4')?.classList.contains('d-none'))
await page.click('#wiz-next-btn')
await page.waitForTimeout(300)
const step5Now = await page.evaluate(() => !document.getElementById('wiz-step-5')?.classList.contains('d-none'))
const reviewYaml = await page.evaluate(() => document.getElementById('wiz-yaml-preview')?.value)
check('wizard advances through sinks to schedule', step3Now && step4Now)
check('wizard reaches the review step', step5Now)
check('review YAML is generated', Boolean(reviewYaml && reviewYaml.trim().length > 0))

// Stale-schema guard: rotate the version, then try Back→Next (freshness check).
await flipVersion()
await page.click('#wiz-back-btn')
await page.waitForTimeout(200)
await page.click('#wiz-next-btn')
await page.waitForTimeout(400)
const staleBanner = await page.evaluate(() => !document.getElementById('wiz-stale-banner')?.classList.contains('d-none'))
const navBlocked = await page.evaluate(() => document.getElementById('wiz-next-btn')?.hasAttribute('disabled'))
const saveBlocked = await page.evaluate(() => document.getElementById('wiz-save-btn')?.hasAttribute('disabled'))
check('stale-schema banner shown after version rotation', staleBanner)
check('navigation blocked while stale', navBlocked)
check('save blocked while stale', saveBlocked)

// Reload the form: the guard clears.
await page.click('#wiz-reload-schema-btn')
await page.waitForTimeout(400)
const bannerHidden = await page.evaluate(() => document.getElementById('wiz-stale-banner')?.classList.contains('d-none'))
const navRestored = await page.evaluate(() => !document.getElementById('wiz-next-btn')?.hasAttribute('disabled'))
check('reload clears the stale banner', bannerHidden)
check('navigation restored after reload', navRestored)

// Template pre-seed route.
await page.goto(`${BASE}/#create?template=kafka-to-os`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(600)
const seededName = await page.evaluate(() => document.getElementById('wiz-name')?.value)
const seededSrc = await page.evaluate(() => document.getElementById('wiz-src-type')?.value)
check('template pre-seeds the pipeline name', seededName === 'kafka-to-os', `got ${JSON.stringify(seededName)}`)
check('template pre-seeds the source type', seededSrc === 'kafka', `got ${JSON.stringify(seededSrc)}`)

// Editor hand-off: sessionStorage prefill + route flag.
await page.goto(`${BASE}/#create`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(400)
await page.evaluate(() => sessionStorage.setItem('tram_wizard_prefill', 'name: handoff-check\n'))
await page.evaluate(() => { window.location.hash = '#editor?from=wizard&return=pipelines' })
await page.waitForTimeout(800)
const editorText = await page.evaluate(() => document.getElementById('editor-textarea')?.value?.split('\n')[0])
check('editor hand-off prefills from sessionStorage', editorText === 'name: handoff-check', `got ${JSON.stringify(editorText)}`)

console.log('SCHEMA_FETCHES:', schemaFetches)
console.log('PAGE ERRORS:', pageErrors.length ? pageErrors : 'NONE')
console.log('CONSOLE ERRORS:', consoleErrors.length ? consoleErrors : 'NONE')

const ok = failureCount() === 0 && pageErrors.length === 0 && consoleErrors.length === 0
console.log(ok ? 'ALL CHECKS PASSED' : 'CHECKS FAILED')
await browser.close()
process.exit(ok ? 0 : 1)