# TRAM — Independent RCA for Open GitHub Issues

**Date:** 2026-09-15
**Method:** One independent root-cause analysis per open issue, each traced against current code with file:line evidence. The highest-impact claims were independently re-verified after the fact (marked ✅). Issue #24 is a feasibility-study proposal, not a defect — RCA does not apply and it is excluded.
**Labels:** `[CONFIRMED]` = traced in code; `[HYPOTHESIS]` = plausible, requires runtime confirmation.

---

## #3 — Alert cooldown consumed even when delivery fails

### Verdict: **Fully fixed at HEAD — close the issue.** ✅ verified

- `tram/alerts/evaluator.py:68-76`: `_set_cooldown` is called only inside `if fired:`; `fired` means "delivery succeeded" — `_fire_webhook` (98-131) calls `raise_for_status()` and returns True only after a 2xx; `_fire_email` (133-182) returns True only after `send_message` completes.
- Fix landed in commit `637a923` ("release: prepare v1.3.0") — the issue was filed against a pre-release state. File is byte-identical since.
- **No bypass exists:** `set_alert_cooldown` has exactly one production caller (`_set_cooldown`); `AlertEvaluator.check` is reached only via `PipelineManager.record_run`.
- **Regression tests already exist and pass** (`tests/unit/test_alerts.py`, 21 passed): HTTP 500, connect error, and SMTP failure all assert no cooldown.
- Residual (non-blocking): cooldown check-then-fire is unsynchronized (narrow double-fire window `[HYPOTHESIS]`); worth adding a test for unknown action types.

**Action:** close #3 referencing `637a923` and `tests/unit/test_alerts.py:350-434`.

---

## #16 — Worker pods retain large anonymous heap after heavy CDR batches

### Verdict: **~85-90% allocator retention (fragmentation after a ~560 MiB transient peak) + one confirmed genuine per-run leak (`_SCHEMA_CACHE`)** ✅ verified

| # | Cause | Status | Est. share |
|---|---|---|---|
| 1 | Allocator retention: pymalloc arenas + glibc heap fragmentation after a ~560 MiB peak (65 MB BER input → ~644 MB decoded JSON; whole-file `fh.read()` at `sftp/source.py:110`, full-copy BER slices at `asn1_serializer.py:117-126`, recursive `_to_json_safe` rebuild at `:33-53`, plus per-transform copies) | `[CONFIRMED]` via code trace + captured pod monitoring (44 MiB baseline → 563 MiB peak → 171-374 MiB retained; sub-linear growth across runs = fragmentation signature) | ~85-90% |
| 2 | **`_SCHEMA_CACHE` unbounded per-run growth**: key includes file mtime (`asn1_serializer.py:177,180`) ✅; `sync_assets` runs before every run (`agent/server.py:406`) and rewrites schemas unconditionally (`assets.py:132,149` `dest.write_bytes(r.content)`) ✅ → new mtime → new `asn1tools.compile_files()` entry (~2 MiB each) retained forever, immune to `gc.collect()` | `[CONFIRMED]`, measured ~2.0-2.2 MiB per entry | ~2 MiB/run, linear and unbounded |
| 3 | Deferred cyclic garbage past the automatic GC point | `[CONFIRMED]` behaviorally — explains the gc.collect() delta (~55-70 MiB) | transient |
| 4 | ClickHouse sink daemon-`Timer` self-rescheduling + `close()` never called (review A9) | Real defect but **not active in this repro** (repro pipelines use local/sftp sinks) | 0 here; latent for ClickHouse pipelines |
| 5 | `WorkerPool._pipeline_workers` growth (review D8) | **Manager-side only** — not in the worker process | 0 for worker heap |
| 6 | `errors_last_window` (review B7) | **No contribution to this repro** (clean run, no errors; cleared each 30s window, dies with the run). Review B7's unbounded-accumulation concern remains mechanically valid for error-storm streams between 30s snapshots. | 0 here |

- **Why `post_batch_cleanup` only partially works:** `gc.collect()` frees the cyclic garbage; `malloc_trim(0)` only returns pages at the top of glibc's main heap — it cannot release pymalloc arenas pinned by a few live objects (the schema entries) or compact intra-arena fragmentation.
- **OOMKilled "run disappeared before callback" is a consequence, not a co-defect:** the reconciler's `is_run_active` probe legitimately reports the run gone after the pod restarts. The OOM trigger is the threaded batch path (`executor.py:856-877`): producer submits ALL source chunks to an unbounded `ThreadPoolExecutor` queue — `thread_workers=2` roughly doubles the ~560 MiB peak → crosses the 1 GiB limit.
- **Fixes (ranked):** (1) key `_SCHEMA_CACHE`/`_MODULE_CACHE` by content and bound the cache; make asset sync skip unchanged content; (2) call `sink.close()` in `batch_run`'s finally; (3) cap in-flight futures at ~2× thread_workers (backpressure); (4) stream source reads instead of whole-file; (5) default `post_batch_cleanup` on for batch runs; (6) `MALLOC_ARENA_MAX=2` and/or jemalloc in `Dockerfile.worker` — the only mitigation that routinely returns fragmented pages.
- **Verify:** `len(_SCHEMA_CACHE)` stays 1 across N runs post-fix; smaps pre/peak/post-cleanup deltas; 10-run soak plateaus; thread_workers=2 A/B with bounded in-flight chunks stays under 900 MiB.

---

## #17 — Cluster/Detail stream visibility drops for long-running manager-mode streams

### Verdict: **Structural defect — single-dispatch (count=1) streams have no durable liveness record; visibility rests on two ephemeral inputs sharing one failure-prone channel.** ✅ verified (`controller.py:754-777`)

- **The disappearing stream must be single-dispatch.** Every non-push source (kafka, gnmi, mqtt, websocket, sql, rest, corba, snmp_poll — `models/pipeline.py:1349-1356` assigns default `count=1`) takes the path at `controller.py:756-777`, which records **only** `self._stream_run_ids[name] = [run_id]` in manager memory — no placement, no DB row (contrast the multi-dispatch branch `:720-754`). Placement-backed streams cannot vanish from the Cluster list (rows render unconditionally, `_stream_views.py:238-241`).
- **Visibility for count=1 streams = `stats_store.all_active()` (90s TTL, in-memory, `stats_store.py:49-59`) + on-demand live probe of worker `/agent/status`** — both travel over the same manager↔worker agent channel. Interrupt both for >90s → the stream vanishes from every runtime view while pipeline status (manager memory) still says "running". The 90s TTL is the only time-based decay — matches "runs fine, then vanishes after a few minutes". `[CONFIRMED mechanism]`
- **Trigger of the channel interruption `[HYPOTHESIS — needs runtime confirmation]`:** (a) manager event-loop starvation — `worker_pool.live_streams()`/`status()` do serial blocking HTTP fan-out (5s timeout per worker) inside async endpoints (`health.py:241`), with the UI polling every 10s; (b) slow-but-alive worker agent (GC pauses, CPU pressure); (c) transient network issues.
- **Why the live-fallback fix didn't fully solve it:** (1) Detail placement endpoint **404s for count=1 streams** (`pipelines.py:136-139` gates the synthetic view on `worker_pool is None`); (2) the workers-table fallback is health-gated — one failed probe zeroes it (`worker_pool.py:310`); (3) the fallback rides the same channel whose failure is the trigger; (4) the fallback's own probes are the blocking-async calls.
- **Review finding A13 (`update_slot_run_id` lost update): REFUTED as the disappearance cause** — the reconciler and view layer are both live-first with worker-key fallback (`reconciler.py:99-102, 151-155`), so a drifted run_id renders stale, never missing. A13's real harm is different: under the same channel-down conditions, a lost update can cause **duplicate stream redispatch** (`controller.py:1124-1177`; the worker's 409-duplicate guard doesn't fire on a new run_id). Real bug, wrong symptom.
- **Review finding B3: REFUTED as heartbeat killer** — worker stats are a 30s timer loop (`agent/server.py:227-229`), independent of message flow. (B3 remains real for stop-latency.)
- **Heartbeat failures are silent** (`_post_stats` swallows at DEBUG, `agent/server.py:146-157`) — zero log output at INFO on either side.
- **NEW related defect `[CONFIRMED]`:** after a manager restart, `_boot_load` re-schedules enabled streams while count=1 streams deliberately keep running on workers (`controller.py:139-143, 188-189`) → the manager dispatches a **second concurrent instance** → duplicate processing. Placement streams are protected by DB restore; count=1 streams are not.
- **Fix (ranked):** (1) give count=1 streams a durable record (1-slot placement through the existing machinery, or a `dispatched_streams` table + stream reconciler) — makes the symptom structurally impossible; (2) drop the `worker_pool is None` gate so Detail renders manager-mode non-placement streams; (3) log heartbeat failures at WARNING + `MGR_STATS_MISSED_TOTAL` metric; (4) move blocking fan-out off the event loop (`run_in_threadpool` or async httpx) and parallelize per-worker probes; (5) fix A13 separately (per-slot SQL update) for the duplicate-redispatch risk.
- **Verify:** repro per issue; when it disappears, curl the worker's `/agent/status` from the manager pod (distinguishes worker-not-reporting from view bug); watch `MGR_PIPELINE_STATS_RECEIVED_TOTAL` rate ~90s before disappearance; post-fix, a 2-minute manager↔worker traffic block must not hide the stream and must not double-dispatch after manager restart.

---

## #18 — `json_flatten`/`_apply_explodes` deepcopy is O(n²)

### Verdict: **Confirmed, no fix in code; the issue's proposed fix is partially correct.**

- The delete-after-copy pattern exists exactly as described: `json_flatten.py:95-102` (`deepcopy(row)` inside the per-element loop, path deleted after) and `explode.py:34-37` (identical). `[CONFIRMED]`
- Complexity trace for the 1×13,199×185 record: Phase A (explode `measInfo`) = 185 full-record copies ≈ 1.9×10⁷ node copies; Phase B adds ~9.3×10⁵ more (~70× amplification). At 10-35 µs/deepcopy-node, the observed ~700s sits in the plausible 200-700s band. `[CONFIRMED algorithm; timing hypothesis]`
- `_apply_zip_groups` (`json_flatten.py:142-148`) **already uses the correct base-copy pattern** — the fix should mirror it: one `base = deepcopy(row)`, delete the path once, then N slim copies.
- **Issue's claim that the upfront `apply()` deepcopy is "unnecessary": REFUTED.** The upfront copy (`:75`) protects (1) per-sink transform isolation — the executor passes the same record objects to every sink's transform chain (`executor.py:546-559`), (2) `_apply_choice_unwrap` which mutates rows in place (`:176-182`), (3) DLQ envelopes capturing original records (`executor.py:362-365`). Keep it.
- Caveats: empty-list + `keep_empty_rows` semantics must be preserved (`:88-91`); `explode.py:39` inserts the **original** element without deepcopy (aliasing inconsistency vs `json_flatten.py:99`) — fix alongside.
- No other transforms share the quadratic pattern (drop, coalesce_fields, value_map, cast, rename, select_from_list, unnest are all O(records)). `[CONFIRMED by grep]`
- **Verify:** micro-benchmark 185×71 fixture (minutes → seconds); regression tests for nested/multiple explode paths, empty-list case, two-sink isolation.

---

## #19 — ASN.1 `split_path`/`split_path_context` for single-dict BER inputs

### Verdict: **Both factual claims confirmed; the design is directionally right but "changes confined to the serializer" is refuted, and the context-sharing design has a real aliasing hazard.**

- `parse_chunks` yields a single-element iterator when `split_records` is false (`asn1_serializer.py:235`), so `record_chunk_size` never engages — confirmed; and without `record_chunk_size`, `_process_chunk_incrementally` is never called (`executor.py:907`), so `split_path` alone would **silently no-op**. `[CONFIRMED]`
- The fan-out axis gap is real: the ASN.1 serializer's only fan-out is the BER frame boundary; a single-frame statsfile bypasses all chunking machinery and feeds #18's quadratic explode.
- **Not confined to the serializer:** `models/pipeline.py` uses `extra: "forbid"` — `Asn1SerializerConfig` will reject the new keys until extended; the `record_chunk_size` coupling needs a pipeline-level validator; and **threaded batch runs bypass `parse_chunks` entirely** (`executor.py:856-866` calls `parse()`), so `split_path` must be defined for `parse()` too or multi-threaded pipelines diverge.
- **Context aliasing hazard `[CONFIRMED RISK]`:** sharing one context dict across N records breaks the moment any transform mutates in place (the `BaseTransform` contract permits it). Current built-ins are copy-on-write, but user plugins aren't. Fix: `deepcopy(context)` per emitted record — negligible cost at header scale.
- **#18 vs #19 relationship:** complementary, both required for the full claim, with #18 doing most of the time work and #19 contributing bounded memory + incremental sinking. #19 alone does **not** fix the time (per-group quadratic survives); #18 alone doesn't bound memory.
- Sanity note: the issue's phrasing "makes json_flatten catastrophic (see #18)" is backwards in one sense — after #18, json_flatten is linear and #19's value is memory/chunking.

---

## #20 — Templates page action row and preview modal don't match shared components

### Verdict: **Both symptoms confirmed; the issue's RCA is partially correct — the list-row buttons were already migrated by the patches; the missed button is the preview-header Deploy, and the complete shared contract the issue asks for already exists as dead CSS.**

- **Symptom 1 (Deploy narrower/shorter than View):** two co-existing causes. (a) `#pl-tpl-deploy-view-btn` (`pipelines.html:112-114`) still uses the pre-patch pattern — no `detail-action-btn`, legacy `me-1` margin, no `<span>`, no `title` — rendering at Bootstrap `.btn-sm` defaults instead of the 12px contract. (b) Systemic: `.detail-action-btn` (`style.css:557-562`) only normalizes icon/text layout + font size — no min-width/height/padding — so content-sized buttons can never render as a balanced pair. Meanwhile **`.shared-action-row .detail-action-btn { width:100%; min-height:34px; justify-content:center }` exists at `style.css:563-572` and is used by zero markup** (grep-verified) — the component was written and never wired up. `[CONFIRMED]`
- **Symptom 2 (preview spacing ≠ shared viewer):** the preview is structurally not the Detail viewer: page-specific height cap `calc(100vh - 12rem)` overriding the shared 70vh (`style.css:1201-1203` vs `:662`); a pseudo-header that is not a real `modal-header` (`.p-3` with `!important` beats `.detail-modal-header`'s 10px padding; border never paints; wrong title typography); double header stack (filters header stays visible above the pseudo-header); and a persistent trailing blank line from the unstripped template text (`templates.py:35,67` raw `read_text()`, all bundled templates end in `\n`). Dead CSS (`.template-preview-dialog`, `.template-card*`) confirms the drift trail. `[CONFIRMED]`
- **Fix:** put the preview-header Deploy on the Detail-viewer header pattern (`detail.html:191-198`); promote `.shared-action-row` for the row pair; rebuild the preview on the Detail viewer structure (real `modal-header` + `modal-body p-0` + `pre.detail-yaml-view`, delete the bespoke cap); strip the trailing newline; delete the dead CSS. Do **not** extend `.detail-action-btn` itself (live global contract).
- **Theme check:** the Templates path is token-clean — the defect is structural, not chromatic. Verify in dark + light with the checklist (equal row pair, single header band with visible border, same-YAML-same-box comparison vs Detail > Versions, behavioral regressions for Back/Deploy/route alias).

---

## #21 — Optionally queue manual run requests when no healthy workers

### Verdict: **Current failure path is state-consistent (no phantoms/leases), but has 4 confirmed bugs the queue must not inherit; the queue itself is a well-supported extension.**

- **Traced failure path:** `POST /run` returns 200 "triggered" immediately (`pipelines.py:324-340`) → `trigger_run` (`controller.py:310-319`) → `_run_batch` manager+worker branch (`:466`) → `dispatch()` filters by health (`worker_pool.py:251-271`) → on `None`: FAILED `RunResult` "No healthy workers available for dispatch" persisted to run history, pipeline status → `error`, K8s service deactivated (`:478-497`). No `_active_batch_runs` entry, no lease, no placement — clean.
- **Bugs found in the path `[CONFIRMED]`:**
  1. **Misleading error label** — `dispatch()` returns `None` both for "no healthy workers" AND for "healthy worker selected but POST /agent/run failed" (`worker_pool.py:445-454`); run history conflates them. **Prerequisite fix for the queue** (split the outcome — `multi_dispatch` already returns distinguishable status).
  2. **TOCTOU on the running guard** (`controller.py:315` vs `:460`, no lock) — matches review finding B1; duplicate dispatch possible.
  3. **Single-probe health flap** — one failed probe marks a worker down, no hysteresis (`worker_pool.py:182-191`); a 5s network blip produces "no healthy workers" failures. A queue built on this signal without debouncing will queue unnecessarily.
  4. Optimistic health init `ok: True` (`:62-64`) — minor, bounded by `start()`'s synchronous first poll.
- **Queue design (grounded):** DB table (`queued_runs`: run_id, pipeline, requested_at, yaml_snapshot, status, expires_at) following the broadcast-placement persistence precedent — NOT in-memory (manager restarts would drop user requests). Enqueue at the `controller.py:478-497` branch but only on genuine no-capacity (after BUG 1). Drain in `BatchReconciler` (already a 10s loop owning lease/lost semantics) — loop authoritative, health-restored events only nudge. **Manual runs only** (scheduled runs retry naturally on interval; queuing them would flood on outages). Per-pipeline dedupe, TTL expiry → same FAILED RunResult with distinct error. `controller.delete` purges queued entries; `_boot_load` re-arms the drain.
- **Risks:** user confusion between queued/failed states (needs #22-visible distinction); stale YAML dispatching after downtime; duplicate drain (needs single claim transition); building on flappy health (BUG 3).

---

## #22 — Dashboard batch stats are completion-based, not live

### Verdict: **The live-stats pipeline for batch runs already exists end-to-end up to the manager's `StatsStore`; the gap is the last mile — `/api/stats` never reads it.** ✅ verified (zero `stats_store` references in `stats.py`)

- **Write side (used today):** run finishes → `/api/internal/run-complete` → `_finalize_batch_result` → `manager.record_run` → `run_history` row. `/api/stats` (`stats.py:34-114`) filters `run_history WHERE finished_at >= :since` or scans in-memory run deques — **neither path touches the StatsStore**. Until finalization, a run contributes zero to every dashboard number. `[CONFIRMED]`
- **Live side (exists, unused):** worker creates `PipelineStats` per run and passes it into `executor.batch_run(..., stats=...)` (`agent/server.py:348-352, 407`); `_stats_loop` emits every 30s **including batch runs** (`:171` has no schedule filter); POSTs reach `manager.stats_store.update` with cumulative per-run totals. Consumers today: placement views (stream-gated), cluster streams, load scoring, reconciler staleness — nothing feeds `/api/stats`. This matches the recorded design intent (`docs/archive/v1.3.0-plan.md:21-22`) — the machinery was built for streams; #22 is the deferred last mile.
- **Mode split:** manager+worker mode = read-side fix only; **standalone mode has no emission either** (local `_run_batch` passes no `stats=` — `controller.py:513`; only `_stream_worker` wires `_LocalRun`). Both modes show the symptom; the fix differs.
- **Inherited defects the fix must handle `[CONFIRMED]`:**
  - `records_out` inflation (review D4, `executor.py:561-565`) — live and final share the same code, so a live fix gets parity for free, but the inflation remains;
  - **retry accumulation mismatch** — `on_error: retry` resets `ctx` (`executor.py:814`) but not the worker's `stats` object → live totals can exceed final DB totals (a card could go *down* at completion);
  - **completion-boundary resurrection race** — a pre-completion snapshot POST can land after `is_final` removal → phantom live entry until staleness (≤90s); guard: drop non-final payloads for runs already in run history.
- **Fix (recommended Option A):** merge `StatsStore.all_active()` into `/api/stats` cards/chart/per-pipeline rows + wire standalone batch stats through `_LocalRun` (mirroring `_stream_worker`). Ordering is safe (worker sends `is_final` before run-complete; manager removes store entry before writing DB). Option B (UI-side poll of stream-shaped views), C (Prometheus — process-local, wrong shape), D (on-demand `/agent/status` fan-out — blocking, per-viewer load) rejected as primary paths.
- **Verify:** unit tests for the read-side merge (fresh/stale/finalized entries), standalone wiring, the resurrection guard, retry parity; integration on kind with a >2-min batch and `TRAM_STATS_INTERVAL=5` asserting mid-run card increments and no double-count at the boundary.

---

# Cross-cutting reconciliation (RCA vs. the earlier code review)

| Review finding | RCA outcome |
|---|---|
| A13 (`update_slot_run_id` lost update) — suggested as #17 root-cause candidate | **Refuted for #17's symptom** (live-first matching masks it); confirmed real but its harm is **duplicate redispatch** under the same conditions — reclassify |
| B7 (`errors_last_window` unbounded) | **Qualified** — no contribution to #16's repro (clean run); mechanically still valid for error-storm streams between 30s windows |
| A9 (ClickHouse Timer leak) | Confirmed real but **not active in #16's repro** (local/sftp sinks); latent for ClickHouse pipelines — fix via `sink.close()` in executor |
| D8 (`_pipeline_workers` growth) | Confirmed but **manager-side only** — irrelevant to worker heap |
| B1 (trigger TOCTOU) | Independently re-confirmed by the #21 RCA (same trace) |
| D4 (`records_out` inflation) | Confirmed and inherited by #22's live-stats fix (parity free, inflation remains) |
| A3 (run_id discarded on retry) | Related #22 finding: worker `stats` also not reset on retry — same retry-loop family |

**New defects found during RCA (not previously tracked anywhere):**
1. Manager restart double-dispatches count=1 streams still running on workers → duplicate stream instances (`controller.py:139-189`, #17 RCA) — HIGH
2. `_SCHEMA_CACHE`/`_MODULE_CACHE` mtime-churn leak (~2 MiB/run, unbounded, gc-immune) (`asn1_serializer.py:177,180` + `assets.py:132,149`, #16 RCA) — MEDIUM
3. Threaded batch path has no backpressure on in-flight chunks → thread_workers=2 doubles peak memory → OOMKill (`executor.py:856-877`, #16 RCA) — HIGH
4. "No healthy workers" error conflates no-capacity with dispatch-failure (`worker_pool.py:445-454`, #21 RCA) — MEDIUM
5. Worker heartbeat failures swallowed at DEBUG — zero operator visibility (`agent/server.py:146-157`, #17 RCA) — MEDIUM
6. Blocking HTTP fan-out inside async manager endpoints (`health.py:241`, `worker_pool.py:346-357`, #17 RCA) — MEDIUM

# Priority synthesis

1. **Close #3** (already fixed, tests exist) — frees the board.
2. **#17 + #16 share the deepest fixes:** durable record for count=1 streams (kills the visibility class AND the restart double-dispatch), `_SCHEMA_CACHE` content-keying, chunk backpressure, event-loop offload.
3. **#18 is a small, high-payoff fix** (mirror the existing zip_groups base-copy pattern; keep the upfront copy; fix explode.py aliasing) — ~700s → single-digit seconds.
4. **#19 needs design completion** (model fields, chunk-size coupling, threaded-path semantics, context deepcopy) before implementation; do it after #18.
5. **#22 is a contained read-side merge + standalone wiring**, gated on deciding the D4 inflation and retry-parity semantics.
6. **#21 should start with the BUG 1 label split** (small fix, immediately improves run-history accuracy); the queue itself follows the broadcast-placement precedent.
7. **#20 is a contained UI fix** (promote the dead `.shared-action-row`, converge the preview on the Detail viewer structure) with no backend impact.
