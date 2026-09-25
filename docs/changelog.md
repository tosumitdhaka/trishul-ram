# Changelog

All notable changes to TRAM are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Fixed
- Navigating with a modal open (browser Back, same-page route change) no longer leaves an orphaned backdrop/scroll-lock — the router closes all modals synchronously before every render swap; a pending `confirmAction()` settles as cancel on navigation instead of hanging forever (#49)
- A Bootstrap mid-fade dispose race in the modal cleanup (uncaught `TypeError` ~150ms after route change) is closed — the pending show-transition callback is consumed on the intact instance before disposal (#49)
- The hardcoded brand version literal in `index.html` is gone — the version renders from `/api/meta` (`v—` placeholder offline, real version once reachable) (#49)
- `interval_seconds: 0` and malformed cron expressions are rejected at config validation; `thread_workers`, `batch_size`, `retry_count`, `retry_delay_seconds` bounds enforced (0 no longer silently means unlimited batch) (#52)
- The file watcher deletes by pipeline name on file deletion (stem fallback only for never-loaded files) — no more misattribution on edge filenames (#52)
- CSV export prefixes formula-leading characters (`= + - @`) — spreadsheet formula injection closed (#52)
- Startup warns loudly when `TRAM_WORKERS > 1` runs without `TRAM_AUTH_SECRET` (#52)
- New lint rule L014: kafka + `enable_auto_commit: false` + `thread_workers > 1` warns about the poll-batch commit race (#52)
- SQLite connections get a 30s busy timeout (`PRAGMA busy_timeout`) — `database is locked` contention gets a retry grace window (D5) (#52)
- SFTP/FTP sinks reuse one connection per run with a single reconnect attempt on transport failure, instead of a fresh connection per write (D7). Note: with `thread_workers > 1` the sink instance is shared across chunk threads and FTP control connections are not cross-thread safe — keep `thread_workers: 1` for SFTP/FTP sink pipelines (#52)

### Changed
- **[deployment]** The Helm chart no longer ships `postgres` as the default database password: credentials are chart-managed (explicit → existing-secret lookup → generated on first install), `TRAM_DB_URL` is wired via `secretKeyRef`, and unresolvable combinations fail the install with a clear message. The sharedStorage PVC only renders when actually mounted (no orphaned volume), and the standalone script no longer injects any default credentials (#51)
- **[deployment]** docs/deployment.md now recommends PostgreSQL for production scale (SQLite concurrency caveats documented) and documents the compose `./output` bind-mount permission setup (#51, D5)
- The 20× copy-pasted sink-config field block is a shared `SinkCommonFieldsMixin` (E1), the Local/SFTP rolling-writer logic is one `RollingWriter` (E3), and connector config extraction is a shared helper (E4) — behavior-identical; wizard form-field ORDER may shift because common fields now precede connector-specific ones (#52)
- Field ordering note: `/api/config/schema` and `/api/plugins` field lists reflect the mixin reordering above (names/types/metadata unchanged) (#52)

### Added
- Every one of the 601 config-schema field descriptors carries an operator-facing `description` (surfaced in the wizard via `/api/config/schema` and on `/api/plugins`); `schema_version` identity hash rotates (`7b7a99657e20` → `583465a50b4b`) (AI Wave A, A.1) (#42)
- Browser smoke gains a `modal-nav` check — modal open + Back navigation, deep-link entry, same-page route change, confirm-dialog reset, brand-version rendering; the boot check now rejects the `v—` placeholder (formerly a vacuous non-empty wait) (#49, #50 follow-up)

## [1.4.7] - 2026-09-24

### Fixed
- `rate_limit_rps: 0` no longer crashes every chunk with `ZeroDivisionError` — rejected at config validation (`gt=0`) plus a defense-in-depth executor guard (historical review finding A5) (#48)
- Top-level `inject_meta` with `thread_workers > 1` is now rejected at config validation — the shared transform instance could annotate records with another file's `source_filename`/`source_path` under concurrent chunks (same gating pattern as stateful transforms; sink-level uses stay allowed) (#48)
- `on_error: abort` is now honored for per-record transform failures — the run FAILS instead of silently degrading to continue (parity with parse/sink abort paths) (#48)
- `record_chunk_size` is honored on the threaded batch path — large `split_path` fan-outs stream incrementally instead of full eager materialization when `thread_workers > 1` (#48)
- Unexpected (non-TramError) exceptions in parallel-sink fan-out are converted to `TramError` so `on_error` handling applies instead of escaping the taxonomy and exploding the run (#48)
- Worker→manager run-complete callbacks now retry with bounded backoff (3 attempts) before falling back to the reconciler adoption path — a transient manager outage no longer loses the completion record (D2) (#55)
- A DLQ sink write failure no longer silently drops the record — the envelope is spooled locally (`TRAM_DLQ_SPOOL_DIR`, default `~/.tram/dlq-spool`) for manual replay; even a spool failure is counted in metrics and logged as ERROR (D1) (#55)
- The per-sink circuit-breaker open window is configurable (`circuit_breaker_window_seconds`, default 60s unchanged) (D3) (#55)
- `errors_last_window` is bounded (deque, maxlen 100) between snapshots (B7) (#55)
- `_add_column_if_missing` swallows only the duplicate-column error per dialect — locks/disk-full and other real failures now raise loudly (B8) (#55)
- `save_pipeline_version` version races are resolved by a `UNIQUE(name, version)` constraint (fresh DBs) plus an idempotent unique-index migration for existing DBs and an IntegrityError retry (B9) (#55)
- A `finalize_source` rename failure after all chunks drained no longer flips a fully-written run to FAILED — it degrades to a recorded error note on the run (B11) (#55)
- Sink-level `inject_meta` is gated to `thread_workers: 1` as well — sink transform instances are shared across chunk threads exactly like top-level ones (review follow-up to #48)
- The `UNIQUE(name, version)` migration (B9) deduplicates legacy rows before creating the index — a legacy DB that already holds duplicate versions no longer fails manager startup on upgrade (review follow-up to #55)
- `on_error: abort` is now honored for per-sink transform failures too — the last abort bypass is closed (review follow-up to #48)
- Worker `HttpFileTracker` batches processed-file checks (one batched request per run, 500-file sub-batches) and marks (one flush per run), reuses a single connection, and short-circuits after the first manager connection failure — a blackholed manager costs one timeout per run, not per file (review follow-up to #54); list-based sources prefetch the run's candidate files against the tracker in one batch
- `TRAM_DLQ_SPOOL_DIR` defaults under `TRAM_DATA_DIR` in worker mode and gets a Helm value (`worker.dlqSpoolDir`) — the spool lands on the worker PVC, not the container overlay (review follow-up to #55 D1)
- The processed-files internal endpoints cap `files` at 1000 per request (400 on overflow) (review follow-up to #54)
- Template examples embedded in AI generation prompts pass through redaction (fail-closed drop) (review follow-up to #41)
- Run-complete callbacks skip retry on 4xx responses — network errors and 5xx/429 still retry (review follow-up to #55 D2)
- Browser-smoke version assertions now derive from `tram/ui/package.json` and cross-check `tests/browser/fixtures/meta.json` — fixture drift fails the check instead of passing silently; release-gate check 2 validates the fixture against the pyproject version (#50)
- The yaml-quote browser check stubs the schema-poll interval (`window.__TRAM_TEST_SCHEMA_POLL_MS__` override) — the hardcoded 61.5s wall-clock wait is gone (check now ~11s) (#50)

### Added
- **[worker mode] `skip_processed` idempotency via a manager-routed tracker** (follow-up to #39): new internal endpoints `POST /api/internal/processed-files/check` and `/mark` (batch-friendly, namespaced by pipeline + source key + filepath, X-API-Key-authenticated) back a worker-side `HttpFileTracker` passed to the worker executor — a file processed once is not reprocessed across runs or worker failover. If the manager is unreachable the run fails loud (ERROR + run-history degradation note, once per run) and reprocesses; the v1.4.6 fail-loud path is now the fallback only (#54)
- **AI generate is template-grounded (A6):** when bundled templates match the requested source/sink types, up to 3 curated examples (name + YAML, size-bounded) are appended to the generation prompt; ungrounded generation is unchanged when nothing matches (#41)
- **AI fix retries once on invalid output (A7):** validation failures feed back into a single retry; the response carries `retried`/`attempts`, and the retry attempt is audited (`retried=True` on the second `tram.ai` log line) (#41)
- **AI run-failure triage (B1):** `POST /api/ai/suggest` `mode: "triage"` with `run_id` — the server assembles the run's failure context (counters, top-level error, grouped skip reasons, redacted pipeline YAML, fails closed by omitting unredactable YAML) and returns `{explanation, run_id, pipeline, status}`; audited with `mode="triage"`. The run-detail page (`#runs/:id`) gains an "Explain this run" button, gated on AI being configured like the editor/wizard (#41)
- **Server-side plugin field metadata (A.4):** `/api/plugins` field descriptors now carry `kind`/`choices`/`secret`/`multiline` from the config schema; the plugins page's client-side dual-fetch + `_enrichedFields` merge is deleted (#42 data-layer foundation)

## [1.4.6] - 2026-09-24

### Fixed
- **[security]** AI prompt redaction masks connector `api_key` fields (REST/ES source+sink) via one shared `SECRET_NAME_TOKENS` constant (`config_schema.py` ↔ `ai.py` can no longer drift) — real keys previously shipped unmasked to LLM providers (#43)
- **[security]** `TRAM_AI_ALLOWED_BASE_URLS` is enforced at call time in `_call_ai` (was save-time only — a stored API key could be sent to any https host set via `/api/ai/config`); with the allowlist set the **effective** endpoint is checked — `base_url` if configured, else the provider's fixed default endpoint (bedrock has no fixed default and requires an allowlisted `base_url`) (#43)
- AI prompt redaction fails closed on unparseable or list-shaped YAML (400 "fix your YAML syntax" instead of passing raw text with live secrets) (#43)
- `ai_save_config` is atomic and type-checked (Pydantic `str | None` body; a later-field 400 no longer leaves earlier fields persisted; booleans/numbers rejected) (#43)
- **[security]** Webhook source queue is bounded by `max_queue_size` (was an unbounded `SimpleQueue`; the "queue full" 503 was dead code) — closes the unauthenticated memory-exhaustion DoS on `/webhooks/` (#45, supersedes review finding A8)
- **[security]** Rate limiting covers `/webhooks/`, and the >500-IP window sweep no longer swaps per-IP locks out from under queued requests; `/api/internal/*` is exempt (worker callbacks carry the machine key and a 429 has no retry) (#45)
- **[security]** Internal endpoints (`/api/internal/*`) require the bearer token in `auth_users`-only deployments even without `TRAM_API_KEY` (was fully unauthenticated regardless of `TRAM_INTERNAL_AUTH_MODE`) (#44)
- **[security]** `/api/connectors/test` rejects **IP-literal** private/loopback/link-local targets across ALL `brokers`/`hosts`/`servers` entries (was: first entry only), and the REST source `test_connection` never follows redirects — a public URL cannot 302 the probe into an internal target. Hostnames that resolve to internal space are a documented residual: names are deliberately never resolved (DNS-rebinding safe) (#44)
- **[security]** Schema-registry proxy strips `x-api-key`/`authorization`/`cookie` from forwarded headers and injects the configured registry credentials (#44)
- 500/502 responses no longer leak raw exception strings (generic detail + correlation id; full error logged server-side) (#44)
- `/api/ready` no longer discloses the absolute `db_path` (#44)
- Stream runs close their sinks on stop/crash (was: ClickHouse flush timers + SFTP/AMQP/NATS connections leaked permanently on every stream restart) (#46)
- `dry_run` best-effort closes built sinks/sources (was: one leaked 2s flush timer per dry-run of a ClickHouse-sink pipeline) (#46)
- Stream stop-watcher thread exits when a crashed stream ends (was: one leaked thread per crash cycle) (#46)
- Batch retry rebuild carries the original `run_id` (was: fresh random run_id breaking the trigger→`queued_runs`→`run_history` contract and the duplicate-callback dedupe) (#47)
- A manual trigger that loses the claim race records a FAILED row under the submitted run_id (was: silent skip — the client's run_id 404ed forever) (#47)
- Fast dispatched runs no longer record a stale lease (was: `BatchReconciler` could mark a completed run FAILED, flipping pipeline status to error) (#47)
- The same fast-run lease race is closed on the queued-run drain path: `commit_queued_dispatch` skips the lease when the run already completed while still transitioning the queued row `dispatching → dispatched` (no re-drain, no duplicate execution, no spurious FAILED) (#47)
- Worker mode fails loud when `skip_processed` cannot be honored (stateless workers have no per-worker tracker — verified against the stateless-worker architecture): ERROR log + degradation marker recorded on the run (was: silent reprocessing/duplicate CDRs) (#39)

### Added
- `TRAM_DOCS_ENABLED` (default true) gates `/docs`/`/redoc`/`/openapi.json` — set `false` in production (#44)
- Startup warnings for fully-disabled auth and for `TRAM_INTERNAL_AUTH_MODE=enforce` without `TRAM_API_KEY` (#44)
- `ActiveRun.degradation_notes` on worker runs, surfaced in run-complete `errors` (#39)

### Changed
- **[migration]** `TRAM_RATE_LIMIT` default 0 (disabled) → 50 requests per 60s window, applying to `/api/` (excluding `/api/internal/*`) and `/webhooks/`; deployments relying on unlimited local access must set `TRAM_RATE_LIMIT=0` explicitly (#44)
- **[migration]** Config-schema `secret` metadata now derives from the shared `SECRET_NAME_TOKENS`; `schema_version` hash rotates (`dfcc2f98ad48` → `0efb15fc2863`) — an identity token, existing AI rows keep their recorded hash (#43)
- Helm values, `.env.example`, and `docs/deployment.md` updated for the new/changed knobs (#44)

## [1.4.5] - 2026-09-23

### Fixed
- `snmp_poll` WALK subtree boundary is tuple-space membership (`oid == base or oid[:len(base)] == base`) — the old string-prefix check with an `rstrip(".0")` char-set strip collected OIDs from sibling subtrees (live-verified: both leak classes gone against a real responder) (#32)
- `snmp_poll` classify+walk without `yield_rows` now raises a `SourceError` naming the collapsed columns instead of silently discarding all but the last row's values; with `yield_rows=true` every row emits intact (#33, live-verified)
- **[wire-type bug]** INTEGER values were classified as `_labels` in production, always: at `lookupMib=False` pysnmp decodes INTEGER as class `Integer`, so the `Integer32` branch never ran on the wire (unit tests fed `"Integer32"` strings and passed for the wrong reason). INTEGER now enters layered resolution via both class names (#35)
- Router surfaces lazily-imported page-chunk init errors (console + toast) instead of silently rendering a dead page — the mechanism behind v1.4.3's unwired detail pages (v1.4.4 review hardening)
- Pipeline-create navigation uses the create response's authoritative `created.name` (env-substituted or nested `name:` keys no longer land on a 404 detail page)

### Added
- Layered INTEGER classification (#35): `metric_patterns` → `label_patterns` → MIB SYNTAX enum (when `resolve_oids=True`) → default metric; patterns are case-sensitive globs (`fnmatch.translate`); pipeline-level and `TRAM_SNMP_METRIC_PATTERNS`/`TRAM_SNMP_LABEL_PATTERNS` env layers EXTEND code defaults (`*Id`, `*ID`, `*Index`, `*Port`), never replace
- Structured index grouping (#36): row indices come from `get_node_location`'s structured tuples instead of string re-splitting; mixed-table polls group correctly per key (no single global `index_depth`); unresolved keys with auto grouping now refuse with a `SourceError` naming the OID (was: silently emitted garbage rows); rows sort numerically (`1, 2, 10` — not `1, 10, 2`); output record contracts (`_index`, `_index_parts`, `_polled_at`, `_snmp_widths`) unchanged
- Dockerfiles: BuildKit cache mounts for pip and npm — wheel downloads persist across local image builds (`--no-cache-dir` dropped so the mount takes effect)

### Changed
- **[migration]** `snmp_poll` classify: the `*Vdom` code-default label pattern was removed — it is deployment-specific Fortigate vocabulary. Fortigate pipelines relying on the old default must add `label_patterns: ["*Vdom"]` to the pipeline (or `TRAM_SNMP_LABEL_PATTERNS=*Vdom`); without it, vdom INTEGER fields now classify as metrics by default (#35)

## [1.4.4] - 2026-09-22

### Fixed
- Wizard AI-assist path (operator-reported, all reproduced headless before fixing): AI generate no longer fires form-step validation toasts ("Select a source type", "Add at least one sink", "Pipeline name is required") for the steps the AI flow bypasses — the Review build collects silently, and "YAML generated — review and save" is an info toast, not error-styled
- Editor hand-off after AI assist delivers the AI-generated YAML verbatim (was rebuilt from the empty form state, showing a `name: ""` placeholder)
- Pipeline detail pages shipped dead (no clickable buttons) — `detail.js` was missing the `createPageController` import (regression in the v1.4.3 page-controller refactor): the chunk threw at evaluation, the router's lazy import swallowed the error, and every detail page rendered unwired; save also navigated with an empty pipeline name after AI assist — both fixed, and the wizard AI-assist regression check now exercises the full path through post-save detail (the boot check cannot see lazily-imported chunk errors)
- Browser-harness stub fidelity: the placement stub now mirrors the real backend (synthetic view for existing pipelines, 404 only for unknown) — the old unconditional 404 passed only while the detail page was dead

## [1.4.3] - 2026-09-22

### Added
- Release gate: new mandatory "UI browser smoke (Playwright)" check (`scripts/release-gate.sh` check 8, after the UI build; `--fast`-skippable, node>=20 via `TRAM_BROWSER_NODE`) — boots the freshly built SPA against fixture-stubbed API responses (`tests/browser/`, captured shapes from the live cluster) and exercises the boot/console-error class, the creation wizard (fields, validation, stale-schema guard, template seed), the editor gutter/typing/anchor, a11y tokens, and wizard YAML quoting; a new `browser-smoke` CI job runs it on every PR. `playwright` pinned exactly (1.63.0) as a `tram/ui` devDependency so the cached browser revision stays stable
- Schema identity (#24, Option A): content-hash `schema_version` (sha256 over canonical `SCHEMA_FIELDS`, 12 hex) exposed on `/api/config/schema`, `/api/plugins`, and `/api/ai/status`; every AI system prompt carries `TRAM schema v: <hash>`; `ai_usage` rows record the schema they were built against (in-place `ALTER TABLE` migration, old rows NULL); `/api/plugins` now cross-checks the Pydantic unions against the runtime registry (`schema_mismatch`: `union_only` / `registry_only`) — the two hand-maintained lists are finally machine-checked. The hash is an identity token for equality, not a semantic version
- Shared page shell (L3): one `createPageController` owns mount/unmount, the poll timer, and error/empty/loading states for every page; focus-preserving re-render keeps keyboard position and expanded rows stable across polls; the five hand-rolled poll loops are gone; `window.navigate` global call sites removed (named import)
- Run-detail route (L4): `#runs/:id` — a real page with run/pipeline/outcome/record cards, the shared failure-reason + grouped-skip-reasons + DLQ rendering (extracted from the expandable row), deep-linkable for incident sharing, with a scoped Back to the pipeline's run history
- Honest run-history totals (L4): new `GET /api/runs/count` endpoint (same filters; registered before `/runs/{run_id}` so `"count"` cannot be captured as an id); the pill shows "showing N of M", load-more is exact, heuristic fallback when persistence reports no total
- Structured creation wizard (L2): `#create` route, five steps (basics → source → sinks → schedule → review) rendering fields entirely from the live `/api/config/schema` descriptors (enum choices, defaults, multiline, `secret` masked with `autocomplete="new-password"`, only required fields revealed, optional behind disclosure); `schema_version` stale-form guard — a 60s poll plus pre-advance/save check blocks submission and offers a reload when the daemon was upgraded under an open form; template pre-seed (`#create?template=`); dry-run validation with issues mapped back to the offending step; "skip to raw YAML"/"Continue In Editor" hand-offs via `sessionStorage` (no window globals)
- Editor upgrade (L5): line-number gutter and read-only YAML highlight layer (revived theme-aware `.yk/.yv/.ys/.yn/.yb/.yc` tokens) with exact shared metrics and IME-safe visible-text mode; dry-run/save failures anchor to the offending YAML line

### Changed
- A11y (L6): muted-text contrast cleared 4.5:1 in both themes (dark `--fg-muted` `#6e7681`→`#8b949e`, light `#8c959f`→`#57606a`); off/cyan badge contrast fixed both themes; the health card is a real `<button>` — opens on focus-within, click pins, Esc closes
- Wizard entry points: dashboard and pipelines "+ New" route to the guided wizard; the raw-YAML editor remains the advanced path

### Fixed
- `melt` transform was registered and tested but absent from the `TransformConfig` union — `type: melt` pipelines failed Pydantic validation; config model added and the union↔registry state is now pinned by a test (any future divergence fails CI instead of shipping) (found by independent review)
- Run listing's queued-run merge is offset-aware — queued rows appeared in every page (duplicated on load-more, skewing the offset so real history rows were skipped); the count endpoint now exactly equals the listing total across pages (found by independent review)
- Naive `from_dt` query timestamps no longer 500 on the runs listing/count (tz-normalized to UTC at the boundary) (found by independent review)
- Wizard-generated YAML quotes numeric/boolean-looking strings (`password: "12345"` instead of a YAML int that Pydantic rejects); numeric pipeline names quoted too (found by independent review)
- Wizard schema-poll timer can no longer start after leaving the page mid-init; legacy `#wizard` bookmarks redirect to `#create`
- Run-issue expanded rows persist correctly across polling refreshes (state was DOM-only and index-keyed — collapsed and misaligned when rows shifted)
- Inherited v1.4.1 wizard sink-card selector mismatch (`.wiz-sink-card` vs `.wizard-sink-card`) that had silently broken sink type changes

## [1.4.2] - 2026-09-21

### Added
- UI wave — stability: single stacking toast container (`aria-live`, per-toast dismiss, 8s dedupe with ×N counter) with poll failures degraded to one inline offline banner; `renderTableState()` shared loading/empty/error(+retry) states across all table pages; `confirmAction()` Bootstrap modal (Enter/Esc semantics, danger variant) replaces every native confirm (Stop, Reload, rollback, deletes); responsive 12-column run tables and card grids
- UI wave — operator trust: run-history polling with pause/resume and Load-more pagination, honest count pill and CSV export cap warning; editor draft guard (`beforeunload` vs baseline YAML, debounced localStorage persistence, Restore/Discard offer, integrated with the AI-undo lifecycle); `aria-labels` on icon-only buttons, `prefers-reduced-motion` support, table rows promoted to real links; import-replace conflict modal with current-vs-uploaded side-by-side diff; next-run on the pipeline detail Schedule card; plugins page extended with schema-derived Notes (choices, secret, multiline) and per-plugin sample usage YAML
- L1 — route parameters: hash routes with path params and query filters (`#detail/:name?tab=`, `#editor/:name?return=&template=`, `#runs/:runId?pipeline=&status=&from=`, dashboard period/granularity in query); every `window._*` global handoff retired (grep-verified zero); table rows are real links; deep links survive refresh and bookmark, Back/Forward work
- Editor plugin pills now derived from `/api/plugins` (previously missing 9 transforms)
- AI settings: explicit `null`-means-clear semantics — `POST /api/ai/config` accepts JSON `null` to deliberately clear a field while blank/absent still mean "no change" (the anti-key-wipe guarantee holds)
- AI redaction: outbound prompts now also mask `headers`/`extra_headers` dict values (keys kept, `${VAR}` intact) and `alerts[].webhook_url` (URLs embed credentials in userinfo/query)
- CI: `#24` schema identifier + registry feasibility study recorded (docs/ideas/schema-registry-feasibility.md — Option A content-hash `schema_version` recommended, gates v1.4.3 L2 design)

### Changed
- CI workflow actions bumped for the Node-20 deprecation: `actions/checkout` v7, `docker/setup-qemu-action` v4, `docker/setup-buildx-action` v4, `docker/login-action` v4, `docker/metadata-action` v6, `docker/build-push-action` v7, `azure/setup-helm` v5
- Dead code deleted (~1,100 lines): the unused wizard (pages + CSS), plugins loading template, never-populated nav badges, dead `statusBadge` mapping; 401 responses now dispatch a `tram:unauthorized` event (login overlay) instead of dead-ending; health-port fallback uses `window.location.origin`

### Fixed
- `GET /api/daemon/status` returned 500 — the router called `scheduler.get_status()`, which does not exist on the controller (`get_scheduler_status()`); over-mocked unit tests hid the break (mocks updated to the real method and response shape)
- Dashboard "+ New" stale-editor bug class structurally eliminated by L1 route parameters (state no longer flows through window globals)

## [1.4.1] - 2026-09-21

### Added
- Release gate: releases are now tag-triggered (merging to main never publishes). `scripts/release-gate.sh` runs the 10 mandatory checks (ruff, pytest + 75% coverage floor, UI build, pipeline-example validation, Helm lint/template, version alignment, changelog, docs-sync, clean tree, tag check) with `--fast`/`--ci` modes; procedure documented in `docs/release-gate.md` with PR-template and AGENTS.md rules
- CI: new `ui-build`, `pipeline-lint`, and `helm-lint` jobs on pull requests
- AI assist: server-side validation of model YAML — `generate`/`fix`/`modify` now return `{yaml, valid, issues}` using the dry-run validation pattern, with provider `stop_reason`/`finish_reason` truncation warnings
- AI assist: secret redaction — `explain`/`fix`/`modify` prompts carry a redacted copy of the pipeline YAML (schema-driven secret-field masking with a name-heuristic fallback for plugin connectors; `${VAR}` env references preserved; the pipeline on disk is untouched)
- AI assist: per-call audit trail — a structured `tram.ai` log line (mode, client host, provider, model, tokens in/out, duration, outcome) plus an append-only `ai_usage` table gated by `TRAM_AI_AUDIT` (default on, fails open)
- AI assist: optional `TRAM_AI_ALLOWED_BASE_URLS` prefix-match allowlist restricting accepted base URLs at save time
- Editor: "Undo AI change" after AI generate/modify/fix (one-level snapshot, retired on typing or saving), auto-opened diff on all AI paths, and immediate validation feedback (`valid`/`issues`) instead of discovering truncation or parse errors only at dry-run

### Changed
- AI provider calls no longer block the event loop: `_call_ai` runs via `asyncio.to_thread` with explicit 60 s timeouts on the anthropic and openai clients (matching the Bedrock path)
- AI `base_url` now enforces `https` — `http` only for loopback/private-range hosts (local Ollama/LiteLLM keeps working; `169.254.0.0/16` deliberately rejected to block cloud-metadata SSRF); checked at save time (400) and call time
- Release workflow authenticates to GHCR with the built-in `GITHUB_TOKEN` instead of the `GHCR_TOKEN` PAT — the token can no longer expire, and pushed packages stay linked to the repository (the `GHCR_TOKEN` secret can be deleted)
- AI settings docs synced: `docs/api.md` AI section rewritten to the real response shapes (including `fix`/`modify`/config/test endpoints), `TRAM_AI_*` documented in `helm/values.yaml` (+ `envSecret` example), `.env.example`, and `docs/deployment.md`; Base URL help text corrected (honored for Anthropic, required for Bedrock); unknown providers rejected with 400 at save time

### Fixed
- Settings no longer wipes stored AI keys: blank/absent config fields mean "no change" (the endpoint can set a value but never clear one), and the UI omits blank fields from the save payload — an untouched password field can no longer delete a DB-stored key
- Dashboard "+ New" no longer opens the editor in edit mode of a previously viewed pipeline (which silently overwrote that pipeline on Save) — editor globals are reset before navigating, mirroring the pipelines page
- Docker images: the UI build stage (`ui-builder`) in `Dockerfile` and `Dockerfile.manager` now runs
  natively on the build host via `--platform=$BUILDPLATFORM` instead of under QEMU emulation on the
  arm64 leg. The static assets are architecture-independent; node-under-QEMU is slow and prone to
  sporadic libuv/V8 deadlocks (the 1.4.0 standalone arm64 build hung 4+ hours in `npm ci` because of it).

## [1.4.0] - 2026-09-16

### Added

- Added warn-only auth mode for internal machine-to-machine surfaces: `TRAM_INTERNAL_AUTH_MODE=off|warn|enforce` (default `warn`) now governs `/api/internal/*` on the manager and `/agent/*` on workers — missing/invalid keys are logged at WARNING and still served, so rolling out keys never 401s a working deployment; `enforce` (401) is the Phase 2 flip with no code changes. An invalid `TRAM_INTERNAL_AUTH_MODE` value is logged at WARNING and falls back to `warn`
- Worker callbacks now authenticate: `_post_run_complete` and `_post_stats` send `X-API-Key` (from the worker's `TRAM_API_KEY`) on every run-complete/stats POST to the manager
- Worker agent API (`/agent/*`) now runs the same `APIKeyMiddleware` as the manager ingress; `/agent/health` (K8s liveness/readiness probe) is unconditionally exempt on both the manager and worker servers
- Webhook ingestion now enforces a configurable body-size limit (`TRAM_WEBHOOK_MAX_BODY_BYTES`, default 10 MiB) — oversized payloads are rejected with 413 instead of being fully buffered in memory
- Added local-image retention cleanup to both `scripts/deploy-docker-standalone.sh` and `scripts/deploy-kind-tram-dev.sh`, keeping the newest 5 `local-*` images per repository by default with a `--keep-images` override
- Added `scripts/deploy-docker-standalone.sh` to build, run, and manage a single standalone TRAM Docker container with a persisted Docker data volume, auto-created host pipeline mounts, optional host output bind mounts, timestamp-tagged local auto-builds for repo workflows, `--ghcr` pull support for the published `ghcr.io/tosumitdhaka/trishul-ram:<tag>` image, log/status helpers, optional UDP port publishing, and a README-friendly GitHub bootstrap flow
- Manager dispatch now distinguishes `no_capacity` (no healthy workers) from `dispatch_failed` (the dispatch attempt errored) and records the real worker error in run history; both outcomes increment `tram_mgr_dispatch_total`, with `dispatch_failed` as a new result label alongside `no_workers` and `accepted`
- Worker health polling now uses hysteresis — a worker is marked down only after 2 consecutive failed probes instead of a single failed probe
- Live in-flight runs (worker streams and standalone batch runs) are now merged into the stats dashboard mid-run: the 15-minute cards, load chart, and per-pipeline rows reflect live counters instead of only completed run history, and late stale stats payloads for already-completed runs are dropped (completion-boundary guard)
- New metric `tram_mgr_stats_missed_total{worker_id}` counts worker→manager stats-post misses (incremented on the worker process)
- `TRAM_STREAM_SINGLE_PLACEMENT` (default `1`, GH #17): count=1 stream dispatch now produces a durable 1-slot placement row — manager-restart adoption, worker-death recovery (within ~40-60s), and stale-config detection become reconciler-driven. Rollback: set `0` and restart the manager; placement rows keep working under either value. An unrecognized value is logged at WARNING and fails open (feature ON)
- Manager boot now materializes a placement from a pre-upgrade live count=1 run (adoption, zero interruption); the reconciler's unplaced-stream pass converts running placement-less streams without a restart, self-heals bookkeeping, and stops duplicate stray runs (earliest kept)
- Config-drift detection: a live stream running stale YAML is stopped and redispatched with the current config, with new `tram_mgr_reconcile_action_total` labels `config_drift_redispatch`, `stream_recover`, and `adopt_materialize`; older agents that do not report `config_sha256` fail open (never acted on)
- The worker agent now exposes `config_sha256` of the dispatched YAML in `/agent/status`, passed through the worker pool's `live_streams()`/`find_pipeline_runs()` (absent means unknown)
- ASN.1 `serializer_in.split_path` + `split_path_context` (GH #19): split the record list inside a single decoded BER document at a dot-notation path (e.g. `measurement.measValues`), with sibling context deep-copied into every record (record fields take precedence; scalars wrapped as `value`). `split_path` requires `record_chunk_size > 0`, is mutually exclusive with `split_records`, and `split_path_context` requires `split_path`; a missing or non-list path fails loud naming the path. The chunked fan-out bounds memory only on sequential runs — `thread_workers > 1` applies the split eagerly
- Queued manual runs (GH #21): a manual run triggered with no healthy workers is durably queued (survives manager restarts; absolute TTL clock) and auto-dispatched when capacity returns, with a worker-restored nudge waking the drain. `TRAM_QUEUE_MANUAL_RUNS` (default `1`; unrecognized values fail open with a WARNING; `0` = legacy fail-fast rollback) and `TRAM_QUEUE_TTL_SECONDS` (default `900`). Pipeline status `queued`; stop/delete purge; `update()` refreshes the queued YAML snapshot; a dispatch failure reverts to queued (retry next pass, TTL-bounded); expiry writes a FAILED run-history row ("no worker capacity within N minutes — queued run expired") even during a full worker outage. Six `tram_mgr_queue_*` metrics (depth, enqueued/dispatched/expired totals, wait histogram, drain outcomes)
- File-done guards on the local and SFTP sources: `file_stability_seconds` (two-phase in-run size+mtime scan — only files whose stats are unchanged across the interval are read), `file_min_age_seconds` (future-mtime tolerant: an SFTP server clock ahead of the manager no longer starves files; one-time skew WARNING), and `file_done_suffix` (collect only renamed-to-done files; the suffix is stripped from `source_filename`, sink `{source_stem}` tokens, and `move_after_read` destinations — note: enabling it on an existing pipeline changes those names). All default off — existing pipelines unchanged
- gNMI `subscription_mode: stream|once|poll` (`poll` = periodic ONCE re-gets every `poll_interval_seconds`); a lost gNMI session no longer silently ends the pipeline — automatic reconnect with `reconnect_delay_seconds` backoff bounded by `max_reconnect_attempts` (successful sessions and successful polls reset the counter); `stop()` closes the active session in all three modes so shutdown is prompt
- `timestamp_normalize.source_timezone` (IANA name, default `None` = today's UTC assumption): naive input timestamps are interpreted in the source timezone (DST-correct via zoneinfo, including spring-forward gap handling) and normalized to UTC; aware timestamps and unix epochs are unaffected; invalid names fail at config load (requires tzdata installed)
- `counter_delta` transform (F.1): per-key counter deltas with Counter32/64 wrap correction (width authoritative from SNMP `_snmp_widths`, else explicit, else auto), reboot/reset detection by gap size (`reset_threshold × width`), optional per-second rate over actual elapsed time, key isolation by source host + key fields, `first_sample: pass|drop` (drop is record-level), `on_error: raise|null|keep` with per-field semantics (`null` keeps already-computed outputs; `keep` returns the pristine record)
- `window_aggregate` transform (F.1): epoch-aligned UTC tumbling windows (default 900s — the 3GPP 15-min standard), watermark finalization (max event time − `allowed_lateness_seconds`), bounded lateness (late-after-finalize dropped and counted), accumulator-based durable state (not samples), `flush_on_close` honored on graceful stream stop; a crashed stream keeps its open windows in state so a redispatch continues them
- Durable per-pipeline transform state (F.1): `transform_state` table (standalone) / `GET|PUT /api/internal/transform-state/{pipeline}` (worker mode; `TRAM_STATE_MAX_BYTES`, default 20 MiB, 413 on excess); state saved only on successful runs, re-hydrated on retries from the same in-run snapshot, discarded on config-hash mismatch; `update()`/`delete()` purge the row
- `state_persist_interval_s` pipeline field (stream state snapshots) and `?flush=true` manual flush runs — a flush run emits open windows as partials and clears them from state. Note: a queued flush run executes as a normal run when capacity returns — re-issue `?flush=true` after capacity returns to flush
- `TRAM_STATEFUL_TRANSFORMS` flag (default `1`, unrecognized values fail open with a WARNING; `0` = stateful transforms rejected at validation + internal state endpoints 404 — rollback without redeploy)
- SNMP `classify: true` records additionally carry `_snmp_widths` (Counter32/Counter64 authoritative wrap width); additive, ignored by older stacks
- New metrics: `tram_transform_counter_wraps_total`, `tram_transform_counter_resets_total`, `tram_transform_state_io_total`, `tram_transform_window_late_dropped_total`, `tram_transform_windows_emitted_total`

### Changed

- Removed the committed plaintext defaults `apiKey: "tram-internal-2026"` and `authUsers: "admin:tram@2026"` from `helm/values.yaml` — both now default to empty (auth disabled for dev/kind); production must set them explicitly or via `envSecret.TRAM_API_KEY` / `envSecret.TRAM_AUTH_USERS`. Note that `TRAM_INTERNAL_AUTH_MODE=enforce` only rejects missing/invalid keys when `TRAM_API_KEY` is actually set — without a key configured, internal surfaces pass through (the server cannot validate a key it does not have). **Upgrade note for Helm users:** the chart no longer ships default `apiKey`/`authUsers`; set them explicitly (or via `envSecret`) in the same upgrade that installs this release, otherwise previously-protected deployments become unauthenticated — the old committed defaults silently authenticated every install
- **Potentially breaking:** webhook ingestion now rejects bodies larger than `TRAM_WEBHOOK_MAX_BODY_BYTES` (default 10 MiB) with `413` instead of buffering them — webhooks sending larger payloads must raise the limit via the env var
- **Potentially breaking:** the ClickHouse `table` config is validated as an identifier at construction time — quoted/bracketed table names (e.g. backticked `` `tbl` ``) and any value containing quotes, semicolons, or other break-out characters are now rejected with a `SinkError`; plain identifiers and qualified `db.table` names remain accepted
- API key authentication via the `?api_key=` query param was removed — clients must send the `X-API-Key` header; query-param keys leaked into access/proxy logs and browser history
- `post_batch_cleanup` now defaults to `true` instead of `false`: existing batch pipelines get a post-batch `gc.collect()` and best-effort `malloc_trim(0)` unless they opt out with `post_batch_cleanup: false`
- The worker image now sets `MALLOC_ARENA_MAX=2` to mitigate glibc per-arena heap fragmentation in long-lived worker processes (GH #16)
- Serializer schema caches (`asn1`, `protobuf`) are now keyed by schema content hash instead of file mtime and bounded by a size-capped LRU; re-syncing unchanged schema assets no longer triggers a recompile, and batch runs close their sinks (including DLQ) after completion
- The Kafka source now defaults to `enable_auto_commit: false` and commits offsets explicitly, once per poll batch, only after the batch has been consumed by the caller (at-least-once). Previously the default auto-committed offsets for messages that were polled but not yet sink-written, silently losing them on a crash. Pipelines may opt back into the legacy at-most-once behavior with `enable_auto_commit: true`. Note: with `thread_workers > 1` the batch commit can fire while up to `2 * thread_workers` messages are still queued for workers — use `thread_workers: 1` for strict at-least-once. Kafka lag metrics are now sampled once per poll batch instead of per message.
- Restarting a local stream pipeline no longer waits up to 15s for the previous stream thread to exit: `_stop_stream()` signals the old thread and returns immediately, and `_start_stream()` starts the replacement without joining, with the old thread's cleanup identity-checked so it can never touch the new thread's bookkeeping. The old and new instances can briefly overlap (bounded by how quickly the source observes the stop event — at most one in-flight chunk), during which a record can be double-written on restart, matching the bounded stop exposure that manager-mode stream stops already have.
- **Potentially breaking (metrics semantics):** `records_out` now counts records actually delivered to at least one sink (max of per-sink written counts) instead of the input chunk size whenever any sink wrote — for condition-filtered or partially-failing sinks the number drops to what was really written, and `bytes_out` still counts full I/O fanout. Note: with disjoint (partitioned) sink conditions the count is a conservative lower bound. Alert thresholds or capacity calculations keyed to the old numbers may need adjustment
- `json_flatten`/`explode` transforms are now linear on large nested lists (a 10k-element explode drops from minutes to milliseconds, GH #18) and exploded records no longer alias the source record
- Manager worker probes (health polling, status, and live-streams fan-out) now run in parallel per worker — a slow or unreachable worker no longer serializes the whole fan-out or stalls the API endpoints that trigger it
- `/api/cluster/nodes` now reflects the debounced (2-consecutive-failure) worker health state instead of a one-shot probe result, and `/api/pipelines/{name}/placement` renders a synthetic single-slot view for streams without a placement row when live stats exist (previously a 404 in manager mode); standalone streams with no stats entry return a clean 404, which the UI renders as a hidden placement card
- Templates page (UI): the YAML preview now uses the shared detail-viewer header (Back / template name / Deploy / close) and the shared capped YAML view; the row View/Deploy pair rides the shared button contract; the filter bar is hidden while previewing (filter state preserved); template YAML is served without trailing newlines
- `POST /api/pipelines/{name}/run` now returns **202** `{"status":"queued","run_id","expires_at"}` in the no-capacity case (previously 200 followed by an immediate FAILED run) — external automation that treats non-200 as failure must be updated; a re-trigger while queued returns the existing run_id (idempotent), including the capacity-returned window before the drain commits
- `GET /api/runs` merges queued runs into the list (status `queued`, `finished_at` null; always on the first page); `GET /api/runs/{id}` returns the queued view instead of 404; pipelines list/detail carry a `queued_run` field when a non-terminal queued row exists
- The six dialect-branching upsert implementations in the persistence layer were consolidated onto one `_upsert` helper (no behavior change; `save_pipeline` preserves `created_at` across saves); new additive `queued_runs` table
- UI: `queued` status badge (amber, pulsing dot), disabled "Queued…" Run Now state with expiry info row on the detail page, queued rows in the runs table, and `queued` status filters on the pipelines and runs pages
- Stateful transforms (`counter_delta`, `window_aggregate`) are rejected at validation when combined with `thread_workers > 1` or placed in sink-level `transforms`, and broadcast stream placement (`workers: count>1|all|list`) with a stateful transform is rejected at runtime in manager mode (pipeline `error`); `tram validate` warns (L013)

### Fixed

- API-key and webhook-secret comparisons now use `hmac.compare_digest` (constant-time) instead of plain `==` — closes the timing side-channel on the global API key (`middleware.py`) and per-webhook `Authorization: Bearer` secrets (`webhooks.py`)
- ClickHouse sink now validates the `table` config as a bare or qualified identifier (`events`, `db.table`) at construction time, rejecting quoted/bracketed names and anything that could break out of the interpolated `INSERT INTO ... VALUES` statement
- Alert-rule edits made via the API now survive controller restarts: the alert save path routes through `controller.update()` and persists to the registered-pipelines store. Previously it re-registered in memory and saved only a version-history row, so alert changes vanished on restart
- The pipeline watcher now stops and removes a pipeline when its file is deleted from the watched directory (previously a swallowed `AttributeError` left streams and jobs running), and file-triggered reloads persist via the DB
- Threaded batch runs (`thread_workers > 1`) no longer mark source files as processed before their records are fully written: sources gain a deferred `finalize()` hook invoked after a file's chunks drain, and the in-flight chunk window is capped at `2 × thread_workers` (closes the crash data-loss window and bounds executor queue memory). Failed retry attempts now close their sinks instead of leaking them
- Syslog-over-TCP now applies RFC 6587 framing (octet-counted with newline-delimited fallback, per-connection mode detection) with a configurable `max_message_size` guard — previously a single `recv` per connection truncated oversized messages and merged multiple messages into one record
- Manager restart no longer double-dispatches a count=1 stream that is still live on a worker: boot load adopts the existing run instead of dispatching a second instance. A worker unreachable at manager boot is marked down immediately instead of passing one poll interval as healthy
- Controller lifecycle transitions (trigger, update, delete, status reads, worker callbacks, broadcast-placement commits) are serialized under a reentrant lock, closing the trigger TOCTOU, the update/delete deregister window, the duplicate-callback race, and the placement-reconciler slot-mutation race; the reconciler now commits slot changes through the controller instead of writing shared state directly
- Placement slot run-id updates are now per-slot compare-and-set writes (keyed on placement group, slot index, and expected run id) — a stale stats payload from a superseded restart can no longer regress the recorded run id, and a lost write race is detected and skipped instead of silently clobbering
- `on_error: retry` now resets the live stats accumulator along with the retry context, so live totals match the final run-history numbers
- Failed stats heartbeats are logged at WARNING with pipeline context and counted (previously DEBUG-swallowed, making manager outages invisible)
- Restored broadcast placements re-register their worker-pool run assignments after a manager restart, so `stop_run` reaches the worker directly instead of falling back to probe-all
- A running stream with no placement record and no live run is now recovered (redispatched, or marked stopped when the pipeline is disabled) instead of being stuck "running" forever (GH #17)
- Broadcast (count=N/all/list) streams can no longer be downgraded to 1-slot placements by the unplaced-stream reconciliation pass (guard mirrors the boot path), and reconciler-side materialization now activates the pipeline's Kubernetes Service and deactivates stale placement rows, matching the boot path
- Worker bookkeeping (`_assignments` / `_pipeline_workers` entries) is now reaped when a worker is marked down, instead of leaking until the manager restarts
- Templates page (UI): removed the dead bespoke templates-preview CSS and the off-contract preview header (GH #20)
- Kafka source `stop()` now closes the consumer so a quiet topic no longer delays stream shutdown by the poll timeout; reconnect backoff is stop-interruptible; at-least-once semantics unchanged
- CORBA `skip_processed` keys are time-bucketed (`dedupe_window_seconds`, default 300, validated ≥ 1): a scheduled collection with constant operation+args no longer runs once and then skips forever; re-invocations within a window still dedupe

---

## [1.3.3] — 2026-05-01

### Added

**Overview and cluster operator visibility**
- Dashboard overview now exposes split 15-minute Records In/Out and Bytes In/Out cards, a bytes-processed load chart, chart metric toggle, bucket tooltip, and manual refresh affordance
- Cluster now surfaces runtime-first summary cards, merged input/output traffic cards, processed records/bytes totals per worker, and live records/bytes in/out metrics in both the summary row and worker details

**Shared UI/runtime helpers**
- Shared triggered-run monitoring now backs dashboard, pipelines, and detail lifecycle actions
- Shared YAML diff rendering now backs both detail and editor version/result flows instead of separate page-local implementations

### Changed

**Shell, routing, and templates**
- Hash navigation now preserves browser-history semantics; `#templates` canonicalizes through Pipelines with the templates modal active, and `#wizard` returns to Pipelines while the wizard stays intentionally disabled
- The old dedicated Templates page was removed; template browsing is consolidated into the Pipelines modal and aligned to the shared modal/button system
- Login/bootstrap flow no longer flashes protected-page unauthorized errors on fresh-session deep links

**Pipeline detail, editor, and settings surfaces**
- Pipeline detail now opens on `Runs` by default, uses a streamlined header/tool layout, and presents version history through a shared scrollable compare/view/copy/download/rollback workflow
- Version history semantics were tightened so duplicate identical YAML versions are suppressed and rollback re-activates the existing historical version instead of cloning it
- Editor, diff, dry-run, connector-test, and AI result surfaces now use theme-safe shared cards/panels instead of page-local dark-only treatments
- Settings remains configuration-focused while live runtime daemon state moved into Cluster

### Fixed

**Runtime truth and action feedback**
- Stopped manual pipelines now consistently surface `Run now` instead of `Start` across overview and detail flows
- Manual/batch trigger attempts with no healthy workers now record an actionable failed run with `No healthy workers available for dispatch` instead of only flipping pipeline state
- Pipeline action feedback now reflects backend truth (`started`, `already active`, `manual`, `disabled in YAML`) rather than optimistic success toasts
- Detail version-YAML caching is now scoped per pipeline, preventing cross-pipeline bleed between version viewers and compare flows

**Supporting pages and assets**
- Schemas and MIBs pages gained search and cleaner shared styling; MIB management now persists raw ASN.1 source files alongside compiled artifacts for later dependency-resolving compiles
- Runs export/filter behavior is aligned to the backend contract, and noisy repeated skip-note rendering was reduced in the shared runs table
- Cluster worker assignment display now follows current controller truth instead of stale worker-pool history

### Validated

- Full manual validation completed across the active UI surface in both manager and standalone modes
- Local release validation completed for `1.3.3`: `ruff check .`, full `pytest tests/ -q`, and `cd tram/ui && npm run build`

---

## [1.3.2] — 2026-04-21

### Added

**Standalone live stats parity**
- Local stream runs on standalone deployments now create a `PipelineStats` entry in `StatsStore`; `GET /api/pipelines/{name}/placement` returns a synthetic single-slot view instead of 404
- A background stats loop emits live `uptime_seconds` updates; a lock-guarded race fix prevents a stopped stream from being re-inserted after exit
- Stats loop is suppressed in manager mode (worker stats flow through the normal stats callback path)

**Manager operational metrics**
- 8 new `tram_mgr_*` Prometheus series: `tram_mgr_dispatch_total`, `tram_mgr_redispatch_total`, `tram_mgr_reconcile_action_total`, `tram_mgr_placement_status`, `tram_mgr_worker_healthy`, `tram_mgr_worker_total`, `tram_mgr_run_complete_received_total`, `tram_mgr_pipeline_stats_received_total`
- All series have `_NoOp` fallbacks when `prometheus_client` is not installed; `/metrics` returns 503 with install hint instead of 500
- `/metrics` docstring clarifies that series are process-local to the manager; worker execution metrics require scraping each worker pod

**UDP multi-worker streams**
- `syslog` and `snmp_trap` sources now support `kubernetes: enabled: true` in manager mode; `KubernetesServiceManager.is_eligible` extended to all push sources
- UDP Services use `protocol: UDP`; service/target port derived from `source.port` (fallback: 514 for syslog, 162 for snmp_trap); overridable via `kubernetes.port` / `kubernetes.target_port`
- `count: N` and `workers.list` use manual `Endpoints` objects targeting only dispatched worker pods; `count: all` uses the broad worker label selector
- `KubernetesServiceConfig` gains optional `port`, `target_port`, `load_balancer_ip`, `annotations` fields; `ClusterIP` added as valid `service_type`
- `delete_service()` now cleans up manual `Endpoints` for both `workers.list` and `count: N` pipelines

**Large-batch resilience**
- `BatchReconciler` now runs alongside `PlacementReconciler` so the manager can scan worker `/agent/status`, adopt orphaned running batch runs after restart, and synthesize failures when a worker-owned batch run disappears before callback
- batch run completion paths now reuse the normal controller finalization logic, so manual, interval, and cron pipelines keep the same status transitions even when a lost run is reconciled instead of completing normally
- `record_chunk_size` was added to pipeline config for bounded record windows in serial batch runs; serializers can implement `parse_chunks(data, record_chunk_size)` and the ASN.1 serializer now decodes concatenated BER payloads incrementally instead of materializing one giant list first

### Changed

**Linter rules**
- L008 removed (was blocking all UDP push sources in manager mode)
- L012 added (error): UDP push sources in manager mode require `kubernetes: enabled: true` — no pre-existing shared UDP ingress exists in the worker chart
- L006 updated: `kubernetes: enabled: true` now permits `count: N` and `workers.list` in addition to `count: all`; the controller threads `dispatched_worker_ids` into the service manager to avoid over-selection

**Safe staged file output cleanup**
- local and SFTP sinks now delete their run-scoped staged temp file on `finalize_source(..., success=False)` instead of leaving it behind
- when a new staged write begins for the same deterministic final filename, stale `.tram-*.tmp` artifacts from failed prior attempts are discarded before the new run writes
- staged safe-finalize is applied only to record-safe serializers (`csv`, `ndjson`) in the serial batch path and publishes output only after a source file completes successfully

**Optional post-batch heap cleanup**
- batch-mode heap cleanup is now pipeline-controlled via `post_batch_cleanup: true` instead of being enabled globally for every batch run
- when enabled, the executor performs `gc.collect()` and best-effort `malloc_trim(0)` after the batch result is finalized; default remains off to avoid surprising process-wide pauses on shared workers

### Validated

- kind cluster: `snmp_trap → file` with `count: all` — NodePort UDP Service created; ECMP routes each sender consistently to one worker
- kind cluster: `snmp_trap → file` with `count: 2` — manual Endpoints target exactly the 2 dispatched worker pods
- kind cluster: `webhook` with `count: 2` + `kubernetes: enabled: true` — no L006; HTTP Endpoints target exactly 2 workers (HTTP regression confirmed)
- kind cluster: `snmp_trap` without kubernetes block — L012 fires; add block — L012 clears, no L008

**ASN.1 structured decode flattening**
- `split_records: true` on the `asn1` serializer splits concatenated BER files into individual top-level TLV records before decode; supports short-form, long-form, and indefinite-length (0x80) encodings
- `message_classes: [...]` accepts an ordered fallback list of root ASN.1 types; all-fail raises `SerializerError` and routes to DLQ; mutually exclusive with the existing `message_class` field
- `bytearray` values are now hex-stringified in `_to_json_safe()` alongside `bytes`
- `json_flatten` transform — now uses the explicit ordered row-shaping contract for nested payloads with `explode_paths`, `zip_groups`, `choice_unwrap`, final dotted-key flattening, and `drop_paths` on flattened keys; this replaces the earlier heuristic `explode_mode` / `zip_lists` behavior
- `hex_decode` transform — registered as `"hex_decode"`; decodes hex-string leaf values produced by `_to_json_safe()`; `mode: utf8_or_hex|latin1_or_hex|hex`; per-path `overrides` with `decode_as`, `format`, optional `bit_length_field`, optional bit-index `mapping`, and `output` for `bit_flags` (`names|indexes|both`); does not re-invoke asn1tools

**CDR record shaping — dotted-path transform support**
- Shared `tram/transforms/path_utils.py` — `get_path`, `set_path`, `delete_path`, `rename_path` helpers with consistent dict-only traversal semantics; used by all path-aware transforms
- `unnest`, `explode`, `drop`, `rename`, `value_map`, `cast` — all `field`/`fields` config keys now accept dotted paths (`a.b.c`); plain top-level keys unchanged; list-index syntax not supported
- `project` transform — declarative final-schema extraction/rename step with compact `output: source.path` form plus expanded `source`, `source_any`, `default`, and `required` options
- `unnest`: missing nested path passes through unchanged; only present non-dict values trigger `on_non_dict` behavior
- `explode`: scalar elements in nested lists write back via `set_path` to the correct nested location
- `rename`: prefix-overlap detection at init time raises `TransformError` for conflicting source paths; both source and destination may be dotted
- All path-mutating transforms use `deepcopy` per record to prevent nested mutation leaking across output rows

**CDR record shaping — new primitives**
- `select_from_list` transform — selects elements from a list field by exact-match predicate or `first_item: true` without exploding the record; multi-select in one invocation via `select: [...]`; projects element fields to top-level output names; `on_no_match: null_fields|raise` (default `null_fields`); `name` optional for error context; duplicate output fields across selections rejected at config load
- `coalesce_fields` transform — writes each output field from the first non-empty candidate path in `sources`; default `empty_values` is `[null, ""]`; `default` used when all candidates miss
- `drop` transform — `fields` now accepts either `list[str]` for unconditional drops or `dict[path, list[value]]` for conditional drops; conditional matching supports dotted paths and removes a field only when its value equals one of the configured values
- light path-pattern support — `hex_decode.overrides[].path` and `json_flatten.drop_paths` now accept simple single-segment `*` wildcards; exact path matches keep precedence over wildcard rules
- Both transforms registered in `tram/transforms/__init__.py` and included in the `TransformConfig` union in `pipeline.py`

---

## [1.3.1] — 2026-04-20

### Added

**Placement and K8s exposure for push streams**
- `workers.count: N` and `workers.list` placement behavior is now implemented for multi-worker push streams in manager mode
- Dedicated per-pipeline Kubernetes Service provisioning is now available for active `webhook` and `prometheus_rw` stream pipelines
- `workers.list` dedicated Services use explicit `Endpoints` targeting only the selected worker pods

**File sink naming and partitioning**
- File sinks now support shared filename variables derived from source context, including `source_stem`, `source_suffix`, and `source_path`
- Executor-side record partitioning is now available for file sinks via dotted `{field.*}` filename variables
- Rolling file output now supports `max_records`, `max_time`, and `max_bytes` in append mode across local, SFTP, FTP, S3, GCS, and Azure Blob sinks

### Changed

**SNMP dependencies**
- SNMP connectors now target `pysnmp>=7,<8` with compatibility helpers for the 7.x HLAPI surface
- Legacy lextudio-specific runtime package references were removed from the implementation path

**Helm / K8s defaults**
- Manager resource settings are now resolved from `manager.resources` before falling back to top-level defaults
- Kind/dev chart values continue to live in `helm/values.yaml`; generic release-oriented defaults live in `helm/values-template.yaml`

### Fixed

- `workers.list` dedicated pipeline Services now repatch manual `Endpoints` when placement changes, including worker disappearance during scale-down
- Dedicated `workers.list` services no longer retain stale pod IPs after a pinned worker becomes unavailable
- Alert cooldown logic, placement reconciliation, and ingress split behavior were revalidated against the `1.3.1` release build on a live kind cluster

### Tests

- Full local release validation completed for `1.3.1`: lint, unit, integration, and coverage
- Helm validation completed, including dependency update and chart lint
- Live kind validation completed for manager/worker rollout, push ingress, placement APIs, dedicated Services, and scale-down stale-slot recovery

---

## [1.3.0] — 2026-04-17

### Added

**Multi-worker streams for HTTP push sources**
- `workers:` config now supports multi-worker placement for `webhook` and `prometheus_rw` in manager mode, defaulting those sources to `count: all`
- `WorkerPool.multi_dispatch()` and placement tracking allow a single stream pipeline to run across all healthy workers
- New placement visibility endpoints:
  - `GET /api/pipelines/{name}/placement`
  - `GET /api/cluster/streams`

**Placement persistence and reconciliation**
- Active multi-worker placements are persisted in `broadcast_placements`
- `PlacementReconciler` detects stale slots, re-dispatches recovered workers, and restores placement state after manager restart
- Placement slot metadata now persists immutable `run_id_prefix` and mutable `current_run_id`

**Unified pipeline stats and load-aware dispatch**
- Batch and stream runs now share `PipelineStats` with records, bytes, error counters, and rolling error windows
- Workers report periodic stats to the manager; final batch totals are persisted through the run-complete path
- `StatsStore` is now keyed by `run_id` with stale-aware lookups for reconciliation and placement views

### Changed

**Worker ingress split**
- Worker pods now run two listeners:
  - internal agent API on `:8766`
  - ingress-only webhook receiver on `:8767`
- Worker `/agent/health` now reports composite status and fails when the ingress listener is down

**Manager Helm deployment**
- Manager changed from `Deployment` to single-replica `StatefulSet`
- Added manager headless service and `manager.persistence.existingClaim` support for Deployment → StatefulSet upgrades
- Worker StatefulSet now exposes ingress port `8767`

**Alerts**
- Alert cooldown is now armed only after confirmed webhook or email delivery succeeds

### Fixed

- Multi-worker stream slot completion no longer drives the pipeline state machine while sibling slots are still running
- Intermediate placement slot completion no longer evicts stats too early and trigger re-dispatch storms
- Stream run completion now persists final counters instead of zero totals in run history

### Tests

- Expanded unit coverage for worker ingress split, placement reconciliation, stats store, worker dispatch, pipeline controller, and placement/streams APIs
- Release-prep validation completed on 2026-04-17 for lint, unit/integration/coverage, Helm, and local kind deployment

---

## [1.2.3] — 2026-04-16

### Fixed

**SNMP poll — `walk` could stall indefinitely at subtree end**
- `tram/connectors/snmp/source.py`: `_do_walk()` now stops when returned OIDs fail to advance numerically, preventing an infinite loop when some agents repeat the terminal OID at the subtree boundary
- Regression test added to `tests/unit/test_snmp_connectors.py`

**Manager + Worker — callback/run metadata correctness**
- Real worker callback timestamps now propagate to manager run history instead of being overwritten locally
- Run IDs in `PipelineController` now remain full UUIDs rather than truncated 8-character values

**Browser auth bootstrap**
- DB-backed browser auth no longer requires `TRAM_AUTH_USERS` once users exist in the database; docs and `.env.example` updated to match implementation

**SNMP trap sink config naming**
- `trap_oid` is now the documented/configured field for outgoing SNMP trap OID selection
- Legacy `enterprise_oid` remains accepted as a backward-compatible alias

### Changed

**ASN.1 serializer**
- Documentation and tests now explicitly describe ASN.1 support as decode-only
- Added coverage for decode behavior, malformed input handling, and schema compile/cache paths

**Example pipelines and docs**
- Bundled pipeline examples were brought back in line with the current schema and are validated by test
- Quick-start/live docs continue to use `latest`, while version-pinned examples were updated to `1.2.3`

**SNMP validation**
- Added SNMPv3 validation pipelines for real-device `GET` and `WALK`
- Verified SNMPv3 `GET` and `WALK` against a live host during release preparation

### Tests

- `ruff check .` passes
- `pytest tests/unit/test_loader.py -q -o log_cli=false` passes
- `pytest tests/unit/test_snmp_connectors.py -q -o log_cli=false` passes
- Live SNMPv3 validation completed:
  - `snmp_get_v3_system_to_sftp_json` — success
  - `snmp_walk_v3_iftable_to_sftp_json` — success

---

## [1.2.2] — 2026-04-15

### Fixed

**CLI — `validate` and `run --dry-run` crashing on valid pipelines**
- `load_pipeline()` returns a `(config, raw_yaml)` tuple; both commands were treating it as a plain object, causing `AttributeError: 'tuple' object has no attribute 'name'`
- Unpacking corrected in `tram/cli/main.py`

**Watcher — hot-reload raising `PipelineAlreadyExistsError`**
- `pipeline_watcher.py` now passes `replace=True` and `yaml_text` to `manager.register()` on file change; previously a changed YAML caused an error instead of updating the pipeline

**Docs — API response shapes drifted from implementation**
- `docs/api.md`: corrected response shape for dry-run (`{valid, issues[]}`), connector test (`{ok, latency_ms, error}`), and change-password (`{ok, username}`)
- `docs/connectors.md`: `on_error` valid values fixed to `continue | abort | retry | dlq`; `stop` was documented but never accepted by the model

**CI — `omniORBpy` pip install failure**
- `omniORBpy` is a system-only package (requires omniORB shared libs); it cannot be installed from PyPI as a wheel
- Removed `tram[corba]` from the `all` pip extra; the `corba` extra itself remains for users who have omniORB installed on their system

**Tests — stale `sha256$` assertion in auth tests**
- `test_auth_utils.py`: `test_returns_sha256_prefix` updated to `test_returns_scrypt_prefix` — the password hasher was upgraded to scrypt in v1.2.1 but the test was not updated

### Changed

**Repository layout**
- `tram-ui/` moved to `tram/ui/` for a cleaner project structure; `Dockerfile` paths updated accordingly
- `CHANGELOG.md` and `CHECKLIST.md` moved into `docs/` as `changelog.md` and `checklist.md`; broken `../CHANGELOG.md` link in `docs/index.md` fixed

**Documentation**
- `README.md` overhauled: rewritten around concrete telecom use cases (PM collection, SNMP trap mediation, gNMI telemetry, syslog aggregation, CORBA mediation) with YAML examples; version history section replaced with link to changelog
- `docs/roadmap.md` created: replaces `docs/roadmap_1.2.0.md`; features/issues only, versioned where confirmed, unassigned items in backlog
- `docs/index.md`: version updated to 1.2.2; roadmap and checklist linked
- `.gitignore`: `CLAUDE.md`, `AGENTS.md`, `.codex` added (AI assistant context files, local only)

### Tests

- Unit coverage raised from ~67% to 78.5% (1,296 passing tests; threshold: 60%)
- 9 new test files: `test_api_ai.py`, `test_api_health_runs.py`, `test_api_middleware.py`, `test_api_stats_db.py`, `test_bytes_serializer.py`, `test_cli_main.py`, `test_daemon_server.py`, `test_pipeline_manager.py`, `test_pipeline_watcher.py`
- Extended: `test_pipeline_controller.py`, `test_loader.py`, `test_protobuf_serializer.py`, `test_snmp_connectors.py`
- 25 ruff lint errors in test files resolved (unused imports, unsorted blocks, unused variables)

---

## [1.2.1] — 2026-04-14

### Fixed

**Manager + Worker — run metrics propagation**
- `records_skipped` now correctly propagates from worker executor through the HTTP callback to the manager DB and UI run history (was always 0 in worker mode)
- Per-record `errors` list now flows through the full worker callback chain (`executor → _post_run_complete → RunCompletePayload → on_worker_run_complete → DB errors_json`) — skip reasons and transform/sink errors are now visible in the run detail expandable row
- `RunCompletePayload` extended with `errors: list[str]` field; `on_worker_run_complete` accepts and stores it

**Executor — skip reason visibility**
- Skip path (no sink wrote — condition filtered or all sinks failed/circuit-open) now logs at WARNING instead of DEBUG, and appends the reason to `ctx.errors` via new `PipelineRunContext.note_skip()` method
- `note_skip()` appends to errors without incrementing `records_skipped` (avoids double-counting)

**Manager logs — health poll noise**
- `httpx` logger set to WARNING in `log_config.py` — individual per-request lines no longer flood the manager log
- `WorkerPool._poll_all()` now emits a single `Worker pool: N/M healthy` summary line only when the healthy count changes; logs at WARNING when degraded, INFO when fully healthy

**Settings page — Daemon Status**
- `/api/ready` now returns a `cluster` field: `"manager · N/M workers"` in manager mode, `"standalone"` otherwise
- Settings Daemon Status row previously showed `disabled (standalone)` for all deployments — now correctly reflects the running mode

### Changed

**UI — Dashboard actions**
- Replaced single Run Now / Stop toggle with separate **Start**, **Stop**, and **Download YAML** buttons per pipeline row
- Run Now removed from dashboard; one-shot trigger remains on the pipeline detail page only

**UI — Pipeline detail**
- Added **Run Now** button (lightning icon) as a separate one-shot trigger independent of the Start/Stop schedule buttons
- Run Now on a stopped pipeline no longer re-schedules it — `_on_run_complete` correctly restores `stopped` status when `_may_schedule()` returns False

**UI — Workers page**
- Renamed "Cluster" → "Workers" in navigation
- Per-worker card now shows `assigned_pipelines` (most recent dispatch per pipeline) with currently-running ones highlighted in green
- `WorkerPool` tracks `_pipeline_worker: dict[str, str]` for dispatch history; exposes `assigned_pipelines` in `status()`

**UI — Light/dark mode**
- Replaced all remaining hardcoded dark hex values (`#0d1117`, `#161b22`, `#30363d`, `#e6edf3`, `#8b949e`) with CSS variables across `editor.html`, `wizard.html`, `cluster.html`, `plugins.html`, `settings.html`, `templates.html`
- Added `aria-label` attributes to unlabelled form controls in `runs.html`, `wizard.html`, `detail.html` (resolves browser accessibility warnings)

**Load balancing**
- `WorkerPool.least_loaded()` uses round-robin tiebreaker (`_rr_counter`) among equally-loaded workers — prevents all pipelines being dispatched to `worker-0` when all workers are idle

**Worker image**
- Dedicated `Dockerfile.worker` validated in production; workers now deploy with `trishul-ram-worker` image (no apscheduler/sqlalchemy/UI assets)

---

## [1.2.0] — 2026-04-10

### Added

**Manager + Worker mode**
- New `TRAM_MODE` env var: `standalone` (default) | `manager` | `worker`
- **Manager Deployment** — owns all scheduling, DB writes, and UI; dispatches pipeline run requests to worker pods via HTTP and receives results via POST callback
- **Worker StatefulSet** — stateless executors: receive a run request, execute the pipeline, POST result back to manager; no DB access, no scheduler, no UI
- Worker discovery via Kubernetes headless DNS: `<release>-worker-N.<release>-worker.<ns>.svc.cluster.local`
- New env vars: `TRAM_WORKER_REPLICAS`, `TRAM_WORKER_SERVICE`, `TRAM_WORKER_NAMESPACE`, `TRAM_WORKER_PORT`

**`tram[manager]` optional extra**
- `apscheduler>=3.10`, `sqlalchemy>=2.0`, `psycopg2-binary`, and `PyMySQL` moved from base dependencies into `tram[manager]`
- Worker image installs only `tram[worker,kafka,snmp,avro,...]` — no scheduler or DB libraries
- `daemon/server.py` checks `TRAM_MODE=worker` before importing the manager module chain — worker boots cleanly without `tram[manager]` installed

**`Dockerfile.worker`**
- Separate worker image: base deps + connector extras only (no `manager` extra)
- UI assets omitted — no `COPY tram-ui/dist /ui` stage
- `EXPOSE 8766`, `ENV TRAM_MODE=worker`, healthcheck on `/agent/health`

**Helm: manager+worker mode** (`manager.enabled=true`)
- Manager Deployment + Worker StatefulSet created; standalone StatefulSet skipped
- `manager.persistence` — dedicated ReadWriteOnce PVC (`manager-data-<release>`) for SQLite DB + schemas + MIBs; RWO is sufficient since only one manager pod writes
- `worker.image` override block — optionally point workers at a dedicated worker image; falls back to main image when unset (`tram.workerImage` helper in `_helpers.tpl`)
- Main Service adds `app.kubernetes.io/component: manager` selector in manager mode — prevents HTTP traffic from reaching worker pods (port 8766)
- Headless service `<release>-worker` for stable pod DNS

**`melt` transform** — wide → long pivot: converts a dict-valued field into one record per key/value pair; supports `label_fields` unnesting, `include_only`/`exclude` key filtering, and configurable output column names (`metric_name_col`, `metric_value_col`)

**`pm_xml` serializer** — 3GPP PM XML (Nokia NCOM / TS 32.432 measData) deserializer; produces one flat record per `measValue`; auto-closes truncated files; configurable managed_element and numeric casting

### Changed

- **`PipelineController`** replaces split `TramScheduler` + `PipelineManager` lifecycle handling — single authority for all pipeline state transitions
- **4-state machine** — `paused` state removed; states are `scheduled`, `running`, `stopped`, `error`
- **`_sync_from_db()` stopped-flag detection** — picks up DB stopped/cleared flags even when pipeline YAML is unchanged
- **`_seen_nodes` tracking** — eliminates infinite cooling-period cycles when detecting newly joined cluster nodes
- **`TRAM_PIPELINE_SYNC_INTERVAL` default** — reduced from 30 s to 10 s for faster convergence
- **UI** — removed `paused`/`resume` buttons and badge; status filter updated (`scheduled`, `running`, `stopped`, `error`)
- **SQLite in manager mode** — manager is the sole DB writer; `sqlite:////data/tram.db` on a RWO PVC is the recommended setup; no external database required
- **`postgresql.enabled` default** — changed to `false`; SQLite on `manager.persistence` is the recommended default

### Removed

- **Deprecated DB columns** — `owner_node`, `runtime_status`, `status_updated`, `status_node` no longer added to `registered_pipelines` at startup
- **Dead cluster DB methods** — `set_pipeline_owner`, `get_pipeline_owner`, `get_pipelines_by_owner`, `get_pipeline_counts_by_node`, `claim_orphaned_pipelines`, `set_runtime_status`, `get_pipeline_runtime`, `claim_run`, `get_all_pipeline_runtime` removed from `TramDB`

---

## [1.1.4] — 2026-04-08

### Added

**AI Assist — pipeline generation and modification in the YAML editor**
- New AI panel in the YAML editor: "Generate" mode for new pipelines (describe in plain text → get YAML), "Modify" mode for existing pipelines (plain-English instruction → diff shown inline)
- Supports three providers: `anthropic` (Claude), `openai` / OpenAI-compatible (Ollama, LiteLLM, etc.), `bedrock` (AWS Bedrock proxy via Bearer token)
- AI config (provider, API key, model, base URL) stored in DB `settings` table — survives pod restarts and overrides `TRAM_AI_*` env vars
- New Settings page card: Save / Test AI config, shows enabled status and key hint
- New API endpoints: `GET /api/ai/config`, `POST /api/ai/config`, `POST /api/ai/test`
- AI context builder (`ai_docs.py`) always includes CRITICAL RULES with full expression syntax reference — prevents AI from generating `{{now()}}` Jinja2-style expressions

**YAML editor improvements**
- Copy-to-clipboard button in editor toolbar
- "Diff vs saved" button (edit mode only): toggles an inline two-pane diff showing current edits vs last saved version; also auto-opens after AI modify
- Save button label changes to "Update Pipeline" in edit mode; no-op toast if YAML is unchanged
- Wider layout (8/4 column split) to accommodate AI panel alongside reference pills

**Extended timestamp functions in `add_field`**
- `now()` → UTC ISO-8601 string; `now('%Y-%m-%d')` / `now('%H:%M:%S')` etc. → strftime-formatted string
- `epoch()` → Unix timestamp float; `epoch_ms()` → Unix milliseconds integer
- Nested function calls work: `str(round(rx_mbps, 2)) + ' at ' + now('%H:%M:%S')`

**Pipeline context in `add_field` expressions**
- Expressions now have access to a `pipeline` variable: `pipeline.name`, `pipeline.source.host`, `pipeline.source.community`, etc.
- Both dot-access (`pipeline.source.host`) and dict-access (`pipeline['source']['host']`) work
- Injected at transform construction time via `_DotDict` wrapper; available in global and per-sink transforms

**DB as single source of truth for pipelines**
- ConfigMap / filesystem pipelines seeded to DB at startup and on reload — not registered to manager directly
- `registered_pipelines` gains a `source` column (`disk` | `api`): disk seed skips pipelines with `source='api'` (user-owned), preventing reload from reverting UI edits
- Reload endpoint uses seed-then-`_load_from_db()` — no more direct disk-to-manager registration or soft-deleted pipeline resurrection

**Cluster pipeline update propagation**
- `_sync_from_db()` detects `yaml_text` changes from other nodes and re-registers the updated config (stop → deregister → re-register → reschedule)
- Eliminates stale-config bug where node-1 kept running old pipeline YAML after node-0 saved an update

**SNMP improvements**
- `snmp_poll` source: `classify: true` mode adds `_index_parts` list metadata alongside `_index` in classified output (multi-component OID index support)
- `snmp_poll` source: real SNMP GET for `sysDescr.0` in `test_connection()` — verifies host, port, and community string with actual latency measurement
- `snmp_trap` source: `test_connection()` verifies UDP port bind availability

**UI auto-refresh and refresh buttons**
- Pipelines page auto-polls at configured interval (default 10 s)
- Refresh icon (`↻`) added to: pipelines page toolbar, global run history page, pipeline detail run history tab
- Pipeline page: separate Refresh (status only) and Reload (disk + DB sync) buttons

**Helm: `hostNetwork` support**
- New `hostNetwork: false` value (default off); set `true` to share the kind/host network namespace
- Required for UDP-based sources (SNMP, syslog) on WSL2 / kind where CNI overlay drops UDP return packets
- Sets `dnsPolicy: ClusterFirstWithHostNet` automatically when enabled

### Fixed

- Pausing a disk-loaded (ConfigMap) pipeline now persists correctly across pod restarts — all pipelines are in DB from startup, so the `paused=1` flag always has a row to update
- `trigger_run()` raises an error if the target pipeline is paused, preventing accidental manual execution
- API key auth rate-limit middleware now exempts `/api/auth/login` and standard metadata endpoints (`/docs`, `/redoc`, `/openapi.json`, `/favicon.ico`) to avoid 429 on browser load

---

## [1.1.3] — 2026-04-01

### Added

**Test coverage — Tier 1 + Tier 2 unit tests**
- 8 new unit test files covering API routers, auth utilities, and serializers:
  - `test_auth_utils.py` — token create/verify, password hash, `parse_users`, `extract_bearer`
  - `test_api_auth_router.py` — login, `/me`, change-password (all happy + error paths)
  - `test_api_pipelines.py` — pipeline CRUD, lifecycle (start/stop/run), dry-run, alerts CRUD, versions, reload
  - `test_api_connectors_router.py` — `/test` and `/test-pipeline` endpoints, host/port extraction helpers
  - `test_api_stats_router.py` — in-memory stats fallback, pipeline status counts, sparkline buckets
  - `test_api_ai_router.py` — AI status, generate/explain modes, error paths (503/502/400)
  - `test_api_misc_routers.py` — webhooks (404/401/202/503), templates (cache, YAML parse), mibs (list/delete/upload)
  - `test_serializers_text_ndjson.py` — `TextSerializer` and `NdjsonSerializer` parse + serialize + error paths
- **846 tests total** (up from 701); coverage **69%** (up from 63%); threshold 60%

---

## [1.1.2] — 2026-03-30

### Added

**ASN.1 serializer (`type: asn1`)**
- Decodes BER/DER/PER/XER/JER binary files using a user-provided `.asn` schema file — same pattern as the `protobuf` serializer (`schema_file` + `message_class`)
- Encoding selectable via `encoding: ber | der | per | uper | xer | jer` (default: `ber`)
- `schema_file` can point to a single `.asn` file or a directory of `.asn` files (compiled together for cross-file imports)
- `_to_json_safe()` converts `datetime` → ISO 8601 string, ASN.1 CHOICE 2-tuples → `{"type": x, "value": y}`, `bytes` → hex
- Schema compiled once per serializer instance and cached for its lifetime (same pattern as `protobuf`)
- Deserialize only (`serializer_in`) — encode path raises `SerializerError` with a clear message pointing to `serializer_out: type: json`
- `.asn` added to `POST /api/schemas/upload` accepted extensions (displayed as type `asn1` in the schemas list)
- New optional extra: `tram[asn1]` = `asn1tools>=0.167`; included in the standard Docker image
- Reference schema `docs/schemas/3gpp_32401.asn` for Ericsson 3GPP TS 32.401 PM statsfiles (BER, IMPLICIT TAGS); uploadable via UI, works with C\* (core) and G\* (HLR/vHLR) variants

### Fixed

**Pipeline visibility across cluster nodes (API-registered pipelines)**
- `POST /api/pipelines` on any pod now writes the pipeline YAML to a shared `registered_pipelines` table in PostgreSQL; `PUT` updates it; `DELETE` soft-deletes (sets `deleted=1`)
- On startup, after loading pipelines from `TRAM_PIPELINE_DIR` (ConfigMap), the scheduler calls `_load_from_db()` — registers any DB pipeline not already loaded from the filesystem; filesystem wins on name collision
- Background thread `_sync_from_db()` polls the DB every `TRAM_PIPELINE_SYNC_INTERVAL` seconds (default 30): registers newly added pipelines, deregisters soft-deleted ones; all pods converge without restart
- Pipeline registered via API on pod-0 becomes visible on pod-1 and pod-2 within one poll interval; status is consistent (hash-based ownership decides which pod executes it)
- New DB table `registered_pipelines(name, yaml_text, created_at, updated_at, deleted)` — auto-created on startup via existing `_create_tables()` pattern; safe on existing databases
- New config: `TRAM_PIPELINE_SYNC_INTERVAL` (integer seconds, default 30)
- SQLite (standalone, single pod): DB persistence still works, sync loop is effectively a no-op

**Balanced pipeline distribution across cluster nodes**
- Replaced simple `sha1(name) % node_count` ownership formula with rank-based assignment: all pipeline names sorted by stable hash then distributed round-robin (`rank % count == position`); guarantees at most 1 pipeline difference between any two nodes regardless of name hashes
- `rebalance_ownership(all_names)` pre-computes and caches the owned set as a `frozenset` on the coordinator; called on topology change, after startup load, and after each DB sync cycle
- `get_state()` (cluster API endpoint) uses the same rank-based formula so UI pipeline counts match actual ownership

**Reload endpoint now restores DB-registered pipelines**
- `POST /api/pipelines/reload` previously cleared all in-memory pipelines and re-scanned only the filesystem, causing API-registered pipelines to disappear until the next DB sync cycle (up to 30 s)
- Fixed: after filesystem scan, reload now calls `_load_from_db()` so all DB-registered pipelines are immediately available; `total` in the response reflects the combined count

**Cluster page — pipeline counts**
- Node count and total pipeline count added to the cluster status line: `Cluster active · N nodes · M pipelines`
- Each node accordion header now shows a badge with its assigned pipeline count, right-aligned before the expand chevron

---

## [1.1.1] — 2026-03-30

### Added

**Run History — expandable error rows**
- Runs with errors or DLQ records show a chevron (▶) in the detail page Runs table
- Clicking chevron inserts an inline sub-row with per-record error lines in monospace red; toggles closed on second click
- DLQ-only runs show "N record(s) sent to DLQ" when no inline errors; clean runs show no chevron
- Backend: `RunResult.errors: list[str]` field populated from `PipelineRunContext.errors`; persisted as `errors_json TEXT` column in `run_history` (auto-migrated on existing DBs)

**Wizard — complete connector coverage**
- `wizard.js` FIELD_SCHEMA now covers all connector types including `websocket`, `gnmi`, `snmp_poll`, `prometheus_rw`, and `corba`
- `snmp_poll` OID list rendered as a YAML sequence (added `oids` to `ARRAY_FIELDS`)
- All FIELD_SCHEMA and TRANSFORM_FIELDS entries have descriptive hint text

**Wizard — step reorder to match YAML field order**
- Steps: Info (name/schedule/on_error) → Source (type + serializer_in + Test) → Transforms → Sinks (global serializer_out + sink cards) → Review
- `serializer_in` moved to Source step; `on_error` added to Info step with inline descriptions; global `serializer_out` added to top of Sinks step
- `buildYaml` emits `serializer_in` as nested block, `on_error` only when non-default, `serializer_out` before sinks block

**Wizard — UX improvements**
- "New Pipeline" toolbar button replaced with split btn-group: **Wizard** | **YAML** (direct editor)
- Template deploy correctly loads template YAML into editor (fixed `window._editorYaml` propagation)
- YAML diff now uses `reqText()` helper in `api.js` — fixes JSON parse error on raw YAML version fetch
- "Advanced: open blank YAML editor" link properly clears editor state

**Connector Test — full coverage**
- `test_connection()` added to all remaining connectors: `amqp` source+sink (TCP probe), `s3` source+sink (`head_bucket`/`list_buckets`), `gcs` source+sink (`get_bucket`), `azure_blob` source+sink (`get_account_information`), `ves` (HTTP HEAD), `websocket` source+sink (TCP probe), `prometheus_rw` (local listener check), `webhook` (local listener check), `corba` (TCP probe on corbaloc)
- `_extract_host`/`_extract_port` TCP fallback in `connectors.py` now parses `url`/`base_url` fields for URL-based connectors

**Helm: pre-mounted connector key files**
- New `keys` section in `values.yaml`: `secretName` / `mountPath` — pre-mounts a single Kubernetes Secret at `/secrets/` on every pod
- Quickstart commands documented inline (create, rotate, reference in pipeline YAML)
- `docs/roadmap_1.2.0.md`: key upload API added to roadmap

### Fixed
- **Pipeline status on startup (cluster mode)**: non-owning nodes now set `status="scheduled"` for interval/cron pipelines instead of leaving them stuck at `"stopped"`; `_rebalance` release also sets `"scheduled"` instead of `"stopped"`
- **SPA routing**: `router.init()` called unconditionally before `checkAuth()` — hashchange listener always registered
- **Templates modal**: button uses `data-bs-toggle/data-bs-target` (not `new bootstrap.Modal().show()`) — fixes silent failure in Vite ESM context; server accepts both `sink:` (singular) and `sinks:` (list) in dry-run
- **Templates view button**: inline YAML preview panel (view-switcher) inside modal — no nested Bootstrap modal
- **Scheduled badge**: `.badge-scheduled` CSS (yellow) + dot color added to `style.css`
- **Password change**: Settings page shows Change Password card when logged in
- **Settings layout**: 3-column grid (col-4 each), no max-width cap
- **Pipeline export**: download YAML button (↓) added to Actions column
- **Detail page tabs**: isolated tab panel rendering — fixes DOM corruption when switching Runs/Versions/Config tabs
- **YAML diff modal**: `bootstrap is not defined` in `detail.js` — added `import * as bootstrap from 'bootstrap'` (Vite ESM modules don't share `window.bootstrap` reliably)
- **Version history table**: Diff and Rollback buttons now show text labels alongside icons
- **Enrich transform missing file**: `_load_lookup()` warns and returns empty dict instead of raising `TransformError` — allows dry-run to succeed for pipelines with runtime-resolved lookup paths
- **All 20 bundled pipeline templates pass dry-run**: validate rules format (`field: required` → `{required: true}`), empty defaults (`${VAR:-}` → named placeholder), `seconds:` → `interval_seconds:`, `add_field` format fixes, `:-placeholder` defaults for bare `${VAR}` env vars
- **`test_connection` port defaults**: syslog=514, snmp_trap=1162; REST connector uses 443/80 based on scheme; sftp/ftp/snmp_poll/gnmi use connector-specific defaults
- **Helm fsGroup + key file permissions**: `securityContext.fsGroup: 1000` on pod spec; `defaultMode: 0440` on keys Secret volume → `root:tram` ownership, readable by tram user without world-read

---

## [1.1.0] — 2026-03-29

### Added

**Pipeline Wizard**
- 5-step UI wizard (Name → Source → Transforms → Sinks → Review) for creating pipelines without writing YAML
- Client-side YAML assembly from wizard state; final step shows live preview and sends to Editor or saves directly
- Accessible from Pipelines page toolbar via "+ New Pipeline" button (Bootstrap modal)
- Server validates both `sink:` (singular) and `sinks:` (list) in template dry-run

**Live Metrics Dashboard**
- `GET /api/stats` — per-pipeline aggregated stats (records in/out, error rate, avg duration) for the last hour
- Dashboard page extended with 10-second polling metrics table and Canvas sparkline graphs per pipeline
- Dialect-aware SQL aggregation: `EXTRACT(EPOCH ...)` PostgreSQL, `TIMESTAMPDIFF` MySQL, `julianday` SQLite

**Alert Rules UI**
- Alert rules CRUD in Pipeline Detail page: `GET/POST/PUT/DELETE /api/pipelines/{name}/alerts`
- YAML mutation approach: rules written back into pipeline YAML config and persisted
- Alert modal uses `import * as bootstrap from 'bootstrap'` (not `window.bootstrap`) for Vite ESM compatibility

**Connector Test**
- `POST /api/connectors/test` — test connectivity for a connector config; TCP fallback for unknown connector types
- `POST /api/connectors/test-pipeline` — test all source and sink connectors in a pipeline YAML
- `ConnectorTestMixin` base class in `tram/core/base.py`; all connectors with network access implement `test_connection()`

**Pipeline Templates**
- `GET /api/templates` — returns list of bundled pipeline YAML templates from `pipelines/` directory
- Templates tab in Pipelines page: browse, preview, and load any template into the Editor
- View YAML inline in modal (no nested Bootstrap modal)
- 20 bundled templates covering SFTP, Kafka, REST, SNMP, Syslog, S3, OpenSearch, InfluxDB, ClickHouse, Protobuf, multi-format fanout, webhook alarm, and more

**AI Assist**
- `POST /api/ai/suggest` — `mode=generate` (create pipeline from description) or `mode=explain` (explain existing YAML)
- `GET /api/ai/status` — returns configured provider/model and whether AI is available
- Configurable via env: `TRAM_AI_API_KEY`, `TRAM_AI_PROVIDER` (openai/anthropic), `TRAM_AI_MODEL`, `TRAM_AI_BASE_URL`
- Editor page "AI Assist" button with textarea for prompt; result inserted into editor

**Password Change**
- `POST /api/auth/change-password` — changes password for authenticated user; stored in `user_passwords` DB table (sha256+salt hash)
- `user_passwords` table: `(username, password_hash, updated_at)`; upsert dialect-aware (SQLite/PostgreSQL vs MySQL)
- Settings page shows "Change Password" card when logged in

**Helm: pre-mounted connector keys**
- `keys.secretName` / `keys.mountPath` in `values.yaml` — pre-mounts a single Kubernetes Secret at `/secrets/` on every pod
- `securityContext.fsGroup: 1000` + `defaultMode: 0440` — key files are `root:tram` owned, readable by tram user without world-read
- Zero-restart key rotation: updating Secret contents propagates via kubelet (~60s); adding a new Secret mount requires rolling restart
- Quickstart docs in `values.yaml` `keys:` section

### Changed
- `helm/Chart.yaml`: version → `1.1.0`; `pyproject.toml`: version → `1.1.0`
- Settings page layout: 3-column grid (col-4 each) with no max-width cap
- Pipeline export: download YAML button (↓) added to Actions column in pipelines list
- Scheduled badge: `.badge-scheduled` (yellow) CSS added to `style.css`
- `tram-ui/src/pages/detail.js`: SPA router always registers hashchange listener unconditionally; Templates button moved to Pipelines page toolbar

---

## [1.0.9] — 2026-03-25

### Added

**Shared RWX storage for schemas and MIBs (cluster mode)**
- New `sharedStorage` Helm section: single `ReadWriteMany` PVC (`tram-shared`) mounted at `/shared` on every pod
- `TRAM_SCHEMA_DIR` and `TRAM_MIB_DIR` auto-pointed to `/shared/schemas` and `/shared/mibs` when `sharedStorage.enabled=true`
- Schemas/MIBs uploaded via the UI are now instantly visible to all replicas — no session pinning required
- `helm/kind/nfs-provisioner.yaml`: deploys [kubernetes-sigs NFS Ganesha server + external provisioner](https://github.com/kubernetes-sigs/nfs-ganesha-server-and-external-provisioner) (`registry.k8s.io/sig-storage/nfs-provisioner:v4.0.8`) in kind clusters; creates StorageClass `nfs-rwx`
- Supported RWX storage classes documented in `values.yaml`: `nfs-rwx` (kind), `efs-sc` (AWS), `azurefile` (Azure), `filestore-rwx` (GKE), `longhorn-rwx`

### Changed
- `persistence.enabled` defaults to `false` in cluster-mode `values.yaml` — per-pod `/data` PVCs are unnecessary when PostgreSQL + `sharedStorage` are both active
- Removed `sessionAffinity: ClientIP` workaround from Service (was pinning browsers to a single pod to paper over per-pod schema visibility; no longer needed)

---

## [1.0.8] — 2026-03-25

### Added

**Browser user authentication**
- `TRAM_AUTH_USERS` env var: comma-separated `username:password` pairs for UI login
- `tram/api/auth.py`: HMAC-SHA256 session tokens (8-hour TTL, invalidated on restart)
- `POST /api/auth/login` — returns `{"token": "...", "username": "..."}` on valid credentials
- `GET /api/auth/me` — returns current user from Bearer token (401 if unauthenticated)
- `APIKeyMiddleware` extended: accepts both `X-API-Key` (machine clients) and `Bearer` token (browser users); `/api/auth/login` added to exempt set
- Login overlay in tram-ui: full-screen login page shown when `TRAM_AUTH_USERS` is configured; 8-hour token stored in `localStorage`; logout button in topbar
- `helm/values.yaml`: new `authUsers` key (injected as `TRAM_AUTH_USERS`); recommended to use `envSecret` for production

**Multi-file upload (schemas & MIBs)**
- Schema and MIB upload zones now accept `multiple` files; uploads proceed sequentially with per-file progress hints
- Drop zone text updated to "Drop files here"

**Dashboard shortcuts**
- "Manage →" and "+ New" buttons on the Active Pipelines card navigate directly to the Pipelines and Editor pages

**Settings — restore base URL**
- Reset button (↺) next to the base URL input restores to `window.location.origin` (same-origin default)
- Removed duplicate "Reload Pipelines" button from Settings (already available on the Pipelines page)

**PostgreSQL subchart (Helm)**
- Bitnami PostgreSQL added as optional dependency (`postgresql.enabled=true` in `values.yaml`)
- When enabled, `TRAM_DB_URL` is auto-wired as `postgresql+psycopg2://<user>:<pass>@<release>-postgresql/<db>`; no manual `TRAM_DB_URL` needed
- `values.yaml`: `postgresql.auth` (username/password/database) and `postgresql.primary.persistence.size`
- Combined with `replicaCount>1` + `clusterMode.enabled=true` for a fully self-contained HA cluster

**Sample pipeline on install**
- `values.yaml` ships with a `sample-health` pipeline (interval 60 s, no-op source, writes status field to `/tmp/tram-sample`) so a fresh install has a visible running pipeline immediately

### Changed
- `pyproject.toml`, `helm/Chart.yaml`: version → `1.0.8`
- `tram-ui/package.json`, `index.html`: version badge → `v1.0.8`
- `helm/values.yaml`: `replicaCount: 3`, `clusterMode.enabled: true`, `postgresql.enabled: true` (kind dev-cluster deployment defaults)
- `helm/values-template.yaml`: new clean-defaults reference file (ClusterIP, replicaCount:1, postgresql:false, `OWNER/tram` placeholder)
- `tram/core/config.py`: added `auth_users` field

---

## [1.0.7] — 2026-03-24

### Added

**`tram-ui` — Bootstrap 5 web UI**
- New `tram-ui/` Vite + Vanilla JS project: fully self-contained frontend (no CDN), suitable for Docker embedding
- Bootstrap 5.3 dark theme base; all custom styles in `src/style.css` via CSS custom properties for full dark/light mode support
- Hash-based SPA router (`#dashboard`, `#pipelines`, `#runs`, etc.) with lazy page `init()` loading
- **Dashboard**: stat cards (total/running/errors/records-out), Active Pipelines table with inline stop/play actions, Recent Runs table
- **Pipelines**: live table with search + status/type filters, per-row start/stop/run/edit/delete, Reload from disk
- **Run History**: filtered by pipeline/status/date, expandable error rows, CSV export
- **Pipeline Detail**: summary cards (source/sinks/schedule/transforms/error policy), run history with filters, Runs/Versions/Config tab switching, version rollback
- **Pipeline Editor**: YAML editor with Tab-key indent, `new-pipeline.yaml` template for new pipelines, loads existing YAML for edits, Dry Run with inline result panel, Save (create/update)
- **Schemas**: schema file list, drag-and-drop upload zone with subdirectory support, per-row delete
- **MIB Modules**: compiled MIB list, drag-and-drop `.mib` upload, bulk download from mibs.pysnmp.com, per-row delete
- **Cluster**: accordion node list from `/api/daemon/status`, online/offline status dots, pipeline assignment per node
- **Plugins**: accordion with Sources 24 / Sinks 20 / Serializers 10 / Transforms 20
- **Settings**: connection form (base URL, API key, poll interval), Save/Test Connection, Daemon Status table, Reload Pipelines
- **Health poller**: 10s interval, sidebar dot + topbar hover card show daemon online/offline state, version, scheduler, DB status
- **Dark/light mode toggle** persisted in `localStorage`; all custom CSS uses CSS variables with full light-mode palette
- Shared `utils.js`: `relTime`, `fmtDur`, `fmtNum`, `statusBadge`, `schedBadge`, `esc`, `toast`
- Full TRAM REST API client in `src/api.js` (pipelines, runs, schemas, MIBs, daemon, health, meta, plugins)
- Build: `npm run build` → self-contained `dist/` (~82 KB gzipped total)

**Image — UI embedded in daemon**
- Multi-stage Dockerfile: new `ui-builder` stage (`node:20-alpine`) runs `npm ci && npm run build`; built `dist/` copied to `/ui` in runtime stage
- FastAPI mounts `StaticFiles` at `/ui` when `TRAM_UI_DIR` points to a valid directory; `GET /` redirects to `/ui/`
- `/ui/*` and `/` exempt from API key authentication — static assets are public
- `TRAM_UI_DIR=/ui` default env var; set to empty string to disable UI serving

**Helm — dedicated UI Service**
- New `helm/templates/service-ui.yaml` — `Service` named `{release}-ui` targeting the same pod port 8765 via a dedicated `ClusterIP:80` (or `NodePort`/`LoadBalancer`) when `ui.enabled=true`
- `values.yaml`: new `ui:` section — `enabled`, `port`, `serviceType`, `nodePort`, `serviceAnnotations`
- `statefulset.yaml`: injects `TRAM_UI_DIR=""` when `ui.enabled=false` to suppress static serving
- `NOTES.txt`: prints UI port-forward command when `ui.enabled=true`

### Changed
- `pyproject.toml`, `helm/Chart.yaml`: version → `1.0.7`
- `tram/api/middleware.py`: `EXEMPT_PREFIX` extended to cover `/ui` and `/` (root redirect)

---

## [1.0.6] — 2026-03-13

### Added
- `LICENSE` file (Apache-2.0 full text) added to repository root
- Helm `service.snmpTrapPorts` (list) replaces the former single `service.snmpTrapPort` scalar — iterate any number of UDP ports for multi-source SNMP trap deployments; each port creates one Service UDP port and one container port; adding/removing ports requires `helm upgrade`
- docker-compose SNMP trap port driven by `TRAM_SNMP_PORT_1` env var (defaulting to `1162`); additional ports can be added as numbered vars and entries in the `ports:` section

### Changed
- `pyproject.toml`: classifier `"Development Status :: 3 - Alpha"` → `"Development Status :: 5 - Production/Stable"`; added `"License :: OSI Approved :: Apache Software License"` classifier; `license` field changed from inline `{text = "Apache-2.0"}` to `{file = "LICENSE"}`
- Helm `Chart.yaml` / `values.yaml` image tag → `1.0.6`

---

## [1.0.5] — 2026-03-13

### Added

**`ndjson` serializer**
- `@register_serializer("ndjson")` — Newline-Delimited JSON (JSON Lines); each non-empty line is parsed as a JSON object
- Arrays flattened into the record stream; scalars wrapped in `{"_value": ...}` unless `strict: true`
- `strict: bool = False` — raises `SerializerError` on non-object lines when enabled
- `ensure_ascii`, `newline` config keys match the `json` serializer for consistency
- Covers Kafka consumer output, Filebeat/Fluentd/Vector JSON output, jq streams, and any source that produces one JSON object per line rather than a wrapped array
- `NdjsonSerializerConfig` in `tram/models/pipeline.py`

**Per-sink `serializer_out` override**
- Each sink config (`SFTPSinkConfig`, `LocalSinkConfig`, `KafkaSinkConfig`, … all 20) gains an optional `serializer_out: Optional[SerializerConfig] = None` field
- When set, that sink uses its own serializer instead of the global `serializer_out`
- Enables multi-format fan-out from a single pipeline: Avro→Kafka + JSON→local + CSV→SFTP
- Example:
  ```yaml
  serializer_out:          # global default
    type: json

  sinks:
    - type: kafka
      topic: pm-avro
      serializer_out:      # per-sink override
        type: avro
        schema_file: /schemas/pm.avsc
    - type: local
      path: /data/output   # inherits global → json
    - type: sftp
      host: archive.example.com
      serializer_out:
        type: csv
  ```
- `_build_sinks()` now returns a 5-tuple `(sink_instance, condition, transforms, sink_cfg, per_sink_ser|None)`
- `_write_one_sink()` resolves: per-sink serializer → global serializer
- Forward-reference resolved with `model_rebuild()` for all sink config classes (Pydantic v2 pattern)

**`serializer_out` optional at pipeline level**
- `PipelineConfig.serializer_out` changed from required to `Optional[SerializerConfig] = None`
- `None` → defaults to `JsonSerializer({})` at runtime in `_build_serializer_out()`
- Pipelines that write JSON (the vast majority) no longer need to declare `serializer_out:`

### Changed
- `tram/models/pipeline.py`: serializer section now has `NdjsonSerializerConfig`; `SerializerConfig` union extended; `_SINK_CONFIG_CLASSES` + `model_rebuild()` block added after union definition
- `tram/pipeline/executor.py`: `_build_sinks()` returns 5-tuple; `_write_one_sink()` handles 3/4/5-tuples; `_build_serializer_out()` handles `None` config
- Helm `values.yaml` / `Chart.yaml` / `image.tag` → `1.0.5`

---

## [1.0.4] — 2026-03-13

### Added

**Schema Registry consolidation**
- `TRAM_SCHEMA_REGISTRY_URL` env var is now a server-level default for both the schema registry proxy (`/api/schemas/registry/*`) and the Avro/Protobuf serializer clients — no need to repeat the URL in every pipeline YAML
- `TRAM_SCHEMA_REGISTRY_USERNAME` / `TRAM_SCHEMA_REGISTRY_PASSWORD` env vars — server-level auth defaults for registry serializers; pipeline YAML fields (`schema_registry_username`, `schema_registry_password`) act as per-pipeline overrides
- `AppConfig`: three new fields — `schema_registry_url`, `schema_registry_username`, `schema_registry_password` (all from env)
- `AvroSerializer` and `ProtobufSerializer`: `registry_url` now resolves from `config.get("schema_registry_url") or os.environ.get("TRAM_SCHEMA_REGISTRY_URL")`; same fallback for `registry_username` / `registry_password`; credentials forwarded to `SchemaRegistryClient`

**Schema Registry proxy**
- `GET/POST/PUT/DELETE /api/schemas/registry/{path}` — transparent reverse proxy to `TRAM_SCHEMA_REGISTRY_URL`; proxies all headers and query params; returns 503 when env var is not set
- Route registered before the `/{filepath:path}` catch-all so it resolves correctly

**Pipeline management**
- `PUT /api/pipelines/{name}` — update/replace a registered pipeline's YAML config in-place (stops → re-registers → restarts if enabled)

**ClickHouse connector**
- `@register_source("clickhouse")` — query ClickHouse using `clickhouse-driver`; configurable `query`, `database`, chunked via `chunk_size`
- `@register_sink("clickhouse")` — insert records into a ClickHouse table; `insert_block_size` batching
- `ClickHouseSourceConfig` / `ClickHouseSinkConfig` in `tram/models/pipeline.py`
- New optional extra: `pip install tram[clickhouse]` (`clickhouse-driver>=0.2`)
- Registered in `tram/connectors/__init__.py`

**REST connector fix (httpx 0.28)**
- `tram/connectors/rest/source.py` + `sink.py`: `verify_ssl` moved from per-request `kwargs` to the `httpx.Client(verify=...)` constructor — resolves `TypeError: Client.request() got an unexpected keyword argument 'verify'` introduced by httpx 0.28

**Example pipelines**
- `pipelines/all-transforms-test.yaml` — exercises all 20 transform types in a single webhook pipeline; documents cross-record transform behaviour in stream mode
- `pipelines/csv-ingest.yaml` — CSV serializer validation via webhook
- `pipelines/xml-ingest.yaml` — XML serializer (defusedxml) validation via webhook
- `pipelines/rest-pipeline.yaml` — REST source (poll) + REST sink (POST) end-to-end
- `pipelines/rest-echo-receiver.yaml` — companion webhook receiver for REST sink loop
- `pipelines/proto-device-event.yaml` — multi-file Protobuf schema: `device_event.proto` imports `severity.proto`, `location.proto`, `interface_stats.proto`, `identity.proto`; all compiled in one `protoc` invocation

### Changed
- `docker-compose.yml`: `TRAM_SCHEMA_REGISTRY_URL: ${TRAM_SCHEMA_REGISTRY_URL:-}` env var wired in; `1162:1162/udp` SNMP trap port exposed
- Helm `values.yaml` / `Chart.yaml` / `image.tag` → `1.0.4`

---

## [1.0.3] — 2026-03-09

### Added

**SNMP MIB management**
- `TRAM_MIB_DIR` env var (default `/mibs`) — global MIB directory; SNMP source/sink connectors auto-prepend it to `mib_dirs` at startup so OID resolution works without per-pipeline config
- `AppConfig.mib_dir` field
- `tram mib download <NAMES...> --out <dir>` — new CLI command; downloads and compiles MIB modules from `mibs.pysnmp.com` using `pysmi-lextudio` (requires `tram[mib]`)
- `tram mib compile` enhanced: now accepts a **directory** in addition to a single file; all `.mib` files in the directory are compiled in one pass so cross-file imports resolve correctly
- MIB management REST API:
  - `GET /api/mibs` — list compiled MIB modules in `TRAM_MIB_DIR`
  - `POST /api/mibs/upload` — upload a raw `.mib` file and compile it (requires `tram[mib]`)
  - `POST /api/mibs/download` — `{"names": [...]}` download+compile from `mibs.pysnmp.com` (requires `tram[mib]`)
  - `DELETE /api/mibs/{name}` — delete a compiled MIB module
- Dockerfile: **three-stage build** — new `mib-builder` stage downloads + compiles `IF-MIB`, `ENTITY-MIB`, `HOST-RESOURCES-MIB`, `IP-MIB`, `TCP-MIB`, `UDP-MIB`, `IANAifType-MIB` from `mibs.pysnmp.com` at build time; compiled `.py` files copied to runtime image; MIB download failures are non-fatal (empty `/mibs` on air-gapped builds)
- Helm: `mibPersistence` section — optional `volumeClaimTemplate` at `/mibs` for persisting runtime-downloaded MIBs across pod restarts

**Schema file management**
- `TRAM_SCHEMA_DIR` env var (default `/schemas`) — global schema directory for serialization schemas
- `AppConfig.schema_dir` field
- Schema management REST API:
  - `GET /api/schemas` — list all schema files under `TRAM_SCHEMA_DIR` recursively; returns `path`, `type`, `size_bytes`, `schema_file` (paste-ready for pipeline YAML)
  - `GET /api/schemas/{filepath}` — read a schema file's raw text content
  - `POST /api/schemas/upload?subdir=<dir>` — upload a `.proto`, `.avsc`, `.json`, `.xsd`, `.yaml`, or `.yml` file; optional `subdir` for multi-file proto packages; atomic write (`.tmp` → rename)
  - `DELETE /api/schemas/{filepath}` — delete a schema file
- Path-traversal protection on all schema endpoints (`_safe_join` with `os.path.normpath`)
- Dockerfile: `/schemas` directory created at build time, `ENV TRAM_SCHEMA_DIR=/schemas` set
- Helm: `schemaPersistence` section — optional `volumeClaimTemplate` at `/schemas` so schemas uploaded via the API survive pod restarts

**Protobuf serializer improvements**
- `framing: none` mode — each file is a single raw serialized proto message (no 4-byte length prefix); required for Cisco EMS PM binary files
- Multi-file proto compile fix: `_compile_proto()` now compiles **all** `.proto` files in the same directory in one `protoc` invocation so import statements resolve correctly at Python import time
- `ProtobufSerializerConfig`: new `framing: Literal["length_delimited", "none"]` field (default `"length_delimited"`)
- Example pipeline: `pipelines/cisco_pm_proto_to_json.yaml` — SFTP binary PM files → protobuf decode → `_pm_type` detection → JSON output on SFTP

**Dependency**
- `python-multipart>=0.0.9` added to core dependencies (required for `UploadFile` in MIB/schema upload endpoints)
- `mib` extra (`pysmi-lextudio`) now included in the default Docker image

### Changed
- Dockerfile: `pip install "${whl}[metrics,postgresql,mysql,snmp,mib]"` — `mib` added to default installed extras; connector extras (`kafka`, `s3`, `avro`, `protobuf_ser`, etc.) remain opt-in via a custom `FROM tram:1.0.3` layer
- Helm `values.yaml` / `Chart.yaml` / `image.tag` → `1.0.3`

### Fixed
- `APIKeyMiddleware`: `AppConfig.from_env()` moved from `dispatch()` to `__init__()` — config is now cached once at startup instead of re-read on every request
- `RateLimitMiddleware._windows`: periodic eviction of idle client entries when dict exceeds 500 keys — prevents unbounded memory growth in long-running daemons
- `tram/core/config.py`: all bare `int()` env var reads replaced with `_env_int()` helper — raises `ValueError` with the variable name on invalid input instead of a cryptic Python traceback
- CI (`ci.yml`): removed dead `develop` branch trigger; added `--cov-fail-under=75` coverage gate to unit test step
- Release (`release.yml`): added `test` job (ruff + unit + integration) that must pass before Docker image is built and pushed
- `docker-compose.yml`: `TRAM_DB_PATH` replaced with `TRAM_DB_URL: sqlite:////data/tram.db`

---

## [1.0.2] — 2026-03-06

### Added

**SNMPv3 USM support**
- New `build_v3_auth()` helper in `tram/connectors/snmp/mib_utils.py`: builds a pysnmp `UsmUserData` object from human-readable config; security level auto-detected (noAuthNoPriv / authNoPriv / authPriv)
- Auth protocols: MD5, SHA (default), SHA224, SHA256, SHA384, SHA512
- Privacy protocols: DES, 3DES, AES / AES128 (default), AES192, AES256; unknown strings fall back gracefully to SHA / AES128
- **`snmp_poll` source** (`SNMPPollSource`): `version: "3"` now issues GET/WALK with `UsmUserData` instead of `CommunityData`; `ContextData(contextName=...)` passed when `context_name` is set
- **`snmp_trap` sink** (`SNMPTrapSink`): `version: "3"` sends traps with `UsmUserData`
- **`snmp_trap` source** (`SNMPTrapSource`): v3 config fields accepted and stored; trap *decoding* is best-effort (falls back to raw hex for encrypted v3 packets — full USM receive engine planned)
- New v3 config fields on `SnmpPollSourceConfig`, `SnmpTrapSourceConfig`, `SnmpTrapSinkConfig`: `security_name`, `auth_protocol`, `auth_key`, `priv_protocol`, `priv_key`, `context_name`

---

## [1.0.1] — 2026-03-06

### Added

**SNMP Poll enhancements**
- `_polled_at` (UTC ISO8601) injected into every SNMP poll record payload and `meta` dict — timestamp reflects the moment the poll was issued
- `yield_rows: bool = False` on `SnmpPollSourceConfig`: when `true`, yields one record per table row instead of one flat dict for the entire WALK result
- `index_depth: int = 0` on `SnmpPollSourceConfig`: controls how the row index is extracted from WALK keys — `0` = auto (split on first dot, correct for MIB-resolved names such as `ifDescr.1`); `>0` = last N OID components form the index (for numeric OIDs or composite indexes)
- Each per-row record carries `_index` (dot-separated compound index string, e.g. `"1.192.168.1.1"`) and `_index_parts` (list of strings, e.g. `["1","192","168","1","1"]`) for downstream parsing

### Changed

**Build / versioning**
- `tram/__init__.py`: `__version__` now read from installed package metadata via `importlib.metadata.version("tram")` — `pyproject.toml` is the single source of truth; fallback to `"0.0.0-dev"` when running from an uninstalled source tree
- `release.yml`: tag push (`v*`) now automatically patches `pyproject.toml`, `helm/Chart.yaml` (both `version` and `appVersion`), and `helm/values.yaml` (`image.tag`) in the ephemeral CI workspace before building — no manual version edits required for future releases

---

## [1.0.0] — 2026-03-06

### Added

**Security**
- `APIKeyMiddleware`: protect all `/api/*` endpoints with `X-API-Key` header or `?api_key=` query param; `TRAM_API_KEY` env var (empty = auth disabled); health/metrics/webhooks paths always exempt
- `RateLimitMiddleware`: sliding-window per-IP rate limiting for `/api/*`; `TRAM_RATE_LIMIT` (req/min, 0 = disabled), `TRAM_RATE_LIMIT_WINDOW` (seconds, default 60)
- TLS support: set `TRAM_TLS_CERTFILE` + `TRAM_TLS_KEYFILE` to enable HTTPS via uvicorn `ssl_*` params
- Helm: `apiKey` and `tls` sections in `values.yaml`; TLS secret volume mount + env vars in StatefulSet

**Reliability**
- Per-sink retry: `retry_count` (int, default 0) and `retry_delay_seconds` (float, default 1.0) on all 19 sink configs; exponential back-off with jitter; DLQ still receives record after all retries exhausted
- Parallel sinks: `PipelineConfig.parallel_sinks: bool = False`; fans out to all sinks concurrently via `ThreadPoolExecutor` when true
- Circuit breaker: `circuit_breaker_threshold` (int, default 0 = disabled) on all sink configs; skips sink for 60s after N consecutive failures; resets on success
- Kafka reconnect: `reconnect_delay_seconds`, `max_reconnect_attempts` on `KafkaSourceConfig`; outer reconnect loop in `stream_run`
- NATS reconnect: `max_reconnect_attempts`, `reconnect_time_wait` passed to `nats.connect()`
- Chunked reads: `read_chunk_bytes` on `SFTPSourceConfig` and `S3SourceConfig`; yields file in N-byte chunks

**SNMP MIB Integration**
- New `tram/connectors/snmp/mib_utils.py`: `build_mib_view()`, `resolve_oid()`, `symbolic_to_oid()`, `oid_str_to_tuple()`, `get_mib_view()` (cached)
- `SnmpPollConfig` + `SnmpTrapSourceConfig`: `mib_dirs`, `mib_modules`, `resolve_oids` fields; OIDs resolved to symbolic names in output records
- `SnmpTrapSinkConfig`: `varbinds: list[VarbindConfig]` for explicit OID/type/field mapping; `symbolic_to_oid()` resolves IF-MIB-style names
- New `VarbindConfig` model: `oid`, `value_field`, `type`
- `tram mib compile <source.mib> --out <dir>`: CLI command to compile raw MIB files (requires `tram[mib]`)
- New optional extra: `tram[mib]` = `pysmi-lextudio>=1.1`

**Observability**
- OpenTelemetry tracing: `tram/telemetry/tracing.py`; `init_tracing()` + `get_tracer()`; `TRAM_OTEL_ENDPOINT` + `TRAM_OTEL_SERVICE` env vars; no-op fallback when SDK not installed; `batch_run()` wrapped in `"batch_run"` span
- Kafka lag metric: `tram_kafka_consumer_lag{pipeline,topic,partition}` Gauge updated after each message poll
- Stream queue depth metric: `tram_stream_queue_depth{pipeline}` Gauge updated in threaded stream mode
- Run history CSV export: `GET /api/runs?format=csv` returns `text/csv` via `StreamingResponse`
- Enhanced readiness: `GET /api/ready` body now includes `db`, `scheduler`, `cluster` fields; returns 503 if DB or scheduler unavailable
- New optional extra: `tram[otel]` = `opentelemetry-sdk>=1.20, opentelemetry-exporter-otlp-proto-grpc>=1.20`

**Operations / DX**
- Pipeline file watcher: `tram/watcher/pipeline_watcher.py`; `TRAM_WATCH_PIPELINES=true` watches `TRAM_PIPELINE_DIR` for YAML changes using watchdog; auto-reloads on create/modify, deregisters on delete
- Pipeline linter: `tram/pipeline/linter.py`; five rules: L001 (source+no sinks), L002 (skip+no DLQ), L003 (stream+workers>1), L004 (batch_size on stream), L005 (email alert+no SMTP); integrated into `tram validate`
- `tram pipeline init <name>`: scaffolds a minimal pipeline YAML to stdout or file
- New optional extra: `tram[watch]` = `watchdog>=3.0`

### Changed
- `tram/api/app.py`: version `"1.0.0"`, middleware registration, OTel init, pipeline watcher in lifespan
- `tram/cli/main.py`: all API calls inject `X-API-Key` header when `TRAM_API_KEY` is set; `validate` calls linter
- `helm/Chart.yaml`, `helm/values.yaml`: version 1.0.0

---

## [0.9.0] — 2026-03-05

### Added

**`thread_workers` — intra-node parallelism**
- `PipelineConfig.thread_workers: int = 1` — number of worker threads per pipeline run
- `batch_run()`: when `thread_workers > 1`, chunks from the source are submitted to a
  `ThreadPoolExecutor(max_workers=thread_workers)` so N chunks process concurrently; single-
  threaded code path unchanged for `thread_workers=1`
- `stream_run()`: when `thread_workers > 1`, a bounded `Queue(maxsize=thread_workers * 2)`
  decouples the source producer from N worker threads, providing natural backpressure
- `PipelineRunContext` is now fully thread-safe: all counter mutations go through
  `threading.Lock`-protected helper methods (`inc_records_in`, `inc_records_out`,
  `inc_records_skipped`, `record_error`, `record_dlq`)

**`batch_size` — record cap per run**
- `PipelineConfig.batch_size: Optional[int] = None` — limits records processed per batch run
- Source read loop breaks once `ctx.records_in >= batch_size`; remaining source chunks skipped
- Works in both single-threaded and multi-threaded modes
- Useful for controlling run duration on large sources (Kafka backlog, large S3 buckets)

**`on_error: "dlq"` — explicit DLQ routing**
- `on_error` Literal extended with `"dlq"` value
- Model validator raises `ValueError` if `on_error="dlq"` is set without a `dlq` sink configured
- Runtime behavior identical to `on_error="continue"` with DLQ sink present — makes intent explicit

**Processed-file tracking**
- New DB table: `processed_files (pipeline_name, source_key, filepath, processed_at)` — PRIMARY KEY on all three name fields; indexed on `(pipeline_name, source_key)` for fast lookup
- `TramDB.is_processed(pipeline, source_key, filepath) -> bool`
- `TramDB.mark_processed(pipeline, source_key, filepath)` — dialect-aware upsert; errors logged and swallowed
- `ProcessedFileTracker` wrapper in `tram/persistence/file_tracker.py` — silences DB errors, safe for use in connectors
- `skip_processed: bool = False` added to `SFTPSourceConfig`, `LocalSourceConfig`, `S3SourceConfig`, `FtpSourceConfig`, `GcsSourceConfig`, `AzureBlobSourceConfig`
- Source connectors check `is_processed` before reading and call `mark_processed` after successful yield + `_post_read`
- `PipelineExecutor._build_source()` injects `_file_tracker` into source config dict when `file_tracker` is present on the executor
- `TramScheduler` and `create_app()` wired to create and pass `ProcessedFileTracker` when DB is available

**CORBA source connector**
- `@register_source("corba")` — DII (Dynamic Invocation Interface) mode; no pre-compiled IDL stubs required
- Supports: direct IOR (`ior:`) or NamingService resolution (`naming_service:` + `object_name:`)
- `operation:` names the CORBA operation; `args:` passes positional scalar arguments via DII
- Result normalised to `list[dict]` via `_corba_to_python()` (handles structs, nested sequences)
- `skip_processed: bool` supported via `ProcessedFileTracker` — invocation key = `operation:args_json`
- `pip install tram[corba]` (pulls `omniORBpy>=4.3`)
- `CorbaSourceConfig` in Pydantic models with `model_validator` requiring `ior` or `naming_service`
- Plugin key: `corba`

**Helm: ConfigMap checksum annotation**
- `checksum/config` annotation added to the StatefulSet pod template (when `pipelines` values are non-empty)
- Value: `sha256sum` of the rendered `configmap.yaml` — changes when any pipeline YAML changes
- Kubernetes detects the pod spec diff and triggers a rolling restart automatically on `helm upgrade`

**Tests** — 62 new tests (`test_thread_workers.py` ×13, `test_batch_size_on_error.py` ×10,
`test_processed_files.py` ×15, `test_corba_connector.py` ×24); **535 total, all passing**

### Changed
- `PipelineExecutor.__init__` gains `file_tracker: ProcessedFileTracker | None = None`
- `TramScheduler.__init__` gains `file_tracker: ProcessedFileTracker | None = None`
- `executor._build_source()` injects both `_pipeline_name` and `_file_tracker` into source config
- `tram/__init__.__version__` → `"0.9.0"`

---

## [0.8.1] — 2026-03-05

### Fixed

**Kafka consumer group isolation**
- `KafkaSourceConfig.group_id` default changed from `"tram"` (shared across every pipeline) to
  `None` — resolved at runtime to the pipeline name, giving each pipeline its own consumer group
- Pipelines that set `group_id:` explicitly in YAML are unaffected
- Added explicit `consumer.commit()` before `consumer.close()` — best-effort offset flush on clean
  shutdown (supplements `enable_auto_commit=True` timer; no-ops on abrupt kill)
- Fallback chain: explicit `group_id` → pipeline name → `"tram"` (if no pipeline name available)

**NATS queue group for cluster mode**
- `NatsSourceConfig.queue_group` default changed from `""` (broadcast — all cluster nodes receive
  every message) to `None` — resolved at runtime to the pipeline name (competing consumers, correct
  for cluster mode where the same pipeline runs on all nodes)
- `queue_group: ""` in YAML still works as an explicit broadcast opt-out
- Fallback chain: explicit `queue_group` (including `""`) → pipeline name → `""` (broadcast)

**Pipeline name injection**
- `PipelineExecutor._build_source()` now injects `_pipeline_name` into the source config dict;
  connectors can use `config.get("_pipeline_name")` as a safe default for group/queue identifiers

**Helm chart**
- `helm/values.yaml` `image.tag` corrected from `"0.6.0"` to `"0.8.1"`

**Tests** — 20 new tests (`test_kafka_connectors.py` ×16, `test_nats_connectors.py` ×5 new);
**473 total, all passing**

---

## [0.8.0] — 2026-03-05

### Added

**StatefulSet self-organizing cluster**
- `tram/cluster/registry.py` — `NodeRegistry`: registers the local node in the shared DB, runs a
  periodic heartbeat thread, expires stale peers (`status='dead'`), deregisters on clean shutdown
- `tram/cluster/coordinator.py` — `ClusterCoordinator`: caches live node topology, determines
  pipeline ownership via consistent hashing: `sha1(pipeline_name) % live_node_count == my_position`
- Ownership uses **sorted position** in live node list (not static ordinal) — handles non-sequential
  ordinals gracefully when a node fails (tram-0, tram-2 become positions 0 and 1)
- Safe fallback: if no live nodes in DB (startup race), the node owns all pipelines
- `detect_ordinal(node_id)` helper: extracts ordinal suffix from StatefulSet hostname (`tram-2` → `2`)

**DB: node_registry table**
- `node_registry` table: `node_id, ordinal, registered_at, last_heartbeat, status`
- New `TramDB` methods: `register_node()` (dialect-aware upsert), `heartbeat()`, `expire_nodes()`,
  `get_live_nodes()`, `deregister_node()`
- Cluster mode requires an external DB (`TRAM_DB_URL`); SQLite is blocked with a warning

**Cluster env vars (AppConfig)**
- `TRAM_CLUSTER_ENABLED` — enable cluster mode (default: `false`)
- `TRAM_NODE_ORDINAL` — override ordinal (default: auto-detected from hostname)
- `TRAM_HEARTBEAT_SECONDS` — heartbeat interval in seconds (default: `10`)
- `TRAM_NODE_TTL_SECONDS` — seconds before a silent node is marked dead (default: `30`)

**Scheduler: dynamic rebalance**
- `TramScheduler` gains `coordinator` and `rebalance_interval` parameters
- Ownership check in `_schedule_pipeline()` — nodes skip pipelines they don't own
- Background `tram-rebalance` thread: polls `coordinator.refresh()` every N seconds; on topology
  change calls `_rebalance()` which starts newly owned pipelines and stops released ones

**Cluster API endpoint**
- `GET /api/cluster/nodes` — returns `cluster_enabled`, `node_id`, `my_position`,
  `live_node_count`, `nodes` list; returns `{"cluster_enabled": false}` in standalone mode

**Helm: always-StatefulSet design**
- `helm/templates/statefulset.yaml` — always rendered; `replicaCount=1` standalone, `N` cluster
- `helm/templates/headless-service.yaml` — always rendered; headless Service for stable pod DNS
- `deployment.yaml` and `pvc.yaml` removed — replaced by `volumeClaimTemplates` in StatefulSet
- `volumeClaimTemplates` auto-provisions `data-tram-N` PVC per pod — survives pod restarts and
  rescheduling; PVC stays bound to the same pod across node reschedules
- `helm/values.yaml` — `clusterMode.enabled: false` controls `TRAM_CLUSTER_ENABLED` env var
- `helm/Chart.yaml` — version bumped to `0.8.0`

**Tests** — 22 new tests (`test_cluster.py`); **453 total, all passing**

### Changed
- `TramScheduler.__init__` gains optional `coordinator: ClusterCoordinator | None` and
  `rebalance_interval: int` parameters (backward compatible — defaults to standalone behaviour)
- `tram/api/app.py` wires `NodeRegistry` + `ClusterCoordinator` from `AppConfig` in lifespan
- `tram/__init__.__version__` → `"0.8.0"`

---

## [0.7.0] — 2026-03-05

### Added

**SQLAlchemy Core DB abstraction**
- `tram/persistence/db.py` rewritten on SQLAlchemy Core — any backend supported via `TRAM_DB_URL`
- SQLite (default), PostgreSQL (`tram[postgresql]`), MySQL/MariaDB (`tram[mysql]`) all work out of the box
- `TRAM_DB_URL` env var (SQLAlchemy URL); falls back to `TRAM_DB_PATH` → SQLite when unset
- Connection pooling (`pool_size=5`, `max_overflow=10`, `pool_pre_ping=True`) for non-SQLite backends
- `sqlalchemy>=2.0` added to core dependencies (was previously in `[sql]` optional only)
- New optional extras: `postgresql = ["psycopg2-binary>=2.9"]`, `mysql = ["PyMySQL>=1.1"]`

**Node identity**
- `AppConfig.node_id` — from `TRAM_NODE_ID` env (default: `socket.gethostname()`)
- `node_id` stored in every `run_history` row — essential for multi-node cluster debugging
- `TramDB(url, node_id)` constructor; node_id auto-stamped on every `save_run()`

**`dlq_count` persisted**
- `RunResult.dlq_count: int = 0` field added; `from_context()` carries it from `PipelineRunContext`
- `to_dict()` now includes `dlq_count`
- `dlq_count` column added to `run_history` table
- `tram_dlq_total` Prometheus counter (`pipeline` label) incremented on every DLQ write

**Graceful shutdown**
- `TramScheduler.stop(timeout: int = 30)` — signals all stream threads, waits for in-flight batch runs via `ThreadPoolExecutor.shutdown(wait=True)`, joins stream threads with timeout
- `TRAM_SHUTDOWN_TIMEOUT_SECONDS` env var (default `30`) wired through `AppConfig` and `lifespan`
- SIGTERM handler in `daemon/server.py` converts SIGTERM → SIGINT so uvicorn gets a clean shutdown (critical for Docker / Kubernetes PID 1)

**Readiness DB check**
- `TramDB.health_check()` executes `SELECT 1`; returns `True/False`
- `GET /api/ready` returns `503` when DB is configured but unreachable

**Run history pagination**
- `GET /api/runs` gains `offset` and `from_dt` query params
- `TramDB.get_runs(offset, from_dt)` — `OFFSET` clause + `started_at >=` filter
- `PipelineManager.get_runs()` and in-memory fallback both support new params
- `TramDB.get_run(run_id)` now queries DB directly (previously only searched in-memory deque)

**Schema migration**
- `_create_tables()` is idempotent: `CREATE TABLE IF NOT EXISTS` + `_add_column_if_missing()` helper
- Existing v0.6.0 SQLite databases upgraded automatically on first start (adds `node_id`, `dlq_count` to `run_history`)

**Tests** — 25 new tests (`test_db_v07.py` ×15, `test_config_v07.py` ×6, `test_runresult_v07.py` ×4); **431 total, all passing**

### Changed
- `TramDB.__init__` signature: `path: Path` → `url: str = "", node_id: str = ""` (uses SQLAlchemy URL)
- `pipeline_versions.id` now TEXT UUID (generated in Python); fresh databases get UUID ids; existing SQLite databases keep their integer ids (SQLite flexible typing)
- `AppConfig` gains `node_id`, `db_url`, `shutdown_timeout` fields (from env: `TRAM_NODE_ID`, `TRAM_DB_URL`, `TRAM_SHUTDOWN_TIMEOUT_SECONDS`)

---

## [0.6.0] — 2026-03-05

### Added

**Dead-Letter Queue (DLQ)**
- `PipelineConfig.dlq: Optional[SinkConfig]` — any sink type can serve as DLQ; receives failed records as JSON envelopes
- Envelope schema: `{_error, _stage, _pipeline, _run_id, _timestamp, record, raw}` where `raw` (base64) is only present for parse-stage failures
- Three failure stages captured: `parse` (serializer_in failed), `transform` (global or per-sink transform raised), `sink` (sink.write() raised)
- Per-record transform isolation: global transforms applied record-by-record; a single bad record no longer aborts the entire chunk
- DLQ write errors are logged and swallowed — never propagate to main pipeline
- `PipelineRunContext.dlq_count` tracks how many records were DLQ'd in a run

**Per-Sink Transform Chains**
- Each sink config gains `transforms: list[TransformConfig]` (default empty)
- Applied **after** global pipeline transforms and **after** condition filtering, **before** serializing for that specific sink
- Sink transforms are independent: different sinks can reshape the same records differently
- Sink transform failures route to DLQ (if configured) and skip that sink; other sinks continue
- `_build_sinks()` now returns `list[tuple[BaseSink, condition, list[BaseTransform]]]`

**Alert Rules**
- `AlertRuleConfig` model: `condition` (simpleeval), `action` (webhook|email), `webhook_url`, `email_to`, `subject`, `cooldown_seconds` (default 300)
- `PipelineConfig.alerts: list[AlertRuleConfig]`
- `AlertEvaluator` in `tram/alerts/evaluator.py` — evaluated after every batch run
- Alert condition namespace: `records_in`, `records_out`, `records_skipped`, `error_rate`, `status`, `failed`, `duration_seconds`
- Cooldown persisted in new SQLite `alert_state` table — survives daemon restarts
- Webhook action: `httpx.POST` with full run payload; email action: `smtplib` STARTTLS
- SMTP configured via env vars: `TRAM_SMTP_HOST/PORT/USER/PASS/TLS/FROM`
- All action errors logged and swallowed
- `PipelineManager` accepts `alert_evaluator: AlertEvaluator | None`; `AlertEvaluator(db=db)` instantiated in `create_app()`

**Helm Chart** (`helm/`)
- `Chart.yaml` — apiVersion v2, version 0.6.0
- `values.yaml` — image, replicaCount (fixed at 1), service, persistence (SQLite PVC), env, envSecret, pipelines ConfigMap, resources, nodeSelector, tolerations, affinity, podAnnotations, serviceAccount
- Templates: `statefulset.yaml`, `service.yaml`, `headless-service.yaml`, `configmap.yaml`, `serviceaccount.yaml`, `_helpers.tpl`, `NOTES.txt`
- Storage managed via `volumeClaimTemplates` (introduced in v0.8.0; v0.6.0 used `deployment.yaml` + `pvc.yaml`)

**GitHub Actions**
- `.github/workflows/ci.yml` — triggers on push to `main`/`develop` and all PRs; runs ruff + pytest on Python 3.11 and 3.12
- `.github/workflows/release.yml` — triggers on `v*` tags; builds multi-arch Docker image (linux/amd64 + linux/arm64) → `ghcr.io/{owner}/trishul-ram:{semver}`; packages + pushes Helm chart → `oci://ghcr.io/{owner}/charts/trishul-ram`

**SQLite**
- New `alert_state` table: `(pipeline_name, rule_name, last_alerted_at)` primary key
- `TramDB.get_alert_cooldown()` / `set_alert_cooldown()` methods

**Tests** — 35 new tests (test_dlq.py ×11, test_sink_transforms.py ×8, test_alerts.py ×16); **406 total, all passing**

### Changed
- `tram/models/pipeline.py` — Transforms section moved before Sinks section to avoid Pydantic v2 forward-reference issues with `list[TransformConfig]` on sink classes
- `_build_sinks()` return type widened to 3-tuple `(BaseSink, condition | None, list[BaseTransform])`

---

## [0.5.0] — 2026-03-03

### Added

**Conditional Multi-Sink Routing**
- `sinks: list[SinkConfig]` replaces `sink: SinkConfig` (backward compat: singular `sink:` auto-wrapped by model_validator)
- Per-sink `condition: Optional[str]` — simpleeval expression evaluated per record; sink is skipped if no records match
- Catch-all sink (no condition) receives all records
- `rate_limit_rps: Optional[float]` on `PipelineConfig` — token-bucket rate limiter across all sink writes

**SQLite Persistence** (`tram/persistence/db.py`)
- `TramDB` wraps `sqlite3`; DB at `~/.tram/tram.db` (or `$TRAM_DB_PATH`)
- Tables: `run_history` (persists `RunResult`), `pipeline_versions` (auto-saved on register)
- `PipelineManager` accepts `db: TramDB | None`; `record_run()` persists to SQLite; `get_runs()` queries SQLite
- API: `GET /api/pipelines/{name}/versions`, `POST /api/pipelines/{name}/rollback?version=N`
- CLI: `tram pipeline history <name>`, `tram pipeline rollback <name> --version N`

**Prometheus Metrics** (`tram/metrics/registry.py`)
- Counters: `tram_records_in_total`, `tram_records_out_total`, `tram_records_skipped_total`, `tram_errors_total` (labeled by `pipeline`)
- Histogram: `tram_chunk_duration_seconds`
- All metrics are no-ops when `prometheus_client` is not installed
- `GET /metrics` endpoint (503 if not installed)
- New optional extra: `pip install tram[metrics]`

**Webhook Source** (`tram/connectors/webhook/source.py`)
- `@register_source("webhook")` — receives HTTP POSTs forwarded from `/webhooks/{path}` on the daemon port
- Module-level `_WEBHOOK_REGISTRY` bridges FastAPI router → source generator
- Optional `secret` for `Authorization: Bearer` validation
- New API router: `POST /webhooks/{path}` → 202 Accepted / 404 / 401

**WebSocket Connector** (`tram/connectors/websocket/`)
- `@register_source("websocket")` — background thread + asyncio loop + SimpleQueue bridge; auto-reconnect
- `@register_sink("websocket")` — `asyncio.run()` connect/send/close per write
- Optional dep: `websockets>=12.0`; new extra `pip install tram[websocket]`

**Elasticsearch Connector** (`tram/connectors/elasticsearch/`)
- `@register_source("elasticsearch")` — search + scroll API
- `@register_sink("elasticsearch")` — `helpers.bulk()` with `index_template` token substitution
- Optional dep: `elasticsearch>=8.0`; new extra `pip install tram[elasticsearch]`

**Prometheus Remote-Write Source** (`tram/connectors/prometheus_rw/source.py`)
- `@register_source("prometheus_rw")` — Snappy-decompress + protobuf `WriteRequest` → `list[dict]`
- Reuses WebhookSource global registry (path-routed via daemon)
- Optional dep: `protobuf>=4.25`, `python-snappy>=0.7`; new extra `pip install tram[prometheus_rw]`

**Schema Registry** (`tram/schema_registry/client.py`)
- `SchemaRegistryClient` — Confluent-compatible REST API (also Apicurio); in-memory cache by schema_id
- `encode_with_magic(schema_id, payload)` / `decode_magic(data)` — Confluent magic-byte `\x00` + 4-byte BE ID framing
- Avro serializer gains `schema_registry_url/subject/id` + `use_magic_bytes` config
- Protobuf serializer gains same registry config

**New Pydantic Models**
- Sources: `WebhookSourceConfig`, `WebSocketSourceConfig`, `ElasticsearchSourceConfig`, `PrometheusRWSourceConfig`
- Sinks: `WebSocketSinkConfig`, `ElasticsearchSinkConfig`
- Serializers: `AvroSerializerConfig` and `ProtobufSerializerConfig` extended with registry fields

**Tests** — 49 new tests; **371 total, all passing**

---

## [0.4.0] — 2026-03-03

### Added

**New Serializers**
- `avro` — fastavro read/write; requires `pip install tram[avro]`
- `parquet` — pyarrow read/write; requires `pip install tram[parquet]`
- `msgpack` — msgpack pack/unpack; requires `pip install tram[msgpack_ser]`
- `protobuf` — runtime .proto compilation via grpcio-tools; length-delimited framing; requires `pip install tram[protobuf_ser]`

**New Source Connectors**
- `mqtt` — paho-mqtt subscriber; TLS support; reconnect on drop
- `amqp` — pika consumer; prefetch, auto-ack configurable
- `nats` — nats-py subscriber; queue groups; credentials file
- `gnmi` — pygnmi subscription (telemetry streaming)
- `sql` — SQLAlchemy; chunked reads
- `influxdb` — influxdb-client Flux query
- `redis` — list LPOP or stream XREAD modes
- `gcs` — google-cloud-storage; blob listing + streaming
- `azure_blob` — azure-storage-blob; container listing + streaming

**New Sink Connectors**
- `amqp` — pika publisher to exchange/routing-key
- `nats` — nats-py publisher
- `sql` — SQLAlchemy insert/upsert
- `influxdb` — line-protocol write
- `redis` — list RPUSH, pubsub PUBLISH, or stream XADD
- `gcs` — google-cloud-storage blob upload
- `azure_blob` — azure-storage-blob upload

**New Transforms**
- `explode` — expand a list field into multiple rows
- `deduplicate` — remove duplicate rows by key fields
- `regex_extract` — extract named capture groups from a string field
- `template` — render Jinja-style `{field}` string templates
- `mask` — redact, hash, or partial-mask sensitive fields
- `validate` — schema validation with `on_invalid: drop|raise`
- `sort` — sort records by field list
- `limit` — keep only first N records
- `jmespath` — JMESPath field extraction
- `unnest` — lift a nested dict field to top level

**Tests** — 322 total, all passing

---

## [0.3.0] — 2026-03-03

### Added

**New Connectors**
- `ftp` source + sink — ftplib; move/delete after read; passive mode
- `s3` source + sink — boto3; endpoint_url override for S3-compatible stores
- `syslog` source — UDP/TCP listener; RFC 3164/5424 parsing
- `snmp_trap` source + sink — pysnmp trap receiver / sender
- `snmp_poll` source — GET/WALK OID polling
- `ves` sink — ONAP VES event batch sender; auth types: none/basic/bearer
- `opensearch` source (scroll) added alongside existing sink

**Tests** — 198 total, all passing

---

## [0.2.0] — 2026-03-03

### Added

**New Transforms**
- `flatten` — recursive dict flattening with configurable `separator`, `max_depth`, and `prefix`
- `timestamp_normalize` — normalizes heterogeneous timestamps to UTC ISO-8601
- `aggregate` — groupby + sum/avg/min/max/count/first/last
- `enrich` — left-join records with a static CSV or JSON lookup file

**New Connectors**
- `local` source + sink — reads/writes local filesystem files
- `rest` source + sink — HTTP polling source and POST/PUT sink (httpx)
- `kafka` source + sink — KafkaConsumer/Producer; SASL/SSL support
- `opensearch` sink — bulk-indexes records via opensearch-py

**Tests** — 124 total, all passing

---

## [0.1.0] — 2026-03-03

### Added

**Core**
- `tram.core.exceptions` — `TramError` hierarchy
- `tram.core.context` — `PipelineRunContext` + `RunResult` + `RunStatus`
- `tram.core.config` — `AppConfig` from environment variables
- `tram.core.log_config` — JSON-structured logging

**Plugin Interfaces** — `BaseSource`, `BaseSink`, `BaseTransform`, `BaseSerializer`

**Plugin Registry** — `@register_*` decorators + `get_*()` lookups + `list_plugins()`

**Pydantic Models** — `PipelineConfig` with discriminated unions; `ScheduleConfig`

**Serializers** — `json`, `csv`, `xml`

**Transforms** — `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`

**Connectors** — `sftp` source + sink

**Pipeline Engine** — `loader.py`, `executor.py` (batch/stream/dry-run), `manager.py`

**Scheduler** — `TramScheduler` (APScheduler batch + threads stream)

**REST API** — FastAPI on port 8765; health, pipelines, runs, daemon endpoints

**CLI** — Typer; direct + daemon-proxy commands

**Tests** — 69 total, all passing

---

<!-- Comparison links -->
[Unreleased]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.3.3...v1.4.0
[1.3.3]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.2.3...v1.3.0
[1.2.3]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.1.4...v1.2.0
[1.1.4]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.1.3...v1.1.4
[1.1.3]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.0.9...v1.1.0
[1.0.9]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.0.8...v1.0.9
[1.0.8]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.0.7...v1.0.8
[1.0.7]: https://github.com/tosumitdhaka/trishul-ram/compare/v1.0.6...v1.0.7
