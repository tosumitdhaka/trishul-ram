# Next-Versions Plan — v1.4.6 → v1.4.8

**Date:** 2026-09-24
**Provenance:** the 2026-09-24 independent full-repo review (`docs/reviews/independent-review-2026-09-24.md`, 59 findings) and open-items inventory (`docs/ideas/open-items-inventory-2026-09-24.md`). All review findings are now tracked as GH issues #43–#52 (clubbed by fix domain).
**Decision (maintainer, 2026-09-24):** fixes first — the security/integrity cluster takes v1.4.6; the AI-expansion cut points from `docs/plans/ai-expansion-plan.md` (originally v1.4.6 = Wave B + A.4, v1.4.7 = A.1 + Wave C) **shift one version each**. That plan's item definitions still stand; only the version slots changed.

## Release model

One branch + one PR per version, progress table in the PR body (same model as PR #25 / v1.4.0). Release gate (`scripts/release-gate.sh`, `docs/release-gate.md`) is mandatory before tagging; never tag past a red gate. All releases stay in the 1.4.x patch series per maintainers' preference (themed waves, not majors).

---

## v1.4.6 — Security & Integrity

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

**Theme:** per-record correctness on the threaded path, verification-gate drift fixes, and the first AI-expansion wave.

| Item | What | Source |
|---|---|---|
| #48 | Per-record correctness: `inject_meta` thread race, `on_error: abort` for transforms, `rate_limit_rps=0` (A5), threaded `record_chunk_size` | review §2.4/2.5/2.9/2.10/2.16 |
| #50 | Browser smoke stale fixture (asserts v1.4.3 vs release), gate validation of `fixtures/meta.json`, 61.5s wait removal | review §4.2/4.9 |
| #54 | Manager-routed `ProcessedFileTracker` — real `skip_processed` idempotency in worker mode (follow-up to #39; HIGH data integrity — duplicate CDRs are wrong billing records) | GH #54 |
| AI B.1 (A7) | Fix-mode iteration loop (validate + one retry) | ai-expansion-plan Wave B |
| AI B.2 (B1) | Run-failure triage: `mode: "triage"` + "Explain this run" UI on the run-detail page | ai-expansion-plan Wave B |
| AI B.3 (A6) | Template-grounded generation (few-shot from `/api/templates`) | ai-expansion-plan Wave B |
| AI A.4 | Server-side payload merge (templates + config schema) | ai-expansion-plan Wave A |

**Entry criteria:** v1.4.6 tagged; **treq vendor decision made** (gates A6 quality and all later streaming/AI work — see `docs/ideas/treq-ai-reuse-feasibility.md`).
**Exit criteria:** #48/#50 closed with tests; browser smoke asserts the live version from `tram/ui/package.json`; AI Wave B items validated against real provider round-trips (mirror the 122-test AI suite pattern); gate green.
**Lane rule:** the B.1 UI piece and any `tram/ui/src/` changes are one single UI lane (no concurrent UI edits).

---

## v1.4.8 — UX/Deploy Polish + AI Wave A.1 + Wave C

**Theme:** close the review's UI/deploy findings and the authoring-UX data layer.

| Item | What | Source |
|---|---|---|
| #49 | UI modal backdrop orphan on Back + deep-link aggravator + stale brand literal | review §4.1/4.6/N1 |
| #51 | Helm: sharedStorage PVC orphan, `admin:admin123` + `postgres: tram` weak defaults, compose EACCES trap | review §4.3–4.5/4.7 |
| #52 | Minor cleanup batch: config validation holes, watcher stem assumption, NITs (fix opportunistically when files are touched) | review §2.14/2.15/NITs |
| AI A.1–A.3 | Per-field descriptions, plugin docstrings, curated examples (data layer for #42) | ai-expansion-plan Wave A |
| AI C.1–C.3 | Plugins detail cards, editor ref-panel upgrade, optional autocomplete | ai-expansion-plan Wave C |

**Entry criteria:** v1.4.7 tagged; AI data layer (A.1–A.3) lands before C.1–C.3 consumes it.
**Exit criteria:** #49/#51 closed (browser smoke includes the modal-Back check from #49's Notes); A.1–A.3 + C.1–C.3 shipped per ai-expansion-plan; #42's remaining scope (if any) re-triaged; gate green.
**Note:** #52 is a grab-bag — items close opportunistically across v1.4.7/v1.4.8 as their files are touched; the issue must be empty (or explicitly re-triaged) before v1.4.8 tags.

---

## Standing rules & open decisions

- **Sequencing:** v1.4.6 → v1.4.7 → v1.4.8 strictly; a version does not start before the previous one is tagged.
- **Overlapping files:** lanes touching the same file queue or combine — never concurrent (repo rule).
- **treq vendor decision** gates the AI rows of v1.4.7/v1.4.8 only; the fix issues (#48/#50, #49/#51/#52) are not blocked by it.
- **SNMP library migration decision** (tsmi/tsmp) is unchanged and orthogonal — blocked on SNMPv1 + v3 crypto breadth upstream (`docs/ideas/trishul-smi-snmp-migration-feasibility.md`).
- **#41 (AI expansion cycle) and #42 (authoring-UX)** remain the umbrella issues for the AI/UX rows above; close them when their last rows ship.
- Not in these versions (stay in backlog): B3–B6 (behind Gate 0 + treq), roadmap.md Backlog items (manager HA, RBAC, DLQ viewer, new connectors), deferred design questions from `docs/ideas/open-items-inventory-2026-09-24.md` §2.
