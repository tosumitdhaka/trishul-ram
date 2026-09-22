// Editor smoke (L5): gutter renders, typing updates the line count, the
// highlight layer tokenizes, textarea interaction (type/cursor/tab) works,
// error anchoring marks the right gutter line, typing clears the marks, the
// draft guard persists, and the wizard Review step still renders (regression).
// Static server + fixture stubs, no backend.
import { chromium } from '../lib/playwright.mjs'
import { installFixtures, json } from '../lib/stub.mjs'
import { check, failureCount } from '../lib/check.mjs'

const BASE = process.env.TRAM_BROWSER_BASE || 'http://127.0.0.1:8899'

const PIPELINE_YAML = [
  'name: pw-pipeline',
  'description: "l5 check"',                     // quoted string
  'schedule:',
  '  type: interval',
  '  interval_seconds: 300 # every 5m',          // number + inline comment
  'source:',
  '  type: kafka',
  '  brokers:',
  '    - localhost:9092',
  '  topic: $(TOPIC)',                           // env substitution
  'sinks:',
  '  - type: opensearch',
  '    hosts:',
  '      - localhost:9200',
  '',
].join('\n')

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const page = await browser.newPage()
const pageErrors = []
const consoleErrors = []
page.on('pageerror', (e) => pageErrors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })

await installFixtures(page, {
  onRoute: async ({ route, pathname }) => {
    if (pathname === '/api/pipelines/dry-run') {
      // Validation-shaped failure naming a key present in the YAML (source).
      await route.fulfill(json({ valid: false, issues: ['1 validation error for PipelineConfig\nsource\n  Field required [type=missing]'] }))
      return true
    }
    return false
  },
})

await page.goto(`${BASE}/#editor`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(800)

// 1. Gutter + highlight render with the default template.
const gutterLines = await page.evaluate(() => document.querySelectorAll('#editor-gutter .editor-gutter-line').length)
const taLines = await page.evaluate(() => document.getElementById('editor-textarea').value.split('\n').length)
const hasTokens = await page.evaluate(() => ({
  key: document.querySelectorAll('#editor-highlight .yk').length,
  str: document.querySelectorAll('#editor-highlight .yv').length,
  comment: document.querySelectorAll('#editor-highlight .yc').length,
}))
check('gutter renders with the default template', gutterLines === taLines, `gutter=${gutterLines} textarea=${taLines}`)
check('highlight layer tokenizes keys and values', hasTokens.key > 0 && hasTokens.str > 0, JSON.stringify(hasTokens))

// 2. Load the multi-feature YAML, verify tokenizer classes per feature.
await page.evaluate((yaml) => {
  const ta = document.getElementById('editor-textarea')
  ta.value = yaml
  ta.dispatchEvent(new Event('input', { bubbles: true }))
}, PIPELINE_YAML)
await page.waitForTimeout(200)
const tokens = await page.evaluate(() => ({
  lines: document.querySelectorAll('#editor-gutter .editor-gutter-line').length,
  keys: document.querySelectorAll('#editor-highlight .yk').length,
  values: document.querySelectorAll('#editor-highlight .yv').length,
  numbers: document.querySelectorAll('#editor-highlight .yb').length,
  env: document.querySelectorAll('#editor-highlight .ys').length,
  comments: document.querySelectorAll('#editor-highlight .yc').length,
  blockSpans: document.querySelectorAll('#editor-highlight .yl').length,
}))
check('all features tokenize (key/value/env/comment)', tokens.keys > 0 && tokens.values > 0 && tokens.env > 0 && tokens.comments > 0, JSON.stringify(tokens))
check('line spans match gutter lines', tokens.lines === tokens.blockSpans, `lines=${tokens.lines} spans=${tokens.blockSpans}`)

// 3. Typing works through the overlay: caret + insertion + gutter sync.
await page.click('#editor-textarea')
await page.keyboard.press('Control+Home')
await page.keyboard.type('\n')
await page.keyboard.type('note: typed-value')
await page.waitForTimeout(800) // draft save is debounced 500ms
const afterType = await page.evaluate(() => ({
  lines: document.querySelectorAll('#editor-gutter .editor-gutter-line').length,
  caret: document.getElementById('editor-textarea').selectionStart,
  draftSaved: Boolean(localStorage.getItem('tram_editor_draft')),
}))
check('typing adds a line and syncs the gutter', afterType.lines === tokens.lines + 1, `lines=${afterType.lines} expected=${tokens.lines + 1}`)
check('caret moved with typing', afterType.caret > 0)
check('draft guard persists after typing', afterType.draftSaved)

// 4. Tab key inserts two spaces and keeps the gutter synced.
await page.keyboard.press('Tab')
const tabResult = await page.evaluate(() => ({
  spaces: document.getElementById('editor-textarea').value.includes('note: typed-value  '),
  gutter: document.querySelectorAll('#editor-gutter .editor-gutter-line').length,
}))
check('tab inserts two spaces at the caret', tabResult.spaces)
check('gutter stays synced after tab', tabResult.gutter === afterType.lines)

// 5. Transparent text + visible caret (overlay prerequisites).
const overlayStyles = await page.evaluate(() => {
  const ta = document.getElementById('editor-textarea')
  const cs = getComputedStyle(ta)
  return { color: cs.color, caretColor: cs.caretColor, wrap: ta.wrap, lineHeight: cs.lineHeight }
})
check('overlay text is transparent, caret visible', /rgba\(0, 0, 0, 0\)/.test(overlayStyles.color) || overlayStyles.color === 'transparent', JSON.stringify(overlayStyles))

// 6. Scroll sync.
await page.evaluate(() => {
  const ta = document.getElementById('editor-textarea')
  ta.scrollTop = 60
  ta.dispatchEvent(new Event('scroll'))
})
await page.waitForTimeout(100)
const scrollSync = await page.evaluate(() => ({
  ta: document.getElementById('editor-textarea').scrollTop,
  pre: document.getElementById('editor-highlight').scrollTop,
  gutter: document.getElementById('editor-gutter').scrollTop,
}))
check('scroll is synced across textarea/highlight/gutter', scrollSync.ta === scrollSync.pre && scrollSync.ta === scrollSync.gutter, JSON.stringify(scrollSync))

// 7. Error anchoring: dry-run failure names `source` → gutter line of `source:` marked.
await page.click('#editor-dry-run-btn')
await page.waitForTimeout(500)
const anchored = await page.evaluate(() => {
  const errLine = document.querySelector('#editor-gutter .editor-gutter-error')
  const errSpan = document.querySelector('#editor-highlight .yl-error')
  const sourceLine = document.getElementById('editor-textarea').value.split('\n').findIndex((l) => l === 'source:') + 1
  return {
    markedLine: errLine ? parseInt(errLine.dataset.line, 10) : null,
    spanLine: errSpan ? parseInt(errSpan.dataset.line, 10) : null,
    actualSourceLine: sourceLine,
  }
})
check('dry-run error anchors to the source line', anchored.markedLine === anchored.actualSourceLine && anchored.spanLine === anchored.actualSourceLine, JSON.stringify(anchored))

// 8. Typing clears the error marks.
await page.keyboard.type('x')
await page.waitForTimeout(150)
const cleared = await page.evaluate(() => document.querySelectorAll('#editor-gutter .editor-gutter-error').length)
check('typing clears the error marks', cleared === 0)

// 9. Wizard Review step still renders (regression check from L2).
await page.goto(`${BASE}/#create`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(700)
await page.fill('#wiz-name', 'regression')
await page.click('#wiz-next-btn'); await page.waitForTimeout(200)
await page.selectOption('#wiz-src-type', 'kafka'); await page.waitForTimeout(200)
await page.fill('#wiz-src-fields [data-field-name="topic"]', 't')
await page.fill('#wiz-src-fields [data-field-name="brokers"]', 'localhost:9092')
await page.click('#wiz-next-btn'); await page.waitForTimeout(200)
await page.click('#wiz-add-sink-btn'); await page.waitForTimeout(150)
await page.click('#wiz-next-btn'); await page.waitForTimeout(600) // sinks validation must block — sink has no type
const sinkDiag = await page.evaluate(() => ({
  activeStep: document.querySelector('#wiz-steps .wiz-step.active')?.dataset.step,
  step4Hidden: document.getElementById('wiz-step-4')?.classList.contains('d-none'),
  cards: document.querySelectorAll('.wizard-sink-card').length,
  selects: Array.from(document.querySelectorAll('.wiz-s-type')).map((s) => s.value),
}))
check('wizard sink validation blocks an empty sink', sinkDiag.step4Hidden, JSON.stringify(sinkDiag))
await page.selectOption('#wiz-sinks-list .wiz-s-type', 'opensearch'); await page.waitForTimeout(200)
await page.fill('#wiz-sinks-list [data-field-name="hosts"]', 'h:9200')
await page.click('#wiz-next-btn'); await page.waitForTimeout(200)
await page.click('#wiz-next-btn'); await page.waitForTimeout(200)
const reviewVisible = await page.evaluate(() => !document.getElementById('wiz-step-5')?.classList.contains('d-none'))
const reviewYaml = await page.evaluate(() => (document.getElementById('wiz-yaml-preview')?.value || '').split('\n')[0])
check('wizard Review renders from the editor', reviewVisible, `first line: ${JSON.stringify(reviewYaml)}`)

console.log('PAGE ERRORS:', pageErrors.length ? pageErrors : 'NONE')
console.log('CONSOLE ERRORS:', consoleErrors.length ? consoleErrors : 'NONE')

const ok = failureCount() === 0 && pageErrors.length === 0 && consoleErrors.length === 0
console.log(ok ? 'ALL CHECKS PASSED' : 'CHECKS FAILED')
await browser.close()
process.exit(ok ? 0 : 1)