# UI/UX Improvement Proposals — TRAM Control Plane

**Date:** 2026-09-17
**Source:** findings from `docs/reviews/ui-ux-review.md` (2026-09-17 re-review). Each proposal references the finding(s) it addresses using that document's section numbering. Effort estimates assume one developer familiar with the codebase.

Prioritization logic: anything that loses user work, hides data, or breaks during an incident outranks structural polish. The prior review's architectural findings (routing, notifications) are still untouched, so they head the "larger efforts" list.

---

## Quick wins

### QW1. Fix the dashboard "+ New" stale-editor bug
- **Addresses:** §1.2 [BUG — high] (dashboard "+ New" opens editor in edit mode of a previously viewed pipeline; Save silently overwrites it)
- **What:** In `dashboard.js:384`, reset `window._editorPipeline` / `window._editorYaml` / `window._editorReturn` before navigating, mirroring `pipelines.js:196-199`.
- **Rationale:** One-line guard against silent production-config overwrite. The cheapest high-severity fix in the backlog.
- **Effort:** ~15 minutes.

### QW2. Stop the toast storms and stacking
- **Addresses:** §3.2 [UX — high] (toasts overlap at identical fixed position; poll failures re-toast every 10s), §4 [A11Y — high] (no `aria-live`)
- **What:**
  1. Give `toast()` (`utils.js:148-158`) a single stacking container: `position: fixed; right: 24px; bottom: 24px; display: flex; flex-direction: column; gap: 8px`, toasts become children instead of siblings pinned to the same spot.
  2. Make the container `role="status" aria-live="polite"` and add a dismiss button to each toast.
  3. Add a dedupe/cooldown: identical error messages inside N seconds update the existing toast instead of spawning another; poll-driven errors (`pipelines.js:16-19`, `dashboard.js:61-64`) degrade to a single inline "daemon unreachable" banner per page.
- **Rationale:** Today a daemon outage produces an unreadable overlapping toast pile every poll cycle, and none of it reaches screen readers. The health dot already says "offline" — the toasts add nothing but noise.
- **Effort:** ~half day.

### QW3. Wrap the wide tables and stack the fixed cards
- **Addresses:** §2 [UX — high] (12-column run tables clipped by `.table-wrap { overflow: hidden }`), §2 [UX — medium] (`col-4` detail cards never stack)
- **What:** Add `.table-responsive` inside `.table-wrap` for Run History (`runs.html:26-34`) and the detail Runs tab (`detail.html:81-89`) — the pattern Plugins and Cluster already use (`plugins.js:117`, `cluster.js:209`). Change `detail.html:46-51` to `col-12 col-md-4`, and `mibs.html`/`schemas.html` `col-8`/`col-4` to `col-12 col-lg-8` / `col-12 col-lg-4`.
- **Rationale:** On a tablet, the most important columns (Status, Issues, actions) are currently invisible. Pure markup changes, no JS.
- **Effort:** ~2 hours.

### QW4. Consistent, styled confirmations for consequential actions
- **Addresses:** §1.4 [UX — medium] (Stop unconfirmed next to other row buttons; native `confirm()` vs styled modals), §1.4 Reload unexplained
- **What:** One `confirmAction({ title, body, confirmLabel, danger })` helper backed by a Bootstrap modal. Use it for Stop (pipelines table `pipelines.js:439`, dashboard `dashboard.js:193`, detail `detail.js:209-211`), Reload (with one sentence: "Re-scans the pipelines directory and re-syncs all pipelines from disk. In-memory edits made only via the API are kept; running streams restart."), rollback, and the existing `confirm()` sites.
- **Rationale:** Stopping a production stream is a one-stray-click action today. Plain-language copy also teaches the reload-vs-rollback-vs-restart distinction the UI has never explained.
- **Effort:** ~half day.

### QW5. Page-level error states that replace "Loading…"
- **Addresses:** §3.1 [UX — high] (API failure leaves skeletons forever)
- **What:** Generalize the Plugins page's `<template>` pattern (`plugins.html:21-31`) into a shared helper — `renderTableState(tbody, 'error' | 'empty' | 'loading', message)` — and call it in the catch blocks of `pipelines.js:26-33`, `runs.js:9-16`, `mibs.js:19-26`, `schemas.js:17-24`, `detail.js:39-41`, with a retry button.
- **Rationale:** During an incident, a page that says "Loading…" for ten minutes is actively misleading. The correct pattern already exists in this codebase and just isn't reused.
- **Effort:** ~half day.

### QW6. Run History: auto-refresh and honest caps
- **Addresses:** §3.3 [UX — medium] (no polling on the incident page; invisible 200-row cap; silent CSV truncation at 1,000)
- **What:**
  1. Poll Run History on the shared interval, with a visible pause/resume toggle (reuse `getSavedPollIntervalMs`).
  2. Show "showing latest 200" next to the count pill (`runs.js:5, 41`).
  3. After CSV export, if the returned row count equals the 1,000 cap (`runs.js:6`, backend `runs.py:46`), toast a warning that the export was truncated and suggest narrowing filters.
- **Rationale:** The page an operator watches during an incident is the only data page that doesn't refresh; and exports silently lie about completeness.
- **Effort:** ~half day.

### QW7. Editor draft guard
- **Addresses:** §1.4 [UX — medium] (no unsaved-changes guard; one Back-click destroys work)
- **What:**
  1. `beforeunload` handler while the textarea differs from the loaded YAML.
  2. Persist a draft to `localStorage` (`tram_editor_draft`) on input (debounced); offer "restore draft?" on the next editor open.
  3. Keep a pre-AI-modify snapshot so "AI Modify" (`editor.js:198-205`) has a one-click revert.
- **Rationale:** Pasting a long pipeline and losing it to navigation is the most likely way this UI destroys operator work today.
- **Effort:** ~half day.

### QW8. Keyboard access to rows and icon buttons
- **Addresses:** §4 [A11Y — high] (rows not keyboard-operable), §4 [A11Y — low] (inconsistent `aria-label`), §4 [A11Y — low] (no `prefers-reduced-motion`)
- **What:** For the three clickable-row tables, add `tabindex="0"`, `role="link"`, an `aria-label` ("Open pipeline X"), and an Enter/Space handler alongside the click listener — or render the pipeline name cell as a real `<a href="#pipelines/X">` once QW/large item L1 lands. Add `aria-label` to every icon-only button in `pipelines.js:439-456` and `dashboard.js:193-197`. Add `@media (prefers-reduced-motion: reduce)` disabling `pulse` (`style.css:189`) and `spin` (`style.css:1667-1668`).
- **Effort:** ~half day.

### QW9. Show a diff before import-replace
- **Addresses:** §1.4 [UX — medium] (Replace overwrites without showing what changes)
- **What:** In the import-conflict modal (`pipelines.html:38-67`), fetch the existing YAML (`api.pipelines.get`) and render saved-vs-uploaded with the existing `renderSideBySideYamlDiff` (`yaml_diff.js:79`) before enabling "Replace current YAML".
- **Rationale:** The diff component already exists and is used two tabs away; this is the one place an overwrite happens where it's needed most.
- **Effort:** ~half day.

### QW10. 401 handling and dead-code cleanup
- **Addresses:** §3.3 [GAP — medium] (session expiry → toast storm, no re-auth), §6 [BOILERPLATE — medium] (dead wizard/nav-badges/unused API calls), §2 [BUG — low] (health port fallback), §1.3 [GAP — low] (hardcoded plugin pills)
- **What:**
  1. In `req()` (`api.js:77-92`), catch `status === 401` once and route to the login overlay instead of throwing per-call.
  2. Delete `wizard.js`, `wizard.html`, the wizard CSS block (`style.css:1521-1665`), `api.configSchema` (`api.js:206-208`), the `plugins-loading-template` (`plugins.html:25-27`), the never-populated nav badges (`index.html:45, 54, 58`), and the dead `statusBadge` `'partial'` mapping (`utils.js:70`). Keep `api.daemon.status` — it's needed by QW11.
  3. Fix `health.js:34` to default the port from `window.location.origin`, matching `api.js:5`.
  4. Derive the editor's plugin reference pills (`editor.html:93-111`) from `/api/plugins` instead of hardcoded lists.
- **Rationale:** ~1,100 lines of dead code currently advertise a wizard that doesn't exist and a schema endpoint nothing uses; expired sessions currently brick the UI until a manual reload.
- **Effort:** ~half day.

### QW11. Surface scheduler "next run" on pipeline detail
- **Addresses:** §1.5 [GAP — medium] (`/api/daemon/status` unused; "when does this run next?" unanswerable)
- **What:** Call `api.daemon.status()` on the detail page and render "next run: <time>" in the Schedule card (`detail.html:48`), falling back to the current `every Xs` label if absent.
- **Effort:** ~2-3 hours (backend already returns it: `runs.py:125-129`).

---

## Larger efforts

### L1. Route parameters and the retirement of `window` globals
- **Addresses:** §1.1 [UX — high] (no deep links/refresh/Back), §1.1 [BUG — low] (`_activeTab` leak), §1.1 [UX — low] (run detail not linkable), §1.2 (root cause of the stale-editor bug), §5 [UX — low] (global-`navigate` coupling)
- **What:** Move the router to parameterized hashes: `#pipelines/:name` (detail), `#editor/:name?` + `?from=detail`, `#runs?pipeline=…&status=…&from=…` (filters in the query so they survive refresh), `#runs/:id` (a real run-detail view). Replace every `window._*` handoff (`detail.js:20`, `editor.js:52`, `runs.js:47-49`, `cluster.js:52-53`) with route state. Serialize detail's active tab into the hash too.
- **Rationale:** This single architectural change fixes bookmarking, sharing into incident channels, refresh, Back-button semantics, and the whole class of stale-global bugs. It is the prior review's #1 recommendation, still open, and everything else in the UI compounds it.
- **Effort:** ~2-3 days including migrating all call sites and manual testing of every flow.

### L2. A structured creation path for operators
- **Addresses:** §1.3 [UX — high] (primary creation path is a raw YAML textarea; wizard disabled with no replacement)
- **What:** Either resurrect `wizard.js` on top of `/api/config/schema` (it was built for exactly this — per-sink serializer/condition/schema forms), or invest the same structure into the editor as a guided form layer above the textarea: pick source/sink from the live registry, generate the skeleton, reveal only the schema-required fields, keep YAML as the "advanced" view. Do not ship both the disabled button and the dead code — pick one.
- **Rationale:** "Edit this YAML, change name and connection details, save" (`pipelines.js:378`) is aimed at the wrong user. The mitigations (templates, AI Assist) still assume comfort with YAML.
- **Effort:** wizard revival ~1 week (forms exist, need schema-driven rewire per the disabled-button message); editor form layer ~2 weeks.

### L3. Shared page shell: data-fetch, polling, and focus-safe rendering
- **Addresses:** §5 [BOILERPLATE — medium] (five hand-rolled poll loops with different cleanup guards), §3.1 (error/empty/loading states), §4 [A11Y — medium] (poll re-render drops focus)
- **What:** One `createPageController({ fetch, render, pollMs })` helper owning: mount/unmount, poll timer lifecycle (single guard, not three different element-existence checks), error/empty/loading states (QW5's helper), and focus-preserving re-render (skip innerHTML replacement when only cell values changed, or restore `document.activeElement` after render — `pipelines.js:435-459`).
- **Rationale:** The three polling pages each reinvented the wheel differently; the two non-polling pages skipped it entirely. Consolidation makes "add polling to Run History" (QW6) and future pages trivial.
- **Effort:** ~3-5 days to migrate all pages without behavior regressions.

### L4. Run pagination and a run-detail route
- **Addresses:** §3.3 [UX — medium] (200-row cap, no pagination despite backend `offset`), §1.1 [UX — low] (failure diagnosis dead-ends at an expanded row)
- **What:** Use the backend's `limit`/`offset` (`runs.py:46-47`) with a "load more" control and total count; promote the run-issues expandable row (`runs_table.js:45-113`) into a `#runs/:id` page (depends on L1) with the failure reason, grouped skip reasons, DLQ block, and links back to the pipeline.
- **Rationale:** The run-issues row is the best failure-diagnosis design in the app, but it can't be shared during an incident, and histories beyond 200 rows are simply inaccessible.
- **Effort:** ~2-3 days (after or alongside L1).

### L5. Editor upgrade: line numbers, syntax highlighting, error anchoring
- **Addresses:** §2 [UX — low] (plain Courier New textarea; dry-run errors don't map to lines; dead `.yk/.yv` color classes)
- **What:** Overlay a line-number gutter on the textarea, apply lightweight YAML tokenization using the existing (currently dead) color classes or a small highlighter, and when the dry-run response carries a line-identifiable issue, scroll/flag the line.
- **Rationale:** The editor is the surface where operators spend the most careful time; it currently looks and behaves like a 1998 form control next to an otherwise crisp UI.
- **Effort:** ~3-5 days for a dependency-free implementation; less if a small highlighter dependency is acceptable.

### L6. Accessibility pass on contrast and the health card
- **Addresses:** §4 [A11Y — medium] ×2 (muted 10-11px labels ≈4.0:1 dark / ≈2.9:1 light; health card hover-only)
- **What:** Bump `--fg-muted` one step (dark: `#8b949e`-adjacent value that clears 4.5:1 at small sizes, or increase those label font sizes to 12px+ where the smaller size isn't essential); make the health card open on `.health-btn:focus-within` and on click (a `<button>` wrapper), not just `:hover` (`style.css:165`).
- **Effort:** ~1 day including light-theme verification (contrast math from token values; final ratios need runtime verification with the actual rendered backgrounds).

---

## Suggested sequencing

1. **Week 1 (stability):** QW1, QW2, QW3, QW4, QW5, QW10 — all small, all high-visibility, several remove real data-loss/overwrite risks.
2. **Week 2 (operator trust):** QW6, QW7, QW8, QW9, QW11.
3. **Then the structural work:** L1 first (everything benefits), then L3, L4, L2, L5, L6 as capacity allows.

The common thread: the UI's bones (design tokens, diff tooling, run monitor, backend contract) are good enough that none of the above requires a rewrite — every proposal fits the existing vanilla-JS architecture.
