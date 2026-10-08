// V18-09 state surfaces (v1.8.0): the browser-visible half of the
// reliability contracts —
//   1. `partial` runs render as their own status (never folded into
//      success/error), `aborted` is distinct from `failed`.
//   2. A stopping pipeline row reads as in-transition, not stuck.
//   3. The lifecycle operations audit trail renders on the detail page,
//      and a manual trigger surfaces its 202 receipt (operation_id) with a
//      link to that view.
//   4. Worker admission state (admitting/draining) shows on the cluster
//      view, with the drain runbook and the `drained` safe-to-restart gate.
//
// Static server + fixture stubs, no backend.
import { chromium } from '../lib/playwright.mjs'
import { installFixtures, json } from '../lib/stub.mjs'
import { check, failureCount } from '../lib/check.mjs'

const BASE = process.env.TRAM_BROWSER_BASE || 'http://127.0.0.1:8899'

const PARTIAL_RUN = '5d1c9b34-2f8a-4e07-9c21-8b4f0a6d3e11'
const ABORTED_RUN = '0ea47f61-93c2-4b55-8d0e-6f2b7c9a1d84'
const PIPELINE = 'sftp-pm-to-kafka'
const MANUAL_PIPELINE = 'manual-report-gen'
const TRIGGER_OP_ID = 'op-3f8a91c2d4e6'

// Extra pipeline rows for this check only — the shared pipelines fixture is
// pinned to two rows by wizard-ai, so the stopping/manual rows are layered on
// per-route here instead of growing the shared capture.
const STOPPING_PIPELINE = {
  name: 'pm-legacy-export',
  description: 'Nightly PM file export to the legacy SFTP drop',
  enabled: true,
  status: 'stopping',
  schedule_type: 'interval',
  interval_seconds: 3600,
  cron_expr: null,
  registered_at: '2026-09-22T06:16:07.310000+00:00',
  last_run: '2026-09-22T05:00:00.000000+00:00',
  last_run_status: 'success',
  source: { type: 'sftp' },
  sinks: [{ type: 'sftp' }],
  queued_run: null,
}
const MANUAL_PIPELINE_OBJ = {
  name: MANUAL_PIPELINE,
  description: 'On-demand report generation',
  enabled: true,
  status: 'stopped',
  schedule_type: 'manual',
  interval_seconds: null,
  cron_expr: null,
  registered_at: '2026-09-22T06:16:07.640000+00:00',
  last_run: '2026-09-21T18:12:00.000000+00:00',
  last_run_status: 'success',
  source: { type: 'file' },
  sinks: [{ type: 'sftp' }],
  queued_run: null,
}

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

// The manual-run trigger returns the v1.8.0 202 receipt: run_id + operation_id.
// Pointing run_id at the fixture's partial run makes the run monitor resolve
// to a terminal partial outcome — which pins the partial outcome toast too.
await installFixtures(page, {
  onRoute: async ({ route, pathname, fixtures }) => {
    if (pathname === '/api/pipelines' && route.request().method() === 'GET') {
      await route.fulfill(json([...fixtures.pipelines, STOPPING_PIPELINE, MANUAL_PIPELINE_OBJ]))
      return true
    }
    if (pathname === `/api/pipelines/${MANUAL_PIPELINE}`) {
      await route.fulfill(json(MANUAL_PIPELINE_OBJ))
      return true
    }
    if (pathname === `/api/pipelines/${MANUAL_PIPELINE}/placement`) {
      // Same synthetic single-slot placement the default stub serves for
      // fixture pipelines — keeps the detail page free of a spurious 404
      // (the check counts console errors).
      await route.fulfill(json({
        pipeline_name: MANUAL_PIPELINE,
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
    if (pathname === `/api/pipelines/${MANUAL_PIPELINE}/run`) {
      await route.fulfill(json({
        name: MANUAL_PIPELINE,
        status: 'triggered',
        run_id: PARTIAL_RUN,
        operation_id: TRIGGER_OP_ID,
      }, 202))
      return true
    }
    return false
  },
})

const errorsSoFar = () =>
  pageErrors.length + consoleErrors.length +
  failedRequests.filter((f) => !/net::ERR_ABORTED/.test(f.failure)).length

// ── 1. Run History: partial and aborted are their own statuses ───────────────
await page.goto(`${BASE}/#runs`, { waitUntil: 'networkidle', timeout: 15000 })
await page.waitForFunction(
  () => document.querySelectorAll('#runs-body tr[data-run-id]').length >= 4,
  { timeout: 10000 },
)
const partialBadge = await page.evaluate((runId) => {
  const badge = document.querySelector(`#runs-body tr[data-run-id="${runId}"] .tram-badge`)
  return { text: badge?.textContent.trim(), cls: badge?.className }
}, PARTIAL_RUN)
check('partial run row renders its own "partial" status (not error/success)',
  partialBadge.text === 'partial' && partialBadge.cls.includes('badge-partial')
    && !partialBadge.cls.includes('badge-failed') && !partialBadge.cls.includes('badge-success'),
  JSON.stringify(partialBadge))
check('partial status filter option exists', await page.evaluate(
  () => Boolean(document.querySelector('#runs-status option[value="partial"]'))))
const abortedBadge = await page.evaluate((runId) => {
  const badge = document.querySelector(`#runs-body tr[data-run-id="${runId}"] .tram-badge`)
  return { text: badge?.textContent.trim(), cls: badge?.className }
}, ABORTED_RUN)
check('aborted run row is distinct from failed (own badge, not failed-red)',
  abortedBadge.text === 'aborted' && abortedBadge.cls.includes('badge-aborted')
    && !abortedBadge.cls.includes('badge-failed'),
  JSON.stringify(abortedBadge))

// ── 2. Run detail: the partial copy and aborted block ────────────────────────
await page.evaluate((runId) => { window.location.hash = `#runs/${runId}` }, PARTIAL_RUN)
await page.waitForFunction(() => document.getElementById('run-detail-content')?.textContent.includes('Completed with losses'), { timeout: 10000 })
check('partial run detail leads with "Completed with losses (partial)"', await page.evaluate(
  () => document.getElementById('run-detail-content')?.textContent.includes('Completed with losses (partial)')))
await page.evaluate((runId) => { window.location.hash = `#runs/${runId}` }, ABORTED_RUN)
await page.waitForFunction(() => document.getElementById('run-detail-content')?.textContent.includes('Run aborted'), { timeout: 10000 })
check('aborted run detail explains "Run aborted" (not "Pipeline failure")', await page.evaluate(() => {
  const content = document.getElementById('run-detail-content')?.textContent || ''
  return content.includes('Run aborted') && !content.includes('Pipeline failure')
}))

// ── 3. Cluster: worker admission + drain runbook ─────────────────────────────
await page.evaluate(() => { window.location.hash = '#cluster' })
await page.waitForFunction(() => document.querySelectorAll('#cluster-nodes tbody tr').length > 0, { timeout: 10000 })
check('Admission column appears when workers report admission_state', await page.evaluate(
  () => Array.from(document.querySelectorAll('#cluster-nodes thead th')).some(th => th.textContent.trim() === 'Admission')))
const admissionCells = await page.evaluate(() =>
  Array.from(document.querySelectorAll('#cluster-nodes tbody tr td .tram-badge')).map(b => b.textContent.trim()))
check('admitting and draining workers both render badges', await page.evaluate(() => {
  const badges = Array.from(document.querySelectorAll('#cluster-nodes tbody tr td .tram-badge')).map(b => b.textContent.trim())
  return badges.includes('admitting') && badges.filter(t => t === 'draining').length === 2
}), JSON.stringify(admissionCells))
check('drained worker shows the safe-to-restart gate', await page.evaluate(
  () => document.querySelector('#cluster-nodes')?.textContent.includes('drained — safe to restart')))
// Expand the still-draining worker (runs in flight) and read the runbook.
await page.click('[data-worker-key="http://trishul-ram-worker-1.trishul-ram-worker.trishul-ram.svc.cluster.local:8766"]')
await page.waitForFunction(() => document.querySelector('#cluster-nodes')?.textContent.includes('Drain status (restart runbook)'), { timeout: 10000 })
check('draining worker expanded shows the drain runbook with "not safe to restart yet"', await page.evaluate(() => {
  const text = document.querySelector('#cluster-nodes')?.textContent || ''
  return text.includes('Drain status (restart runbook)') && text.includes('Not safe to restart yet')
}))

// ── 4. Pipeline detail: lifecycle operations audit trail ─────────────────────
await page.evaluate((name) => { window.location.hash = `#detail/${name}?tab=operations` }, PIPELINE)
// Real rows carry .type-pill kind chips — the static "Loading…" row the
// panel ships with would satisfy a plain tr-count wait.
await page.waitForFunction(() => document.querySelectorAll('#detail-operations-body tr .type-pill').length > 0, { timeout: 10000 })
const ops = await page.evaluate(() => ({
  rows: document.querySelectorAll('#detail-operations-body tr').length,
  kinds: Array.from(document.querySelectorAll('#detail-operations-body .type-pill')).map(el => el.textContent.trim()),
  states: Array.from(document.querySelectorAll('#detail-operations-body .tram-badge')).map(el => el.textContent.trim()),
}))
check('operations tab lists rows', ops.rows === 5, JSON.stringify(ops))
check('operations kinds render humanized (force release style)', ops.kinds.includes('boot adopt') && ops.kinds.includes('trigger'), JSON.stringify(ops.kinds))
check('operation states render pending/complete/failed badges',
  ops.states.includes('pending') && ops.states.includes('complete') && ops.states.includes('failed'), JSON.stringify(ops.states))

// ── 5. Stopping pipeline row is visibly in transition ────────────────────────
await page.evaluate(() => { window.location.hash = '#pipelines' })
await page.waitForFunction(() => document.querySelectorAll('#pl-body tr[data-pipeline-name]').length >= 4, { timeout: 10000 })
const stopping = await page.evaluate(() => {
  const row = document.querySelector('#pl-body tr[data-pipeline-name="pm-legacy-export"]')
  // Column 5 is the status cell — the schedule badge in column 4 is also a
  // .tram-badge, so query by position.
  const badge = row?.querySelector('td:nth-child(5) .tram-badge')
  const button = row?.querySelector('td:last-child button')
  return {
    badge: badge?.textContent.trim(),
    hasStoppingClass: badge?.classList.contains('stopping'),
    buttonDisabled: button?.disabled,
  }
})
check('stopping pipeline renders pulsing "stopping" badge with a disabled transition button',
  stopping.badge === 'stopping' && stopping.hasStoppingClass && stopping.buttonDisabled === true,
  JSON.stringify(stopping))

// ── 6. Manual trigger: 202 receipt surfaces and links to operations ─────────
await page.evaluate((name) => { window.location.hash = `#detail/${name}` }, MANUAL_PIPELINE)
await page.waitForFunction(() => document.getElementById('detail-run-btn')?.textContent.includes('Run Now'), { timeout: 10000 })
await page.click('#detail-run-btn')
await page.waitForFunction(() => !document.getElementById('detail-receipt-info')?.classList.contains('d-none'), { timeout: 10000 })
const receipt = await page.evaluate(() => {
  const el = document.getElementById('detail-receipt-info')
  const link = el?.querySelector('.detail-receipt-link')
  return {
    text: el?.textContent.trim() || '',
    href: link?.getAttribute('href') || '',
  }
})
check('trigger receipt shows the operation_id', receipt.text.includes('receipt') && receipt.text.includes(TRIGGER_OP_ID.slice(0, 8)), JSON.stringify(receipt))
check('trigger receipt links to the operations view', receipt.href === `#detail/${MANUAL_PIPELINE}?tab=operations`, JSON.stringify(receipt))
// Follow the receipt link — the operations tab loads with its audit rows.
await page.click('#detail-receipt-info .detail-receipt-link')
await page.waitForFunction(() =>
  !document.getElementById('tab-panel-operations')?.classList.contains('d-none')
  && document.querySelectorAll('#detail-operations-body tr .type-pill').length > 0,
  { timeout: 10000 },
)
check('receipt link opens the operations tab with rows', await page.evaluate(() =>
  !document.getElementById('tab-panel-operations')?.classList.contains('d-none')
  && document.querySelectorAll('#detail-operations-body tr .type-pill').length > 0))
// The run monitor resolves the partial outcome — a warning toast, never an error.
const partialToast = await page.waitForFunction(
  () => Array.from(document.querySelectorAll('.tram-toast-msg')).some(el => el.textContent.includes('completed with losses')),
  { timeout: 12000 },
).then(() => true).catch(() => false)
check('partial outcome toast fires ("completed with losses (partial)")', partialToast)

check('no page/console/request errors across all surfaces', errorsSoFar() === 0,
  JSON.stringify({ pageErrors, consoleErrors, failedRequests }))

// ── Final verdict ──────────────────────────────────────────────────────────────
const realFailures = failedRequests.filter((f) => !/net::ERR_ABORTED/.test(f.failure))
console.log('PAGE ERRORS:', pageErrors.length ? pageErrors : 'NONE')
console.log('CONSOLE ERRORS:', consoleErrors.length ? consoleErrors : 'NONE')
console.log('FAILED REQUESTS:', realFailures.length ? JSON.stringify(realFailures) : 'NONE')

const ok = failureCount() === 0 && pageErrors.length === 0 && consoleErrors.length === 0 && realFailures.length === 0
console.log(ok ? 'ALL CHECKS PASSED' : 'CHECKS FAILED')
await browser.close()
process.exit(ok ? 0 : 1)
