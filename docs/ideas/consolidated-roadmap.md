# Consolidated Roadmap — Pending Work, Grouping & Versioning

**Date:** 2026-09-18
**Purpose:** single planning view over ALL pending work — findings from both 2026-09-17 reviews, all improvement proposals, open GitHub issues, and the treq reuse feasibility — grouped by cross-cutting pattern and assigned to releases. All releases stay in the 1.4.x patch series per maintainers' preference (themed waves, not majors).

**Inputs consolidated:**

| Source | Contents |
|---|---|
| `docs/reviews/ui-ux-review.md` | 54 UI/UX findings (9 high) |
| `docs/reviews/ai-support-review.md` | 19 AI-support findings (2 BUG + 2 SECURITY high) |
| `docs/ideas/ui-ux-improvements.md` | QW1–QW11 quick wins, L1–L6 larger efforts |
| `docs/ideas/ai-integration-improvements.md` | A1–A11 improvements, B1–B8 integration ideas |
| `docs/ideas/treq-ai-reuse-feasibility.md` | treq `_providers/` vendoring assessment (deferred) |
| Open issues | #24 (schema registry feasibility), #26 (editor plugin reference), #27 (plugins page restructure) |
| Housekeeping | 5 uncommitted doc files (the two reviews, two improvement docs, treq feasibility) |

---

## 1. Cross-cutting patterns

Every pending item belongs to one of six patterns. Grouping by pattern (not by source doc) is what makes the release boundaries coherent:

**P1 — AI trust & safety.** Everything flowing through the single `_call_ai` path in `tram/api/routers/ai.py`: blocking execution, secret exfiltration, key wipe, missing validation, unrestricted `base_url`, zero audit. Items: A1, A2, A3, A4, A10, A11 (+ A5 as the UI-side guard).

**P2 — One source of truth for component knowledge.** The frontend hardcodes knowledge the backend already serves via `/api/plugins`, `/api/config/schema`, and `/api/templates`. Items: #26 (editor pills miss 9 transforms), QW10.4 (same fix), #27 (plugins page presentation), #24 (schema registry — the deep version), A6 (template-grounded generation), L2 (schema-driven creation forms — the consumer).

**P3 — State & navigation integrity.** No URL parameters, all cross-page state on `window` globals; refresh/bookmark/Back broken; stale-global bug class. Items: QW1 (the data-loss instance), L1 (the structural fix), QW8 (real links), QW6, QW7.

**P4 — Feedback during async/destructive operations.** Missing confirmations, broken toasts, eternal "Loading…", overwrite without diff or undo. Items: QW2, QW4, QW5, QW9, A5, A9.

**P5 — Boilerplate & dead code.** ~1,100 lines of dead wizard + five hand-rolled poll loops + dead CSS. Items: QW10, L3, L5 (dead color classes).

**P6 — Invisible data.** Clipped 12-column tables, hidden 200-row cap, silent CSV truncation, unanswerable "when does this run next?". Items: QW3, QW6, QW11, L4, L6.

---

## 2. Release plan

### Step 0 — Housekeeping (now, minutes)

Commit the 5 uncommitted docs as a `docs:` commit: the two reviews (one modified, one new) and three ideas docs. None of the work below should start on top of uncommitted reference material.

### v1.4.1 — AI trust & safety patch (~3–4 days)

Theme: make the existing AI feature safe and honest before any expansion. All backend except two small UI pieces.

| Item | What | Effort |
|---|---|---|
| A1 | Un-block event loop (`asyncio.to_thread` + explicit timeouts on anthropic/openai) — fixes [SEC/high] | XS |
| A2 | Stop the Settings key-wipe (blank ≠ delete) — fixes [BUG/high] | XS |
| A8 | Sync `docs/api.md`, add `TRAM_AI_*` to `helm/values.yaml`, fix Base URL label, validate provider string | XS |
| A3 | Validate model YAML server-side before returning it (`yaml.safe_load` + `load_pipeline_from_yaml`) | S |
| A4 | Secret redaction in explain/fix/modify prompts (reuse `config_schema.py:160` secret metadata, keep `${VAR}` intact) — fixes [SEC/high] | S–M |
| A11 | `base_url` scheme enforcement + optional allowlist — fixes [SEC/med] | M |
| A10 | Audit trail: per-call log line (mode, user, model, tokens), optional `ai_usage` store | M |
| A5 | Editor "Undo AI change" snapshot on generate/fix/modify | S |
| QW1 | Dashboard "+ New" stale-editor one-line guard (data-loss fix rides here so it doesn't wait for the UI wave) | 15 min |

**Exit criteria:** no sync SDK call on the event loop; no unredacted secret leaves the server in a prompt; no unvalidated YAML accepted by the editor; `helm`/docs match reality.

### v1.4.2 — Operator trust, UI wave (~2 weeks)

Theme: QW batch first (fast, user-visible), L1 after (per decision). **Single sequential UI lane** — QWs, #26/#27, and L1 all touch the same files; no concurrent UI lanes (repo rule).

| Slot | Items |
|---|---|
| Week 1 — stability | QW2 toasts (stacking container, `aria-live`, dedupe/cooldown), QW3 responsive tables + stacking cards, QW4 `confirmAction()` modal for Stop/Reload/rollback, QW5 shared error/empty/loading states, QW10 dead-code deletion + 401 handling + health-port fix + **#26 fix** (derive editor pills from `/api/plugins`) |
| Week 2 — operator trust | QW6 run-history polling + honest caps, QW7 editor draft guard (`beforeunload`, localStorage, AI-revert), QW8 keyboard/a11y rows + labels + reduced-motion (temporary tabindex; real links after L1), QW9 import-replace diff, QW11 next-run on detail, **#27** plugins-page restructure (detail cards, field explanations, sample usage) |
| Tail — structure | **L1** route parameters + retire `window` globals (`#pipelines/:name`, `#editor/:name?`, `#runs/:id`, filters in query). Absorbs QW1's bug class; QW8 rows become real links |

**In parallel (read-only, no file overlap): run the #24 feasibility study** (schema identifier + registry for AI-assisted adaptation) during this window — it gates v1.4.3's L2 design and shares ground with A6. Deliverable: another `docs/ideas/` feasibility note.

**Exit criteria:** all QWs land; #26/#27 closed; deep links survive refresh; no `window._*` handoffs remain.

### v1.4.3 — Structure & creation (~2–3 weeks)

Theme: consolidate the UI's structural debt and give operators a structured creation path.

| Item | What | Effort | Depends on |
|---|---|---|---|
| L3 | Shared page shell (`createPageController` — fetch/poll/render/focus) | 3–5 d | — |
| L4 | Run pagination (`limit`/`offset`) + `#runs/:id` run-detail page | 2–3 d | L1 |
| L2 | Structured creation path — wizard revival vs editor form layer, decided by #24's outcome | 1–2 w | #24 study |
| L5 | Editor upgrade: line numbers, YAML highlighting, error-to-line anchoring | 3–5 d | — |
| L6 | A11y contrast pass + health-card focus | 1 d | — |

L5 may slip to the next release without breaking anything else.

### v1.4.4 — AI expansion (conditional — starts only when AI is prioritized)

Theme: convert the AI feature from editor tooling into operator tooling, on top of a hardened base.

| Item | What | Effort | Note |
|---|---|---|---|
| treq vendor | Vendor `treq/_providers/` per `docs/ideas/treq-ai-reuse-feasibility.md` (4-coupling adaptation list) | ~3 d | Supersedes A1's interim wrapper; brings streaming, retries, cost engine |
| A6 | Template-grounded generation (few-shot from `/api/templates`) | S | Needs A3/A4 from v1.4.1 |
| A7 | Fix-mode iteration loop (validate + one retry) | S | Same |
| B1 | Run-failure triage "Explain this run" | S–M | Pairs with L4's run-detail page; needs A4 redaction |
| B3 | MIB compile-error explanation | S | |
| B4 | Alert-rule authoring from natural language | S–M | |
| A9 | Streaming output | M | **Deliberately sequenced after the treq decision** — treq has streaming built in; building SSE into the current sync layer would be double work |
| B5, B6 | Throughput-anomaly explanation (only with run-history join), connector-error explain | M, S | After B1/B3 |

---

## 3. Dependency & sequencing rules

- **v1.4.1 precedes all AI expansion** (A-items are prerequisites; stated in `ai-integration-improvements.md`).
- **L4 depends on L1** (v1.4.2 tail).
- **B1 lands best on L4's run-detail page** — AI triage after the run route exists.
- **#24 gates L2's design** — run the study during v1.4.2, decide wizard-revival vs form-layer before v1.4.3 starts.
- **A9 gates on the treq decision** — do not build streaming into the current layer.
- **Single UI lane per release window** — v1.4.2's items all touch `tram/ui/src/`; queue or combine, never concurrent (repo rule for overlapping files).
- **Release model:** one branch + one PR per version, progress table in the PR body (same model as PR #25 / v1.4.0).

## 4. Deferred / deliberately rejected

| Item | Status | Rationale |
|---|---|---|
| B7 DLQ record analysis | Parked | DLQ-browsing API is a prerequisite feature in its own right; AI second |
| B8 template NL search | Rejected as low value | Tag/keyword filtering suffices; library's real value is A6 few-shot grounding |
| Dashboard AI chat widget | Rejected | Generic fluff; maintenance cost buys nothing over targeted modes |
| AI-generated names/descriptions | Rejected | Zero operator pain |
| Whole-dashboard AI summarizer | Rejected | Confabulation risk without causal join |
| NL query over run history | Rejected | Filters + CSV answer it deterministically |
| L2 dual path (keep wizard button + dead code) | Rejected | Pick one path post-#24; the other's code gets deleted |

## 5. Open items & triggers

- **Trigger — AI expansion (v1.4.4):** maintainer prioritizes AI tooling; treq feasibility doc has the cut list ready.
- **Decision needed before v1.4.3:** L2 path choice, informed by #24.
- **L5 slip tolerance:** may move one release without dependencies.
