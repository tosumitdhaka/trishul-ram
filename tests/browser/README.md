# Browser smoke suite (Playwright)

End-to-end browser checks against the **built** UI (`tram/ui/dist`) with the
API stubbed at the network level (`page.route`) from checked-in fixtures —
no backend, no cluster, fully deterministic. This is the release gate's
"UI browser smoke (Playwright)" check (check 8, `scripts/release-gate.sh`)
and the `browser-smoke` CI job (`.github/workflows/ci.yml`).

## What it covers

| Check | File | Covers |
|---|---|---|
| boot | `checks/boot.mjs` | Every main page route boots without console/page errors, failed requests, or a frozen shell; the shell renders the released version (the v1.4.3 "missing import crashed the SPA" class) |
| wizard | `checks/wizard.mjs` | Schema-driven wizard: step flow, required/secret/disclosure fields, inline validation, stale-`schema_version` guard + reload, template pre-seed, editor hand-off |
| wizard-ai | `checks/wizard-ai.mjs` | Full AI-assist path: generate → Review (no step-validation toasts, info-toned success toast) → Continue In Editor hands the AI YAML over intact → Save lands on the created pipeline's detail page with wired buttons (the three v1.4.4 operator bugs) |
| editor | `checks/editor.mjs` | Gutter, typing/line sync, tokenizer classes, tab, transparent overlay, scroll sync, dry-run error anchoring, draft guard, wizard Review regression |
| a11y | `checks/a11y.mjs` | Contrast tokens in both themes, health-card `<button>` semantics (focus/click/Esc), editor gutter after theme switch |
| yaml-quote | `checks/yaml-quote.mjs` | Wizard review-YAML quoting of numeric/boolean-looking strings, the 60s schema-poll page-leave guard, legacy `#wizard` redirect |

Every check also **fails loudly** on any `pageerror`, console error, or
failed request — not just on assertion failures.

## Running

Requires **node >= 20** (Playwright 1.63's floor — node 18 fails with a
clear message). If your default node is older, point `TRAM_BROWSER_NODE` at
a node 20+ binary; both `run.mjs` and the release gate honor it:

```bash
cd tram/ui && npm ci && npm run build          # fresh dist first
node ../tests/browser/run.mjs                  # or: npm run test:browser
```

Or, from the repo root, with the ad-hoc node 22 used to develop this:

```bash
TRAM_BROWSER_NODE=/path/to/node20 node tests/browser/run.mjs
```

Run a single check standalone (needs a server; `TRAM_BROWSER_BASE` defaults
to `http://127.0.0.1:8899`):

```bash
node tests/browser/checks/wizard.mjs
```

The runner exits non-zero if any check fails. Each check exits non-zero on
any assertion failure or browser-side error.

## Playwright installation

`playwright` is pinned exactly (`1.63.0`) as a devDependency of `tram/ui`.
The pin is intentional: the browser revision bundled by `playwright-core`
is what `npx playwright install chromium` downloads, so a `^` bump could
silently require a fresh browser download on every dev machine and in CI.
`npm ci` in `tram/ui` installs it; CI additionally runs
`npx playwright install --with-deps chromium`.

## Fixtures

API shapes live in `fixtures/` (see `fixtures/README.md` for the capture
date, the regeneration commands, and the deliberate trims). To regenerate
after an API contract change: capture the live cluster endpoints with curl,
then re-trim to the deterministic subset the checks drive.