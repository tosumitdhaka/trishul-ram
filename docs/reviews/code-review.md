# TRAM Code Review — Backend (`tram/`, ~24K lines Python)

**Date:** 2026-09-15
**Scope:** Full read of `tram/pipeline/`, `tram/persistence/`, `tram/models/`, `tram/api/`, `tram/agent/`, `tram/watcher/` and the highest-risk connectors, plus supporting infrastructure and `tests/`.
**Method:** Line-by-line static review with traced file:line evidence for every claim. The highest-severity findings were independently re-verified against the code after the review.

**Finding labels:** `[CONFIRMED BUG]` = exact fault path traced; `[LIKELY BUG]` = traced but requires unlucky timing; `[DESIGN ISSUE]`, `[BOILERPLATE]`, `[GAP]`, `[SECURITY]` as marked.

---

## A. Confirmed bugs (exact fault path traced)

### A1. [CONFIRMED BUG] `skip_processed` is silently disabled in manager+worker mode — idempotency only works standalone
- **Evidence:** `tram/agent/server.py:354` — `executor = PipelineExecutor()` (no `file_tracker`). `tram/pipeline/executor.py:228-229` — tracker injected into source config only `if self._file_tracker is not None`. `tram/daemon/server.py:26-41` — the worker branch never constructs a `TramDB`/`ProcessedFileTracker` (that's manager-only, `tram/api/app.py:175-179`). `tram/connectors/sftp/source.py:82` — `if self.skip_processed and self._file_tracker:` → False → no skip, and `:121` → never marked.
- **Impact:** Every file-source pipeline with `skip_processed: true` (and no `move/delete_after_read`) **reprocesses every file on every run** when dispatched to a worker. For a mediation system this means duplicate CDRs/records — the exact failure the feature exists to prevent. No warning is logged anywhere.
- **Severity: HIGH**
- **Verification: independently re-verified** — grep confirms no `file_tracker` anywhere in `tram/agent/server.py`.

### A2. [CONFIRMED BUG] File sources move/delete/mark files *before* sink writes complete when `thread_workers > 1`
- **Evidence:** `tram/pipeline/executor.py:856-867` — threaded batch path submits `(raw, meta)` to a pool and immediately advances the source generator. `tram/connectors/sftp/source.py:120-124` — after the last chunk is *submitted* (not processed), the generator resumes and calls `_post_read` (move/delete) then `mark_processed`. Same in `local/source.py:81-83`.
- **Impact:** Crash (or `on_error: continue` sink failure) after the file is moved but before the worker threads finish writing → **permanent data loss** while the system presents the file as processed. The staged `.tmp`/`finalize_source` safety net only engages in the single-threaded path (`executor.py:898` sets `enable_safe_finalize` only there), so threaded runs get no atomic-commit protection at all.
- **Severity: HIGH** (silent at-least-once violation)
- **Verification: independently re-verified** — read `executor.py:840-949` and `sftp/source.py:85-134`; the generator advances on `pool.submit` without waiting on futures. See "Reconciled contradiction" below.

### A3. [CONFIRMED BUG] Batch-level retry discards the caller-supplied `run_id`
- **Evidence:** `tram/pipeline/executor.py:814` — on retry, `ctx = PipelineRunContext(pipeline_name=config.name)` with **no run_id**, so a fresh UUID4 is generated. `tram/pipeline/controller.py:317` returns the original `run_id` to the API caller; `_active_batch_runs` / worker callbacks key on the original.
- **Impact:** After a retry, `RunResult.run_id ≠` the run_id the user got from `POST /{name}/run`, and the run history/counters from the failed first attempt are thrown away (new ctx). Run lookup by returned run_id 404s.
- **Severity: MEDIUM**

### A4. [CONFIRMED BUG] One shared rate limiter across all pipelines
- **Evidence:** `tram/pipeline/controller.py:82` — controller builds **one** `PipelineExecutor` for all local runs. `executor.py:190-217` — `self._tokens`/`self._last_refill` are instance state; every `_rate_limit(rps)` call from *any* concurrent pipeline drains/refills the same bucket.
- **Impact:** Two concurrently running pipelines each get roughly half their configured RPS (or worse with different `rps` values interleaved — the refill rate depends on which caller ran last). Per-pipeline `rate_limit_rps` is simply wrong under concurrency.
- **Severity: MEDIUM**

### A5. [CONFIRMED BUG] `rate_limit_rps: 0` crashes the run with ZeroDivisionError
- **Evidence:** `tram/models/pipeline.py:1311` — `rate_limit_rps: float | None = None` with **no `gt=0` validation**. `executor.py:210` — `self._tokens = min(0, …) = 0.0`; `executor.py:216` — `sleep_time = (1.0 - self._tokens) / rps` → ZeroDivisionError. The exception is not `TramError`, so it propagates through `_process_records`/`_process_chunk` (both catch `TramError` only) and kills the whole run/stream (`controller.py:515-518` / `_stream_worker` error path).
- **Severity: MEDIUM** (one bad config value kills the pipeline with an opaque crash)

### A6. [CONFIRMED BUG] Alert-rule edits via API are never persisted to the pipeline registry
- **Evidence:** `tram/api/routers/pipelines.py:418-437` — `_save_alerts_data` does `manager.deregister` + `manager.register(config, yaml_text=new_yaml)`. `register` (`tram/pipeline/manager.py:115-116`) only calls `db.save_pipeline_version`, **not** `db.save_pipeline`. Boot load reads from `registered_pipelines` (`controller.py:175`, `db.get_all_pipelines`).
- **Impact:** Alert rules added/edited/removed via `POST/PUT/DELETE /{name}/alerts` **disappear on daemon restart**. Also, re-registering resets state to `stopped` while a live APScheduler job still exists (status lies until next run), and this path bypasses `controller.update()`'s stop/restart and DB persistence entirely.
- **Severity: HIGH** (silent loss of user config)

### A7. [CONFIRMED BUG] PipelineWatcher calls a method that doesn't exist; deleted pipelines keep running; reload doesn't persist
- **Evidence:** `tram/api/app.py:98-99` passes `manager=controller.manager` (a `PipelineManager`). `tram/watcher/pipeline_watcher.py:64` calls `manager.stop_pipeline(name)` — **`PipelineManager` has no `stop_pipeline`** (see `manager.py:75-237`; the method exists only on the controller, `controller.py:271`) → AttributeError swallowed by `except Exception: pass` at `:65-66`, then deregister proceeds.
- **Impact:** On file delete: stream threads and APScheduler jobs are never stopped — the "deleted" pipeline keeps consuming/sinking until process restart. Additionally `_reload` (:73-83) only re-registers in memory (no `db.save_pipeline` → changes revert on restart) and resets a running pipeline's status to `stopped` while its execution continues on the old config object.
- **Severity: HIGH**
- **Verification: independently re-verified** — grep confirms `stop_pipeline` appears in the watcher and controller but not in `manager.py`.

### A8. [CONFIRMED BUG] Webhook source: `max_queue_size` is dead config; backpressure impossible
- **Evidence:** `tram/connectors/webhook/source.py:14,46` — `queue.SimpleQueue` is **unbounded** and its `put_nowait` never raises. `tram/api/routers/webhooks.py:51-54` — the `except` → 503 "queue full" branch is unreachable. `max_queue_size` (source.py:32, models `WebhookSourceConfig`) is never used.
- **Impact:** If webhook ingress outpaces the pipeline, memory grows without bound (OOM under flood); the documented message cap does nothing. Also, two pipelines on the same `path` silently overwrite each other's queue in `_WEBHOOK_REGISTRY` (source.py:49-50).
- **Severity: MEDIUM-HIGH**

### A9. [CONFIRMED BUG] ClickHouse sink `close()` is never called by the executor; flush timer leaks forever
- **Evidence:** `tram/connectors/clickhouse/sink.py:91-100` — docstring says "Called by executor on stream stop", but no sink `.close()` call exists anywhere in `tram/pipeline/` or `tram/agent/`; `BaseSink` (`tram/interfaces/base_sink.py`) has no close lifecycle. The `_timer_flush` → `_schedule_flush` loop (sink.py:66-79) reschedules itself forever.
- **Impact:** (1) On stream stop/pod restart, buffered rows (< batch_size) are lost — the `batch_flush_on_stop` config is dead. (2) Every ClickHouse sink instance is kept alive forever by its own daemon `Timer`, re-flushing empty buffers every 2s — a thread+object leak per pipeline run.
- **Severity: MEDIUM**

### A10. [CONFIRMED BUG] File-sink rolling state is not thread-safe (`thread_workers > 1` / `parallel_sinks`)
- **Evidence:** `tram/connectors/local/sink.py:60-63, 116-148` (and the identical structure in `sftp/sink.py:63-66, 154-195`): `self._states` / `_current_paths` / `_part_counters` are plain dicts mutated via read-check-write sequences. Two worker threads processing chunks of the **same source file** (e.g. `read_chunk_bytes > 0`) race: both see `state is None`, both allocate part_index 1, both append to the same rendered filename → interleaved/corrupt output.
- **Impact:** Corrupted output files under a documented concurrency feature. (Different files → different state keys → mostly safe; same file → real corruption window.)
- **Severity: MEDIUM**

### A11. [CONFIRMED BUG] RateLimitMiddleware eviction is ineffective (slow memory leak)
- **Evidence:** `tram/api/middleware.py:124-126` — `self._windows = {k: v for … if v}` only removes **empty** deques, but old timestamps are only popped inside the per-IP lock when that *same IP makes a new request* (:107-109). A one-request IP leaves a permanently non-empty 1-element deque that is never evicted.
- **Impact:** Unbounded growth proportional to unique client IPs (scan traffic, spoofed sources). The intended filter should be `v and v[-1] >= now - window`.
- **Severity: LOW-MEDIUM**

### A12. [CONFIRMED BUG] Syslog TCP mode: one `recv` per connection, no framing
- **Evidence:** `tram/connectors/syslog/source.py:199-205` — `raw = conn.recv(self.buffer_size)` then `conn.close()`. TCP is a byte stream: messages larger than one segment are truncated, and multiple newline-framed messages (RFC 6587) delivered in one segment are merged into one record.
- **Impact:** Wrong/parsed-garbage records for any non-trivial TCP syslog sender. UDP path is fine.
- **Severity: MEDIUM**

### A13. [CONFIRMED BUG] `update_slot_run_id` read-modify-write clobbers concurrent slot updates
- **Evidence:** `tram/persistence/db.py:902-926` — reads the whole placement (all slots), mutates one slot, writes back the whole `slots_json`. Called from `on_pipeline_stats` (`controller.py:1102-1109`) which runs on the API event loop; concurrent stats payloads for different slots of the same placement (also vs. `PlacementReconciler.run_once`, which writes via `update_broadcast_placement_status` at `reconciler.py:226-231`) lose updates.
- **Impact:** Lost slot state → reconciler may consider a live slot stale → spurious redispatch of a stream → duplicate stream instances writing to sinks.
- **Severity: MEDIUM**

---

## B. Likely bugs / races (traced, timing-dependent)

### B1. [LIKELY BUG] `trigger_run` TOCTOU → duplicate concurrent runs
`controller.py:310-319` checks `state.status == "running"` then submits to `_thread_pool`; `_run_batch` (:454-460) re-checks and sets `"running"` on the worker thread with **no lock**. Two near-simultaneous `POST /{name}/run` both pass → two concurrent runs of the same pipeline (APScheduler's `max_instances=1` does not guard the thread-pool path). **Severity: MEDIUM**

### B2. [LIKELY BUG] No concurrency control on pipeline CRUD
`controller.update` (`controller.py:214-239`): `deregister` → `register` window. Two concurrent `PUT /{name}` (FastAPI sync handlers run in a threadpool): the second's `deregister` may hit the first's re-registered state or race `_stop_execution` — best case 500, worst case a half-stopped pipeline with an orphaned APScheduler job/stream thread. Same for concurrent update+delete. **Severity: MEDIUM**

### B3. [LIKELY BUG] gNMI and Kafka sources cannot be stopped promptly
`gnmi/source.py` has **no `stop()`**; `executor.py:957-963` stop-watcher only unblocks sources that implement `stop()`. `kafka/source.py` also has no `stop()` and its `read()` loop (`:115-154`) never checks any stop event — it only yields control back to `stream_run` when a message arrives.
**Impact:** `controller.stop()` join times out (10-30s), the old stream thread keeps consuming (and holding Kafka group partitions) until the next message arrives; for ON_CHANGE subscriptions that can be never. Restart flow can briefly run two stream instances. Note `syslog`, `webhook`, and `snmp_trap` *do* implement stop correctly — the pattern is inconsistently applied. **Severity: MEDIUM**

### B4. [LIKELY BUG] Kafka source does a broker round-trip **per message** for the lag metric
`kafka/source.py:132-144` — `consumer.end_offsets(list(assignment))` inside the per-message loop. Synchronous network call to brokers per consumed message — destroys throughput on busy topics. Should be sampled periodically. **Severity: MEDIUM (performance)**

### B5. [LIKELY BUG] In-flight run survives `update()` with stale config
`controller.py:228` `_stop_execution` removes the scheduler job but does not (and cannot cheaply) abort a running `_run_batch`; the run captured `state.config` at `:512` and finishes writing with the **old** config after the update is acknowledged. Combined with A2's early `mark_processed`, a failed old-config run can still consume input files. **Severity: LOW-MEDIUM**

### B6. [LIKELY BUG] Kafka default `enable_auto_commit: true` undermines at-least-once
`models/pipeline.py:93` + `kafka/source.py:45`: auto-commit every ~5s of offsets for messages that may not yet be sink-written. Crash after auto-commit, before sink write → message lost with no DLQ record. For at-least-once, commit should follow sink success (or be documented as at-most-once default). **Severity: MEDIUM (design-adjacent)**

### B7. [LIKELY BUG] `PipelineStats.errors_last_window` grows unbounded between snapshots
`tram/agent/metrics.py:46` — `self.errors_last_window.extend(errors[-10:])` — appends up to 10 strings per increment with no cap; cleared only on the 30s snapshot. An error-storm stream at 1K rec/s accumulates ~300K strings (~tens of MB) between snapshots. Should be a `deque(maxlen=…)`. **Severity: LOW**

### B8. [LIKELY BUG] `_add_column_if_missing` swallows *all* exceptions
`db.py:60-68` — non-Postgres `try: ALTER … except Exception: pass`. A locked/disk-full DB is indistinguishable from "column exists" → silent schema drift and later opaque crashes. Should catch only the duplicate-column error per dialect. **Severity: LOW**

### B9. [LIKELY BUG] `save_pipeline_version` version-number race
`db.py:367-371` — `SELECT MAX(version)+1` then insert; two concurrent saves can mint the same version (no unique constraint on `(name, version)`). Low probability, but version list then contains duplicates that `rollback` picks nondeterministically. **Severity: LOW**

### B10. [LIKELY BUG] `PipelineManager` is not thread-safe despite its docstring
`manager.py:76` claims "Thread-safe registry" — there is **no lock**. GIL makes individual dict ops atomic, but `_run_batch`'s `exists()`-then-`get()` (`controller.py:446-454`) can race a concurrent delete → `PipelineNotFoundError` raised *inside the except handler* at `:518` (`set_status` → `get` raises), bubbling raw errors into APScheduler. **Severity: LOW**

### B11. [LIKELY BUG] `_finalize_source_for_sinks` exceptions propagate uncaught
`executor.py:310-316` calls `finalize_source` with no try/except; `LocalSink.finalize_source` (`local/sink.py:223-226`) raises `SinkError`. In the single-threaded batch `else` clause (`executor.py:930-932`) this escapes `batch_run`'s `except TramError` handling chain (it *is* a TramError so it's caught at `_batch_run_inner:799` — but it aborts the whole run as failed *even though all data was already written successfully*, because a rename of one staged file failed). **Severity: LOW-MEDIUM**

---

## C. Security

### C1. [SECURITY] `/api/internal/*` is fully unauthenticated
`middleware.py:28` — `EXEMPT_PREFIX = ("/webhooks/", "/ui", "/api/internal/")`. `routers/internal.py:54-106` — anyone with network reach to the manager can POST arbitrary `run-complete` (forge success/failure of any run_id, poison run history and trigger state transitions incl. service deactivation) or fake `pipeline-stats` (drive PlacementReconciler to redispatch/duplicate streams). There is no shared secret between workers and manager, even though `TRAM_API_KEY` exists. **Severity: HIGH (in-cluster attack surface / cross-namespace pod)**
**Verification: independently re-verified.**

### C2. [SECURITY] Worker agent API (:8766) has no authentication at all
`agent/server.py:273-277` — the agent FastAPI app adds no `APIKeyMiddleware` (contrast the ingress app at :491-493 which does when `api_key` set). `POST /agent/run` accepts **arbitrary pipeline YAML** from any caller → unauthenticated remote code-adjacent execution (file writes, SFTP with creds embedded in the posted YAML, SSRF to internal REST endpoints via sinks). **Severity: HIGH if the port is reachable beyond trusted pods; MEDIUM otherwise.** Deserves at minimum the same API-key middleware as ingress.

### C3. [SECURITY] Non-constant-time comparisons for secrets
`middleware.py:48` — `key == settings.api_key` (plain `==`); `webhooks.py:40` — `auth_header != f"Bearer {secret}"`. The codebase already uses `hmac.compare_digest` in `auth.py:73,117,133` — these two paths were missed. Timing side-channel on both the global API key and per-webhook secrets. **Severity: LOW-MEDIUM**
**Verification: independently re-verified.**

### C4. [SECURITY] API key accepted via query parameter
`middleware.py:47` — `?api_key=` ends up in access logs, proxy logs, browser history. Also there is **no request body size limit** on `/webhooks/*` (`await request.body()` at webhooks.py:43 buffers the entire payload in memory) — trivially DoS-able even without auth. **Severity: LOW-MEDIUM**

### C5. [SECURITY] ClickHouse table name interpolated into SQL
`clickhouse/sink.py:132` — `f"INSERT INTO {self.table} VALUES"`. Config-controlled, not per-request, so exploitation requires pipeline-edit rights — but a malicious/typo'd table like `t (x) VALUES SELECT …` is possible. Document or validate as an identifier. **Severity: LOW**

### C6. Security — positive notes
`yaml.safe_load` everywhere (verified loader.py, pipelines.py:415). Passwords hashed with scrypt + constant-time verify (auth.py:37-75). `verify_ssl` defaults True in REST/ES. Static file serving via Starlette `StaticFiles` (safe against traversal). No path traversal found in local/SFTP file connectors (patterns are fnmatch'd against directory listings, not user-supplied paths). Legacy `sha256$` password fallback is documented and comparable.

---

## D. Design issues

### D1. [DESIGN ISSUE] DLQ write failures are silently swallowed → total data loss
`executor.py:142-168` — `_write_dlq_envelope` logs and returns on DLQ sink failure. When the primary sink *and* DLQ both fail (often the same network partition), records vanish with only a log line. For a mediation product with an "at-least-once" story, the fallback should be a local spool/disk DLQ or run-failure propagation (`on_error=abort`).

### D2. [DESIGN ISSUE] Worker→manager run-complete callback has no retry
`agent/server.py:131-143` — single POST, errors logged and swallowed. A transient manager restart makes `BatchReconciler` (`reconciler.py:269-289`) mark a **successful** run as failed ("disappeared before callback"), recording a phantom FAILED run and error status for a pipeline whose data was written. At minimum, retry with backoff; the duplicate-callback guard (`on_worker_run_complete` existing-run check, `controller.py:654-664`) already makes retries idempotent.

### D3. [DESIGN ISSUE] Circuit breaker window is hardcoded 60s
`executor.py:517` — `open_until = time.monotonic() + 60.0`, not configurable, and the open-state check logs "Circuit breaker open" once per *chunk* → log flooding while open. Also failures are counted per *partition* per chunk (`:512-524`), so threshold semantics vary with fan-out cardinality.

### D4. [DESIGN ISSUE] `records_out` metric inflation
`executor.py:561-565` — if *any* sink wrote, `records_out += len(records)` for the whole batch even when other sinks failed, were circuit-open, or condition-filtered. Multi-sink runs over-report delivered records.

### D5. [DESIGN ISSUE] SQLite as default DB with `check_same_thread=False`, no busy timeout
`db.py:39-54` — concurrent writers across APScheduler/API/stream threads will hit "database is locked" under load (default 5s busy timeout, shared QueuePool). The retry-on-init loop at `app.py:146-165` is good, but Postgres should be strongly recommended for manager mode; currently every warning path degrades silently to a DB that's unsafe at this concurrency.

### D6. [DESIGN ISSUE] `controller.stop()` contains a dead no-op
`controller.py:142-143` — `if self._worker_pool is None: pass` — leftover from removed code; misleading given the comment above it.

### D7. [DESIGN ISSUE] SFTP/FTP sinks open a new connection per write
`sftp/sink.py:138` — `write()` starts with `self._connect()` (full TCP + SSH auth handshake) and closes in `finally` (:263-271) — **per chunk**. `ftp/sink.py:66-68` identical; `rest/sink.py:77` creates a new httpx.Client per write (no keep-alive reuse). For stream pipelines with frequent chunks this is a handshake per message and will not scale. (Source side connects once per `read()` — the sink side should mirror that or pool.)

### D8. [DESIGN ISSUE] `worker_id_for_url` identity churn
`worker_pool.py:169-177` — worker restarts reuse the same URL with a new `worker_id` (hostname-based); `_worker_ids` is updated, but placements pinned by `pinned_worker_id` and `_pipeline_workers` history are not reconciled — old ids linger in `_pipeline_workers` (never pruned, unbounded growth, `worker_pool.py:456-460`).

---

## E. Boilerplate / duplication

### E1. [BOILERPLATE] 20× copy-pasted sink-config field block
`models/pipeline.py` — `condition` / `transforms` / `retry_count` / `retry_delay_seconds` / `circuit_breaker_threshold` / `serializer_out` repeated verbatim in **all 20 sink classes** (grep counts: 20/20/20/21 occurrences). `FileSinkConfigMixin` already proves the mixin pattern works in this file — a `SinkCommonFieldsMixin` would delete ~120 lines and guarantee the fields never drift.

### E2. [BOILERPLATE] Dialect-specific upsert SQL re-implemented 6 times
`db.py` — `set_alert_cooldown` (:472-519), `mark_processed` (:535-582), `set_password_hash` (:603-625), `save_pipeline` (:682-721), `save_broadcast_placement` (:793-855), `set_setting` (:768-784) each branch on sqlite/postgres/mysql with near-identical bodies. One `_upsert(conn, table, row, conflict_cols)` helper would remove ~200 lines and eliminate the chance of one dialect silently diverging (e.g. the generic-dialect plain INSERT in `mark_processed` raises where the others don't).

### E3. [BOILERPLATE] File-sink rolling logic duplicated between LocalSink and SFTPSink
`local/sink.py:104-200` vs `sftp/sink.py:137-249` — the entire roll/stage/partition state machine is line-for-line duplicated with only `Path.open("ab")` vs `sftp.open(..., "ab")` differing. The staging bookkeeping (A10's thread-unsafe dicts) is therefore also duplicated. A template-pattern (pass an open/append/rename strategy) would keep A10's fix in one place.

### E4. [BOILERPLATE] Connector `__init__` config extraction
71 connector files each hand-parse `config.get(...)` for the same fields (mib_dirs auto-prepend duplicated verbatim in `snmp/source.py:76-78` and `:241-243`; SNMPv3 USM field block duplicated in both SNMP sources and both SNMP sinks). Pydantic models already exist for every connector — connectors could receive the validated model rather than re-deriving typed fields from a dict (currently models validate at load, then are `model_dump()`ed back to a dict and re-parsed by hand — double parsing, triple bookkeeping).

### E5. [BOILERPLATE] `_save_alerts_data` duplicates `controller.update` semantics
`routers/pipelines.py:418-437` re-implements deregister/register/restart instead of routing through `controller.update()` — which is exactly why it diverged and lost persistence (A6).

---

## F. Dead code / unused modules

- **[GAP] `tram/cluster/` and `tram/scheduler/` are empty directories** containing only stale `__pycache__` (`coordinator.cpython-312.pyc`, `registry.cpython-312.pyc`, `scheduler.cpython-312.pyc`). Zero live references anywhere in `tram/`, `tests/`, `scripts/` (grep verified). The v1.2.0 commit removed the sources. Delete the directories and stale .pyc files — they mislead readers into looking for cluster code.
- **[GAP] Legacy pause API with zero callers:** `db.pause_pipeline` / `resume_pipeline` / `is_pipeline_paused` / `get_paused_pipeline_names` (`db.py:629-643`) — no references outside db.py itself (grep verified, only tests of v0.7 API). Also `db.get_latest_version` (:443) is referenced only by a unit test.
- **[GAP] `controller.stop()` no-op if-block** (`controller.py:142-143`), and `PipelineConfig.sink` singular field kept only for legacy YAML compat (fine, but worth a deprecation note).
- **[GAP] `controller.stop_pipeline` on the *controller*** is what the watcher needed — the mismatch (A7) suggests the watcher was never run against a live pipeline deletion in testing.

---

## G. Test coverage gaps

Strong suite overall (100 unit + 8 integration files; retry, circuit breaker, thread-worker counters, reconciler, worker pool, DLQ, processed-files all have dedicated tests). Gaps that map directly onto the confirmed bugs above:

1. **No test of `skip_processed` in manager+worker mode** (A1) — `test_processed_files.py` only exercises the standalone path with an injected tracker.
2. **No test of `skip_processed` + `thread_workers > 1` ordering** (A2) — `test_thread_workers.py` mocks `_process_chunk`; the mark-before-write window is invisible to it. The "lock tests" at `test_thread_workers.py:22-50` are `hasattr` smoke checks, not concurrency tests (the one real concurrency test, `:56`, only covers ctx counters).
3. **No test for ClickHouse sink lifecycle** — `close()` is untestable through the executor because nothing calls it (A9); no test asserts the timer stops.
4. **No test that webhook backpressure ever engages** (A8) — `max_queue_size` behavior is untested; a test would immediately reveal `SimpleQueue` can't be full.
5. **No test for alert CRUD persistence across "restart"** (A6) — a boot-load round-trip test would catch it.
6. **No test for the watcher delete path** (A7) — `test_pipeline_watcher.py` exists but evidently doesn't cover deletion (the `stop_pipeline` AttributeError is silently swallowed).
7. **No test for `rate_limit_rps: 0`** (A5), syslog TCP framing (A12), concurrent `PUT /pipelines/{name}` (B2), or shared-executor rate-limit cross-talk (A4 — two pipelines with distinct RPS in one process).
8. **Integration tests are thin for manager mode** (6 files) — no end-to-end test of worker dispatch → callback loss → BatchReconciler behavior (D2's phantom-failed-run path).

---

## Reconciled contradiction: mark-before-write vs. safe ordering

An initial domain-level review pass claimed `mark_processed` fires only after the executor finishes each chunk (safe generator-resumption ordering), while this review claimed files are marked before threaded sink writes complete. Both positions were re-verified against the code directly:

- **Single-threaded path** (`executor.py:894-932`): safe — the consumer processes each chunk before pulling the next one from the generator, and `enable_safe_finalize` staging engages.
- **Threaded path** (`executor.py:856-867`): unsafe — the main loop `pool.submit`s each chunk and immediately advances the generator; `_post_read`/`mark_processed` (`sftp/source.py:120-124`) run after the last chunk is *submitted*, not after its future completes.

**Conclusion: A2 stands.** The safe ordering is a property of the single-threaded consumer only.

---

## H. Overall assessment

**This is a well-documented, unusually careful codebase with several genuinely good engineering decisions — and a handful of high-impact correctness holes concentrated in exactly the places the authors themselves flagged as hard.**

Strengths: consistent error taxonomy (`TramError` hierarchy), locked run-context counters, stable circuit-breaker keys with an explicit comment about `id()` reuse, dialect-aware SQL, scrypt auth, `yaml.safe_load`, staged-file atomic finalize with `enable_safe_finalize` gating, and thoughtful comments explaining *why* (e.g. executor.py:811-814, context.py:29-31). Test breadth is above average for this genre.

Weaknesses cluster in three themes:

1. **The at-least-once guarantee is weaker than advertised, and the weak spots are the untested combinations.** Idempotency works in the single-threaded standalone path (the one that's tested); it breaks in manager+worker mode (A1) and threaded batch mode (A2), and the Kafka default config (B6) plus swallowed DLQ failures (D1) mean the safety net has holes precisely when networks are bad — which is when it matters. Make "idempotency parity across all execution modes" the top priority, followed by a durable local spool for DLQ failures.

2. **Concurrency control is done with conventions instead of locks.** The controller/manager layer relies on check-then-act status fields shared across APScheduler threads, FastAPI's threadpool, and stream threads, with no mutex anywhere (`PipelineManager` even claims thread-safety it doesn't have). None of these races corrupt data on their own, but they produce duplicate runs, zombie streams, and 500s under operations traffic. A single controller-level RLock around lifecycle transitions would close B1/B2/B10 cheaply.

3. **Copy-paste at the edges.** The 20× sink-config block, the 6× upsert dialect branching, and the duplicated file-sink state machine are maintenance debt that already caused divergence (the alert-edit path forgetting DB persistence, the watcher calling the wrong object). The connector layer would benefit most from shared mixins in both directions (config models → connectors, and the Local/SFTP sink state machine).

The security posture is reasonable for an internal cluster tool but has two easy wins: put `TRAM_API_KEY` auth on the worker agent API and on `/api/internal/*`, and switch the two non-constant-time comparisons to `hmac.compare_digest`.

**Priority order for fixes:** A1, A2, A6, A7, C1, C2 (correctness/security) → A3-A5, A8-A13, B1-B6 (robustness) → boilerplate consolidation.
