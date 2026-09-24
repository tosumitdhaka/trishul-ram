# TRAM Open Items & Gaps Inventory — 2026-09-24

- **Scope:** all roadmap items, open issues, and known gaps at HEAD post-v1.4.5 (`main` @ `8470ac8`). Research inventory only — no tracking actions taken.
- **Companion doc:** the same-day independent full-repo review is `docs/reviews/independent-review-2026-09-24.md`. New findings from that review that have no tracking anywhere are listed in §7 below.

## 1. GitHub issues (tosumitdhaka/trishul-ram) — 3 open

| # | Title | Gist |
|---|---|---|
| #39 | skip_processed idempotency silently disabled in worker mode — duplicate CDRs on every run (HIGH) | Worker agent constructs `PipelineExecutor` without `file_tracker` (`tram/agent/server.py:411`) → every file source honoring `skip_processed` (default `True`) reprocesses on every run in manager+worker mode. |
| #41 | AI expansion cycle: treq provider-layer vendor decision + A6/A7/B1 (+A9, B3–B6 behind Gate 0) | Gate 0 = treq `_providers/` vendor yes/no (~3 days, gates A9 + B3–B6); A6 few-shot from `/api/templates`; A7 validate-and-retry-once; B1 `mode: "triage"` + "Explain this run" UI. |
| #42 | Authoring-UX + data layer: per-field descriptions, curated examples, plugins detail cards, editor reference upgrade | Wave A data layer (A.1–A.4) + Wave C UI residuals (detail cards, editor ref-panel search/grouping/insert, optional autocomplete). |

Recently closed (context only, no open work): #3–#11, #16–#22, #24, #26/#27, #28, #32/#33/#35/#36.

## 2. docs/ roadmap and plan documents

### docs/roadmap.md — Backlog (32 unchecked items, predates v1.4.0/v1.4.5; partially stale)
- **Connector fixes (13):** Kafka source (reconnect/offset/consumer-group), Kafka sink (producer error/retry/serializer), OpenSearch sink, ClickHouse source+sink, InfluxDB source+sink, REST source+sink, gNMI source (⚠ subscription modes + reconnect shipped v1.4.0; TLS/client certs still open), SFTP source+sink (⚠ file-done guards shipped v1.4.0), FTP, S3, MQTT, AMQP, NATS.
- **Ops & observability (8):** pipeline cloning; scheduled alert evaluation; DLQ viewer/replay; per-sink record counts; pipeline dependency graph; bulk actions; live WebSocket log streaming; node health detail page.
- **Security & multi-tenancy (4):** RBAC scopes (viewer/operator/admin); per-pipeline API-key scoping; key upload API; audit log.
- **New connectors & serializers (5):** SMTP sink; gRPC sink; Syslog sink (RFC 5424); Kafka schema registry (Avro+Protobuf); PM-XML source (TS 32.435).
- **Infrastructure (2):** manager HA (standby + DB-backed leader election); graceful worker drain (`/drain` + Helm pre-stop hook).
- **Stale:** the four unchecked v1.3.1 boxes (`workers.count:N`, `workers.list`, dynamic K8s Services, pysnmp 7.x) all shipped per changelog — not real gaps.

### docs/ideas/consolidated-roadmap.md (the living plan)
- **AI expansion, unscheduled (9):** treq vendor (~3d, gates the cycle); A6 template-grounded generation; A7 fix iteration; B1 run-failure triage; B3 MIB compile-error explanation; B4 alert-rule authoring; A9 streaming (deliberately after treq decision); B5 throughput-anomaly explanation (needs run-history join); B6 connector-error explain.
- **Open decisions (2):** AI-expansion start (treq decision); SNMP library migration (tsmi/tsmp swap — blocked on SNMPv1 + v3 crypto breadth).
- **Deferred/rejected:** B7 DLQ analysis (parked behind DLQ browsing); B8 NL template search, AI chat widget, etc. (rejected — not collectable work).

### docs/plans/ai-expansion-plan.md (post-v1.4.5)
- Gate 0 (treq vendor decision — ~0 code), Wave A (A.1–A.4 data layer), Wave B (B.1–B.3 = A6/A7/B1), Wave C (C.1 detail cards, C.2 editor ref-panel, C.3 optional autocomplete), behind-Gate-0 set (A9, B3–B6). #24 adaptation machinery deferred by design (per-YAML provenance, schema diff-history; revisit triggers in schema-registry-feasibility.md).
- Natural cut points: v1.4.6 (Wave B + A.4), v1.4.7 (A.1 + Wave C).

### docs/plans/issue-implementation-plan.md (waves A–F shipped)
- **B.3 review residuals (verified, deferred):** (1) threaded *stream*-mode file-finalize window (up to 2× thread_workers messages unwritten); (2) sink-side `enable_safe_finalize` sequential-batch-only; (3) B.3 throughput benchmark + `thread_workers=2` PGW <900 MiB repro pending kind verification (kind soak was a JSON workload, not the PGW ASN.1 repro).
- **Parked table (12 rows), still-relevant:** B5 (stale-config in-flight run), B7 (`errors_last_window` unbounded), B8 (`_add_column_if_missing` swallows exceptions), B9 (`save_pipeline_version` race), B11 (`finalize_source` rename aborts written run), D8 (`_pipeline_workers` growth — likely fixed by v1.4.0 reaping; needs confirming close-out).
- **Remaining code-review backlog:** D1 (DLQ spool), D2 (worker→manager callback retry — no retry in `agent/server.py`), D3 (circuit breaker window hardcoded 60s), D5 (SQLite-default / Postgres recommendation), D7 (SFTP/FTP per-write connection), E1/E3/E4 boilerplate.

### docs/plans/v1.4.0_plan.md
- Post-merge follow-up: flip `TRAM_INTERNAL_AUTH_MODE=enforce` (C.2 Phase 2) after keys confirmed on all clients — default is still `warn`.

### Shipped-feature design docs — open (deferred) questions
- `e2-queued-manual-runs-design.md`: Q3 partial unique index (tied to B9); Q5 queue depth >1 semantics.
- `f1-counter-delta-design.md`: Q2 state-blob compaction at fleet scale; Q3 `align_timezone` knob (F.4 shipped — revisit trigger met); Q4 `max_gap_seconds` default feedback; Q5 dispatch affinity escape hatch.
- `d2-count1-stream-placement-design.md`: none.

## 3. TODO/FIXME/XXX/HACK in code

None found (single grep hit is a test's tampered-token string, not a marker).

## 4. Prior reviews / ideas — UNRESOLVED items

- `docs/reviews/code-review.md`: **A1 still open** (→ issue #39). Spot-verified still present and **untracked by any issue**: A5 (`rate_limit_rps` no `gt=0` — `models/pipeline.py:1468`), A8 (`max_queue_size` dead config — `webhook/source.py:32`), A11 (rate-limit windows dict unbounded), D2 (callback retry). Plan's B.8 (A5+A8) never shipped.
- `docs/reviews/telecom-domain-review.md` — open concerns (not in the fixed list): SNMP trap community string never verified (spoofing); SNMPv3 trap privacy undecryptable; no Counter64 varbind in trap sink; APScheduler UTC-only + hardcoded `misfire_grace_time=60` (`controller.py:200, 941, 961`); no missed-window backfill for poll pipelines; CORBA DII-only; no hot-loadable vendor-quirk logic; no KPI/unit library; no FM alarm lifecycle model; no 3GPP JSON PM output (TS 28.550); DLQ fire-and-forget (D1); CDR sustained-throughput benchmarks absent.
- `docs/ideas/tram-improvements.md` — G1–G9 gaps still true and untracked elsewhere: G1 hot-loadable logic, G2 thread-based execution, G3 plugin catalog size, G4 no CRD/operator, G5 no exactly-once, G8 no DLQ viewer/live logs (G6 manager HA, G7 RBAC = roadmap backlog; G9 stale docs likely fixed).
- `docs/ideas/treq-ai-reuse-feasibility.md` — vendor decision open (= #41 Gate 0).
- `docs/ideas/trishul-smi-snmp-migration-feasibility.md` — migration decision open.
- `docs/ideas/ai-integration-improvements.md` — A6/A7/B1 pending, A9/B3–B6 treq-gated (= #41).
- All other review/ideas docs: fully shipped or purely historical.

## 5. changelog [Unreleased]

None — no `[Unreleased]` section exists; top section is `[1.4.5]`. (Note: `docs/checklist.md` instructs adding entries under `## [Unreleased]`, which doesn't currently exist to receive them.)

## 6. Backlog / known-limitations in ops docs

- `docs/deployment.md`: `TRAM_INTERNAL_AUTH_MODE=enforce` documented as production end-state; the flip is pending (default `warn`).
- `docs/release-gate.md`, `docs/checklist.md`, `docs/api.md` / `architecture.md` / `connectors.md` / `transforms.md`: no open gaps.

## 7. NEW untracked findings from the 2026-09-24 independent review

These have **no GitHub issue and no roadmap entry** as of this inventory (details and file:line evidence in `docs/reviews/independent-review-2026-09-24.md`):

- **Security (default-posture exploits):** AI redaction misses `api_key` connector fields (§3.2, HIGH); webhook `max_queue_size` dead / unbounded queue DoS (§3.1, HIGH — supersedes A8); AI base_url allowlist not enforced at call time → stored-key exfiltration (§3.3, HIGH); open control plane defaults (§3.5, documented but effective posture).
- **Execution core:** stream runs never close sinks (§2.1, HIGH); dry_run sink leak (§2.2); retry loses run_id (§2.3); `inject_meta` thread race (§2.5); trigger/claim TOCTOU phantom run_id (§2.6); fast-run stale-lease spurious FAILED (§2.8); `on_error: abort` not honored for transforms (§2.9); + 2.4 = A5 confirmed with new ZeroDivisionError evidence.
- **UI/deploy/tests:** modal backdrop orphan on Back (§4.1); browser smoke stale v1.4.3 fixture (§4.2); sharedStorage PVC orphan (§4.3); weak deploy defaults (§4.4, §4.5).

## 8. Deduplication / cross-references

| Gap | Appears in |
|---|---|
| skip_processed worker-mode bug | #39 ↔ code-review.md A1 |
| AI cycle + treq vendor decision | #41 ↔ consolidated-roadmap ↔ ai-integration-improvements ↔ treq-ai-reuse-feasibility ↔ ai-expansion-plan |
| Authoring-UX residuals | #42 ↔ ai-expansion-plan Waves A/C |
| Manager HA / RBAC / audit log / DLQ viewer | roadmap backlog ↔ tram-improvements G6/G7/G8 ↔ telecom-domain review |
| DLQ write-failure spool | implementation-plan D1 ↔ telecom-domain review |
| `TRAM_INTERNAL_AUTH_MODE=enforce` flip | v1.4.0_plan ↔ implementation-plan C.2 ↔ deployment.md |
| SNMP library migration decision | consolidated-roadmap ↔ tsmi/tsmp feasibility doc |
| A5 / A8 / A11 / D2 small bugs | code-review.md ↔ implementation-plan B.8 — **no GH issue** (A5/A8 now also confirmed by the 2026-09-24 review) |
| Counter64 trap-sink varbind | telecom-domain review ↔ roadmap.md:15 |
| Scheduler UTC-only / misfire 60s | telecom-domain review ↔ implementation-plan parked row |
| Exactly-once / thread-exec / plugin catalog / CRD / hot-loadable logic | tram-improvements G1–G5 ↔ telecom-domain review ↔ telegraf comparison (no roadmap tracking) |

## 9. Audit findings about the inventory itself

1. `docs/roadmap.md` Backlog is stale relative to v1.4.0: gNMI/SFTP/Kafka rows partially shipped, four v1.3.1 checkboxes shipped but unchecked.
2. A5/A8/A11/D2 and the telecom residuals (trap community, SNMPv3 privacy, Counter64 varbind, scheduler timezone) have **no GitHub issue and no roadmap entry** — the only genuinely untracked open items found (now joined by §7).
3. No `[Unreleased]` changelog section exists despite checklist.md expecting one — post-v1.4.5 work has nowhere to land.

## 10. Count summary

| Source | Count |
|---|---|
| 1. GitHub issues (open) | 3 |
| 2. Roadmap/plans: backlog 32 + AI cycle 9 + open decisions 3 + B.3 residuals 3 + code-review backlog 6 + parked (still-relevant, incl. verify-close D8) ~6 + design-doc open questions 7 | ~66 |
| 3. Code TODO/FIXME | 0 |
| 4. Prior reviews/ideas unresolved | 1 confirmed (A1/#39) + 4 untracked bugs (A5/A8/A11/D2) + ~11 telecom concerns + 2 open decisions + G1–G5/G8 architectural gaps |
| 5. Changelog [Unreleased] | 0 |
| 6. Ops-docs known limitations | 1 (enforce flip) |
| 7. New from 2026-09-24 review (untracked) | 14 highlighted (11 confirmed bugs + top security items; full list in the review doc) |
