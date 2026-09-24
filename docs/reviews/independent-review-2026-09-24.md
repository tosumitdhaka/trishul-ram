# Independent Full-Repo Review — 2026-09-24

- **Scope:** full repo, clean slate (no anchoring to prior review docs). TRAM v1.4.5, `main` at `8470ac8`.
- **Method:** three independent review lanes — (1) execution core & data path, (2) API/control plane & security, (3) UI / deployment / test suite — plus orchestrator spot-verification of the three highest-severity claims (all three confirmed against code; see §5).
- **Finding labels:** CONFIRMED BUG (code path traced end-to-end, file:line evidence), LIKELY BUG (strong suspicion, evidence cited), SECURITY CONCERN, DESIGN CONCERN, NIT.

## 1. Executive summary

Nothing found invalidates v1.4.5 as shipped. The codebase is unusually disciplined — written race reasoning, a coherent error taxonomy, uniform XSS escaping in the UI, zero broken patch targets across the test suite, and strong AI-endpoint hardening (constant-time compares, DNS-rebinding-aware base_url checks, per-call audit). The dominant weaknesses:

1. **Execution-core asymmetry:** the batch path is fully hardened (sink close, retry rebuild, in-flight caps) but the stream path and cross-component seams leak resources and drop guarantees — most notably stream runs never close sinks (§2.1).
2. **Security: strong mechanisms, weak defaults.** Every layer fails open when unset — no API key + no users = fully unauthenticated control plane on 0.0.0.0, internal endpoints warn-and-serve, AI base_url allowlist unset and never re-checked at call time. Two confirmed secret-exfiltration/DoS-class bugs sit in that default posture (§3.1, §3.2, §3.3).
3. **Verification-gate drift:** the browser smoke asserts a stale `v1.4.3` fixture version against release 1.4.5 — it passes while no longer testing what it claims.

Highest-priority fixes (all exploitable in a default no-auth deployment):
- §3.2 AI redaction misses `api_key` connector fields → plaintext secrets sent to LLM providers.
- §3.1 Webhook queue is unbounded (`max_queue_size` dead config) → anonymous memory-exhaustion DoS.
- §3.3 AI base_url allowlist never enforced at call time → stored API-key exfiltration via attacker-controlled `base_url`.
- §2.1 Stream runs never close sinks (ClickHouse flush timers, connections) — permanent thread/connection leak per stream restart.

## 2. Lane 1 — Execution core & data path

### Architecture assessment

Thread-based design in four layers: `PipelineController` (lifecycle authority, one RLock serializing transitions), `PipelineExecutor` (batch/stream/dry-run with threaded chunk fan-out), decorator-keyed plugin registry, SQLAlchemy-Core persistence, plus a manager/worker split with HTTP dispatch reconciled by two background threads. Concurrency reasoning is written into the code (design citations on nearly every lock/CAS), and the at-least-once story for file sources (deferred `finalize()` after drain), Kafka (explicit post-consumption commit), and stateful-transform snapshots is genuinely well engineered. The dominant weakness is asymmetry: the batch path got the full hardening; the stream path and several seams still leak resources or drop guarantees. The controller (2,433 lines) is at the edge of maintainability.

### CONFIRMED BUG

**2.1 Stream runs never close sinks (resource + contract violation) — HIGH**
`executor.py:1439-1473` (stream `finally`) vs `:1123-1126` (batch `finally`), `interfaces/base_sink.py:33-40`.
The stream `finally` does `_save_state_to_store` → `_close_source` and exits; `_close_sinks` is only invoked on the retry path (`:1089`) and in the batch finally (`:1125`). `BaseSink.close()` documents "Called by the executor after a run finishes", and `ClickHouseSink.__init__` (`connectors/clickhouse/sink.py:70-79`) starts a self-rescheduling `threading.Timer` that only stops in `close()`. Every stream stop/crash/restart leaves that timer running forever; each restart adds another permanent timer thread. SFTP/AMQP/NATS connections held for the stream's lifetime are likewise never released.
*Fix:* call `self._close_sinks(sinks, dlq_sink)` in the stream `finally` (after flush-record routing).
**Orchestrator-verified 2026-09-24:** the stream `finally` at executor.py:1439-1473 contains no sink-close call.

**2.2 `dry_run` leaks sink resources — MEDIUM**
`executor.py:1572-1609`, `api/routers/pipelines.py:52`.
`dry_run()` calls `_build_sinks(config)` (constructing e.g. a `ClickHouseSink`, starting its 2s flush timer) and never closes anything. The router constructs a fresh `PipelineExecutor()` per request, so every dry-run of a ClickHouse-sink pipeline permanently leaks a timer thread in the manager process.
*Fix:* best-effort close of built sinks/sources in `dry_run`, or make sink constructors lazy (start timers on first `write`).

**2.3 Batch retry rebuild loses the run_id — MEDIUM**
`executor.py:1007-1008` vs `:1094`.
First attempt: `PipelineRunContext(pipeline_name=config.name, **{"run_id": run_id})`. On `on_error: retry`, the rebuild is `PipelineRunContext(pipeline_name=config.name)` — the original `run_id` is dropped, so the final `RunResult` and the worker's run-complete callback (`agent/server.py:493`) carry a fresh random run_id. Breaks the E.2 run_id contract (202 response = queued_runs row = run_history row), the manager's duplicate-callback dedupe (`controller.py:1334`), and any client holding the trigger's run_id (404s).
*Fix:* pass `run_id=run_id` into the retry-context rebuild.

**2.4 `rate_limit_rps: 0` crashes every chunk with ZeroDivisionError — MEDIUM**
`executor.py:229-242`; `models/pipeline.py:1468` (`rate_limit_rps: float | None = None` has no `gt=0`).
With rps=0: `self._tokens = min(0, …) = 0 < 1.0` → `sleep_time = (1.0 - 0) / 0` → `ZeroDivisionError`. Not a `TramError`, so it bypasses `on_error` handling and fails the run via the generic handler.
*Fix:* `Field(default=None, gt=0)` plus a guard in `_rate_limit`. (Same finding as historical review A5 — never tracked or fixed.)

**2.5 `inject_meta` runtime-meta race with `thread_workers > 1` — MEDIUM**
`transforms/inject_meta.py:30-34, 42`; `executor.py:579, 1266-1272`.
`_set_transform_runtime_meta` writes `self._meta = dict(meta)` on the shared transform instance; `apply()` reads `self._meta.items()` — both from concurrent worker threads with no lock. A record can be annotated with another file's `source_filename`/`source_path`. The stateful gating that protects `counter_delta` from this exact hazard does not cover `inject_meta`.
*Fix:* pass meta as an `apply` argument, restrict runtime-meta transforms to `thread_workers == 1` (mirror the stateful validator), or lock.

**2.6 Phantom run_id from trigger/claim TOCTOU — MEDIUM**
`controller.py:546-579` (check + async submit) vs `:990-1007` (claim).
`trigger_run` rejects only when `status == "running"` at check time, then submits `_run_batch` to the pool. If a concurrent run (e.g. an APScheduler fire) claims first, the manual `_run_batch` hits the skip at `:1000-1007` and returns silently — no run-history row, no error — while the API already returned `TriggerResult(run_id, "dispatched")`; the client's run_id 404s forever. The E.2 DB dedupe fixed this class for the no-capacity queue path but not the lost-claim-race path.
*Fix:* on claim-skip for `origin="manual"`, write a FAILED/skipped RunResult under the submitted run_id (or make the claim synchronous).

**2.7 Stop-watcher thread leak on crashed streams — LOW/MEDIUM**
`executor.py:1378-1387`.
`_stop_watcher` blocks forever on `stop_event.wait()`; the event is set only via the controller. If the stream exits via the exception path (source crash), the watcher thread is leaked — a crash-looping stream accumulates one leaked thread per cycle.
*Fix:* in the stream `finally`, signal the event or use `wait(timeout)` polling `thread.is_alive()`.

### LIKELY BUG

**2.8 Fast dispatched run records a stale lease → spurious FAILED result — MEDIUM**
`controller.py:1092-1116` vs `on_worker_run_complete` `:1301-1378`, `agent/server.py:362-534`.
The worker returns 202 and runs the batch on a daemon thread; for a fast-completing run the worker can finish and POST run-complete before the manager's dispatch thread re-acquires the lock and records the lease. The CAS checks pipeline existence/config identity but not whether the run already completed. `BatchReconciler` then probes `is_run_active(run_id)` → False → `mark_active_batch_run_lost` (`reconciler.py:404-424`) writes a FAILED `RunResult` for a run that succeeded: the DB row is skipped as duplicate (`db.py:451-452`) but in-memory `last_run_status` flips to "failed" and pipeline status to "error".
*Fix:* in the CAS, also check `self.manager.get_run(run_id) is None` before recording the lease.

**2.9 `on_error: abort` not honored for per-record transform failures — MEDIUM**
`executor.py:574-597`.
The per-record global-transform loop catches all exceptions, writes DLQ, and continues — even when `on_error == "abort"` (enforced only for parse failures `:875` and sink write failures `:753-754`). A pipeline configured `abort` with a failing transform silently degrades to `continue`.
*Fix:* re-raise (as `TramError`) when `on_error == "abort"`.

### DESIGN CONCERN

- **2.10 `record_chunk_size` silently ignored on the threaded batch path — MEDIUM** (`executor.py:1229-1330` vs `:1195-1201`): the incremental path exists only in the sequential loop; with `thread_workers > 1` a user's memory bound on large `split_path` fan-outs is not honored, and no lint rule warns. Fix: use `_process_chunk_incrementally` in the threaded path or add a lint warning.
- **2.11 `records_out = max(written_counts)` undercounts disjoint sinks — LOW** (`executor.py:786-798`): two condition-routed sinks partitioning records report max, not total. Documented as a conservative lower bound; still wrong for per-sink throughput accounting.
- **2.12 Kafka at-least-once silently degrades with `thread_workers > 1` — LOW** (`connectors/kafka/source.py:40-54`, `executor.py:1283-1297`): a poll batch's commit can fire while up to `2×workers` chunks are still queued. Documented tradeoff; suggests a dedicated lint rule.
- **2.13 Boot-time worker HTTP probes under the lifecycle lock — LOW** (`controller.py:262-299` → `worker_pool.py:563-587`): serial 5s probes per count=1 stream across all workers while holding the controller RLock — N streams × M unreachable workers can block boot and the API. Fix: snapshot candidates under the lock, probe outside, re-verify (the established redispatch CAS pattern).
- **2.14 Config-validation holes in scalar execution knobs — LOW** (`models/pipeline.py:18-29, 1459-1478`): `interval_seconds` may be 0/negative (fails at schedule time, not config time), `batch_size=0` silently means unlimited, `thread_workers`/`retry_count`/`retry_delay_seconds` unconstrained, cron expressions unvalidated until `CronTrigger.from_crontab` raises at runtime. Collectively these push failures from `tram validate` time to run time.

### NIT

- **2.15** `watcher/pipeline_watcher.py:88-101` — watcher assumes filename stem == pipeline name; a YAML whose `name:` differs deletes the wrong/no pipeline.
- **2.16** `executor.py:776-779` — dead `except TramError: raise` in parallel-sink fan-out; non-TramError exceptions from `_write_one_sink` escape the taxonomy and bypass retry entirely.
- **2.17** `controller.py:177` — fixed 10-worker `_thread_pool` with unbounded queueing; beyond 10 concurrent local batch runs, manual triggers queue invisibly after "dispatched" was returned.
- **2.18** `executor.py:1558-1563` — `_stream_run_threaded` abandons workers after a 30s join; a slow sink write continues on a daemon thread while state is saved and the source closed.

## 3. Lane 2 — API / control plane & security

### Posture assessment

Genuine security craftsmanship in spots: constant-time comparisons everywhere, scrypt hashing, DNS-rebinding-aware base_url scheme checking, origin+directory-boundary allowlist matching, per-call AI audit logging. But the default posture is fail-open at every layer: no API key and no users = the entire control plane (pipeline CRUD, daemon shutdown, AI config writes) unauthenticated on `0.0.0.0`; internal worker endpoints default to warn-and-serve; the AI allowlist is unset and never re-checked at call time; rate limiting disabled by default. Pattern: strong mechanisms, weak defaults.

### CONFIRMED BUG

**3.1 Webhook `max_queue_size` never enforced; queue unbounded; "queue full" 503 is dead code (DoS) — HIGH**
`api/routers/webhooks.py:90-94`, `connectors/webhook/source.py:14, 32, 46`.
The router does `q.put_nowait((body, meta))` in a `try/except → 503 "Webhook queue full"`, but `q` is a `queue.SimpleQueue()` — unbounded; `put_nowait` never raises `Full`. The source stores `self.max_queue_size = config.get("max_queue_size", 1000)` but it is never referenced again. The endpoint is auth-exempt (`EXEMPT_PREFIX` includes `/webhooks/`, `middleware.py:46`) with a 10 MiB body cap — continuous posting grows process memory without bound.
*Fix:* bounded `queue.Queue(maxsize=…)` owned by the source, or a `qsize()` re-check returning 429/503; apply the rate limiter to `/webhooks/`.
**Orchestrator-verified 2026-09-24:** `SimpleQueue` at source.py:46; `max_queue_size` read at :32 and dead. (Same finding as historical review A8 — never tracked or fixed.)

**3.2 AI prompt redaction misses `api_key` connector fields (secret exfiltration to LLM provider) — HIGH**
`api/routers/ai.py:432`, `api/config_schema.py:160`, `models/pipeline.py:89, 341, 879, 1174`.
Both secret heuristics are `("password", "token", "secret")`. Four connector configs define `api_key: str | None` (RestSourceConfig, ElasticsearchSourceConfig, RestSinkConfig, ElasticsearchSinkConfig), and `_mask_block` masks only schema-marked `secret` names or heuristic-matching names — so a real `api_key` under a REST/ES source or sink is embedded unmasked in the outbound prompt for `explain`/`fix`/`modify`.
*Fix:* add `"api_key"` (ideally one shared constant) to both token tuples; add a unit test with an `api_key` field.
**Orchestrator-verified 2026-09-24:** `_SECRET_NAME_TOKENS = ("password", "token", "secret")` at ai.py:432 — `"api_key"` matches none of the three substrings.

### LIKELY BUG

**3.3 AI base_url allowlist never enforced at call time; unset by default → stored API-key exfiltration — HIGH (security concern)**
`api/routers/ai.py:143-150, 263-265, 277, 308, 341-349`.
`_allowed_base_urls()` returns `[]` when `TRAM_AI_ALLOWED_BASE_URLS` is unset ("no allowlist restriction applies"), and the call-time defense-in-depth check re-runs only `_base_url_problem` (scheme), not the allowlist — which is checked only in `ai_save_config`. Chain: a client who can reach `/api/ai/config` sets `base_url: https://attacker.tld` (scheme check passes), then `/api/ai/test` — the daemon sends the stored API key to the attacker's host (anthropic `x-api-key`, openai `Authorization`, bedrock `Authorization: Bearer`). `ai_test`/`ai_suggest` gate only on the key existing, not the destination. Unauthenticated exfiltration in default deployments.
*Fix:* enforce the allowlist in `_call_ai`; and/or refuse to attach the stored key when a non-default `base_url` is in effect with no allowlist configured.

**3.4 Rate-limiter eviction can swap per-IP locks out from under queued coroutines — LOW**
`api/middleware.py:183-185`.
The >500-IP eviction rebuilds `_windows`/`_locks` outside any lock; a coroutine awaiting the old lock can be bypassed by a fresh lock/deque, undercounting the window. Requires >500 concurrent client IPs.
*Fix:* evict under the same per-IP lock, or sweep `last_seen` timestamps with per-slot locking.

### SECURITY CONCERN

- **3.5 Default deployment is a fully open control plane on 0.0.0.0 — HIGH (documented, but the effective out-of-box posture):** `middleware.py:71-72`, `core/config.py:175, 187`, `helm/values.yaml:325`, `routers/runs.py:203-214`. No `TRAM_API_KEY` + no `TRAM_AUTH_USERS` passes every request; Helm ships `apiKey: ""`; `TRAM_HOST` defaults to `0.0.0.0`; `POST /api/daemon/stop` is anonymous remote shutdown; rate limit defaults to 0 (disabled), so `/api/auth/login` brute-forcing is unlimited when auth is enabled; failed `X-API-Key` attempts never consume rate-limit budget (APIKeyMiddleware is outermost). *Fix:* generate/inject a random API key by default in helm/compose; default rate limit to a sane positive value.
- **3.6 Prompt redaction fails open on unparseable YAML — MEDIUM:** `ai.py:511-517` returns input unchanged when `yaml.safe_load` raises (docstring admits it) — a mid-edit YAML with live secrets goes to the provider verbatim. *Fix:* refuse with 400, or regex-mask `(password|token|secret|api_key):` on parse failure.
- **3.7 Internal worker endpoints fail open; `enforce` does nothing without an API key — MEDIUM:** `middleware.py:52-59, 83-84`. `TRAM_INTERNAL_AUTH_MODE` defaults to `warn` (invalid values also fall back to `warn`), and `is_internal and not settings.api_key` returns before the browser-token check — so in an `auth_users`-only deployment, `/api/internal/*` is completely unauthenticated even with `enforce` set. Attackers can spoof run-complete/pipeline-stats and read/overwrite transform-state blobs (`routers/internal.py:54-107, 145-203`).
- **3.8 `/api/connectors/test` is an arbitrary-destination TCP probe (SSRF/port-scan oracle) — MEDIUM:** `routers/connectors.py:17-26, 110-116, 189-196`. Client-supplied `type`+`config` runs a plugin test or `socket.create_connection((host, port))` with host/port extracted from any `host/brokers/hosts/servers/url/base_url` field; the response is a clean open/closed oracle against the daemon's network position. *Fix:* reject private/link-local targets (mirror `_base_url_problem`), or restrict to hosts referenced by registered pipelines.
- **3.9 Schema-registry proxy forwards the caller's TRAM credentials to the registry — MEDIUM:** `routers/schemas.py:125-126`. `X-API-Key`/`Authorization`/`Cookie` are forwarded verbatim to `TRAM_SCHEMA_REGISTRY_URL`, while the configured `schema_registry_username/password` are never used. *Fix:* strip credential headers, inject configured registry credentials.
- **3.10 Raw internal exception strings returned to clients — LOW/MEDIUM:** the `except Exception as exc: raise HTTPException(500, detail=str(exc))` pattern throughout `routers/pipelines.py`, `schemas.py`, `mibs.py`, `ai.py` (ai.py's 502 catch-alls include SDK errors; the bedrock path embeds upstream error bodies). *Fix:* log server-side, return a generic detail + correlation ID.
- **3.11 Unauthenticated information disclosure via exempt endpoints — LOW:** `/api/ready` returns the absolute `db_path`; `/metrics` is exempt and exposes pipeline names/topology; `/docs`/`/redoc`/`/openapi.json` are exempt, handing over the complete API map. *Fix:* drop `db_path` from readiness; gate `/metrics`; disable docs in production builds.
- **3.12 Password change does not revoke outstanding sessions; no logout/revocation exists — LOW:** stateless HMAC tokens, fixed 8h TTL, no token store/jti; a stolen token survives a password rotation. `routers/auth.py` docstrings advertise `logout` and `POST /api/auth/users` endpoints that do not exist.

### DESIGN CONCERN

- **3.13 `TRAM_WORKERS > 1` silently breaks auth and rate limiting — MEDIUM:** `api/auth.py:28` falls back to `secrets.token_hex(32)` per process — with multiple workers and no `TRAM_AUTH_SECRET`, tokens fail verification intermittently and in-memory state fragments. *Fix:* refuse to start (or warn loudly) on `workers > 1` without `TRAM_AUTH_SECRET`.
- **3.14 AI API key stored plaintext in the `settings` DB table — LOW:** masked in GETs, never logged, but DB-read access yields the provider key. *Fix:* document, or wrap in symmetric encryption keyed from env.
- **3.15 `ai_save_config` applies fields non-atomically and coerces types — LOW:** per-field `db.set_setting` in a loop means a later 400 (bad `base_url`) leaves earlier fields saved; `str(value)` coerces booleans to `"True"`; no Pydantic model on the body. *Fix:* validate whole body first, persist in one transaction.
- **3.16 `TRAM_AUTH_USERS` carries plaintext passwords in the process environment — LOW:** visible via `/proc/<pid>/environ` and container specs. *Fix:* document a bootstrap flow or support `TRAM_AUTH_USERS_FILE`.
- **3.17 CSV export is formula-injection prone — LOW:** `routers/runs.py:104-119` writes `error`/`pipeline` values unescaped; `=`/`+`/`-`/`@` prefixes execute in Excel. *Fix:* prefix risky leading characters with `'`.

### NIT

- `middleware.py:46` — `EXEMPT_PREFIX` contains `"/ui"` without trailing slash; any future `/ui*` route silently bypasses auth.
- `ai.py:693-695` — 400 detail says allowlist "must prefix-match" but the implemented match is origin-exact + directory-boundary (message is stale, implementation is stronger).
- `api/auth.py:84-86` — docstring references a nonexistent `POST /api/auth/users`; only `change-password` writes `user_passwords`.
- `ai.py:110-115` — `_base_url_problem` accepts `https://` with empty host; reject early.
- `routers/auth.py:30-38` — `_resolve_password` does no dummy work for unknown usernames → username-enumeration timing oracle.
- `api/auth.py:92-94` — `parse_users` strips whitespace, so env passwords cannot contain leading/trailing spaces.
- `app.py:292` — `ai_docs.router` registered but defines no routes.
- `routers/internal.py:191-198` — transform-state `Content-Length` fast path runs after FastAPI already buffered the body.
- `routers/pipelines.py:437` — router calls private `controller._boot_load()`.

## 4. Lane 3 — Web UI, deployment assets, test suite

### Sub-area assessments

**Web UI** — an unusually disciplined vanilla-JS SPA: hash router with params/query, shared `createPageController` lifecycle (mount/refresh/poll/unmount, generation-based stale-load invalidation), focus-preserving re-renders, toast dedup, editor draft/undo/IME handling. XSS discipline is strong — `esc()` applied consistently across every renderer audited; no broken imports or dead routes (all 12 `inits[]` entries map to existing modules). The real defects are edge cases: an orphaned Bootstrap modal backdrop on Back-navigation (browser-only), a stale `v1.2.0` brand literal, and a few unescaped numeric interpolations.

**Deployment** — the chart is careful: non-root uid 1000, fsGroup, key Secret `0440`, sane probes, resources on all StatefulSets, correct headless services, auto-generated `TRAM_AUTH_SECRET` via lookup-preserving pre-install hook, no plaintext secrets in chart auth values. Problems: `sharedStorage` documentation drift in manager+worker mode (RWX PVC never mounted on workers — they use emptyDir + `sync_assets()`), a weak `admin:admin123` default in the standalone deploy script, and `password: tram` in the generic values template.

**Test suite** — large and mostly high-quality: 2,212 unit + 44 integration + 77 browser assertions, zero skips/xfails. All 93 string `patch("...")` targets and all `patch.object(...)` targets were mechanically verified against live modules — **zero resolve to nonexistent attributes**. Critical paths are genuinely strong (dispatch tests assert POST payloads; placement tests use real SQLite + real controller; SNMP classify tests assert full row semantics; AI has 122 tests covering redaction/base_url/audit). Weaknesses: the boot check asserts stale `v1.4.3`, and one browser check burns a hardcoded 61.5s wait.

### CONFIRMED BUG

**4.1 Orphaned Bootstrap modal backdrop on Back-navigation (browser-only) — MEDIUM**
`ui/src/router.js:146`, `ui/src/pages/pipelines.js:407-410`.
Modals live inside `#content`; on hashchange (Back) the router replaces `#content` but nothing disposes the Bootstrap instance or removes `.modal-backdrop` from `document.body` (nor `body.modal-open` / inline `overflow:hidden`). The overlay is undismissable except by F5. `doTemplateDeploy()` manually strips backdrops for exactly this reason — but only on that one path. Not covered by the browser smoke (never navigates with a modal open).
*Fix:* on `tram:page-leave`, dispose all `.modal.show` instances + the same cleanup `doTemplateDeploy` uses; add a smoke check.

**4.2 Browser smoke asserts a stale release version (`v1.4.3` vs 1.4.5) — MEDIUM (process/verification gap)**
`tests/browser/checks/boot.mjs:44-45`, `tests/browser/fixtures/meta.json:1`.
The check claims "the shell renders the released version" but asserts against a fixture captured 2026-09-22 from release/v1.4.3; current release is 1.4.5. It passes because fixture and assertion agree — but no longer tests what it claims, and release-gate check #2 verifies pyproject/chart/package.json only, not the browser fixture. This is the same drift class gate check #11 exists for.
*Fix:* read the expected version from `tram/ui/package.json` in the check and/or have the gate validate `fixtures/meta.json`.

### LIKELY BUG

- **4.3 `helm/values.yaml` sharedStorage guidance stale; enabling it orphans a PVC — MEDIUM** (`values.yaml:140-145`, `worker-statefulset.yaml:109-138`, `manager-statefulset.yaml:182-190, 232-236`): the documented recommended combo (`manager.persistence.enabled=true` AND `sharedStorage.enabled=true`) creates an RWX PVC that is mounted nowhere. *Fix:* update comments to the sync_assets model; lint-guard or drop the combination.
- **4.4 Weak default UI credential in the standalone deploy script — MEDIUM** (`scripts/deploy-docker-standalone.sh:34, 587-588`): injects `admin:admin123` when `TRAM_AUTH_USERS` is unset, contradicting the Helm chart's no-default posture. *Fix:* default to empty, or generate and print a random password once.
- **4.5 Weak default PostgreSQL password in the generic release baseline — MEDIUM** (`helm/values-template.yaml:163`, `values.yaml:406`): `postgresql.auth.password: tram` defeats Bitnami's random generation and is interpolated plaintext into `TRAM_DB_URL` in both StatefulSets. *Fix:* set `password: ""` or require `envSecret.TRAM_DB_URL`.
- **4.6 `#pipelines/templates` deep link combines with 4.1 — LOW** (`ui/src/pages/pipelines.js:415-424`): aggravator of 4.1; fixed by the same centralized modal disposal.

### DESIGN CONCERN

- **4.7 `docker-compose.yml:24` bind-mount permission trap — LOW:** `./output:/data/output` on a gitignored, absent host dir → Docker auto-creates root-owned; container uid 1000 hits EACCES. *Fix:* `mkdir -p output` or document.
- **4.8 Unescaped API interpolations in two renderers — LOW:** `ui/src/pages/editor.js:661`, `create.js:577-613`, `utils.js:73` — daemon-side values, so exploitation requires a compromised backend, but standing exceptions to otherwise-uniform `esc()` discipline. *Fix:* wrap in `esc()`.
- **4.9 Browser smoke has a hardcoded 61.5s wall-clock wait — LOW:** `tests/browser/checks/yaml-quote.mjs:112` (`waitForTimeout(61500)`). *Fix:* make the poll interval injectable for tests.
- **4.10 Kind-profile values baked into tracked `helm/values.yaml` — LOW:** `existingClaim` + local image tags; easy to cargo-cult. `values-template.yaml` should be the surfaced baseline.

### NIT

- `ui/index.html:32` — stale literal `v1.2.0` brand version (overwritten by the health poller once `/api/meta` responds).
- `ui/src/pages/settings.js:85, 96` — inner `keyEl` shadows outer.
- `docker-compose.yml:128` — dev SFTP credentials `tram:tram123` (documented dev fixture).
- Router tests build `MagicMock()` controllers without `spec=PipelineController` — currently correct, but rename-fragile; consider `spec=`.
- `helm/Chart.yaml:20` — dependency `version: "16.x.x"` range is loose (Chart.lock pins 16.7.27 in-repo).

## 5. Orchestrator verification notes

Three highest-severity claims were independently re-verified against the working tree on 2026-09-24, before this report was finalized:

| Claim | Verification |
|---|---|
| §3.1 webhook queue unbounded | Confirmed: `webhook/source.py:46` creates `queue.SimpleQueue()`; `max_queue_size` read at `:32` and never referenced again. |
| §3.2 `api_key` redaction gap | Confirmed: `ai.py:432` `_SECRET_NAME_TOKENS = ("password", "token", "secret")` — `"api_key"` matches none. |
| §2.1 stream sinks never closed | Confirmed: stream `finally` at `executor.py:1439-1473` ends with `_save_state_to_store` → `_close_source`; no `_close_sinks` call. |

## 6. Strengths worth keeping (cross-lane)

- Deferred `finalize()` contract for file sources; Kafka explicit post-consumption commit; in-flight cap + backpressure (RCA #16 fix); stateful-transform snapshot/retry discipline with per-slot CAS.
- Written race reasoning (B1/B2/B10/D.2/E.2/F.1 design citations) on nearly every lock/CAS — makes regression hunting dramatically cheaper.
- Constant-time compares everywhere it matters; scrypt hashing; DNS-rebinding-aware, origin-exact + directory-boundary AI base_url checks; per-call AI audit with schema-version binding.
- `createPageController` lifecycle with generation tokens and focus-preserving re-renders; uniform `esc()` XSS discipline including inside the YAML highlighter/diff emitters.
- Helm secret hygiene (no committed plaintext auth defaults, lookup-preserving `TRAM_AUTH_SECRET` hook); multi-arch Dockerfiles with `BUILDPLATFORM`; release-gate design with never-silent-skip semantics.
- Test realism: wire-payload assertions, real SQLite/controller in placement tests, full-row SNMP semantics, 122 AI security tests, zero skips, zero broken patch targets.

## 7. Count summary

| Lane | CONFIRMED BUG | LIKELY BUG | SECURITY CONCERN | DESIGN CONCERN | NIT | Total |
|---|---|---|---|---|---|---|
| 1. Execution core | 7 | 2 | — | 5 | 4 | 18 |
| 2. API / security | 2 | 1 | 9 | 5 | 9 | 26 |
| 3. UI / deploy / tests | 2 | 4 | — | 4 | 5 | 15 |
| **Total** | **11** | **7** | **9** | **14** | **18** | **59** |

Highest-priority fixes: §3.2 (`api_key` redaction), §3.1 (webhook DoS), §3.3 (AI allowlist at call time), §3.5 (open-by-default posture), §2.1 (stream sink leak).
