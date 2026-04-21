# TRAM Roadmap

Planned features and known issues. Items are assigned to a version once scope is confirmed;
unconfirmed work lives in the backlog at the bottom.

---

## v1.2.3 — SNMP Poll v3 Validation & ASN.1 Decode Hardening

- [ ] SNMP poll source — validate SNMPv3 USM on real-device GET and WALK; keep existing walk / yield_rows coverage green
- [x] ASN.1 serializer — decode-path coverage and wording updated for explicit decode-only behavior
- [ ] SNMP trap source — deferred; blocked by push-source architecture gap (issue #11, resolved in v1.3.0)
- [ ] SNMP trap sink — deferred until a reachable real receiver/test target is available

---

## v1.3.0 — Broadcast Streams & Push-Source Scaling

> Full design: [`docs/archive/v1.3.0-broadcast-streams-design.md`](archive/v1.3.0-broadcast-streams-design.md)
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

- [ ] **`workers.count: N` runtime** — logical slot model (slot number ≠ worker assignment); `PlacementReconciler` spare-worker gap fill; slot reassignment on failover; `WorkerPool.resolve()` + `multi_dispatch()` N-worker paths
- [ ] **`workers.list: [...]` runtime** — named-worker placement; per-slot pinned re-dispatch on recovery
- [ ] **Dynamic K8s Service provisioning** — `kubernetes:` pipeline block; manager creates/deletes NodePort Services via K8s API; RBAC Role + RoleBinding; `tram[k8s]` optional extra
- [x] **`source_stem` / `source_suffix` filename tokens** (issue #9) — added to all 6 file-based sinks
- [ ] **Migrate SNMP to pysnmp 7.x** (issue #10) — update import paths; `pysmi` migration

---

## v1.3.2 — Metrics/Stats Parity & UDP Multi-Worker Streams

> Follows v1.3.1 runtime stabilization. Scope is observability model cleanup plus UDP push-source validation.

- [x] **Standalone live stats parity** — feed local active runs into `StatsStore` so standalone exposes the same live stream/placement-style stats model as manager mode instead of only history + process-local Prometheus metrics
- [x] **Manager operational metrics** — add manager-side Prometheus series for dispatch/re-dispatch attempts, placement status counts, reconcile actions, worker health, and callback receipt (not direct failure counts — failures are inferred from the dispatch/receipt delta); document that `/metrics` is process-local and worker scraping is still required for cluster-wide execution metrics
- [x] **UDP multi-worker streams** — `syslog` and `snmp_trap` sources; per-pipeline NodePort Service via `kubernetes: enabled: true` (shared selector for `count: all`, manual Endpoints for `count: N` / `workers.list`); `kubernetes: enabled: true` required in manager mode

---

## v1.3.3 — UI Revalidation & Backend Contract Sync

> Follows v1.3.2 backend work. Scope is intentionally UI-heavy: revalidate every page against the shipped API and remove contract drift.

- [ ] **Full UI/backend contract audit** — verify every page, action, filter, export, and status badge against current API fields and route behavior
- [ ] **Cluster and placement UX** — finalize stream placement, slot health, stale/degraded/reconciling states, and manager-vs-standalone presentation
- [ ] **Metrics/stats UX sync** — align dashboard, cluster streams, run history, and Prometheus guidance with the actual manager/worker vs standalone stats model
- [ ] **Pipeline detail completeness** — schedule, alert, run-history, versioning, placement, and error-policy views all reflect current backend fields without fallback mismatches
- [ ] **Plugin/templates/settings pages revalidation** — remove stale assumptions, ensure live API-backed rendering, and verify empty/error states
- [ ] **Responsive/browser pass** — light/dark mode, mobile/tablet layout, console-clean build, and page-level smoke checks

---

## Backlog (unversioned)

### Connector Fixes (deferred from v1.2.4–v1.2.7)
- [ ] **Kafka source** — reconnect, offset commit, consumer group edge cases
- [ ] **Kafka sink** — producer error handling, retry, serializer integration
- [ ] **OpenSearch sink** — bulk write, index template, auth, retry on 429
- [ ] **ClickHouse source/sink** — query execution, batch insert, type coercion
- [ ] **InfluxDB source/sink** — line protocol, bucket/org resolution, token auth
- [ ] **REST source/sink** — auth types, pagination, retry, SSL verify
- [ ] **gNMI source** — subscription modes, path encoding, TLS
- [ ] **SFTP source/sink** — file glob, move-after-read, skip_processed, key auth
- [ ] **FTP source/sink** — passive mode, directory listing, file write
- [ ] **S3 source/sink** — bucket/prefix, multipart upload, credential chain
- [ ] **MQTT source/sink** — QoS levels, reconnect, topic wildcards
- [ ] **AMQP source/sink** — exchange/queue binding, ack/nack, prefetch
- [ ] **NATS source/sink** — subject routing, JetStream, reconnect

### Operations & Observability
- [ ] **Pipeline cloning** — copy a pipeline with a name prompt in the UI
- [ ] **Scheduled alert evaluation** — cron-based alert checks independent of pipeline runs
- [ ] **Dead-letter queue viewer** — browse and replay DLQ records via the UI
- [ ] **Per-sink record counts** — run metrics broken down per sink
- [ ] **Pipeline dependency graph** — visualize pipeline chains when A feeds B
- [ ] **Bulk actions** — start/stop/delete multiple pipelines from the list view
- [ ] **Live log streaming** — WebSocket tail of log output for running stream pipelines
- [ ] **Node health detail page** — per-worker pipeline assignments and load in manager mode

### Security & Multi-tenancy
- [ ] **Role-based access** — read-only vs admin token scopes (viewer/operator/admin)
- [ ] **Per-pipeline API key scoping** — restrict a key to specific pipelines
- [ ] **Key upload API** — `POST /api/keys/upload` / `GET /api/keys` / `DELETE /api/keys/<name>`
- [ ] **Audit log** — record who triggered, modified, or deleted pipelines

### New Connectors & Serializers
- [ ] **SMTP sink** — outbound email delivery (alerts, reports)
- [ ] **gRPC sink** — generic gRPC unary call sink
- [ ] **Syslog sink** — forward records to remote syslog (RFC 5424)
- [ ] **Kafka schema registry** — full Avro + Protobuf with Confluent wire format
- [ ] **PM-XML source** — ingest 3GPP TS 32.435 PM XML files natively

### Infrastructure
- [ ] **Manager HA** — standby manager with DB-backed leader election
- [ ] **Graceful worker drain** — `POST /api/workers/{id}/drain`; Helm pre-stop hook
- [x] **Coverage target increase** — CI threshold raised to 75%; current coverage ~80%

---

## Released

| Version | Theme |
|---------|-------|
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
