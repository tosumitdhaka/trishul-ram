// Resolves the playwright module from tram/ui/node_modules (the suite lives
// at tests/browser but the dependency is declared with the UI, so it is
// installed alongside the rest of the UI deps by `npm ci` in tram/ui), and
// enforces node >= 20 up front — playwright 1.63 refuses node 18 with a
// confusing error, so we fail loudly with a remediation message instead.
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const uiPkgJson = join(here, '..', '..', '..', 'tram', 'ui', 'package.json')

const major = Number(process.versions.node.split('.')[0])
if (major < 20) {
  console.error(
    `[browser-smoke] node ${process.version} is too old — Playwright 1.63 requires node >= 20.\n` +
      `  Install Node 20+ or set TRAM_BROWSER_NODE=/path/to/node20 and re-run ` +
      `(the gate and run.mjs honor it; see tests/browser/README.md).`
  )
  process.exit(2)
}

// createRequire rooted at tram/ui/package.json → resolution starts in
// tram/ui/node_modules, so `require('playwright')` finds the pinned dep.
const requireFromUi = createRequire(uiPkgJson)

export const { chromium } = requireFromUi('playwright')