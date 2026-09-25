# Next-Versions Plan — v1.4.6 → v1.5.0

> v1.4.6, v1.4.7, and v1.4.8 shipped (2026-09-24/25); their sections below are the release records. v1.5.0 is the active plan.

**Date:** 2026-09-24
**Provenance:** the 2026-09-24 independent full-repo review (`docs/reviews/independent-review-2026-09-24.md`, 59 findings) and open-items inventory (`docs/ideas/open-items-inventory-2026-09-24.md`). All review findings are now tracked as GH issues #43–#52 (clubbed by fix domain).
**Decision (maintainer, 2026-09-24):** fixes first — the security/integrity cluster takes v1.4.6; the AI-expansion cut points from `docs/plans/ai-expansion-plan.md` (originally v1.4.6 = Wave B + A.4, v1.4.7 = A.1 + Wave C) **shift one version each**. That plan's item definitions still stand; only the version slots changed.

## Release model

One branch + one PR per version, progress table in the PR body (same model as PR #25 / v1.4.0). Release gate (`scripts/release-gate.sh`, `docs/release-gate.md`) is mandatory before tagging; never tag past a red gate. All releases stay in the 1.4.x patch series per maintainers' preference (themed waves, not majors).

---

## v1.4.6 — Security & Integrity

> **Status: shipped 2026-09-24 — PR #53, tag `v1.4.6`** (see `docs/changelog.md` `[1.4.6]`). Scope text below is the original plan, kept as the release record.

**Theme:** close every HIGH finding from the 2026-09-24 review plus the duplicate-CDR regression (#39) — the "no exploitable default, no silent data corruption" release.

| Issue | What | Sev |
|---|---|---|
| #39 | `skip_processed` idempotency disabled in worker mode — duplicate CDRs on every run (`agent/server.py:411` no `file_tracker`) | HIGH |
| #43 | AI secret exfiltration: `api_key` redaction gap, call-time base_url allowlist, fail-open YAML redaction, non-atomic `ai_save_config` | HIGH |
| #44 | Fail-open control plane: unauthenticated defaults, internal endpoints w/o API key, connectors/test SSRF, proxy credential forwarding, `str(exc)` leakage, info disclosure | HIGH |
| #45 | Webhook unbounded-queue DoS (`max_queue_size` dead config; supersedes A8) + rate-limiter hardening (A11) | HIGH |
| #46 | Stream/dry-run lifecycle: sinks never closed (ClickHouse timer leak), stop-watcher leak, thread abandonment | HIGH |
| #47 | run_id integrity: retry rebuild loses run_id, trigger/claim TOCTOU phantom run_id, fast-run stale lease → spurious FAILED | MED |

**Entry criteria:** none — start immediately.
**Exit criteria:** all six issues closed with regression tests (per-issue Notes sections name them); `/api/plugins` schema_mismatch still empty; release gate fully green; changelog `[1.4.6]` includes migration notes for the default flips in #44/#45 where applicable (helm values, deployment docs).
**Suggested waves:** (a) #43+#45 (AI/webhook security, same API lane), (b) #44 (control-plane defaults, API middleware lane), (c) #46+#47 (executor lane), (d) #39 (worker agent lane). (a)/(b) touch `tram/api/` — serialize or combine per the overlapping-files rule; (c)/(d) are independent of the API lanes.

---

## v1.4.7 — Execution Correctness + Verification + AI Wave B

> **Status: shipped 2026-09-24 — PR #56, tag `v1.4.7`** (see `docs/changelog.md` `[1.4.7]`). Scope text below is the original plan, kept as the release record.

**Theme:** per-record correctness on the threaded path, verification-gate drift fixes, and the first AI-expansion wave.

| Item | What | Source |
|---|---|---|
| #48 | Per-record correctness: `inject_meta` thread race, `on_error: abort` for transforms, `rate_limit_rps=0` (A5), threaded `record_chunk_size` | review §2.4/2.5/2.9/2.10/2.16 |
| #50 | Browser smoke stale fixture (asserts v1.4.3 vs release), gate validation of `fixtures/meta.json`, 61.5s wait removal | review §4.2/4.9 |
| #54 | Manager-routed `ProcessedFileTracker` — real `skip_processed` idempotency in worker mode (follow-up to #39; HIGH data integrity — duplicate CDRs are wrong billing records) | GH #54 |
| #55 | Code-review backlog bugs: DLQ spool (D1), callback retry (D2), breaker window (D3), parked-table items (B5/B7/B8/B9/B11), D8 close-out verification | GH #55 |
| AI B.1 (A7) | Fix-mode iteration loop (validate + one retry) | ai-expansion-plan Wave B |
| AI B.2 (B1) | Run-failure triage: `mode: "triage"` + "Explain this run" UI on the run-detail page | ai-expansion-plan Wave B |
| AI B.3 (A6) | Template-grounded generation (few-shot from `/api/templates`) | ai-expansion-plan Wave B |
| AI A.4 | Server-side payload merge (templates + config schema) | ai-expansion-plan Wave A |

**Entry criteria:** v1.4.6 tagged ✓; **treq vendor decision (2026-09-24): proceed on the current `ai.py`** — vendoring deferred; Wave B + A.4 build on the existing layer, while A9 (streaming) and B3–B6 stay gated on a future revisit of `docs/ideas/treq-ai-reuse-feasibility.md`.
**Exit criteria:** #48/#50/#54 closed with tests; #55 closed or its opportunistic items explicitly re-triaged to v1.4.8; browser smoke asserts the live version from `tram/ui/package.json`; AI Wave B items validated against real provider round-trips (mirror the 122-test AI suite pattern); gate green.
**Lane rule:** the B.1 UI piece and any `tram/ui/src/` changes are one single UI lane (no concurrent UI edits).

---

## v1.4.8 — UX/Deploy Polish + AI Wave A.1 + Wave C

> **Status: shipped 2026-09-25 — PR #57, tag `v1.4.8`** (see `docs/changelog.md` `[1.4.8]`). Scope text below is the original plan, kept as the release record. Delta at ship time: only AI A.1 rode this release — A.2/A.3 and Wave C (C.1–C.3) did not and remain open under GH #42.

**Theme:** close the review's UI/deploy findings and the authoring-UX data layer.

| Item | What | Source |
|---|---|---|
| #49 | UI modal backdrop orphan on Back + deep-link aggravator + stale brand literal | review §4.1/4.6/N1 |
| #51 | Helm: sharedStorage PVC orphan, `admin:admin123` + `postgres: tram` weak defaults, compose EACCES trap | review §4.3–4.5/4.7 |
| #52 | Minor cleanup batch: config validation holes, watcher stem assumption, NITs (fix opportunistically when files are touched) | review §2.14/2.15/NITs |
| D5/D7/E1/E3/E4 | Code-review design + boilerplate items, opportunistic alongside #52: Postgres recommendation + SQLite busy-timeout hardening (D5), SFTP/FTP sink connection pooling (D7), sink-config field-block dedup ×20 (E1), file-sink rolling-writer extraction (E3), connector config-extraction helper (E4) | `docs/reviews/code-review.md` §D/§E |
| AI A.1–A.3 | Per-field descriptions, plugin docstrings, curated examples (data layer for #42) | ai-expansion-plan Wave A |
| AI C.1–C.3 | Plugins detail cards, editor ref-panel upgrade, optional autocomplete | ai-expansion-plan Wave C |

**Entry criteria:** v1.4.7 tagged; AI data layer (A.1–A.3) lands before C.1–C.3 consumes it.
**Exit criteria:** #49/#51 closed (browser smoke includes the modal-Back check from #49's Notes); A.1–A.3 + C.1–C.3 shipped per ai-expansion-plan; #42's remaining scope (if any) re-triaged; gate green.
**Note:** #52 is a grab-bag — items close opportunistically across v1.4.7/v1.4.8 as their files are touched; the issue must be empty (or explicitly re-triaged) before v1.4.8 tags. The D5/D7/E1/E3/E4 batch follows the same rule (no separate GH issue — close opportunistically or explicitly re-triage before tagging).

---

## v1.5.0 — AI Provider Layer + SNMP Library Swap

> Both scope-defining decisions made by the maintainer on 2026-09-25: (1) vendor treq's `_providers/` layer, (2) option C for the SNMP library swap.

| Item | Scope | Issue |
|---|---|---|
| treq `_providers/` vendoring | Copy the layer (~1,900 + ~5,400 test lines, no library extraction) with the adaptation list from the feasibility doc (decouple the openai global-settings import, parameterize the Rakuten hostname, add anthropic base_url, keep lazy optional SDK imports). The v1.4.6 security properties — A10 audit rows, A11 base_url policy incl. call-time effective-endpoint enforcement, redaction, three-state config semantics — must be proven to apply on the new call path | #71 |
| AI Wave C — A9 | Streaming responses end-to-end on the vendored layer | #41 |
| AI Wave C — B3–B6 | Structured output, tool use, batch, evals (scope per ai-expansion-plan) | #41 |
| A.2/A.3 | Editor inline-validation UX + per-plugin examples (ungated; confirm scope at wave planning) | #42 |
| SNMP swap — option C | Full pysnmp/pysmi → trishul-snmp/tsmp swap behind a feature flag (default off): poll + trap paths, v3 USM full crypto matrix, MIB compile/resolve via tsmi | #72 |

**Entry criteria:** v1.4.8 tagged ✓; **ON HOLD (maintainer, 2026-09-25): an upstream tsmi/tsmp improvement round is in progress — v1.5.0 waits for it, and the SNMP swap scope will include that round's output**; upstream trishul-snmp #28 (SHA-2 HMAC tag length) + #29 (DES-CBC priv) resolved + smoke harness re-run green (addendum in `docs/ideas/trishul-smi-snmp-migration-feasibility.md`). Wave C rows additionally require the vendored AI layer landed and reviewed.
**Exit criteria:** flag-off path behavior-identical (full suite green on pysnmp) + flag-on live kind verification (v1/v2c/v3 roundtrips + trap paths) + vendored AI layer with the v1.4.6 security properties test-proven on the new call path + independent diff review + release gate green.

---

## Standing rules & open decisions

- **Sequencing:** v1.4.6 → v1.4.7 → v1.4.8 shipped; v1.5.0 is next and starts only when its entry criteria are met (upstream SHA-2 fix + green harness re-run).
- **Overlapping files:** lanes touching the same file queue or combine — never concurrent (repo rule).
- **treq vendor decision — DECIDED 2026-09-25:** vendor `_providers/` in v1.5.0 (GH #71); Wave C (A9, B3–B6) builds on the vendored layer.
- **SNMP library migration — DECIDED 2026-09-25:** option C in v1.5.0 behind a feature flag (GH #72), gated on upstream trishul-snmp #28 + a green harness re-run.
- **#41 (AI expansion cycle) and #42 (authoring-UX)** remain the umbrella issues for the AI/UX rows; close them when their last rows ship.
- Not in these versions (stay in backlog): the roadmap backlog rows now tracked as GH #58–#70.
