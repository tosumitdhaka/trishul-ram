# TRAM Roadmap

Planned features and known issues. Items are assigned to a version once scope is confirmed;
unconfirmed work lives in the backlog at the bottom.

---

## v1.2.3 — SNMP Poll v3 Validation & ASN.1 Decode Hardening

> Historical planning section — v1.2.3 shipped 2026-04-10 and the released-version table below is authoritative. All four items below are closed (two shipped, two deferred and superseded).

- [x] SNMP poll source — validate SNMPv3 USM on real-device GET and WALK; keep existing walk / yield_rows coverage green
- [x] ASN.1 serializer — decode-path coverage and wording updated for explicit decode-only behavior
- [x] SNMP trap source — deferred at v1.2.3; blocked by push-source architecture gap (issue #11, resolved in v1.3.0) and shipped via the v1.3.x UDP multi-worker work
- [x] SNMP trap sink — deferred at v1.2.3 (no reachable real receiver/test target); never re-scoped — see the backlog

---

## v1.3.0 — Broadcast Streams & Push-Source Scaling

> Full design: [`docs/archive/v1.3.0-plan.md`](archive/v1.3.0-plan.md)
> Skips v1.2.4–v1.2.7 due to severity of push-source architecture gap (issue #11).
> Scope is intentionally limited to HTTP push sources (`webhook`, `prometheus_rw`).
> UDP sources (`syslog`, `snmp_trap`) and dynamic K8s service provisioning are v1.3.1.

### A — `workers:` block & multi-dispatch (issue #11)
- [x] `WorkersConfig` Pydantic model — `count: 1|all` (v1.3.0 runtime) + `count:N` and `list:` forward-compatible in schema; validators: mutual exclusion, `count >= 1`, `list` non-empty + no duplicates; source-type defaults at validation (`count: all` for push HTTP, `count: 1` for all others)
- [x] `BroadcastResult` dataclass — accepted/rejected worker URLs, `running | degraded | error` status
- [x] Linter rules L006–L010 — manager mode only; suppressed entirely in standalone; L007 fires on any multi-worker spec (`count > 1`, `count: all`, or `list:`) for poll/batch sources; no double-warning on queue sources (L009 for queue, L010 for non-queue); `tram validate` reads `TRAM_MODE` from env or `--mode`
- [x] `WorkerPool._worker_ids` — worker_id → URL mapping from health poll
- [x] `WorkerPool.resolve()` — v1.3.0: `count:1` and `count:all`; `count:N` and `list:` raise `NotImplementedError`
- [x] `WorkerPool.multi_dispatch()` — v1.3.0: `count:1` and `count:all` paths
- [x] `WorkerPool.dispatch()` — thin wrapper around `multi_dispatch(count=1)` for backwards compat
- [x] Controller: `_broadcast_placements`, `_active_placement_group`, `_make_placement_group_id()`; `degraded` pipeline status value

### B — Worker Public Ingress Security
- [x] `create_worker_ingress_app()` — minimal FastAPI on `:8767`; webhooks only, no `/agent/*`
- [x] Coupled shutdown: either listener thread exits → `SIGTERM` self → K8s restarts pod
- [x] Composite `GET /agent/health` returns `ok: false` when ingress thread is dead
- [x] `TRAM_WORKER_INGRESS_PORT` (default `8767`); readiness probe stays on `:8766` (composite)
- [x] Worker StatefulSet: `ingress` containerPort `8767`
- [x] `middleware.py` import isolation contract enforced by `test_worker_import_isolation.py`

### C — Unified Pipeline Stats & Load-Aware Dispatch
- [x] `PipelineStats` dataclass (`tram/agent/metrics.py`) — thread-safe; records + bytes (`bytes_in`, `bytes_out`) + rolling error window; covers both stream and batch runs; replaces `StreamMetrics`
- [x] `ActiveRun.stats: PipelineStats` for all run types
- [x] `executor.stream_run()` and `executor.batch_run()` — `stats` param; `bytes_in` from source read, `bytes_out` from sink write; batch emits `is_final: true` stats report immediately before `_post_run_complete`
- [x] `RunCompletePayload` gains `bytes_in: int = 0` and `bytes_out: int = 0`; `on_worker_run_complete` writes them to `run_history` — this is the authoritative final total path
- [x] Worker stats reporting thread (`_stats_loop`) — periodic reports `is_final: false`; batch completion emits `is_final: true`; posts every `TRAM_STATS_INTERVAL` (default 30s; replaces `TRAM_HEARTBEAT_INTERVAL`)
- [x] `POST /api/internal/pipeline-stats` — `is_final: true` → `StatsStore.remove(run_id)` (load eviction only, not the bytes persistence path); otherwise `StatsStore.update()`
- [x] `StatsStore` (`tram/agent/stats_store.py`) — keyed by `run_id`; staleness contract: `get_by_run_id()` stale-aware (reconciler + placement API per-slot); `for_worker()`, `for_pipeline()`, `all_active()` exclude stale (load-scoring and aggregate views); explicit `remove(run_id)`; replaces `HeartbeatStore`
- [x] `on_worker_run_complete` — writes `bytes_in`/`bytes_out` to `run_history`; calls `StatsStore.remove(run_id)` as fallback for crashed runs
- [x] `WorkerPool.load_score()` — resolves `worker_url → worker_id` via `_worker_ids` before `StatsStore.for_worker()`; fallback to `active_runs × 1 MB proxy`

### D — PlacementReconciler
- [x] `PlacementReconciler` background thread (`tram/agent/reconciler.py`) — runs every `min(TRAM_STATS_INTERVAL, 10)s`; owns all stale detection, re-dispatch, and reconciling-window timeout; stats endpoint is write-only
- [x] Stale slot detection — `StatsStore.get_by_run_id(slot.current_run_id)`; age > `3 × interval` → mark stale → re-dispatch same worker; updates `slot.current_run_id` in slots_json + DB (count:all only in v1.3.0)
- [x] Reconciling-window timeout — after `2 × interval`: matched → `running`; partial → `degraded`; none → re-dispatch
- [x] Unit tests: stale → re-dispatch, reconciling timeout, partial recovery → degraded

### E — Manager Restart Reconciliation
- [x] DB table `broadcast_placements` — `slots_json` entries carry `run_id_prefix` (immutable) and `current_run_id` (mutable, updated on each re-dispatch)
- [x] `TramDB`: `save_broadcast_placement()`, `get_active_broadcast_placements()`, `update_broadcast_placement_status()`, `update_slot_run_id()`
- [x] `_boot_load()` seeds `_broadcast_placements` from DB; sets `reconciling` status
- [x] Stats receiver matches incoming stats by `run_id_prefix` → resolves reconciling slot; updates `current_run_id`
- [x] Unit tests: cold restart, partial recovery → degraded, unmatched → re-dispatch; `current_run_id` updated after re-dispatch

### F — Placement API
- [x] `GET /api/pipelines/{name}/placement` — iterates `slots_json` as source of truth; per-slot stats via `StatsStore.get_by_run_id(slot.current_run_id)`; per-sec fields zeroed for stale; stale slots visible
- [x] `GET /api/cluster/streams` — aggregate totals from `StatsStore.for_pipeline()` (non-stale) + group status/counts from `broadcast_placements`

### G — Manager StatefulSet
- [x] Replace `manager-deployment.yaml` with `manager-statefulset.yaml` + `volumeClaimTemplates`
- [x] `manager-headless-service.yaml` for stable pod DNS
- [x] Remove separate manager PVC from `pvc.yaml`; `existingClaim` migration path in `values.yaml`
- [x] `helm/NOTES.txt` upgrade migration note

### H — Alert cooldown on confirmed delivery only (issue #3)
- [x] `_fire_webhook()` / `_fire_email()` return `True`/`False`
- [x] `_set_cooldown()` called only on `True`
- [x] Tests: HTTP 500, connection error, SMTP failure → no cooldown

---

## v1.3.1 — Placement Completion & Targeted Backend Follow-ups

> Depends on v1.3.0 TCP path being validated end-to-end.

- [x] **`workers.count: N` runtime** — logical slot model (slot number ≠ worker assignment); `PlacementReconciler` spare-worker gap fill; slot reassignment on failover; `WorkerPool.resolve()` + `multi_dispatch()` N-worker paths *(shipped v1.3.1)*
- [x] **`workers.list: [...]` runtime** — named-worker placement; per-slot pinned re-dispatch on recovery *(shipped v1.3.1, incl. dedicated-Services Endpoints repatching on scale-down)*
- [x] **Dynamic K8s Service provisioning** — `kubernetes:` pipeline block; manager creates/deletes NodePort Services via K8s API; RBAC Role + RoleBinding; `tram[k8s]` optional extra *(shipped v1.3.1 for push streams; UDP sources via v1.3.2)*
- [x] **`source_stem` / `source_suffix` filename tokens** (issue #9) — added to all 6 file-based sinks
- [x] **Migrate SNMP to pysnmp 7.x** (issue #10) — update import paths; `pysmi` migration *(shipped v1.3.1 — `pysnmp>=7,<8` with 7.x HLAPI compatibility helpers)*

---

## v1.3.2 — Metrics/Stats Parity & UDP Multi-Worker Streams

> Follows v1.3.1 runtime stabilization. Scope is observability model cleanup plus UDP push-source validation.

- [x] **Standalone live stats parity** — feed local active runs into `StatsStore` so standalone exposes the same live stream/placement-style stats model as manager mode instead of only history + process-local Prometheus metrics
- [x] **Manager operational metrics** — add manager-side Prometheus series for dispatch/re-dispatch attempts, placement status counts, reconcile actions, worker health, and callback receipt (not direct failure counts — failures are inferred from the dispatch/receipt delta); document that `/metrics` is process-local and worker scraping is still required for cluster-wide execution metrics
- [x] **UDP multi-worker streams** — `syslog` and `snmp_trap` sources; per-pipeline NodePort Service via `kubernetes: enabled: true` (shared selector for `count: all`, manual Endpoints for `count: N` / `workers.list`); `kubernetes: enabled: true` required in manager mode
- [x] **ASN.1 structured decode flattening** — BER multi-record split for concatenated files; ordered `message_classes` fallback; generic `json_flatten` and `hex_decode` transforms with configurable ASN.1-oriented options and docs
- [x] **CDR record shaping** — dotted-path support on `unnest`, `explode`, `drop`, `rename`, `value_map`, `cast`; shared `path_utils` helpers; `select_from_list`, `coalesce_fields`, and `project` primitives; conditional `drop`; and narrow single-segment wildcard support on `hex_decode` / `json_flatten` for explicit row-semantic CDR pipelines (LTE/SGW/PGW)
- [x] **Batch reconciliation** — dedicated `BatchReconciler`, active batch lease tracking, worker-scan adoption after manager restart, and lost-run synthesis through the existing run-finalization path
- [x] **Incremental large-record batch processing** — pipeline-level `record_chunk_size`, serializer `parse_chunks(...)` hook, and incremental ASN.1 BER decode for bounded-memory serial batch processing
- [x] **Safe staged batch file output** — record-safe serial batch sinks (`csv`, `ndjson`) finalize output per source file, delete staged temp artifacts on failure, and support optional `post_batch_cleanup` heap trimming per pipeline

---

## v1.3.3 — UI Revalidation & Backend Contract Sync

> Follows v1.3.2 backend work. Scope is intentionally UI-heavy: revalidate every page against the shipped API and remove contract drift.

- [x] **Full UI/backend contract audit** — verify every page, action, filter, export, and status badge against current API fields and route behavior
- [x] **Cluster and placement UX** — finalize stream placement, slot health, stale/degraded/reconciling states, and manager-vs-standalone presentation
- [x] **Metrics/stats UX sync** — align dashboard, cluster streams, run history, and Prometheus guidance with the actual manager/worker vs standalone stats model
- [x] **Pipeline detail completeness** — schedule, alert, run-history, versioning, placement, and error-policy views all reflect current backend fields without fallback mismatches
- [x] **Plugin/templates/settings pages revalidation** — remove stale assumptions, ensure live API-backed rendering, and verify empty/error states
- [x] **Responsive/browser pass** — light/dark mode, mobile/tablet layout, console-clean build, and page-level smoke checks
---

## v1.4.0 — Implementation Waves A–F (GH #16–#22)

> Full plan: [`docs/plans/issue-implementation-plan.md`](plans/issue-implementation-plan.md) · release record: [`v1.4.0_plan.md`](plans/v1.4.0_plan.md) · verification: [`reviews/kind-verification.md`](reviews/kind-verification.md)

- [x] **A — stopgaps (#16 mitigation, #21 labeling)**: `MALLOC_ARENA_MAX=2` + `post_batch_cleanup` default on + schema-cache LRU + asset-sync skip + sink close; `no_capacity` vs `dispatch_failed` run-history labeling with health hysteresis; dead-code removal
- [x] **B — correctness core**: alert CRUD persistence, watcher delete lifecycle, threaded batch-path rework (deferred source finalize + bounded in-flight cap), syslog RFC 6587 + concurrent TCP, controller lifecycle RLock, manager-restart adopt guard, Kafka `enable_auto_commit=false` default
- [x] **C — security rollout (phase 1, warn-only)**: `TRAM_INTERNAL_AUTH_MODE`, bidirectional `X-API-Key` on worker↔manager traffic, probe exemptions, constant-time compares, webhook body-size limit, ClickHouse identifier validation, chart defaults rotated out
- [x] **D — visibility/stats (#17, #22, #18)**: durable count=1 stream placements with worker-death recovery + manager-restart adoption + config-drift redispatch; live mid-run dashboard stats; `json_flatten`/explode linear-time fix; per-slot placement CAS
- [x] **E — enhancements (#19, #21, #20)**: ASN.1 `split_path`/`split_path_context`; queued manual runs (durable queue, auto-dispatch, TTL, six metrics); templates-page shared action contract
- [x] **F — domain gaps**: `counter_delta` + `window_aggregate` stateful transforms with durable per-pipeline state (manager-mediated in worker mode); local/SFTP file-done guards; gNMI subscription modes + reconnect; Kafka stop + lag; CORBA dedupe window; `source_timezone` for timestamp normalization

---

## v1.5.0 — AI Provider Layer + SNMP Library Swap

> Both scope-defining decisions made by the maintainer on 2026-09-25. Entry-gated: the SNMP swap starts only after upstream trishul-snmp #28 (SHA-2 HMAC tag length) ships and the smoke harness re-runs green.

- [ ] **treq `_providers/` vendoring (GH #71)** — copy the layer (no library extraction), adaptation list per `docs/ideas/treq-ai-reuse-feasibility.md`; preserve the v1.4.6 security properties (A10 audit, A11 base_url policy, redaction, three-state config) on the new call path
- [ ] **AI Wave C on the vendored layer (GH #41)** — A9 streaming, B3–B6 per the ai-expansion-plan
- [ ] **A.2/A.3 authoring-UX (GH #42)** — editor inline-validation UX + per-plugin examples (ungated; confirm scope at wave planning)
- [ ] **SNMP swap, option C (GH #72)** — full pysnmp/pysmi → trishul-snmp/tsmp swap behind a feature flag (default off); flag-off path must remain behavior-identical

---

## Backlog (unversioned)

### Connector Fixes (deferred from v1.2.4–v1.2.7)
- [ ] **Kafka source** — reconnect, offset commit, consumer group edge cases *(stop + lag and `enable_auto_commit=false` default shipped v1.4.0; reconnect/offset/consumer-group open)* (GH #58)
- [ ] **Kafka sink** — producer error handling, retry, serializer integration (GH #58)
- [ ] **OpenSearch sink** — bulk write, index template, auth, retry on 429 (GH #60)
- [ ] **ClickHouse source/sink** — query execution, batch insert, type coercion (GH #60)
- [ ] **InfluxDB source/sink** — line protocol, bucket/org resolution, token auth (GH #60)
- [ ] **REST source/sink** — auth types, pagination, retry, SSL verify (GH #60)
- [ ] **gNMI source** — subscription modes, path encoding, TLS *(subscription modes + reconnect shipped v1.4.0; TLS/client certs open)* (GH #59)
- [ ] **SFTP source/sink** — file glob, move-after-read, skip_processed, key auth *(file-done guards shipped v1.4.0; remaining items open)* (GH #59)
- [ ] **FTP source/sink** — passive mode, directory listing, file write (GH #59)
- [ ] **S3 source/sink** — bucket/prefix, multipart upload, credential chain (GH #60)
- [ ] **MQTT source/sink** — QoS levels, reconnect, topic wildcards (GH #61)
- [ ] **AMQP source/sink** — exchange/queue binding, ack/nack, prefetch (GH #61)
- [ ] **NATS source/sink** — subject routing, JetStream, reconnect (GH #61)

### Operations & Observability
- [ ] **Pipeline cloning** — copy a pipeline with a name prompt in the UI (GH #62)
- [ ] **Scheduled alert evaluation** — cron-based alert checks independent of pipeline runs (GH #62)
- [ ] **Dead-letter queue viewer** — browse and replay DLQ records via the UI (GH #62)
- [ ] **Per-sink record counts** — run metrics broken down per sink (GH #62)
- [ ] **Pipeline dependency graph** — visualize pipeline chains when A feeds B (GH #62)
- [ ] **Bulk actions** — start/stop/delete multiple pipelines from the list view (GH #62)
- [ ] **Live log streaming** — WebSocket tail of log output for running stream pipelines (GH #62)
- [ ] **Node health detail page** — per-worker pipeline assignments and load in manager mode (GH #62)

### Security & Multi-tenancy
- [ ] **Role-based access** — read-only vs admin token scopes (viewer/operator/admin) (GH #63)
- [ ] **Per-pipeline API key scoping** — restrict a key to specific pipelines (GH #63)
- [ ] **Key upload API** — `POST /api/keys/upload` / `GET /api/keys` / `DELETE /api/keys/<name>` (GH #63)
- [ ] **Audit log** — record who triggered, modified, or deleted pipelines (GH #63)

### New Connectors & Serializers
- [ ] **SMTP sink** — outbound email delivery (alerts, reports) (GH #64)
- [ ] **gRPC sink** — generic gRPC unary call sink (GH #64)
- [ ] **Syslog sink** — forward records to remote syslog (RFC 5424) (GH #64)
- [ ] **Kafka schema registry** — full Avro + Protobuf with Confluent wire format (GH #64)
- [ ] **PM-XML source** — ingest 3GPP TS 32.435 PM XML files natively (GH #64)

### Infrastructure
- [ ] **Manager HA** — standby manager with DB-backed leader election (GH #68)
- [ ] **Graceful worker drain** — `POST /api/workers/{id}/drain`; Helm pre-stop hook (GH #68)
- [x] **Coverage target increase** — CI threshold raised to 75%; current coverage ~80%

### Telecom Domain Hardening (from `docs/reviews/telecom-domain-review.md`)
- [ ] **SNMP trap community-string verification** — trap source does not verify the community string (spoofing vector) (GH #65)
- [ ] **SNMPv3 trap privacy handling** — undecryptable v3 traps surfaced/handled explicitly (GH #65)
- [ ] **Counter64 varbind in SNMP trap sink** — trap sink lacks Counter64 varbind support (GH #65)
- [ ] **Timezone-aware scheduling** — APScheduler is UTC-only and `misfire_grace_time` is hardcoded 60s; make both configurable (GH #66)
- [ ] **Missed-window backfill for poll pipelines** — downtime windows are skipped, not backfilled (GH #66)
- [ ] **CORBA Notification Service** — source remains DII-only (no Notification Service / typed args) (GH #66)
- [ ] **KPI/unit library** — shared telecom KPI definitions and unit normalization (GH #66)
- [ ] **FM alarm lifecycle model** — alarm state machine (raise/clear/correlate) for fault pipelines (GH #66)
- [ ] **3GPP JSON PM output** — TS 28.550 output format for PM pipelines (GH #66)
- [ ] **CDR sustained-throughput benchmarks** — reproducible benchmark suite for CDR ingestion rates (GH #66)

### Extensibility
- [ ] **Hot-loadable custom logic** — Starlark/execd-equivalent per-vendor quirk handling without redeploys (G1 in `docs/ideas/tram-improvements.md`; also the telecom review's vendor-quirk residual) (GH #67)

### Design Follow-ups (from shipped-feature design docs)
- [ ] **Queue depth >1 for manual runs** — one queued run per pipeline today (re-trigger is idempotent, returns the same run_id); decide deeper-queue semantics (E.2 design Q5, `docs/plans/e2-queued-manual-runs-design.md`) (GH #69)
- [ ] **Stateful-transform state-blob compaction at fleet scale** — per-pipeline state blobs grow with key cardinality (F.1 design Q2, `docs/plans/f1-counter-delta-design.md`) (GH #69)
- [ ] **`align_timezone` knob for window alignment** — revisit trigger already met (F.4 `source_timezone` shipped) (F.1 design Q3) (GH #69)
- [ ] **`max_gap_seconds` default tuning** — confirm the default against real stream feedback (F.1 design Q4) (GH #69)
- [ ] **Dispatch-affinity escape hatch** — pinning a count=1 stateful pipeline to a specific worker for cache locality (F.1 design Q5) (GH #69)
- [x] **Partial unique index for `queued_runs`** (E.2 design Q3) — resolved via B9 in v1.4.7 (GH #55)

### Open Decisions
- [x] **treq provider-layer vendoring — DECIDED 2026-09-25: vendor in v1.5.0 (GH #71)** — supersedes the 2026-09-24 proceed-on-current-ai.py decision. Unblocks AI streaming (A9) and B3–B6 of the AI expansion cycle — `docs/ideas/treq-ai-reuse-feasibility.md`
- [x] **SNMP library migration — DECIDED 2026-09-25: option C (full swap behind a feature flag) in v1.5.0 (GH #72), gated upstream** — the v0.5.1 re-validation confirmed the former blockers fixed (SNMPv1, crypto matrix in-stack, silent-drop, walk boundaries, cross-stack interop) but found one new wire-level defect: every USM HMAC is truncated to 12 bytes while RFC 7860 requires 16/24/32/48 for SHA-224/256/384/512 (filed as trishul-snmp #28 with the root-cause chain). The swap proceeds once that fix lands and the harness re-runs green — addendum in `docs/ideas/trishul-smi-snmp-migration-feasibility.md`
- [ ] **Flip `TRAM_INTERNAL_AUTH_MODE=enforce`** — ops task, not development: after all clients carry keys, flip `warn` → `enforce` per `docs/deployment.md` (v1.4.6 adds the misconfiguration startup warning)

> Architectural positions, not backlog items: thread-based execution (G2), no CRD/operator (G4), at-least-once without exactly-once (G5) — deliberate trade-offs documented in `docs/ideas/tram-improvements.md` and `docs/ideas/tram-vs-telegraf-comparison.md`. G3 (plugin catalog) is covered by the Connector Fixes section above; G6/G7/G8 already appear above as Manager HA, RBAC, and DLQ viewer/live log streaming.

---

## Released

> **Superseded by [`docs/ideas/consolidated-roadmap.md`](ideas/consolidated-roadmap.md)** — that document is the living plan for pending work and version assignment. This page keeps the released-version table and the historical planning sections above.

| Version | Theme |
|---------|-------|
| v1.4.8 | UX/deploy polish + cleanup + AI A.1: modal lifecycle on navigation + modal-nav browser check (GH #49), chart-managed postgres credentials + PVC orphan guard + envSecret precedence (GH #51), config validation bounds + CSV formula guard + SQLite busy_timeout (D5) + SFTP/FTP connection reuse (D7) + E1/E3/E4 refactors (GH #52), 601 schema field descriptions (A.1, `schema_version` → `583465a50b4b`) — PR #57, tag `v1.4.8` |
| v1.4.7 | Execution correctness + verification + AI Wave B: per-record correctness — A5, `inject_meta` gates, abort parity, threaded chunking (GH #48), manager-routed `ProcessedFileTracker` closing the #39 architecture follow-up (GH #54), code-review backlog incl. B9 unique index + legacy dedup (GH #55), browser-smoke fixture drift gate (GH #50), AI triage/fix-retry/template-grounded generation + A.4 plugin metadata (GH #41/#42) — PR #56, tag `v1.4.7` |
| v1.4.6 | Security & integrity: AI secret redaction + call-time allowlist (GH #43), fail-open control plane + rate limiter (GH #44), webhook queue DoS (GH #45), stream/dry-run lifecycle (GH #46), run_id integrity (GH #47), #39 fail-loud — PR #53, tag `v1.4.6` |
| v1.4.5 | SNMP data-integrity pass: tuple-space walk boundaries (GH #32), refuse-on-collapse (GH #33), layered INTEGER classification + `*Vdom` migration (GH #35), structured index grouping (GH #36) — PR #38, tag `v1.4.5` |
| v1.4.4 | Wizard AI-assist fixes (operator-reported; unplanned) — PR #37, tag `v1.4.4` |
| v1.4.3 | Structure & creation: guided creation wizard (`#create`), run-detail route (`#runs/:id`), shared page shell, schema identity (GH #24, Option A), editor gutter/anchoring, a11y; UI browser smoke gate check — PR #31, tag `v1.4.3` |
| v1.4.2 | Operator-trust UI wave: QW1–QW11, L1 route parameters (deep links), GH #26/#27 — PR #30, tag `v1.4.2` |
| v1.4.1 | AI trust & safety: A1–A5, A8, A10, A11; tag-triggered release gate — PR #29, tag `v1.4.1` |
| v1.4.0 | Implementation waves A–F: correctness core, security rollout, visibility/stats, queued runs, stateful transforms, domain gaps (GH #16–#22) |
| v1.3.0 | Broadcast streams; push-source scaling; placement reconciliation; manager StatefulSet |
| v1.2.3 | SNMP Poll v3 validation; ASN.1 decode hardening |
| v1.2.2 | Stability & polish; CI fixes; docs alignment |
| v1.2.1 | Worker callback chain; run metrics propagation; dashboard UX |
| v1.2.0 | Manager + Worker cluster architecture |
| v1.1.4 | PipelineController; melt transform; SNMP fixes |
| v1.1.1 | YAML diff modal; enrich missing file; template fixes |
| v1.1.0 | Pipeline Wizard; Live Metrics; Alert Rules UI; AI Assist; Connector Test |
| v1.0.8/9 | Browser auth; PostgreSQL subchart; shared RWX storage |
| v1.0.7 | Bootstrap 5 web UI; dedicated K8s UI service |
| v1.0.6 | ndjson serializer; per-sink serializer_out |
| v1.0.4 | Schema registry; ClickHouse connector; pipeline update API |
| v1.0.3 | MIB management API; schema management API; standard MIBs in image |
| v1.0.2 | SNMPv3 USM (auth/priv) |
| v1.0.1 | SNMP poll yield_rows; dynamic version |
| v1.0.0 | API key auth; rate limiting; TLS; per-sink retry; circuit breaker; OTel |
| v0.9.0 | Thread workers; batch_size; DLQ; CORBA source; processed-file tracking |
