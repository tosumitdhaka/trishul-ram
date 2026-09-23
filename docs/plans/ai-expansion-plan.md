# AI Expansion + Authoring-UX Plan (post-v1.4.5)

> **Date:** 2026-09-23 · **Scope:** the pending work from the AI-integration program
> (`docs/ideas/ai-integration-improvements.md`), issue #41 (AI expansion cycle), the
> #26/#27 residuals (authoring-UX tiers beyond what v1.4.2 shipped), and the #24
> deferred parts.
> **Status of this document:** plan — every "Verified" line was checked against
> current HEAD (`main` @ 38ed1b7, post-v1.4.5) on 2026-09-23.

---

## 0. Verified state — done vs pending (HEAD @ 38ed1b7)

| Item | Status | Evidence (HEAD) |
|---|---|---|
| A1 async + timeouts | ✅ shipped v1.4.1 | `asyncio.to_thread(_call_ai …)` — `ai.py:605` |
| A2 key-wipe fix | ✅ shipped v1.4.1 | three-state config semantics (memory #119) |
| A3 validated output | ✅ shipped v1.4.1 | `yaml.safe_load + load_pipeline_from_yaml` check — `ai.py:389-398` |
| A4 secret redaction | ✅ shipped v1.4.1 | `_redact_yaml` — `ai.py:498`; applied in explain/fix/modify (`ai.py:749,768,785`) |
| A5 undo AI change | ✅ shipped v1.4.1 | commit d8d051e |
| A8 docs/Helm/label sync | ✅ shipped v1.4.1 | changelog `[1.4.1]` |
| A10 audit trail | ✅ shipped v1.4.2 | `_run_ai_call` audit + `ai_usage` — `ai.py:550-588` |
| A11 base_url policy | ✅ shipped v1.4.2 | `_base_url_allowed` — `ai.py:185,689` |
| #24 Option A schema identity | ✅ shipped v1.4.3 | `schema_version()` — `config_schema.py:246-249`; embedded `ai.py:592,623`; registry↔union cross-check; `melt` union member restored (`pipeline.py:660`) |
| #26 core bug (hardcoded pills) | ✅ shipped v1.4.2 | pills now rendered from `/api/plugins` — `editor.js:464-471` into empty containers (`editor.html` `id="ref-transforms"` etc.) |
| #27 partial (metadata merge + snippet) | ✅ shipped v1.4.2 | `_enrichedFields` merges choices/secret/multiline client-side (`plugins.js:162`); auto-generated YAML snippet + copy (`plugins.js:172-200, 309-321`) |
| B1 landing surface | ✅ exists | runs table computes `failureReason`/`reasonGroups` (`runs_table.js:37-40`); L1 route params shipped |
| A6 template-grounded generation | ❌ pending | no template few-shot anywhere in `ai_docs.py`; templates endpoint ready with `source_type`/`sink_types` tags (`templates.py:47-65`) |
| A7 fix iteration loop | ❌ pending | fix mode is single-shot — `ai.py:766-782` (no validate-retry) |
| B1 run-failure triage mode | ❌ pending | modes are generate/explain/fix/modify only (`ai.py:736-785`) |
| treq provider-layer vendor decision | ❌ pending | gates A9 + B3–B6 (issue #41) |
| #26 UX tiers (cards, search, grouping, insert, autocomplete) | ❌ pending | pills still a flat non-interactive list |
| #27 restructure + backend enablers | ❌ pending | expand is still table-in-table (`plugins-row-detail` — `plugins.js:231`); per-field descriptions: **zero** `description=` in `models/pipeline.py`, none in `SCHEMA_FIELDS`; most plugin classes still one-line docstrings (verified: `rename`, `mask`) → description stays empty; snippet is auto-generated from schema fields, not curated examples; no "used by N pipelines" |
| #24 adaptation machinery (per-YAML provenance, diff history) | ⏸ deferred by design | revisit triggers documented in `docs/ideas/schema-registry-feasibility.md` §5 |

**Open issues covering this space:** #41 (AI expansion cycle: vendor decision + A6 + A7 + B1),
#39 (skip_processed — separate correctness track, out of scope here).

---

## 1. The one shared insight

Every pending item attacks the same gap from a different side: **TRAM knows field
*names* but has no machine-usable knowledge of idiomatic *usage*.**

- A6 attacks it on the AI side (few-shot from worked templates)
- #27's enablers attack it on the data side (descriptions, curated examples)
- #26's tiers attack it on the editor side (field cards, skeletons)

So the plan leads with the **data layer** once, and the three consumers reuse it.

---

## 2. Gate 0 — treq provider-layer vendor decision (decision, ~0 code)

The feasibility work is done (`docs/ideas/treq-ai-reuse-feasibility.md`, §adaptation
list in #41). What remains is a maintainers' yes/no with two honest paths:

- **Vendor now, then build the cycle on it (~3 days):** `_providers/` vendored into
  TRAM; `ai.py` shrinks to prompt-building + validation. Buys async throughout,
  real httpx timeouts, retries, streaming (A9), cost engine, `list_models()`.
- **Build A6/A7/B1 on current `ai.py`, defer vendoring:** A6/A7/B1 are
  prompt/mode work, not transport work — none of the three touches `_call_ai`'s
  transport. They are **not blocked** by this decision. The later port cost is
  unchanged (the vendor port rewrites transport, not modes).

**Recommendation:** decide Gate 0 first anyway (it is already fully analyzed — the
decision meeting is cheap), but treat it as gating only **A9 and B3–B6**.
A6/A7/B1 proceed immediately either way.

---

## 3. Wave A — the data layer (shared foundation, M)

The #27 backend enablers, built once, consumed by everything downstream.

**A.1 Per-field descriptions (the keystone, M)**
- Add `Field(..., description=...)` to the Pydantic config models in
  `tram/models/pipeline.py` (84 types; seed from `docs/connectors.md` +
  `docs/transforms.md`, which already carry this knowledge for humans).
- Surface through `SCHEMA_FIELDS`: extend `_field_descriptors`
  (`tram/api/config_schema.py`) with a `description` key.
- Phase it: descriptions for the ~15 most-used connector types first
  (sftp/kafka/rest/local/snmp_poll sources, kafka/sql/local/opensearch sinks,
  the top transforms), the long tail in a follow-up.
- Note: adding `description=` rotates `schema_version()` (content hash) —
  expected, it is an identity token not a semantic version.

**A.2 Plugin descriptions (S, content authoring)**
- Multi-line docstrings on plugin classes (summary + description split already
  works — `health.py:148-155`; the content is the missing part). Seed from the
  same docs. ~90 classes; batch with the A.1 phasing.

**A.3 Curated examples (S–M)**
- `example` key per plugin in the `/api/plugins` details payload: one minimal
  worked YAML fragment per type (source from the docs' examples + the template
  library where a type is covered).
- Distinct from the existing auto-generated `_yamlFragment`: curated examples
  show *idiomatic* values (a real `oid` list, a real condition string), the
  generated fragment shows field shapes.

**A.4 Server-side payload merge (XS)**
- Fold `choices`/`secret`/`multiline` (and A.1's descriptions, A.3's examples)
  into `_build_plugin_details` (`health.py`), deleting the client-side
  dual-fetch + `_enrichedFields` merge in `plugins.js`.

**Exit criteria:** `/api/plugins` alone is sufficient to render a complete
plugin detail (no second fetch); descriptions non-empty for the phase-1 type
set; `schema_mismatch` stays empty; contract test asserting the new payload keys.

---

## 4. Wave B — AI expansion core (#41 items)

**B.1 A6 template-grounded generation (S)** — one function in `ai_docs.py`:
pick the template whose `source_type`/`sink_types` tags best match the
generate-mode prompt (`templates.py:47-65` already serves the tags), include it
in the system prompt as a worked example alongside `build_ai_context`.
Guardrail: cap template size in the prompt.

**B.2 A7 fix iteration loop (S)** — in the fix path (`ai.py:766-782`): after
`_call_ai`, validate with `load_pipeline_from_yaml`; if invalid, feed the new
error back for **at most one retry**; return best attempt + validation issues
(the response shape A3 already established). Cap total wall time (reuse the
A1 timeout discipline).

**B.3 B1 run-failure triage (S–M)** — `mode: "triage"` on `/api/ai/suggest`
taking `run_id`: fetch the run (error, `errors[]`, counters), join the pipeline
YAML server-side, apply `_redact_yaml`, prompt with config + error + grouped
skip reasons + counters. UI: "Explain this run" on the run-issues row
(`runs_table.js` reasonGroups already pre-groups the context) and/or the run
detail route. Every call flows through the existing audit path (schema_version
column included).

**Exit criteria:** all three modes emit valid, redacted, audited calls; A6
prompt includes a template; A7 returns validated-or-issues; B1 explains a real
failed run end-to-end in the browser.

---

## 5. Wave C — authoring-UX residuals (#26/#27 UI tiers)

**C.1 Plugins page detail-card restructure (M)** — replace the nested
table-in-table expand (`plugins.js:231`) with the detail card layout designed in
issue #27: list master, click-through card with description (Wave A.2), fields
as labeled rows (per-field descriptions from A.1, lock icons for secrets,
choices as chips), curated example (A.3), "used by N pipelines" (pipelines API
already exposes pipeline configs), copy-skeleton.

**C.2 Editor ref-panel upgrade (S–M)** — keep the dynamic pills, add: search
filter, intent grouping for transforms (Shape/Filter/Enrich/Time-Aggregate/
Format-Mask/Order-Validate per #26), click-to-insert skeleton at the textarea
cursor (skeletons now come from Wave A data), expandable per-plugin mini-card.

**C.3 Editor schema-aware autocomplete (L, optional last)** — CodeMirror fed by
`/api/config/schema` for `type:` values and field names. Do this only after
C.1/C.2 prove out; it is the endgame that makes the sidebar a fallback.

**Exit criteria:** no nested-table expand remains; every plugin detail shows
non-empty description + curated example for the phase-1 type set; editor
insert-at-cursor works for all four categories; browser checks in
`tests/browser/` extended to pin the new surfaces.

---

## 6. Behind Gate 0 (vendor decision) — A9, B3–B6

Unchanged from issue #41: A9 streaming, B3 MIB compile-error explanation,
B4 alert-rule authoring, B5 throughput-anomaly explanation (needs the
run-history join — do not build the un-joined version), B6 connector
test-failure explanation. Deliberately rejected items stay rejected
(B8, AI chat widget, dashboard narration, NL run queries).

## 7. Deferred by design — #24 adaptation machinery

Per-YAML provenance and schema diff-history stay deferred until real
migration demand appears (`schema-registry-feasibility.md` §5). The shipped
`schema_version` token is the anchor any future work will need; nothing here
forecloses it. Revisit triggers: external API consumers materializing, or
connector-schema churn making hand-migration painful.

---

## 8. Sequencing and dependencies

```
Gate 0 (decision, parallel)      Wave A (data layer)
        │                              │
        │ no dependency between them   ▼
        │                      Wave B (A6/A7/B1)   Wave C (UI tiers)
        │                              │                 │
        └── gates ──► A9, B3–B6 ◄──────┴── B1 triage UI lands in C-era UX
```

- Wave A first (A.1 phases into A.4). B and C are parallel after A.4; B.1/B.2
  (A6/A7) need no Wave A dependency at all if prioritized ahead of A.1 —
  the templates exist already. If the cycle must produce visible AI value
  earliest: **B.1 → B.2 → A.4 → A.1 → B.3 → C.1 → C.2**, with A.2/A.3 riding
  the A.1 phasing.
- Release vehicles: stays in the 1.4.x patch series per maintainer preference —
  natural cut points: v1.4.6 (Wave B AI expansion + A.4), v1.4.7 (Wave A.1
  descriptions + Wave C), A9/B3–B6 as the vendor-gated release after.
- Every wave: changelog entries, `ruff check .`, full unit suite, browser
  checks for UI work (real browser, per the release-gate manual validation).

## 9. What this plan does NOT cover

#39 (skip_processed in worker mode) is a separate correctness track. SNMP
follow-ups and any new operator-reported defects supersede sequencing here.
