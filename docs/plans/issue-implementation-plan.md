# TRAM — Issue Implementation Plan (v2)

**Date:** 2026-09-15 (v2, revised after two independent reviews — see `docs/reviews/implementation-plan-reviews.md`)
**Purpose:** Consolidated implementation-planning artifact covering every known issue — GitHub issues, code-review findings, and RCA-discovered defects — with source, root cause, solution, effort, risk, dependencies, release vehicle, and verification.
**Inputs:** `docs/reviews/code-review.md`, `docs/reviews/telecom-domain-review.md`, `docs/reviews/ui-ux-review.md`, `docs/reviews/issue-tracker-summary.md`, `docs/reviews/issue-rca.md`, and the GitHub tracker. Detailed evidence lives in those documents.

**What changed in v2 (per the reviews):**
- Waves are **risk-ordered and release-anchored** (A-F), not theme-based. Old wave IDs are kept in parentheses for traceability.
- **W-1.2 + W-2.3 merged** into one threaded-batch-path rework (B.3) — same code, conflicting invariants if done separately; re-estimated **L**.
- **W-1.1 rewritten:** manager-mediated tracker API is the primary design (worker image has no sqlalchemy by design, `daemon/server.py:22-25`; worker `/data` is an emptyDir; dispatch is not sticky). The storage decision is a **blocking prerequisite**.
- **W-1.5 rebuilt as a two-phase rollout** (Wave C): as originally written it would 401 K8s probes (CrashLoopBackOff), turn every worker-dispatched run into a phantom FAILED (workers post to `/api/internal/*` with no key), and secure surfaces behind committed plaintext defaults.
- **Fast-tracked the #16 stopgaps** (Wave A patch release) — the OOMKill is live today and two mitigations touch no correctness-wave files.
- **Reclassified misplaced correctness bugs:** manager-restart double-dispatch guard (B.6) and A12 syslog TCP framing (B.4) moved into the correctness wave; A12 removed from the domain-gap wave.
- **Controller RLock (B1/B2/B10) pulled ahead** of the queue work item — the queue's single-claim mechanism sits on the TOCTOU'd running guard.
- **B6 (Kafka auto-commit) housed** in the correctness wave; remaining unhoused findings explicitly parked with rationale (§Parked).
- Dependency corrections: removed the false W-3.1→W-3.3 edge; W-3.2 strictly after W-3.1; W-4.1 gated on the threaded-path rework (not just W-2.1); **A13 before W-3.1** (making count=1 streams placements widens the duplicate-redispatch blast radius until A13 is fixed).
- Added per-wave exit criteria, rollback, release vehicle, test-strategy mapping, and design-doc gates for E.1/E.2.

**Effort scale:** S ≤ 1 day · M = a few days · L = a week+. **Risk:** blast radius / semantic risk.
**Scope realism:** single-maintainer program; Waves A-E span roughly 3-4 months. Each wave is a release cut point, not a sprint.

---

## Master sequencing

| Wave | Theme | Release vehicle | Exit criteria |
|---|---|---|---|
| A | Stopgaps (~1 wk) | patch release | #16 mitigations live; #3 closed; dispatch errors truthful |
| B | Correctness core (3-5 wk) | minor release | idempotency parity across execution modes; no data-loss windows; no duplicate dispatch |
| C | Security rollout (1-2 wk, overlaps B tail) | minor release | internal + agent surfaces authenticated with zero broken runs/probes |
| D | Visibility & stats (3-4 wk) | minor release | stream/visibility parity for count=1 streams; live dashboard batch stats |
| E | Enhancements | minor release(s) | #19/#21/#20 shipped feature-flagged |
| F | Domain gaps | roadmap | per-item |

Dependency edges (corrected): B.3 → E.1. A.6 → E.2, and B.5 → E.2. D.1 (A13) → D.2. D.2 → D.3. The D4 decision gates D.4. W-2.1 (D.7) is a recommended ordering relative to E.1, not a code dependency.

**Merge-serialization map** (if a second contributor joins, this is the only clean split — never both clusters at once): the **executor batch-path cluster** (B.3, D.4/D.5, E.1, A.5's `sink.close()`) vs. the **controller/DB cluster** (B.1/B.2/B.5/B.6/B.7, D.1/D.2/D.4, E.2). `agent/server.py` and `db.py` are touched across waves — sequence, don't parallelize.

---

## Wave A — Stopgaps (patch release, ~1 week)

### A.1 · Close issue #3 (W-0.1) — Source: GH #3
Verified fixed at HEAD (commit `637a923`); regression tests pass (`tests/unit/test_alerts.py:350-434`). **Effort: S. Risk: none.**

### A.2 · Remove dead code (W-0.2) — Source: code review §F
Empty `tram/cluster/`, `tram/scheduler/` dirs (stale `.pyc` only); legacy pause API (`db.py:629-643`); `controller.stop()` no-op block. **Effort: S. Risk: none.**

### A.3 · Label the open issues (W-0.3) — Source: improvements doc Q4. **Effort: S.**

### A.4 · Allocator mitigation for #16 (W-2.4) — Source: GH #16 · RCA §#16
`MALLOC_ARENA_MAX=2` (and/or evaluate jemalloc via `LD_PRELOAD`) in `Dockerfile.worker`; flip `post_batch_cleanup` default to `True` for batch runs (`models/pipeline.py:1322`).
**Rollout notes (v2):** the default flip is a silent behavioral change to every existing pipeline — changelog entry required; perf sanity check on short-interval scheduled batches (gc + `malloc_trim` latency); image-level env means rollback = image rollback; per AGENTS.md update `.env.example`, `docs/deployment.md`, and Helm values. **Effort: S. Risk: S.**

### A.5 · Genuine leak fixes for #16 (W-2.2 part) — Source: GH #16 · RCA §#16
Content-hash (or size+mtime) keys for `_SCHEMA_CACHE`/`_MODULE_CACHE` (`asn1_serializer.py:177,180`, `protobuf_serializer.py:75-77`) + bounded LRU; asset sync skips unchanged content (`assets.py:132,149`) so mtime stops churning; executor calls `sink.close()` in `batch_run`'s finally (idempotent) — fixes the ClickHouse timer/buffer pinning (review A9).
**Verify:** `len(_SCHEMA_CACHE)` stays 1 across N runs in worker mode. **Effort: S-M. Risk: S.**

### A.6 · Dispatch outcome labeling + health hysteresis (W-3.6) — Source: GH #21 · RCA §#21
The conflation point is `dispatch()` collapsing `multi_dispatch`'s result to `accepted[0] or None` (`worker_pool.py:562-581`) — split "no healthy workers" from "dispatch attempt failed" and persist the real cause in run history. Add debounce so a single failed health probe doesn't mark a worker down (`:182-191`). **Prerequisite for E.2.** **Effort: S. Risk: S.**

**Wave A exit criteria:** 10-run soak plateaus (no per-run RSS growth); run history distinguishes no-capacity from dispatch-failure; `gh` board triaged. **Rollback:** revert the patch release; the default flip and env are individually revertable.

---

## Wave B — Correctness core (minor release, 3-5 weeks)

Critical path: B.1 → B.2 (week-1 quick wins) → B.3 → B.4/B.5/B.6 → B.7.

### B.1 · Alert CRUD persistence (W-1.3 / A6) — HIGH · Source: code review A6
Route `_save_alerts_data` through `controller.update()` (persist + proper stop/restart) instead of re-implementing deregister/register (`routers/pipelines.py:418-437`, `manager.py:115-116` saves a version, not the pipeline — edits vanish on restart). **Effort: S. Risk: S.**

### B.2 · Watcher lifecycle fix (W-1.4 / A7) — HIGH · Source: code review A7
`watcher/pipeline_watcher.py:64` calls `manager.stop_pipeline` (exists only on the controller); the AttributeError is swallowed and deleted pipelines keep streams/jobs alive. Pass the controller (or add a delegating façade), stop swallowing, persist watcher reloads via `db.save_pipeline`. **New test required:** watcher delete-path coverage doesn't exist. **Effort: S. Risk: S.**

### B.3 · Threaded batch path rework — merged W-1.2 (A2) + W-2.3 (backpressure) — HIGH · Source: code review A2 · RCA §#16
- **Root cause (A2):** threaded path submits chunks and advances the generator immediately (`executor.py:856-886`); `_post_read`/`mark_processed` run after the last chunk is *submitted*, not completed (`sftp/source.py:120-124`, `local/source.py:81-83`) — crash in the window = permanent data loss presented as processed. The staged-finalize machinery is **sink-side only** (`executor.py:310-316`) — there is no source-side finalize to reuse.
- **Root cause (OOMKill):** the same loop submits ALL source chunks to an unbounded `ThreadPoolExecutor` queue; `thread_workers=2` doubles the ~560 MiB peak → OOMKill.
- **Solution (one rework, two acceptance criteria):** (1) **deferred source-finalize hook** — move `_post_read`/`mark_processed` out of the connector generator into an explicit finalize API invoked by the executor after draining that source key's futures (connector API change across file sources); (2) **bounded in-flight cap** (~2× `thread_workers`, submit/drain loop) inside the same structure. Neither fix is correct alone: a cap without the finalize hook still allows post-read with futures pending; a drain-before-every-pull without the cap serializes the loop.
- **Verify:** new **real concurrency test** (existing `test_thread_workers.py` mocks `_process_chunk` and cannot catch either defect); mid-run failure leaves files unmarked and unmoved; `thread_workers=2` PGW repro peak < 900 MiB; throughput benchmark before/after.
- **Wave A review residual folded in:** the retry path rebuilds sinks per attempt while `batch_run`'s finally closes only the last-built set (`executor.py:836-845` vs `:854-857`), so failed attempts' sink instances (e.g. ClickHouse flush timers) leak — the rework must close sinks across retry attempts, and the A.5 executor tests' "closes sinks in finally" claim only covers the final attempt.
- **Wave B review residuals (verified, deferred):** (1) threaded *stream* mode still finalizes a file when the producer queues the next file's first chunk — up to `2× thread_workers` messages of the previous file can be unwritten (equal to pre-B.3 exposure, no new window); stream-mode finalize semantics need their own pass. (2) Sink-side staged finalize (`enable_safe_finalize`) remains sequential-batch-only — the threaded path never calls `_finalize_source_for_sinks`. (3) B.3's throughput benchmark and the `thread_workers=2` PGW repro (< 900 MiB peak) are pending kind-cluster verification.
- **Effort: L. Risk: M-H** (hot loop; this is the plan's highest-risk item). **Gates E.1.**

### B.4 · Syslog TCP framing (A12, moved out of W-5.4) — Source: code review A12 · telecom review
RFC 6587 framing for syslog-over-TCP (one `recv` per connection currently truncates/merges records, `syslog/source.py:199-205`). This is a confirmed correctness bug, not a domain gap. **Effort: M. Risk: S.**

### B.5 · Controller lifecycle RLock (B1/B2/B10, pulled ahead of E.2) — Source: code review B1/B2/B10 · re-confirmed by RCA §#21
One controller-level RLock around lifecycle transitions (trigger/update/delete/status) closes the trigger TOCTOU (`controller.py:315` vs `:460`), the CRUD deregister→register window (`:214-239`), and the manager thread-safety gap. **Must land before E.2** — the queue's single-claim mechanism otherwise inherits a duplicate-dispatch race. **Effort: S-M. Risk: S.**

### B.6 · Manager-restart double-dispatch guard (W-3.1's HIGH half, minimal scope) — Source: RCA §#17 new defect
After a manager restart, `_boot_load` re-schedules enabled count=1 streams while they deliberately keep running on workers (`controller.py:139-189`) → second concurrent instance → duplicate sink writes. Minimal guard now (adopt-or-skip semantics keyed on worker-reported live runs), with the full durable-record work in D.2. **Also fix the startup hysteresis window (Wave A review):** `WorkerPool.start()` probes once, so with the new threshold-2 debounce a worker that is down at manager boot reports healthy for one poll interval and boot-time dispatches are attempted against it (truthfully recorded as `dispatch_failed`) — the boot path should mark first-probe failures down. **Effort: M. Risk: M.**

### B.7 · Kafka at-least-once default (B6, housed in v2) — Source: code review B6 · telecom review (flagged by two independent reviews)
`enable_auto_commit: true` (`models/pipeline.py:93`) commits offsets for messages not yet sink-written — crash = loss. Flip the default to `false` and commit after sink success (or document the current default as at-most-once). Coordinate with parked item B4 (per-message `end_offsets`) and B3 (no `stop()`) — bundle if convenient. **Effort: M. Risk: M** (throughput/ordering semantics).

### B.8 · Small confirmed bugs (W-1.6) — Source: code review A5, A8
`rate_limit_rps: gt=0` validation + `TramError` in `_rate_limit` (A5); webhook `max_queue_size` — bounded queue, real 503 path, per-path registry collision fix (A8). **Effort: S each.**

**Wave B exit criteria:** idempotency parity across all execution modes (standalone, manager+worker, threaded) verified by the new concurrency tests; alert edits survive restart; watcher delete stops pipelines; Kafka default documented/flipped. **Rollback:** each item is an isolated revert; B.3 ships feature-flagged (`thread_workers` fallback path retained for one release if needed).

---

## Wave C — Security rollout (minor release, overlaps B tail)

### C.1 · Phase 1 — clients, exemptions, secrets (no enforcement)
- `_post_run_complete` and `_post_stats` (`agent/server.py:97-143, 146-157`) start sending `X-API-Key` (today no headers are sent at all).
- Add `/agent/health` (and probe endpoints) to the exemption set when the agent gets middleware — K8s liveness/readiness probes hit it keyless (`worker-statefulset.yaml:118-127`); without exemption, rollout = 401 → CrashLoopBackOff.
- **Rotate the committed defaults** (`helm/values.yaml:275` `apiKey: "tram-internal-2026"`, `:287` `authUsers: "admin:tram@2026"`), remove plaintext defaults from the chart (use `envSecret`, `manager-statefulset.yaml:100-105`), fail-closed guidance in docs.
- Ship a **warn-only mode** for enforcement on `/api/internal/*` (log would-be-rejected requests, don't 401).
**Effort: S-M. Risk: S.**

### C.2 · Phase 2 — enforcement (next release)
Enforce `X-API-Key` on `/api/internal/*` (remove from `EXEMPT_PREFIX`, `middleware.py:28`) and on the worker agent API (`agent/server.py:273-277` gets the same middleware pattern as ingress `:491-493`). Upgrade ordering: workers first (they now send the key), then manager enforcement. Also: `hmac.compare_digest` for the two non-constant-time compares (C3, `middleware.py:48`, `webhooks.py:40`); body-size limit on `/webhooks/*` **and** drop API-key-via-query-param (C4, `middleware.py:47`); validate ClickHouse table name as an identifier (C5, `clickhouse/sink.py:132`).
**Rollback:** enforcement flag reverts to warn-only. **Effort: S-M. Risk: M** (deploy coordination).

**Wave C exit criteria:** zero phantom FAILED runs during rollout; probes green; defaults rotated and out of the repo.

---

## Wave D — Visibility & stats (minor release, 3-4 weeks)

### D.1 · A13 slot-update fix — before D.2 — Source: code review A13 · RCA §#17
Per-slot SQL update (or optimistic concurrency) for `update_slot_run_id` (`db.py:902-926`). **Must precede D.2:** routing count=1 streams through placement semantics widens the duplicate-redispatch blast radius while A13 is unfixed. **Effort: S-M. Risk: S.**

### D.2 · Durable record for count=1 streams (W-3.1) — Source: GH #17 · RCA §#17
Route count=1 dispatch through the existing 1-slot placement machinery (`multi_dispatch` already supports `target_slots=1`, `worker_pool.py:486-521`; `_record_broadcast_placement`/`_restore_broadcast_placement`/reconciler all exist). **Design doc required before code** (v2): poll-vs-push source semantics under placement, lease/adoption rules on manager restart (today streams deliberately keep running, `controller.py:139-143`), idempotent re-dispatch, and the A13 ordering (D.1 first). **Wave B review additions (mandatory):** include liveness reconciliation for adopted count=1 streams — after B.6's adopt-or-skip guard, a worker death post-adoption leaves the pipeline stuck "running" with no re-probe (manual recovery today), and adoption preserves a stale config when the YAML changed during manager downtime. Use W-3.3's spec quality as the template. **Effort: M-L. Risk: M.**

### D.3 · View-layer completion (W-3.2) — after D.2 — Source: RCA §#17
Drop the `worker_pool is None` gate on the placement endpoint (`pipelines.py:136-139`); ungate the workers-table live probe from the single failed health poll (`worker_pool.py:310`). After D.2, the manager-mode half is mostly redundant — scope accordingly (standalone mode remains). **Effort: S. Risk: S.**

### D.4 · Merge live StatsStore into `/api/stats` (W-3.3) — Source: GH #22 · RCA §#22
Read `stats_store.all_active()` in `stats.py` (today zero references) into the 15m cards, current chart bucket, per-pipeline rows; add the completion-boundary guard (drop non-final payloads for runs already in run history); decide D4 (`records_out` inflation, `executor.py:561-565`) and retry-parity (stats not reset with ctx on retry, `executor.py:814`) **first**. **Effort: M. Risk: M.**

### D.5 · Standalone batch stats (W-3.4) — Source: RCA §#22
Wire the local `_run_batch` path with `PipelineStats` + `_LocalRun` exactly as `_stream_worker` does (`controller.py:806-823`, `:513` passes no stats today). **Effort: S-M. Risk: S.**

### D.6 · Transport + observability hardening (W-3.5) — Source: RCA §#17
Move `worker_pool.live_streams()`/`status()` fan-out off the event loop (`run_in_threadpool` or async httpx), parallelize per-worker probes (`health.py:241`, `worker_pool.py:346-357`); log heartbeat failures at WARNING + `MGR_STATS_MISSED_TOTAL` (currently DEBUG-swallowed, `agent/server.py:146-157`). **Effort: M. Risk: S-M.**

### D.7 · `json_flatten`/`explode` O(n²) fix (W-2.1) — Source: GH #18 · RCA §#18
Mirror `_apply_zip_groups`' base-copy pattern in `_apply_explodes` (`json_flatten.py:95-102`); **keep** `apply()`'s upfront copy (per-sink isolation + in-place `choice_unwrap`); preserve the empty-list + `keep_empty_rows` contract (`:88-91`); fix `explode.py` aliasing in **both** branches (lines 38-41), not just line 39. Touches no other wave's files — can be pulled into Wave A/B if capacity allows (independent, S-M, low risk, ~700s → seconds). **Effort: S-M. Risk: S.**

**Wave D exit criteria:** a 2-minute manager↔worker traffic block doesn't hide a count=1 stream; manager restart doesn't double-start it (extends B.6's guard); dashboard cards increment mid-run on kind with `TRAM_STATS_INTERVAL=5`. **Rollback:** D.2 feature-flagged (old dispatch path retained one release); D.4 is display-only and reverts cleanly.

---

## Wave E — Enhancements

### E.1 · ASN.1 `split_path`/`split_path_context` (W-4.1) — Source: GH #19 · RCA §#19
**Prerequisites (v2): after D.7 (value ordering) AND after B.3 (threaded-path rework — `parse()` semantics must be defined against the new loop).** Design completion per RCA §#19: extend `Asn1SerializerConfig` (`extra: "forbid"` rejects new keys today); require `record_chunk_size > 0` via pipeline-level validation (else silent no-op); define `parse()` for threaded runs; `deepcopy(context)` per emitted record; fail-loud on missing/non-list path; mutual exclusion with `split_records`; keep the fan-out lazy. **Effort: M. Risk: M.**

### E.2 · Queue manual runs (W-4.2) — Source: GH #21 · RCA §#21
**Prerequisites (v2): after B.5 (RLock) and A.6 (label split).** `queued_runs` DB table (run_id, pipeline, requested_at, yaml_snapshot, status, expires_at) following the broadcast-placement precedent; enqueue at the `controller.py:478-497` branch on genuine no-capacity only; drain inside `BatchReconciler` (loop authoritative, worker-restored events nudge; `queued → dispatching` claim transition under the B.5 lock); manual runs only; per-pipeline dedupe; TTL expiry → FAILED with distinct error; `controller.delete` purges; `_boot_load` re-arms and re-resolves. Do E2's `_upsert` helper (`db.py`) at latest now — don't write a 7th dialect-branching upsert. **Design doc required before code.** API gains `status: "queued"` / 202. **Effort: M-L. Risk: M.** Feature-flagged.

### E.3 · Templates UI consistency (W-4.3) — Source: GH #20 · RCA §#20
Independent, anytime. Put `#pl-tpl-deploy-view-btn` on the Detail-viewer header pattern (`pipelines.html:112-114`, `detail.html:191-198`); promote the dead `.shared-action-row` contract (`style.css:563-572`) for the row pair; rebuild the preview on the Detail viewer structure (real `modal-header` + `modal-body p-0` + `pre.detail-yaml-view`, shared 70vh cap — delete `style.css:1201-1203`); strip trailing newline (`templates.py:67` or `pipelines.js:347`); delete dead `.template-preview-dialog`/`.template-card*` CSS. Do **not** extend `.detail-action-btn` itself. Dark+light verification checklist in RCA §#20. **Effort: S-M. Risk: S.**

---

## Wave F — Domain gaps (roadmap)

| ID | Gap | Solution sketch | Effort |
|---|---|---|---|
| F.1 (W-5.1) | No counter delta/rate; no time-windowed aggregation | `rate`/`delta` transform with Counter32 wrap correction + windowed aggregate (window, watermark, late arrival) backed by a state store | L |
| F.2 (W-5.2) | No file-done semantics on PM collection | mtime-age/size-stability guard + `.done` suffix convention + min-age skip. **v2 split:** ship the guard, verify in target environments, **then** remove the PM-XML truncation hack in a follow-up — never in the same change | M |
| F.3 (W-5.4) | gNMI reliability + CORBA idempotency | gNMI reconnect loop + `once`/`poll` modes (bundle B3's missing `stop()` and parked B4's per-message `end_offsets` here); time-window component in the CORBA idempotency key (`corba/source.py:158-169`) | M |
| F.4 (W-5.3) | No source-timezone/DST in timestamp normalization | `source_timezone` param (`timestamp_normalize.py:44-65`) | S |

---

## Parked, with rationale (auditable against sources)

| Finding | Why parked |
|---|---|
| B3 — gNMI/Kafka no `stop()` | Real but stop-latency only (RCA refuted it as a #17 trigger); bundled with F.3 gNMI work |
| B4 — Kafka per-message `end_offsets` | Perf, not correctness; bundle with F.3/B.7 Kafka work |
| B5 — stale-config in-flight run survives `update()` | Bounded by at-least-once; revisit after B.3's rework changes in-flight semantics |
| B7 — `errors_last_window` unbounded between 30s snapshots | No contribution to #16's clean-run repro; fix opportunistically (deque maxlen) with D.5's stats work |
| B8 — `_add_column_if_missing` swallows all exceptions | Low severity; catch duplicate-column error per dialect when next touching db.py (E.2) |
| B9 — version-number race in `save_pipeline_version` | Low probability; unique constraint opportunistically with E.2's table work |
| B11 — `finalize_source` rename failure aborts a fully-written run | Low-medium; re-examine after B.3's finalize-hook design lands |
| D8 — `_pipeline_workers` unbounded growth | Manager-side only (not worker heap); bound opportunistically with D.2 |
| UI #2 — wizard disabled, YAML-only creation path | HIGH UX but strategic scope (restore wizard vs guided editor) — needs its own decision, tracked in ui-ux-review.md |
| UI #10 — run history no auto-refresh; CSV truncation | HIGH UX, contained; schedule with any wave D UI touchpoint |
| UI #6, #11-#13, a11y items | See `ui-ux-review.md` ranked table |
| SNMP trap community unverified; SNMPv3 privacy; no Counter64 trap-sink varbind; APScheduler UTC-only + `misfire_grace_time=60` | Minor individually, telecom-hardening items for the F-wave; see telecom-domain-review.md §6, §8, §11 |

Remaining code-review backlog (D1 DLQ spool, D2 callback retry, D3 breaker window, D5 Postgres recommendation, D7 sink connection pooling, E1-E5 boilerplate incl. the E2 `_upsert` scheduled with E.2): see `code-review.md` §D/§E.

---

## Test strategy mapping (v2)

**Existing guard tests that do NOT cover their wave's items (deliverables, not options):**
- `test_thread_workers.py` mocks `_process_chunk` — **cannot catch B.3's** mark-before-write or backpressure defects. B.3 must add a real concurrency test (mid-run failure → file unmarked/unmoved; in-flight cap honored).
- `test_processed_files.py` is standalone-only — B.7/B.3 need worker-mode idempotency integration tests.
- No watcher-delete test exists (B.2); no alert-restart round-trip test (B.1) — both would have caught their bugs.

**Perf verification repeatability:** the kind-based verifications (B.3's `thread_workers=2` A/B peak, A.4's 10-run soak, D.2's 2-minute traffic block) should be scripted against `scripts/deploy-kind-tram-dev.sh` and kept as repeatable gates, not one-shot manual runs.

**Environment caveat (AGENTS.md):** full-suite pytest (`pytest tests/ -q`) must run outside the sandbox — it blocks the asyncio wakeups FastAPI `TestClient` needs. All wave verifications that touch API/integration tiers are affected.

## Design-doc gates (v2)

**E.1, E.2, and D.2 require a short approved design doc before code** — the three items where implementing exactly as sketched can regress at-least-once semantics or K8s rollout behavior. Use D.4's specification style (boundary guard, retry parity, decision-first) as the template.
