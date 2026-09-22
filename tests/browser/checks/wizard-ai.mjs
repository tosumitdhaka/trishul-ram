// Wizard AI-assist path — regression for the three v1.4.4 operator bugs:
//   1. AI generate must NOT fire form-step validation toasts ("Select a
//      source type", "Add at least one sink", "Pipeline name is required")
//      for the empty form state the AI path never fills, and the
//      informational "YAML generated — review and save" toast must render as
//      info, not as a success/error.
//   2. Continue In Editor after AI assist must hand the AI-generated YAML
//      over intact (the Review textarea, not a rebuild from the empty form
//      state).
//   3. Save Pipeline must land on the created pipeline's detail page with
//      working buttons — never a stale/empty-name redirect to a page whose
//      controls are dead (unwired after a 404, or covered by an overlay).
// Static server + fixture stubs, no backend.
import { chromium } from '../lib/playwright.mjs'
import { installFixtures, json } from '../lib/stub.mjs'
import { check, failureCount } from '../lib/check.mjs'

const BASE = process.env.TRAM_BROWSER_BASE || 'http://127.0.0.1:8899'
const SHOT_DIR = process.env.TRAM_SHOT_DIR || '/tmp/opencode/tram-pw'

const AI_YAML = [
  'name: ai-generated-pipe',
  'description: "produced by AI assist"',
  'schedule:',
  '  type: interval',
  '  interval_seconds: 300',
  'source:',
  '  type: kafka',
  '  brokers:',
  '    - localhost:9092',
  '  topic: ai-topic',
  'serializer_in:',
  '  type: json',
  'sinks:',
  '  - type: opensearch',
  '    hosts:',
  '      - localhost:9200',
].join('\n')

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const page = await browser.newPage()
const pageErrors = []
const consoleErrors = []
page.on('pageerror', (e) => pageErrors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })

let suggestCount = 0
let createCount = 0
const createdByName = new Map() // pipeline name → yaml, as served back to the SPA

await installFixtures(page, {
  onRoute: async ({ route, pathname, url, fixtures }) => {
    if (pathname === '/api/ai/status') {
      await route.fulfill(json({
        enabled: true,
        provider: 'anthropic',
        model: 'claude-haiku-4-5-20251001',
        schema_version: fixtures.schema.schema_version,
      }))
      return true
    }
    if (pathname === '/api/ai/suggest') {
      suggestCount += 1
      await route.fulfill(json({ yaml: AI_YAML, valid: true, issues: [] }))
      return true
    }
    if (pathname === '/api/pipelines' && url.pathname === '/api/pipelines') {
      if (route.request().method() === 'POST') {
        createCount += 1
        const body = route.request().postData() || ''
        const m = body.match(/^\s*name:\s*(.+)$/m)
        const name = m ? String(m[1]).split('#')[0].trim().replace(/^["']|["']$/g, '') : 'unknown'
        createdByName.set(name, body)
        await route.fulfill(json({ ok: true }))
        return true
      }
      return false
    }
    // Serve back pipelines created during the check so the detail page
    // renders fully (the static fixtures only list pre-existing ones).
    if (pathname.startsWith('/api/pipelines/')) {
      const rest = pathname.slice('/api/pipelines/'.length)
      const [name = '', ...sub] = rest.split('/')
      const decodedName = decodeURIComponent(name)
      if (sub.length === 0 && createdByName.has(decodedName)) {
        const yaml = createdByName.get(decodedName)
        await route.fulfill(json({
          name: decodedName,
          status: 'stopped',
          schedule_type: 'interval',
          interval_seconds: 300,
          enabled: true,
          source: { type: 'kafka' },
          sinks: [{ type: 'opensearch' }],
          yaml,
        }))
        return true
      }
      if (sub[0] === 'placement' && createdByName.has(decodedName)) {
        // The default stub only serves placement for fixture pipelines —
        // mirror it for the AI-created one so the detail page logs no 404.
        await route.fulfill(json({
          pipeline_name: decodedName,
          placement_group_id: null,
          status: 'stopped',
          active_slots: 0,
          slot_count: 1,
          started_at: null,
          records_out_per_sec: 0,
          error_count: 0,
          slots: [],
        }))
        return true
      }
      return false
    }
    return false
  },
})

const toastSnapshot = () => page.evaluate(() =>
  Array.from(document.querySelectorAll('#tram-toast-stack .tram-toast')).map((t) => {
    const cs = getComputedStyle(t)
    return {
      text: t.querySelector('.tram-toast-msg')?.textContent || '',
      cls: t.className,
      bg: cs.backgroundColor,
    }
  }))

// ── Phase 1: AI generate on step 1 → Review, toast hygiene ──────────────────
await page.goto(`${BASE}/#create`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(700)

const aiEnabled = await page.evaluate(() => !document.getElementById('wiz-ai-gen-btn')?.disabled)
check('AI generate button enabled (status stub says enabled)', aiEnabled)

await page.fill('#wiz-ai-prompt', 'Poll SFTP every 5 minutes and push to Kafka')
await page.click('#wiz-ai-gen-btn')
await page.waitForTimeout(700)

const step5Visible = await page.evaluate(() => !document.getElementById('wiz-step-5')?.classList.contains('d-none'))
const reviewYaml = await page.evaluate(() => document.getElementById('wiz-yaml-preview')?.value || '')
const toasts1 = await toastSnapshot()
console.log('TOASTS_AFTER_AI_GENERATE:', JSON.stringify(toasts1))

check('AI generate jumps to the Review step', step5Visible)
check('Review textarea holds the AI YAML', reviewYaml.trim() === AI_YAML, `first line: ${JSON.stringify(reviewYaml.split('\n')[0])}`)
check('no "Select a source type" validation toast', !toasts1.some((t) => t.text.includes('Select a source type')))
check('no "Add at least one sink" validation toast', !toasts1.some((t) => t.text.includes('Add at least one sink')))
check('no "Pipeline name is required" validation toast', !toasts1.some((t) => t.text.includes('Pipeline name is required')))
const generatedToast = toasts1.find((t) => t.text.includes('YAML generated — review and save'))
check('"YAML generated" toast is an info toast (not success/error)',
  Boolean(generatedToast) && generatedToast.cls.includes('tram-toast-info'),
  `got ${JSON.stringify(generatedToast)}`)
await page.screenshot({ path: `${SHOT_DIR}/bug1-ai-toasts.png` })

// ── Phase 2: Continue In Editor hands the AI YAML over intact ───────────────
await page.click('#wiz-open-editor-btn')
await page.waitForTimeout(900)

const editorText = await page.evaluate(() => document.getElementById('editor-textarea')?.value || '')
console.log('EDITOR_TEXTAREA_FIRST_LINE:', JSON.stringify(editorText.split('\n')[0]))
check('editor textarea holds the AI YAML after hand-off', editorText.trim() === AI_YAML,
  `first line: ${JSON.stringify(editorText.split('\n')[0])}, expected ${JSON.stringify(AI_YAML.split('\n')[0])}`)
await page.screenshot({ path: `${SHOT_DIR}/bug2-editor-handoff.png` })

// ── Phase 2b: navigate from the editor to the pipelines page, buttons live ──
await page.click('#editor-cancel-btn')
await page.waitForTimeout(900)
const pipelinesRows = await page.evaluate(() => document.querySelectorAll('#pl-body tr[data-pipeline-name]').length)
const newBtnClickable = await page.evaluate(() => {
  document.getElementById('pl-new-btn')?.click()
  return true
})
await page.waitForTimeout(500)
const hashAfterNew = await page.evaluate(() => window.location.hash)
check('editor Cancel lands on the pipelines page', pipelinesRows === 2, `rows=${pipelinesRows}`)
check('pipelines "New Pipeline" button responds to click', newBtnClickable && hashAfterNew.startsWith('#create'), `hash=${hashAfterNew}`)

// ── Phase 3: Save Pipeline after AI assist (operator path, name typed) ──────
await page.goto(`${BASE}/#create`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(700)
await page.fill('#wiz-name', 'typed-pipe-name')
await page.fill('#wiz-ai-prompt', 'Ingest logs to OpenSearch')
await page.click('#wiz-ai-gen-btn')
await page.waitForTimeout(700)
await page.click('#wiz-save-btn')
await page.waitForTimeout(1200)

const afterSave = await page.evaluate(() => ({
  hash: window.location.hash,
  detail: Boolean(document.getElementById('detail-title')),
  detailErr: Boolean(document.querySelector('#detail-runs-body .table-state-error')),
  runBtn: document.getElementById('detail-run-btn')?.textContent?.trim() || '',
  editWired: typeof document.getElementById('detail-edit-btn')?.onclick === 'function',
}))
console.log('AFTER_SAVE_TYPED_NAME:', JSON.stringify(afterSave))
check('Save lands on the created pipeline detail page', afterSave.detail && afterSave.hash.startsWith('#detail/'), JSON.stringify(afterSave))
check('detail page loaded without an error state', !afterSave.detailErr, JSON.stringify(afterSave))
check('detail page buttons are wired (edit responds)', afterSave.editWired, JSON.stringify(afterSave))
check('Save Pipeline POST reached the API', createCount === 1, `createCount=${createCount}`)
await page.screenshot({ path: `${SHOT_DIR}/bug3-after-save-typed-name.png` })

// ── Phase 4: Save Pipeline after AI assist without typing a name ────────────
await page.goto(`${BASE}/#create`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(700)
await page.fill('#wiz-ai-prompt', 'Collect metrics into OpenSearch')
await page.click('#wiz-ai-gen-btn')
await page.waitForTimeout(700)
await page.click('#wiz-save-btn')
await page.waitForTimeout(1200)

const afterSave2 = await page.evaluate(() => ({
  hash: window.location.hash,
  detail: Boolean(document.getElementById('detail-title')),
  editWired: typeof document.getElementById('detail-edit-btn')?.onclick === 'function',
}))
console.log('AFTER_SAVE_NO_NAME:', JSON.stringify(afterSave2))
check('Save without a typed name lands on the AI-named pipeline detail page',
  afterSave2.detail && afterSave2.hash.startsWith('#detail/ai-generated-pipe'), JSON.stringify(afterSave2))
check('detail page buttons wired in the no-name variant too', afterSave2.editWired, JSON.stringify(afterSave2))
await page.screenshot({ path: `${SHOT_DIR}/bug3-after-save-no-name.png` })

console.log('SUGGEST_COUNT:', suggestCount)
console.log('CREATE_COUNT:', createCount)
console.log('PAGE ERRORS:', pageErrors.length ? pageErrors : 'NONE')
console.log('CONSOLE ERRORS:', consoleErrors.length ? consoleErrors : 'NONE')

const ok = failureCount() === 0 && pageErrors.length === 0 && consoleErrors.length === 0
console.log(ok ? 'ALL CHECKS PASSED' : 'CHECKS FAILED')
await browser.close()
process.exit(ok ? 0 : 1)