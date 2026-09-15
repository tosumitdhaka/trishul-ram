# TRAM Control-Plane UI — UX/Design Review

**Date:** 2026-09-15
**Scope reviewed:** `tram/ui/` — vanilla-JS SPA, Bootstrap 5.3 + Bootstrap Icons, custom token layer in `src/style.css` (1,690 lines), hash router, no framework. Entry: `index.html`, `src/main.js`, `src/router.js`, `src/api.js`. All 14 pages + helpers read in full (`dashboard`, `pipelines`, `detail`, `runs`, `runs_table`, `run_monitor`, `editor`, `wizard`, `mibs`, `schemas`, `plugins`, `cluster`, `settings`, `login`, `template_helpers`), plus `health.js`, `yaml_diff.js`, `utils.js`, and `package.json`. Audience: telecom operators/NOC engineers.

---

## 1. Information Architecture & Routing

**[WELL-DONE]** The nav taxonomy (`index.html:36-71`) is genuinely good: Overview / Pipelines / Assets / System is the correct mental model for a mediation control plane, and it maps cleanly onto the pipeline lifecycle. Badge counts in the sidebar (`#nav-badge-pipelines`, etc.) add orientation without clutter.

**[FLOW ISSUE — Critical]** Routes carry no parameters, and all cross-page state travels on `window` globals. `router.js:102-122` builds hashes as bare page names (`#detail`, `#editor`), while the data travels via `window._detailPipeline` (`detail.js:20`), `window._editorPipeline`/`_editorYaml` (`editor.js:52`, `pipelines.js:195-210`), and `window._runsFilters` (`detail.js:169-171`). Consequences:

- Refreshing on `#detail` redirects to the pipelines list (`detail.js:21` — no name → `navigate('pipelines')`).
- A NOC engineer cannot bookmark, share, or paste a link to "the failing pipeline" or "run 4f2a" in an incident channel. For a NOC tool this is arguably the single most damaging architectural decision in the UI.
- Browser Back from detail→pipelines silently discards context.

**[UX ISSUE]** The wizard is disabled but its corpse remains: the "Wizard Soon" button (`pipelines.html:29-31`) is permanently greyed out with the message "Wizard temporarily disabled while schema-driven flow is refined", and `router.js:61-63` silently rewrites `#wizard` → `#pipelines`. Meanwhile `wizard.js` (746 lines, fully functional per-sink config) still exists. Users see a disabled feature with no ETA. See finding 2 below for the operator-impact consequences.

**[UX ISSUE]** Run detail does not exist as a route. A run's full story (issues, DLQ, error text) lives in an expandable row (`runs_table.js:45-113`) rather than a `#runs/:id` page, so a specific failed run cannot be linked, and the "failure diagnosis path" dead-ends at an expanded table row.

## 2. Task Flows

**[FLOW ISSUE — High]** For non-developer operators, the primary creation path is now a **raw YAML textarea**. "New Pipeline" (`pipelines.html:32-34`), the dashboard "+ New" (`dashboard.js:384`), and template deploy (`pipelines.js:359-369`) all land in the editor. The mitigations (templates modal, AI Assist, plugin reference pills at `editor.html:89-114`) are good, but templates dump YAML with the instruction "edit name and connection details, then save" (`pipelines.js:368`) — editing YAML by hand is exactly what a NOC operator should not be forced to do. The disabled wizard covered per-sink serializer, condition, and per-field schema forms (`wizard.js:488-507` — genuinely well built); nothing equivalent replaced it.

**[FLOW ISSUE]** The editor has **no unsaved-changes guard**. Cancel (`editor.js:116`) and any hash navigation instantly wipe the textarea — there is no `beforeunload`, no confirm, no draft persistence. An operator who pastes a 200-line pipeline and grazes the browser Back button loses everything.

**[FLOW ISSUE]** "Reload" (`pipelines.js:178-193`) is a global, consequential action — it hot-reloads all pipelines from disk — yet it has no confirmation, and its only explanation is the button tooltip "Reload pipelines". There is no indication whether running streams restart, whether in-memory edits are affected, or how it differs from the per-pipeline "Restart". The reload-vs-rollback distinction the UI needs is never taught anywhere.

**[FLOW ISSUE]** Import-replace overwrites an existing pipeline with the uploaded YAML **without showing a diff** (`pipelines.html:52-54`, `pipelines.js:96-107`). The app has an excellent side-by-side diff component (`yaml_diff.js`) already used for version compare — this is the one place it's needed most and it's absent.

**[WELL-DONE]** Wizard → editor handoff, when it existed, was well designed: "Continue In Editor" pre-loads generated YAML and explains the scope split honestly (`wizard.html:80-86`, `wizard.js:717-724`). The same handoff pattern survives in template deploy.

**[WELL-DONE]** `pipelineStartFeedback` (`utils.js:160-173`) distinguishes "disabled in YAML", "manual schedule — use Run Now", and "already running" instead of a generic "Started". This is exactly the kind of state-aware messaging operators need.

## 3. Run Monitoring

**[WELL-DONE]** The run-issues expandable row (`runs_table.js:45-113`) is the best detail in the app: grouped, de-duplicated skip reasons with counts, separated "Pipeline failure" vs "records skipped" vs "DLQ" blocks, plus a tooltip preview of the top 3 reasons on the collapsed cell (`runs_table.js:134-143`). This is a serious failure-diagnosis affordance.

**[WELL-DONE]** Triggered-run monitoring with toast outcome (`run_monitor.js:9-34`, wired in `detail.js:218-241`, `pipelines.js:452-463`): click Run Now, stay on the page, get a success/failure toast when the run lands, with a 120s timeout. Good async feedback model.

**[UX ISSUE]** The Run History page (`runs.js`) is the only data page **without** a poll timer — dashboard, pipelines, and cluster all auto-refresh, but the page an operator stares at during an incident does not. Filters are also thin: status filter offers only success/failed/aborted (`runs.html:5-10`) — no "running"; date filter is from-only, no "to"; no pagination and no visible indicator that the list is capped at 200 (`runs.js:5`).

**[UX ISSUE]** CSV export silently truncates at 1,000 rows (`runs.js:6,62-69`) with no warning when the filter matches more.

**[VISUAL ISSUE]** Relative timestamps (`relTime`, `utils.js:3-11`) are the right choice, but they never update between polls — a "just now" stays "just now" for 10+ seconds on non-polling pages.

## 4. States, Errors & Notifications

**[UX ISSUE — High]** API failure leaves pages stuck on "Loading…" forever. Every page's `init` catch just fires a toast and returns without replacing the skeleton (`pipelines.js:26-33`, `runs.js:9-16`, `mibs.js:19-26`, `schemas.js:17-24`, `detail.js:39-41`). Contrast with the plugins page, which does this correctly with dedicated error/empty templates (`plugins.html:21-31`) — that pattern exists in the codebase and isn't reused.

**[UX ISSUE — High]** Toast storm on daemon outage: the dashboard polls every ~10s and each failure fires a new error toast (`dashboard.js:61-64`); pipelines does the same (`pipelines.js:16-19`). Worse, `.tram-toast` is `position: fixed; right: 24px; bottom: 24px` with **no stacking container** (`style.css:1361-1374`, `utils.js:147-158`) — concurrent toasts render on top of each other as an unreadable smear. During an outage you get an overlapping toast pile every 10 seconds. The health dot already communicates offline state; page-level polling should degrade to an inline banner, not repeat toasts.

**[UX ISSUE]** Toast quality generally: no dismiss control, no aria-live, 4s flat timeout (long multi-line messages like the connector-test result in `detail.js:632-645` — which also collapse newlines since `textContent` doesn't honor `\n` — vanish or wrap into mush).

**[UX ISSUE]** Destructive-action confirmation is present but wildly inconsistent: native `confirm()` for pipeline delete (`pipelines.js:250`), MIB delete (`mibs.js:138`), schema delete (`schemas.js:80`), rollback (`detail.js:722`), alert delete (`detail.js:734`) — while the app elsewhere uses styled Bootstrap modals. **Stop** has no confirmation anywhere (adjacent to other row buttons, one stray click stops a production stream: `pipelines.js:429`, `dashboard.js:193`), and global Reload has none (finding above).

**[WELL-DONE]** The health poller (`health.js`) with sidebar dot, topbar dot, and hover card is a persistent, calm connectivity signal — the right pattern for a NOC tool.

## 5. Forms & Validation

**[WELL-DONE]** The dry-run feedback panel (`editor.js:261-298`) is strong: pass/fail line, issue list, warnings in yellow, and — when AI is enabled — inline "Explain" / "AI Fix" actions bound directly to the errors. Pairing validation with explanation is ahead of most tooling.

**[WELL-DONE]** The editor's inline "Changes vs saved" diff (`editor.js:340-366`) with add/delete stats, synced scroll (`yaml_diff.js:61-77`), and auto-open after AI modify (`editor.js:203-205`) is the correct trust model for AI-assisted editing.

**[UX ISSUE]** Outside the (disabled) wizard, validation is toast-only: "Pipeline name is required" (`wizard.js:196-199`), "Condition is required" (`detail.js:613`) appear as transient toasts instead of inline field errors next to the offending input. Required-field marking exists in wizard HTML (`wizard.html:31`, red asterisk via `.wizard-required`) but there's no live inline validation anywhere.

**[UX ISSUE]** The wizard's interval round-trip loses precision: `wizard.js:153-155` forces the unit to minutes and computes `Math.round(seconds/60)`, so a 90-second interval becomes "2 minutes" on re-entry.

**[VISUAL ISSUE]** The editor textarea is a plain `Courier New` box (`style.css:820`, `editor.html:40-41`) — no line numbers, no YAML syntax highlighting, no error-line anchoring (dry-run errors don't map to a line). The `yk/yv/yn` YAML color classes exist in CSS (`style.css:1051-1053`) but are never used by the editor.

## 6. Visual Hierarchy & Consistency

**[WELL-DONE]** This is a real design system: ~40 CSS custom properties for both dark and light themes (`style.css:4-46`), a consistent badge grammar (`.tram-badge` with animated pulse for running, `style.css:173-202`), shared card/table/toolbar primitives, and disciplined `esc()` escaping in every template string. Status color semantics (green running, yellow scheduled/partial, red error/failed, cyan paused/stream, grey stopped) are applied consistently through one `statusBadge` function (`utils.js:57-73`).

**[VISUAL ISSUE]** `.tram-badge.partial` declares `has-dot` (`utils.js:69`) but no `.tram-badge.partial::before` color exists (`style.css:177-187`) — partial-status badges render an invisible dot.

**[VISUAL ISSUE]** Typography is Bootstrap-default system font — serviceable and coherent, but undistinctive; there's no display/numeric hierarchy beyond size, and `Courier New` for the most important surface (the YAML editor) looks dated next to the otherwise crisp GitHub-dark aesthetic.

**[VISUAL ISSUE]** Hardcoded accent hex values bypass the token system in ~15 places (`#388bfd` sprinkled through `style.css:131, 213, 217, 1084, 1095-1096, 1150…`), so a future re-theme requires archaeology. Minor maintainability smell, invisible to users today.

## 7. Responsiveness

**[UX ISSUE]** The 248px sidebar never collapses (`style.css:100-106`) — no toggle, no off-canvas. On a tablet used at a NOC desk, a quarter of the viewport is permanently chrome.

**[UX ISSUE — High]** The 12-column run tables (`runs.html:25-33`, `detail.html:79-87`) sit in `.table-wrap` with `overflow: hidden` (`style.css:234`) and **no** `.table-responsive` wrapper (which the plugins and cluster pages *do* use — `plugins.js:117`, `cluster.js:209`). On narrow screens the last columns (Status, Issues, actions) are simply clipped — the most important columns.

**[VISUAL ISSUE]** Detail cards use `col-4` with no stacking breakpoint (`detail.html:44-49`), so on a phone the Source/Sinks/Schedule cards render at one-third width each. Same pattern on the MIBs/Schemas pages (`mibs.html:2,21`, `schemas.html:2,21` — `col-8`/`col-4`).

**[WELL-DONE]** The two media breakpoints that do exist (`style.css:1414-1531`) are thorough for the pages they cover — toolbar stacking, diff panes going single-column, editor toolbar wrapping.

**[UX ISSUE]** No consideration for NOC wall displays: everything is locked to a 13-14px base (`body font-size: 14px`, `style.css:50`; tables 13px), with no density/zoom control. Defensible for a desk tool, but a NOC wall-display scenario is unsupported.

## 8. Accessibility

**[A11Y]** Clickable table rows have no keyboard path: rows are `<tr>` with click handlers (`dashboard.js:199` + `dashboard.js:361-368`, `pipelines.js:434` + `85-93`, `cluster.js:314`) with no `tabindex`, `role="link"`, or Enter handling. Keyboard-only users cannot open pipeline detail at all (the Edit button is the only keyboard-reachable workaround).

**[A11Y]** Toasts are appended to `<body>` with no `role="status"`/`aria-live` region (`utils.js:148-151`) — screen readers get zero announcement of success/error feedback, which is the app's entire feedback channel.

**[A11Y]** The health card is CSS-hover-only (`style.css:165` — `.health-btn:hover .health-card { display:block }`): invisible to keyboard focus and unreliable on touch.

**[A11Y]** Contrast is borderline throughout the secondary layer: `--fg-muted #6e7681` on `--bg-surface #161b22` (`style.css:13`, `5`) computes to roughly 4.0:1, and it's used at 10-11px for labels, hints, and table headers (`style.css:277, 504, 116-118`) — below the 4.5:1 WCAG AA threshold for text that size.

**[A11Y]** The 10-second poll re-renders wipe `tbody.innerHTML` (`pipelines.js:35-38` via `renderTable`), destroying focus if a keyboard user has focus inside the table — focus is silently dropped to `<body>` mid-interaction.

**[A11Y]** Icon-only action buttons mostly rely on `title` (e.g., stop/start in `dashboard.js:193-196` — one has `aria-label`, its siblings don't), and the sparkline tooltip is mouse-only (`dashboard.js:435-471`). No `prefers-reduced-motion` handling for the pulse/spin animations (`style.css:188, 1689-1690`).

**[WELL-DONE]** Login form is correctly labeled with autocomplete hints and a `role="alert"` error region (`login.html:18-26`); wizard status spans use `aria-live="polite"` (`wizard.html:22, 101`).

## 9. Overall Aesthetic & Coherence

**[WELL-DONE]** This is a coherent, committed design: a GitHub-dark-inspired control plane with a proper token layer, restrained accent usage (blue actions, amber brand), consistent 8px-radiused surfaces, tabular numerals for metrics (`font-variant-numeric: tabular-nums`, `style.css:278`), and a working dark/light toggle. It looks like a professional ops tool, not a Bootstrap default. The inconsistencies that do exist (native `confirm()` vs styled modals, hover-card vs dialogs) read as a system mid-maturation, not as chaos.

---

## Ranked findings (by user impact)

| # | Finding | Label | Evidence |
|---|---|---|---|
| 1 | No URL parameters — deep links, refresh, sharing, and Back all break; state lives on `window` globals | FLOW (Critical) | `router.js:102-122`, `detail.js:20-21` |
| 2 | Primary creation path for non-developers is a raw YAML textarea; wizard disabled with no replacement | FLOW (High) | `pipelines.html:29-34`, `dashboard.js:384`, `router.js:61-63` |
| 3 | Toast storm + overlapping toasts on API failure; pages stuck on "Loading…" forever | UX (High) | `dashboard.js:61-64`, `style.css:1361-1374`, `pipelines.js:26-33` |
| 4 | No unsaved-changes guard in the editor — one click/hashchange destroys work | FLOW (High) | `editor.js:116` |
| 5 | 12-col run tables clipped on narrow screens (missing `.table-responsive`) | UX (High) | `runs.html:20-33`, `style.css:234` |
| 6 | Import-replace overwrites pipeline YAML without showing a diff | FLOW | `pipelines.js:96-107` |
| 7 | Global Reload unexplained & unconfirmed; reload-vs-rollback distinction never taught | FLOW | `pipelines.js:178-193` |
| 8 | Keyboard users can't open pipeline detail (non-focusable clickable rows); toasts not announced; poll re-render drops focus | A11Y | `dashboard.js:361-368`, `utils.js:148-151`, `pipelines.js:35-38` |
| 9 | Confirmation pattern inconsistent; Stop unconfirmed next to other row buttons | UX | `pipelines.js:250` vs `429`, `detail.js:722` |
| 10 | Run History doesn't auto-refresh during incidents; filter gaps; silent CSV truncation at 1,000 | UX | `runs.js:5-16, 62-69` |
| 11 | Validation errors delivered as transient toasts instead of inline field errors | UX | `wizard.js:196-199`, `detail.js:613` |
| 12 | `partial` badge dot invisible; `col-4` cards never stack on mobile | VISUAL | `utils.js:69`, `detail.html:44-49` |
| 13 | Hover-only health card; contrast of muted 10-11px labels ~4.0:1 | A11Y | `style.css:165`, `13`, `277` |

## Overall assessment

**The five highest-impact UX improvements:**

1. **Put parameters in the URL.** `#pipelines/:name`, `#runs/:id`, and preserved filter state would fix deep-linking, refresh, Back-button, and incident-shareability in one architectural move (`router.js`).
2. **Ship a real creation path for operators.** Either restore the (already-built, already-good) wizard with its schema-driven per-sink serializer/condition forms, or invest the same structure into the editor as a guided form layer. The current answer — "edit this YAML" — is aimed at the wrong user.
3. **Build a proper notification/error system:** a single stacking toast container with dismiss + `aria-live`, page-level error states that replace "Loading…" (copy the plugins-page template pattern), and a persistent offline banner instead of per-poll toasts.
4. **Protect user work and intent:** editor unsaved-changes guard + localStorage draft, a diff preview before import-replace, and consistent styled confirmations for stop/reload/rollback with plain-language explanations of what each will do.
5. **Fix the responsive skeleton:** `.table-responsive` on the wide run tables, collapsing sidebar, stacking `col-4` cards, and keyboard-operable rows — this makes the tool usable from a tablet on the NOC floor.

**What is genuinely good:** a disciplined dark/light token system applied with unusual consistency for a framework-less SPA; the run-issues expandable row (grouped skip reasons, DLQ, tooltips) which is genuinely excellent failure-diagnosis design; the triggered-run monitor with outcome toasts; dry-run feedback paired with AI explain/fix; the inline saved-vs-current diff with synced scrolling; `pipelineStartFeedback`'s state-aware messages; the health poller as a calm ambient signal; and systematic HTML escaping in every rendered template. The bones are strong — the highest-leverage fixes are architectural (routing, state, notifications), not cosmetic.
