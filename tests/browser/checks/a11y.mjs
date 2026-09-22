// A11y smoke (L6): muted-text/badge contrast tokens in both themes, the
// health card is a real <button> (focus reveals, click pins, Esc closes),
// and the editor gutter still renders after a theme switch (L5 regression).
import { chromium } from '../lib/playwright.mjs'
import { installFixtures } from '../lib/stub.mjs'
import { check, failureCount } from '../lib/check.mjs'

const BASE = process.env.TRAM_BROWSER_BASE || 'http://127.0.0.1:8899'

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })
const page = await browser.newPage()
const pageErrors = []
const consoleErrors = []
page.on('pageerror', (e) => pageErrors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()) })

await installFixtures(page)

await page.goto(`${BASE}/#dashboard`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForTimeout(600)

const readCssVar = (name) =>
  page.evaluate((n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim(), name)

const darkMuted = await readCssVar('--fg-muted')
check('dark theme --fg-muted meets contrast (#8b949e)', darkMuted === '#8b949e', `got ${JSON.stringify(darkMuted)}`)

// Health card: it is a <button>; focus reveals; click pins; Esc closes.
const healthIsButton = await page.evaluate(() => document.getElementById('health-btn')?.tagName)
check('health card is a <button>', healthIsButton === 'BUTTON', `got ${JSON.stringify(healthIsButton)}`)
await page.focus('#health-btn')
await page.waitForTimeout(150)
const focusOpens = await page.evaluate(() => getComputedStyle(document.getElementById('health-card')).display !== 'none')
check('focus reveals the health card', focusOpens)
await page.click('#health-btn')
await page.waitForTimeout(150)
const clickPins = await page.evaluate(() => document.getElementById('health-btn').classList.contains('open')
  && document.getElementById('health-btn').getAttribute('aria-expanded') === 'true')
check('click pins the health card (open + aria-expanded)', clickPins)
await page.keyboard.press('Escape')
await page.waitForTimeout(150)
const escCloses = await page.evaluate(() => !document.getElementById('health-btn').classList.contains('open')
  && document.getElementById('health-btn').getAttribute('aria-expanded') === 'false')
check('Esc unpins the health card', escCloses)

// Switch to light theme and re-read tokens.
await page.click('#theme-btn')
await page.waitForTimeout(300)
const lightMuted = await readCssVar('--fg-muted')
const lightBadgeCyan = await readCssVar('--badge-cyan-fg')
const lightBadgeOff = await readCssVar('--badge-off-fg')
check('light theme --fg-muted meets contrast (#57606a)', lightMuted === '#57606a', `got ${JSON.stringify(lightMuted)}`)
check('light theme --badge-cyan-fg (#0a6a8c)', lightBadgeCyan === '#0a6a8c', `got ${JSON.stringify(lightBadgeCyan)}`)
check('light theme --badge-off-fg (#636c76)', lightBadgeOff === '#636c76', `got ${JSON.stringify(lightBadgeOff)}`)

// Editor still renders with the gutter after token changes (L5 regression).
await page.evaluate(() => { window.location.hash = '#editor' })
await page.waitForTimeout(800)
const editorOk = await page.evaluate(() => ({
  gutter: document.querySelectorAll('#editor-gutter .editor-gutter-line').length,
  tokens: document.querySelectorAll('#editor-highlight .yk').length,
  gutterColor: getComputedStyle(document.getElementById('editor-gutter')).color,
}))
check('editor gutter renders in light theme', editorOk.gutter > 0 && editorOk.tokens > 0, JSON.stringify(editorOk))

console.log('PAGE ERRORS:', pageErrors.length ? pageErrors : 'NONE')
console.log('CONSOLE ERRORS:', consoleErrors.length ? consoleErrors : 'NONE')

const ok = failureCount() === 0 && pageErrors.length === 0 && consoleErrors.length === 0
console.log(ok ? 'ALL CHECKS PASSED' : 'CHECKS FAILED')
await browser.close()
process.exit(ok ? 0 : 1)