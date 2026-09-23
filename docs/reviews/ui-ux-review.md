# TRAM Control-Plane UI — UX/Design Review

> **Status (2026-09-23): the 54 findings shipped v1.4.2–v1.4.3** — QW1–QW11 and L1 (route
> parameters) in v1.4.2, L2–L6 (creation wizard, page shell, run detail, editor, a11y) in
> v1.4.3 (see `docs/changelog.md` `[1.4.2]` / `[1.4.3]`). This review is the historical record.

**Date:** 2026-09-17
**Scope reviewed:** `tram/ui/` — vanilla-JS SPA (Vite build), Bootstrap 5.3 + Bootstrap Icons, token layer in `src/style.css` (1,668 lines), hash router, no framework. Entry chain: `index.html` → `src/main.js` → `src/router.js` → `src/api.js`. All 10 routed pages read in full (`dashboard`, `pipelines`, `detail`, `editor`, `runs`, `schemas`, `mibs`, `cluster`, `plugins`, `settings`) plus `login`, the dead `wizard`, and shared modules (`utils.js`, `health.js`, `run_monitor.js`, `yaml_diff.js`, `runs_table.js`, `template_helpers.js`), `package.json`, `vite.config.mjs`. Backend contract verified against `tram/api/routers/` (`pipelines.py`, `runs.py`, `stats.py`, `health.py`, `mibs.py`, `schemas.py`, `templates.py`, `connectors.py`, `ai.py`, `auth.py`) and `tram/pipeline/manager.py` response shapes. Audience: telecom operators / NOC engineers.

**Prior review:** a 2026-09-15 review exists at this path; every one of its findings was re-derived from today's code. A reconciliation table is at the end. This document replaces the old review; git history preserves it.

---

## Methodology

- Read every UI source file end-to-end; no runtime testing, no builds (review-only constraint). Anything that cannot be confirmed from code alone is labeled "needs runtime verification".
- Cross-checked every frontend API call in `src/api.js` against the FastAPI routers: path, method, query parameters, request body shape, and response shape (including `PipelineState.to_dict()` / `to_detail_dict()` in `tram/pipeline/manager.py:40-72` and the stats payload in `tram/api/routers/stats.py:149-177`).
- Each finding carries a type tag — [BUG] (verified against code), [GAP], [BOILERPLATE], [UX], [A11Y], [ENHANCEMENT] — and a severity (high / medium / low), with `file:line` evidence.

---

## Executive summary

The UI is a disciplined, GitHub-dark-styled control plane with a genuinely good failure-diagnosis design in the run-issues table and a thoughtful async run-monitor. The frontend↔backend contract is in unusually good shape — every endpoint the UI calls exists with matching shapes, and response edge cases (queued runs, placement 404, dry-run variants) are handled defensively.

However, none of the prior review's architectural findings have been addressed: routes still carry no parameters (all state travels on `window` globals), the primary creation path is still a raw YAML textarea with the wizard's 746-line corpse still in the tree, pages still hang on "Loading…" forever when the API fails, and toasts still stack on top of each other with no dismiss or screen-reader announcement. The v1.4.0 work (commit `1bc7f97`) went into dashboard/cluster/plugins/MIB-contract polish, not into the structural UX debt. One new regression-class bug was found: the dashboard's "+ New" button can silently open the editor in *edit* mode for a previously viewed pipeline because editor state is never reset there.

Counts: 9 high, 16 medium, 29 low across 54 findings (per-section above; the summary table at the end is the exact rollup).

---

## 1. Overall UX — information architecture, navigation, task flows

### 1.1 No URL parameters — deep links, refresh, Back, and sharing are all broken

**[UX — high]** Routes are bare page names. `router.js:102-105` builds `const targetHash = '#${page}'`, and all cross-page state travels on `window` globals: `window._detailPipeline` (`detail.js:20`), `window._editorPipeline` / `window._editorYaml` / `window._editorReturn` (`editor.js:52`, `pipelines.js:196-210`), `window._runsFilters` (`detail.js:182-184`, consumed at `runs.js:47-49`), and cluster's placement handoff (`cluster.js:52-53`). Consequences, all code-verifiable:

- Refreshing on `#detail` redirects to the pipelines list (`detail.js:21` — no name → `navigate('pipelines')`).
- A NOC engineer cannot bookmark, share, or paste "the failing pipeline" or "run 4f2a" into an incident channel.
- Browser Back from detail → pipelines silently discards context.

For a NOC tool this remains the single most damaging architectural decision in the UI.

**[UX — low]** Run detail does not exist as a route — a run's full story (failure reason, grouped skip reasons, DLQ) lives in an expandable table row (`runs_table.js:26, 45-113`), so a specific failed run cannot be linked. This is a subset of the routing problem.

**[BUG — low]** Detail's active tab is a module-level variable that persists across pipelines: `_activeTab` (`detail.js:14`) survives page re-inits, so viewing pipeline A's Versions tab, going back, then opening pipeline B lands B on the Versions tab. Stale state leak caused by the same no-URL-state design.

### 1.2 New regression-class bug: dashboard "+ New" can open the editor in edit mode of a stale pipeline

**[BUG — high]** `dashboard.js:384` wires `dash-new-btn` to a bare `navigate('editor')` — it does not reset `window._editorPipeline` / `window._editorYaml` / `window._editorReturn` the way the pipelines page correctly does (`pipelines.js:196-199`). Those globals are only cleared by `_leaveEditor()` (`editor.js:32-47`), i.e., via the editor's own Cancel/Save. Reproduction path from code: click Edit on pipeline X → leave the editor via the sidebar or browser Back (globals remain set: `window._editorPipeline = 'X'` from `pipelines.js:207-210`) → go to Dashboard → click "+ New" → `editor.js:52-56` reads the stale `window._editorPipeline`, enters edit mode, loads X's YAML, titles the page "X", and the Save button performs `api.pipelines.update('X', …)` (`editor.js:309-311`). An operator who believes they are creating a new pipeline silently overwrites X.

### 1.3 Creation path is still a raw YAML textarea; the wizard is disabled dead weight

**[UX — high]** The primary creation path for non-developer operators is a plain `<textarea>` (`editor.html:40-41`, reached from `pipelines.html:32-34`, `dashboard.js:384`, and template deploy at `pipelines.js:369-379`). The wizard remains permanently disabled ("Wizard temporarily disabled while schema-driven flow is refined", `pipelines.html:29-31`) and `router.js:61-63` silently rewrites `#wizard` → `#pipelines`, while `wizard.js` (746 lines) and `wizard.html` (185 lines) are not imported by the router at all (`router.js:2-11`) — dead code. See §6 for the dead-code inventory.

**[GAP — low]** The editor's "Available Plugins" reference pills are hardcoded static strings (`editor.html:93, 99, 105, 111`) — they will silently drift from the live registry that the Plugins page renders from `/api/plugins` (`plugins.js:52`). The correct data source is one fetch away.

### 1.4 Discoverability and confirmation of consequential actions

**[UX — medium]** Global "Reload" hot-reloads all pipelines from disk with no confirmation and no explanation of blast radius (`pipelines.js:178-193`; backend semantics at `tram/api/routers/pipelines.py:419-440` — re-scans `pipeline_dir`, re-seeds DB, re-boots). The reload-vs-rollback-vs-restart distinction is never taught anywhere; it exists only as button tooltips.

**[UX — medium]** "Stop" — the action that halts a production stream — has no confirmation anywhere: pipelines table (`pipelines.js:439`), dashboard (`dashboard.js:193`), detail page (`detail.js:209-211`). Meanwhile deletes and rollback use native `confirm()`: pipeline delete (`pipelines.js:250`), MIB delete (`mibs.js:138`), schema delete (`schemas.js:80`), rollback (`detail.js:753`), alert delete (`detail.js:765`). Two problems in one: unguarded destructive stop, and inconsistent confirmation idiom (native `confirm()` vs the app's styled Bootstrap modals).

**[UX — medium]** Import-replace no longer silently overwrites — a conflict modal now offers "Replace current YAML" or "Import with new name" (`pipelines.html:38-67`, `pipelines.js:271-296`) — but Replace still shows no diff of what will change (`pipelines.js:96-107`), despite a full side-by-side diff component (`yaml_diff.js:79-124`) already being used for version compare two tabs away.

**[UX — medium]** The editor has no unsaved-changes guard. Cancel (`editor.js:116`) and any hash navigation instantly destroy the textarea; there is no `beforeunload` handler anywhere in `src/` (grep-verified), no draft persistence. Pasting a 200-line pipeline and grazing Back loses everything.

**[UX — low]** `AI Modify` overwrites the textarea with the AI result and offers no revert path (`editor.js:198-205`): the auto-opened diff shows saved-vs-current, but the pre-AI draft is gone unless the operator hand-copied it.

### 1.5 Backend capabilities the UI never surfaces

**[GAP — medium]** The run trigger supports a `flush` query parameter (stateful transforms emit open windows as partials and clear state — `tram/api/routers/pipelines.py:370-375`, documented as F.1 §5). `grep flush tram/ui/src` returns nothing; the UI never offers a "Run with flush" option anywhere. For windowed-aggregate pipelines this is an operator-relevant control.

**[GAP — medium]** `/api/daemon/status` returns scheduler state including next scheduled runs (`tram/api/routers/runs.py:125-129`). `api.js:177-179` defines the call but no page invokes it — "when does this cron pipeline run next?" is unanswerable from the UI.

**[GAP — low]** The pipelines status filter offers running/scheduled/stopped/queued/error (`pipelines.html:5`) but the backend also produces `degraded` and `reconciling` pipeline statuses (`tram/pipeline/controller.py:290, 1955`) — pipelines in those states cannot be filtered for.

**[UX — low]** Empty sidebar badge elements are never populated: `#nav-badge-pipelines`, `#nav-badge-schemas`, `#nav-badge-mibs` (`index.html:45, 54, 58`) have no corresponding code in any JS module (grep-verified). Dead UI.

---

## 2. Visual design — layout, hierarchy, consistency, responsiveness

**[UX — high]** The 12-column run tables sit in `.table-wrap` with `overflow: hidden` (`style.css:236`) and no `.table-responsive` wrapper: Run History (`runs.html:26-34`) and the detail Runs tab (`detail.html:81-89`). On narrow screens the trailing columns — Status, Issues, actions — are simply clipped. The Plugins and Cluster pages *do* wrap their tables in `.table-responsive` (`plugins.js:117`, `cluster.js:209`), so the correct pattern exists in the codebase and wasn't applied where the data matters most.

**[UX — medium]** The 248px sidebar never collapses (`style.css:100-106`) — no toggle, no off-canvas. A quarter of a tablet viewport is permanently chrome.

**[UX — medium]** Detail cards use `col-4` with no stacking breakpoint (`detail.html:46-51`), so on a phone the Source/Sinks/Schedule cards render at one-third width each. The same fixed `col-8`/`col-4` split is on MIBs (`mibs.html:2, 21`) and Schemas (`schemas.html:2, 21`). Notably the dashboard, editor, and settings pages *do* use responsive column classes (`dashboard.html:12`, `editor.html:2`, `settings.html:2`) — inconsistency, not ignorance.

**[UX — low]** The editor textarea is plain `Courier New` with no line numbers, no YAML syntax highlighting, and no error-line anchoring (`style.css:830-843`, `editor.html:40-41`). Dry-run issues can't be mapped to a line. The `yk/yv/yn/ys/yb` YAML color classes exist in CSS (`style.css:1062-1064`) but are never used by any JS — dead CSS.

**[BOILERPLATE — low]** Hardcoded accent hexes bypass the token layer in ~15 places: `#388bfd` / `rgba(56,139,253,…)` at `style.css:131, 215, 219, 221, 225, 1095, 1106-1107, 1271-1274, 1384, 1389, 1528`. A future re-theme requires archaeology. Invisible to users today.

**[UX — low]** Everything is locked to a 14px body / 13px table base (`style.css:50, 228`) with no density or zoom control — the NOC wall-display scenario is unsupported.

**[UX — low]** Dashboard per-pipeline column header says "Out/hr" (`dashboard.js:213` rewrites the thead every poll) but the cell shows last-hour *totals* (`stats.py:224-252` sums `records_out` over the window), not a rate. Mislabelled metric.

**[UX — low]** Brand version is hardcoded `v1.2.0` in `index.html:32` (package.json is 1.4.0) and only corrected after the first successful health poll (`health.js:47-48`) — stale/wrong until then.

**Done well:** ~40 CSS custom properties for both themes applied with real consistency (`style.css:4-46`); a coherent badge grammar (`.tram-badge` with animated pulse for running/queued, `style.css:173-204`); one `statusBadge` function governing status color semantics (`utils.js:57-74`); the detail hero with gradient tint (`style.css:509-514`); the two media-query blocks that do exist are thorough for the pages they cover (`style.css:1392-1509`).

---

## 3. Interaction quality — states, feedback, polling

### 3.1 Loading / error / empty states

**[UX — high]** API failure leaves pages stuck on "Loading…" forever. Every page's init catch fires a toast and returns without replacing the skeleton row: pipelines (`pipelines.js:26-33`), runs (`runs.js:9-16`), MIBs (`mibs.js:19-26`), schemas (`schemas.js:17-24`), detail (`detail.js:39-41`). The Plugins page has the correct pattern — dedicated error/empty `<template>`s (`plugins.html:21-31`) rendered into the body (`plugins.js:56, 98`) — and it is not reused. (The detail page's Runs tab also does it right, `detail.js:358-362`: inline "Could not load run history" + toast.)

### 3.2 Toasts — the app's entire feedback channel is structurally broken

**[UX — high]** `.tram-toast` is `position: fixed; right: 24px; bottom: 24px` (`style.css:1339-1342`) with no stacking container — concurrent toasts render at the identical position and overlap into an unreadable smear (`utils.js:148-158` appends each to `<body>`). There is no dismiss control, no `aria-live`, and a flat 4-second timeout (`utils.js:154`) regardless of message length.

**[UX — high]** Toast storm on daemon outage: the pipelines poll fires an error toast every cycle (`pipelines.js:16-19`), the dashboard does the same (`dashboard.js:61-64`), and each poll failure adds another overlapping toast. The health dot (`health.js:51-60`) already communicates offline state calmly; page polling should degrade to an inline banner, not repeat toasts.

**[UX — low]** Multi-line connector-test results on the detail page still go through `toast(lines.join('\n'), …)` (`detail.js:663-676`) — `textContent` doesn't honor `\n`, so the message wraps into mush. The *editor* already solved this with a proper results panel (`editor.js:233-258`); the detail page wasn't updated to match.

### 3.3 Long-running operations and polling

**[UX — medium]** The Run History page — the page an operator stares at during an incident — has no poll timer (`runs.js` registers none), while dashboard (`dashboard.js:36-44`), pipelines (`pipelines.js:15-19`), and cluster (`cluster.js:25-32`) all auto-refresh.

**[UX — medium]** CSV export silently truncates at 1,000 rows: the UI requests `limit=1000` (`runs.js:6, 62-69`) which is the backend's hard cap (`runs.py:46`, `le=1000`), with no warning when the filter matches more. The on-screen list is capped at 200 with no "showing latest 200" indicator (`runs.js:5`) and no pagination, even though the backend supports `offset` (`runs.py:47`).

**[UX — low]** Relative timestamps (`relTime`, `utils.js:3-11`) never re-render between polls on non-polling pages — "just now" sticks for minutes on Run History.

**[UX — low]** Row action buttons (start/stop/run/delete) are never disabled during in-flight operations (`pipelines.js:213-259`): a double-click double-fires the POST. No optimistic locking or concurrency guard anywhere for concurrent operators acting on the same pipeline.

**[GAP — medium]** No global 401 handling: sessions expire after 8h (documented at `settings.html:96`), after which every API call throws with `status: 401` (`api.js:77-92` has no interceptor) and each page just toasts the error — the operator is never re-authenticated or redirected to login until a full page reload.

**[UX — low]** The sparkline canvas doesn't re-render on window resize (only on poll, `dashboard.js:125-178`) — up to 10s of stretched pixels; needs runtime verification for visual severity. The tooltip is mouse-only and the canvas is not focusable, so the `blur` listener at `dashboard.js:469` can never fire (dead code).

**[BUG — low]** `health.js:34` falls back to `http://localhost:8765` when computing the displayed port, while `api.js:5` defaults to `window.location.origin` — when no base URL is saved, the sidebar shows `:8765` even if the UI is served from another origin (e.g. Vite dev on :5173).

**Done well:** the triggered-run monitor (`run_monitor.js:9-43`) polls the run, extends its timeout budget while the run sits `queued` (documented against GH #21), and delivers a success/failure toast with the run's error text — wired from all three surfaces (`pipelines.js:462-473`, `detail.js:775-790`, `dashboard.js:264-285`). `pipelineStartFeedback` (`utils.js:161-174`) distinguishes disabled-in-YAML / manual-schedule / already-running using the backend's own status vocabulary (`pipelines.py:312-328`). The cluster page deduplicates concurrent refreshes with an in-flight promise and degrades individual endpoint failures via `Promise.allSettled` (`cluster.js:65-91`).

---

## 4. Accessibility

**[A11Y — high]** Clickable table rows have no keyboard path: rows are `<tr>` with click listeners and no `tabindex`, `role`, or Enter/Space handling — pipelines (`pipelines.js:444` + row listener at `85-93`), dashboard (`dashboard.js:199` + `361-368`), cluster streams (`cluster.js:314`). Keyboard-only users cannot open pipeline detail at all; the Edit button is the only reachable workaround.

**[A11Y — high]** Toasts are appended to `<body>` with no `role="status"` / `aria-live` region (`utils.js:148-152`) — screen readers get zero announcement of the app's entire success/error feedback channel.

**[A11Y — medium]** The health card is CSS-hover-only (`.health-btn:hover .health-card { display: block }`, `style.css:165`): invisible to keyboard focus, unreliable on touch.

**[A11Y — medium]** Contrast of the secondary layer is below WCAG AA at the sizes used: `--fg-muted #6e7681` on `--bg-surface #161b22` computes to ≈4.0:1 and is used at 10-11px for labels, table headers, and hints (`style.css:13, 116-120, 277-281, 506`). The light theme is worse: `--fg-muted #8c959f` on `#ffffff` (`style.css:37`) computes to ≈2.9:1. (Computed estimates from the token values.)

**[A11Y — medium]** The 10-second poll re-renders replace `tbody.innerHTML` wholesale (`pipelines.js:435-459` via `renderTable`), destroying focus if a keyboard user is focused inside the table — focus silently drops to `<body>` mid-interaction.

**[A11Y — low]** Icon-only action buttons rely on `title`, with `aria-label` applied inconsistently: the dashboard's manual-run button has one (`dashboard.js:195`) but its Stop/Start/Download siblings (`dashboard.js:193, 196-197`) and all pipelines-table row buttons (`pipelines.js:439-456`) don't.

**[A11Y — low]** No `prefers-reduced-motion` handling for the badge pulse (`style.css:189`, applied to running/queued badges at `180, 184`) or the refresh-spinner `spin` animation (`style.css:1667-1668`).

**[A11Y — low]** Modals lack `aria-labelledby`/`aria-describedby` wiring to their titles (e.g. `pl-import-modal` at `pipelines.html:39-44`, alert modal at `detail.html:129-134`) — Bootstrap's focus trap works, but the dialog's purpose is not announced beyond the focus shift.

**Done well:** the login form is correctly labeled with `autocomplete` hints and a `role="alert"` error region (`login.html:16-26`); plugin/worker toggles carry both `title` and `aria-label` (`plugins.js:147-155`, `cluster.js:353-360`); the wizard status spans use `aria-live="polite"` — though that page is dead code (`wizard.html`).

---

## 5. Frontend code quality

**Contract verification (positive):** every endpoint the UI calls exists with matching shapes — pipelines list/detail fields (`manager.py:40-72` vs `pipelines.js:435-459`, `detail.js:111-124`), dry-run normalization (`api.js:66-75` vs `pipelines.py:33-56`), templates (`templates.py:59-68` vs `template_helpers.js`), stats (`stats.py:149-177` vs `dashboard.js:95-121`), queued-run merge (`runs.py:16-100` vs `runs_table.js` and `run_monitor.js`), placement 404 fallback (`detail.js:30`), deprecated `pause`/`resume` correctly *not* exposed (`pipelines.py:285-294`), and the dangerous `/api/daemon/stop` (`runs.py:132-144`) correctly left out of the UI. HTML escaping via `esc()` is applied systematically in every rendered template string across all pages (spot-verified in `pipelines.js`, `runs_table.js`, `detail.js`, `mibs.js`, `cluster.js`).

**[BOILERPLATE — medium]** No shared page-shell/data-fetch abstraction: five pages hand-roll their own init/load/render/toast-on-error/poll loop with subtly different cleanup guards — pipelines checks `pl-table` (`pipelines.js:17`), dashboard checks `dash-sparkline` (`dashboard.js:37`), cluster checks `cluster-streams` (`cluster.js:26`), runs and MIBs/schemas don't poll at all. The three polling loops also each re-implement spinner toggling for their refresh buttons.

**[BUG — low]** `_extractName` uses `/^\s*name:\s*(\S+)/m` (`pipelines.js:399-402`): the `m` flag plus `^\s*` matches the first *indented* `name:` key too, so a YAML whose top-level `name` follows some nested `name:` (or a file where a nested block appears first) is misidentified during import-conflict detection. Same flaw in `_patchName` (`pipelines.js:404-406`).

**[BUG — low]** `doTemplateDeploy` manually strips `.modal-backdrop`, `modal-open`, and inline body styles (`pipelines.js:370-373`) — a fragile workaround for navigating away with a Bootstrap modal open, instead of `modal.hide()`.

**[BUG — low]** `relTime`/`fmtDur` produce negative output ("-5s ago") when clock skew makes a timestamp future-dated (`utils.js:3-19` has no clamp).

**[BOILERPLATE — low]** Duplicated helpers: `fmtInterval` exists in `utils.js:91-96` and again in `detail.js:688-693`; `fmtSize` in `mibs.js:269-273` and `schemas.js:126-130`; `wireDropZone`/`updateHint` are near-identical implementations in `mibs.js:168-186` and `schemas.js:100-118`.

**[GAP — low]** No i18n layer: every user-facing string is inline English across all modules (representative: `pipelines.js:250`, `runs_table.js:13`, `mibs.js:81`).

**[UX — low]** Cross-page communication relies on bare globals: `window.navigate` (`main.js:14`) and the `window._*` family — pages call `navigate(…)` without importing it (e.g. `pipelines.js:199`, `detail.js:21`), which works only because of the global assignment. This is the root cause of finding 1.1 and the 1.2 bug.

**Type safety:** there are no types, JSDoc, or schema validation of API responses anywhere in `src/` — response-shape drift would surface as `undefined` rendering (`—` fallbacks partially mask it). No frontend tests exist (no test script in `package.json:5-9`).

---

## 6. Boilerplate, redundancy, dead code, dependencies

**[BOILERPLATE — medium]** Dead-code inventory (all grep-verified as unreferenced):

| Item | Evidence |
|---|---|
| `wizard.js` — 746 lines, not imported by router | `router.js:2-11` imports 10 pages; wizard absent |
| `wizard.html` — 185 lines | same; only consumed by `router.js` never |
| Wizard CSS block (~145 lines) | `style.css:1521-1665` (`.wizard-*`, `.wiz-*`) |
| `api.configSchema.get()` | `api.js:206-208`; sole caller is dead `wizard.js:50` |
| `api.daemon.status()` | `api.js:177-179`; zero callers in any page |
| `plugins-loading-template` | `plugins.html:25-27`; never referenced by `plugins.js` |
| `statusBadge` `'partial'` mapping | `utils.js:70`; backend produces no `partial` status (statuses: running/scheduled/stopped/queued/degraded/reconciling/error — `controller.py:290, 1955`; run statuses success/failed/aborted) |
| YAML color classes `.yk/.yv/.yn/...` | `style.css:1062-1064`; never emitted by any JS |
| Sidebar nav badges | `index.html:45, 54, 58`; never populated |
| `.dot-green`, `.run-row-failed`, `.run-chevron` | `style.css:138, 1098-1099`; no matching markup/JS |

**Dependencies:** `package.json:10-16` declares only `bootstrap`, `bootstrap-icons`, and `vite` (dev) — all used; no unused dependencies. Clean.

---

## 7. Bugs, gaps, and edge cases (cross-cutting)

**[UX — low]** Empty-pipeline-list edge cases are handled (dedicated empty rows: `pipelines.js:431-434`, `runs_table.js:12-16`, `mibs.js:38-45` distinguishes "no MIBs yet" vs "no match"), and long names degrade via truncation cells (`style.css:1252-1260`) — but pipeline names in the table itself have no truncation (`pipelines.js:445`), so very long names stretch the first column. Minor.

**[UX — low]** Validation is toast-only: alert-condition required (`detail.js:644`), import rename required (`pipelines.js:112`), editor prompts (`editor.js:168, 190`) — no inline field errors, no focus move to the offending input.

**[GAP — low]** `main.js:76-79` treats *any* non-401 error from `/api/auth/me` (network down, 500, CORS) as "auth disabled" and shows the full shell — the user lands in a dead UI with an offline dot and toast storms rather than a connection-problem state.

**[UX — low]** Settings changes to the poll interval (`settings.js:20-24`) don't restart any active poller — running pages keep the old cadence until re-navigated.

---

## Reconciliation with the prior review (2026-09-15)

Prior findings were re-derived from today's code, not carried over. Classification of the prior review's 13 ranked findings:

| # | Prior finding | Status | Evidence (current code) |
|---|---|---|---|
| 1 | No URL parameters; state on `window` globals | **Open** | `router.js:102-105`, `detail.js:20-21`, `editor.js:52` — unchanged design |
| 2 | Creation path is raw YAML; wizard disabled, no replacement | **Open** | `pipelines.html:29-31`, `router.js:61-63`, `editor.html:40-41`; `wizard.js` now fully dead code |
| 3 | Toast storm + overlapping toasts; pages stuck on "Loading…" | **Open** | `pipelines.js:16-19, 26-33`, `dashboard.js:61-64`, `style.css:1339-1342` |
| 4 | Editor unsaved-changes guard missing | **Open** | `editor.js:116`; no `beforeunload` anywhere (grep-verified) |
| 5 | 12-col run tables clipped (no `.table-responsive`) | **Open** | `runs.html:26-34`, `detail.html:81-89`, `style.css:236`; plugins/cluster still have the fix (`plugins.js:117`, `cluster.js:209`) |
| 6 | Import-replace overwrites without a diff | **Partially fixed** | Conflict modal with Replace/Rename choice added (`pipelines.html:38-67`, `pipelines.js:271-296`) — no longer a silent overwrite; diff preview still absent (`pipelines.js:96-107`) |
| 7 | Global Reload unexplained & unconfirmed | **Open** | `pipelines.js:178-193` |
| 8 | Keyboard users can't open detail; toasts unannounced; poll re-render drops focus | **Open** | `pipelines.js:85-93, 435-459`, `utils.js:148-152` |
| 9 | Confirmation inconsistent; Stop unconfirmed | **Open** | 5× `confirm()` (`pipelines.js:250`, `mibs.js:138`, `schemas.js:80`, `detail.js:753, 765`); zero on Stop (`pipelines.js:439`, `dashboard.js:193`, `detail.js:209-211`) |
| 10 | Run History no auto-refresh; filter gaps; CSV truncation | **Partially fixed** | "Queued" status filter added (`runs.html:7`); "no running option" is moot (backend has no `running` run-status in history); from-only date matches the backend (no `to_dt` exists, `runs.py:44-49`); no polling, silent 1,000-row CSV truncation, and invisible 200-row list cap all still open |
| 11 | Validation errors as toasts not inline | **Open** | `detail.js:644`, `pipelines.js:112` (prior's wizard examples are now dead code) |
| 12 | `partial` badge dot invisible; `col-4` cards never stack | **Partially fixed / reclassified** | The dot gap is moot — `partial` is a dead mapping, backend never emits it (`utils.js:70`; status inventory in `controller.py`); `col-4` stacking still open (`detail.html:46-51`, `mibs.html:2, 21`, `schemas.html:2, 21`) |
| 13 | Hover-only health card; muted-label contrast ~4.0:1 | **Open** | `style.css:165`, `13` — and the light theme is worse (≈2.9:1, `style.css:37`) |

**Totals: 0 fully fixed, 3 partially fixed, 10 open.** The prior review's section-level "done well" observations (run-issues expandable row, run monitor, dry-run + AI assist, health poller, token system, systematic escaping) all re-verified as still accurate, with the queued-run handling (`run_monitor.js:29-36`) and cluster degradation handling (`cluster.js:65-91`) as notable improvements since.

---

## Finding count summary

| Type | High | Medium | Low | Total |
|---|---|---|---|---|
| [UX] | 6 | 8 | 15 | 29 |
| [A11Y] | 2 | 3 | 3 | 8 |
| [BUG] | 1 | 0 | 5 | 6 |
| [GAP] | 0 | 3 | 4 | 7 |
| [BOILERPLATE] | 0 | 2 | 2 | 4 |
| **Total** | **9** | **16** | **29** | **54** |

## Overall assessment

The bones remain strong: a disciplined token system, an unusually good run-issues diagnosis surface, a well-engineered run monitor, and a frontend/backend contract that holds up under line-by-line verification. The highest-leverage fixes are unchanged from the prior review because none were attempted: **(1)** put parameters in the URL and retire `window` globals — this alone also eliminates the new dashboard-"+ New" stale-editor bug; **(2)** build a real notification/error system (stacking, dismiss, aria-live, page-level error states, offline banner); **(3)** guard user work and destructive intent (editor draft guard, diff before import-replace, consistent styled confirmations for stop/reload/rollback); **(4)** fix the responsive skeleton (`.table-responsive`, stacking cards, collapsible sidebar, keyboard-operable rows); **(5)** expose the operator-relevant backend capabilities the UI already has wiring for (`flush` runs, scheduler next-run times) and delete the 900+ lines of dead wizard code that currently suggest a feature that doesn't exist.
