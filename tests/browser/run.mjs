#!/usr/bin/env node
// TRAM browser smoke runner — the release gate's "UI browser smoke" check.
//
// What it does:
//   1. Verifies node >= 20 (Playwright 1.63 rejects node 18 — fail loudly
//      with a remediation message rather than a confusing stack trace).
//   2. Starts a dependency-free static file server over tram/ui/dist.
//   3. Runs each check (tests/browser/checks/*.mjs) as its own process with
//      TRAM_BROWSER_BASE pointing at the server; every check stubs the API
//      at the network level from tests/browser/fixtures (page.route), so no
//      backend is involved.
//   4. Prints per-check PASS/FAIL plus a summary and exits non-zero if any
//      check failed or exited non-zero (checks fail loudly on console/page
//      errors, failed requests, or any assertion).
//
// Run:   node tests/browser/run.mjs            (needs the UI built first)
//        (cd tram/ui && npm run test:browser)  (same thing via npm)
//
// Node gate: run with node >= 20. If your default node is older, point
// TRAM_BROWSER_NODE at a node 20+ binary (the release gate honors it too).
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { startStaticServer } from './lib/server.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = join(here, '..', '..')
const DIST = join(REPO_ROOT, 'tram', 'ui', 'dist')
const CHECK_NAMES = ['boot', 'wizard', 'wizard-ai', 'editor', 'a11y', 'yaml-quote', 'modal-nav']

// ── Node gate ────────────────────────────────────────────────────────────────
const nodeMajor = Number(process.versions.node.split('.')[0])
if (nodeMajor < 20) {
  console.error(
    `[browser-smoke] node ${process.version} is too old — this suite needs node >= 20 (Playwright 1.63's floor).\n` +
      `  Install Node 20+ or set TRAM_BROWSER_NODE=/path/to/node20 and re-run with that binary.`
  )
  process.exit(2)
}

// ── Preflight: fresh dist ────────────────────────────────────────────────────
if (!existsSync(join(DIST, 'index.html'))) {
  console.error(
    `[browser-smoke] no UI build found at ${DIST}.\n` +
      `  Run the UI build first: (cd tram/ui && npm ci && npm run build)`
  )
  process.exit(2)
}

const server = await startStaticServer({ root: DIST, port: 0 })
console.log(`[browser-smoke] static server: ${server.base} (serving ${DIST})`)

// ── Run checks ───────────────────────────────────────────────────────────────
const results = []
let failures = 0
for (const name of CHECK_NAMES) {
  const t0 = Date.now()
  const child = spawn(process.execPath, [join(here, 'checks', `${name}.mjs`)], {
    env: { ...process.env, TRAM_BROWSER_BASE: server.base },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.stdout.on('data', (d) => process.stdout.write(d))
  child.stderr.on('data', (d) => process.stderr.write(d))
  const code = await new Promise((resolve) => child.on('close', resolve))
  const ok = code === 0
  const ms = Date.now() - t0
  results.push({ name, ok, ms })
  if (!ok) failures += 1
  console.log(`\n[${name}] ${ok ? 'PASS' : 'FAIL'} (${ms}ms, exit ${code})\n`)
}

await server.close()

// ── Summary ──────────────────────────────────────────────────────────────────
console.log('================ browser smoke summary ================')
for (const r of results) console.log(`  [${r.ok ? 'PASS' : 'FAIL'}] ${r.name} (${r.ms}ms)`)
console.log('--------------------------------------------------------')
if (failures > 0) {
  console.log(`BROWSER SMOKE RED — ${failures} check(s) failed. See the per-check output above.`)
  process.exit(1)
}
console.log('BROWSER SMOKE GREEN — all checks passed.')
process.exit(0)