# Missing Test Report

Date: `2026-05-01`

Coverage source:
- `.venv/bin/pytest tests/ --cov=tram --cov-report=term-missing -q`
- Current overall backend coverage: `82%`
- Current full-suite result: `1652 passed`

This report focuses on important code paths: the manager/worker control plane, the core execution engine, persistence, and the API surface that the UI depends on. It intentionally does not treat every low-coverage connector as equally important.

## Priority 1

### 1. Pipeline controller

Target:
- `tram/pipeline/controller.py` (`84%`)

Why it matters:
- This is the main state machine for scheduling, worker dispatch, run completion, placement recovery, standalone live stats, and Kubernetes service reconciliation.

Important missing tests:
- `_run_batch()` orphan-job cleanup when a scheduled batch fires for a deleted pipeline.
- `_run_batch()` local exception path that leaves the pipeline in `error`.
- `adopt_active_batch_run()` rejection for missing pipelines and stream pipelines, plus `started_at` string parsing.
- `mark_active_batch_run_lost()` with and without an active lease, including worker-id lookup and fallback timestamps.
- `on_worker_run_complete()` duplicate callback handling, unknown status fallback, and the branch that delays finalization while other stream run ids are still active.
- `_restore_broadcast_placement()`, `_update_broadcast_placement_status()`, and `on_pipeline_stats()` reconciliation transitions.
- `redispatch_broadcast_slot()` failure branches: missing slot, missing pipeline, no replacement worker, and dispatch rejection.
- `_activate_kubernetes_service()` and `_deactivate_kubernetes_service()` warning-only failure handling.
- `_emit_local_stats_once()` and `_local_stats_loop()` error-tolerance behavior.

Suggested test files:
- Extend `tests/unit/test_pipeline_controller.py`
- Extend `tests/unit/test_controller_standalone_stats.py`
- Extend `tests/unit/test_controller_metrics.py`

### 2. Pipeline executor

Target:
- `tram/pipeline/executor.py` (`76%`)

Why it matters:
- This is the hot path for parsing, transform execution, sink fan-out, bytes/records accounting, retries, DLQ behavior, and stream backpressure.

Important missing tests:
- `_process_records()` with per-sink conditions filtering everything.
- Per-sink transform failure with DLQ enabled vs disabled.
- Circuit-breaker behavior: trip threshold, open-sink skip, and reset on later success.
- Partitioned writes produced by filename-template fields, including bytes-out accounting across partitions.
- `parallel_sinks=True` path, especially exception propagation when `on_error="abort"`.
- `_process_chunk_incrementally()` using `parse_chunks()`, including `record_chunk_size` and `batch_size` truncation.
- `_batch_run_inner()` retry path that rebuilds source, sinks, serializers, transforms, and DLQ sink after a failed attempt.
- `_run_batch_chunks()` threaded mode cancellation/error path and single-thread source finalization on both success and failure.
- `stream_run()` stop watcher calling `source.stop()` for blocking sources.
- `_stream_run_threaded()` queue-depth metric reset and worker-thread exception handling.

Suggested test files:
- Extend `tests/unit/test_executor.py`
- Add `tests/unit/test_executor_stream.py` if separation is cleaner

### 3. Persistence layer

Target:
- `tram/persistence/db.py` (`75%`)

Why it matters:
- The database layer backs run history, pipeline version history, stopped flags, settings, and broadcast placement recovery.

Important missing tests:
- `_build_engine()` non-SQLite engine kwargs and SQLite path creation fallback.
- `_add_column_if_missing()` PostgreSQL branch and generic ignore-on-duplicate branch.
- `save_run()` duplicate `run_id` handling.
- `_row_to_run_result()` malformed `errors_json` fallback to `[]`.
- Pipeline version failure cases: missing version, missing active version.
- `set_alert_cooldown()` PostgreSQL, MySQL, and generic fallback SQL branches.
- `mark_processed()` PostgreSQL, MySQL, generic fallback, and warning-only exception path.
- Stopped-flag aliases: `pause_pipeline()`, `resume_pipeline()`, `is_pipeline_paused()`, `get_paused_pipeline_names()`.
- Shared pipeline registry behavior: save/update/delete/source ownership and deleted-name queries.
- Settings CRUD: `get_setting()`, `set_setting()`, `delete_setting()`.
- Broadcast placement persistence: save, load active placements, update status, and `update_slot_run_id()`.

Suggested test files:
- Add `tests/unit/test_persistence_db.py`
- Keep `tests/unit/test_db_v07.py` for migration-specific regression coverage

## Priority 2

### 4. FastAPI app factory and lifespan

Target:
- `tram/api/app.py` (`67%`)

Why it matters:
- This wires together DB fallback, controller startup, worker pool startup, reconcilers, watchers, middleware, and UI mounting.

Important missing tests:
- `create_app()` DB retry loop, including final fallback to in-memory mode after repeated failures.
- Alert evaluator init failure should not break app creation.
- Manager mode with no workers configured should warn but still build the app.
- Reconciler creation only when manager mode, worker pool, and DB are all present.
- UI mount path only when `ui_dir` exists.
- `lifespan()` startup/shutdown ordering for worker pool, controller, reconcilers, watcher, and DB close.
- `lifespan()` disk seeding behavior when a pipeline is already user-owned (`source="api"`).
- Watcher import failure vs generic watcher startup failure.

Suggested test files:
- Add `tests/unit/test_api_app.py`
- Keep integration smoke in `tests/integration/test_api_full.py`

### 5. Pipelines router

Target:
- `tram/api/routers/pipelines.py` (`80%`)

Why it matters:
- These endpoints drive most CRUD/lifecycle actions in the UI and are the public control-plane contract.

Important missing tests:
- `pause` and `resume` alias endpoints as explicit compatibility routes.
- `stop` and `restart` 404/500 branches.
- `reload` disk seeding behavior, especially skipping API-owned pipelines and returning correct seeded counts.
- `_read_alerts_data()` returning `503` when YAML is not stored.
- `_save_alerts_data()` invalid regenerated YAML returning `400`.
- `_save_alerts_data()` scheduler stop/start failures being swallowed while the alert update still succeeds.
- `rollback` returning `503` on runtime failure and tolerating restart failure with warning-only behavior.

Suggested test files:
- Extend `tests/unit/test_api_pipelines.py`

### 6. Schemas router

Target:
- `tram/api/routers/schemas.py` (`75%`)

Why it matters:
- This backs schema browsing/upload and the Schema Registry proxy used by the UI.

Important missing tests:
- Schema Registry proxy when `TRAM_SCHEMA_REGISTRY_URL` is unset (`503`).
- Proxy forwarding of method, query params, body, and filtered headers.
- Proxy passthrough of downstream status/body/headers.
- Proxy `502` behavior on upstream client failure.
- `get_schema()` read-failure branch returning `500`.
- `upload_schema()` traversal rejection through `subdir`.
- `upload_schema()` atomic-write cleanup when the temp write or replace fails.
- `delete_schema()` success path and filesystem side effects.

Suggested test files:
- Extend `tests/unit/test_schemas_api.py`

## Priority 3

### 7. Kubernetes service manager

Target:
- `tram/pipeline/k8s_service_manager.py` (`79%`)

Why it matters:
- This controls dedicated push-ingress Services and Endpoints for stream pipelines in standalone and manager modes.

Important missing tests:
- `_get_api()` dependency-missing and kube-config-missing branches, including disabled-reason caching.
- `_get_pod_labels()` failure path when the current pod cannot be read.
- `_build_selector()` when app identity labels are missing.
- `_read_worker_pod()` non-404 failure path.
- `ensure_service()` non-404 lookup failure path.
- `_ensure_endpoints()` non-404 lookup failure path.
- `_delete_endpoints()` non-404 delete failure path.
- `delete_service()` non-404 delete failure path.

Suggested test files:
- Extend `tests/unit/test_k8s_service_manager.py`

### 8. Worker pool

Target:
- `tram/agent/worker_pool.py` (`89%`)

Why it matters:
- Coverage is already decent, but the remaining misses are on control-plane edge handling rather than trivial helpers.

Important missing tests:
- `worker_status()` failure path returning `None`.
- `status()` and `live_streams()` behavior when some workers are healthy in cache but their live status endpoint fails.
- `multi_dispatch()` with pinned worker ids that are missing or unhealthy, producing stale slots and degraded results.
- `dispatch_to_worker()` returning `False` for an unhealthy worker without attempting HTTP dispatch.
- `_stop_run_on_worker()` failure branch.
- `stop_pipeline_runs()` partial status-probe failure and partial stop failure handling.

Suggested test files:
- Extend `tests/unit/test_worker_pool.py`

### 9. Metrics registry fallback

Target:
- `tram/metrics/registry.py` (`67%`)

Why it matters:
- This is lower risk than controller/executor work, but it is the central fallback layer when `prometheus_client` is absent.

Important missing tests:
- Import-fallback path that exposes no-op counters/gauges/histograms.
- No-op gauge `inc()` and `dec()` smoke coverage.
- One smoke test that the exported manager metric symbols still exist in both import modes.

Suggested test files:
- Extend `tests/unit/test_metrics_registry.py`

## Lower Priority, Still Undercovered

These are clearly low-coverage, but they are not the first place to spend test effort unless they are actively used in production:

- `tram/connectors/prometheus_rw/source.py` (`16%`)
- `tram/connectors/opensearch/sink.py` (`17%`)
- `tram/connectors/kafka/sink.py` (`18%`)
- `tram/connectors/websocket/source.py` (`30%`)
- `tram/connectors/websocket/sink.py` (`49%`)
- `tram/connectors/rest/source.py` (`57%`)
- `tram/connectors/snmp/source.py` (`60%`)

## Recommended Order

If the goal is highest risk reduction per test added, take the work in this order:

1. `tram/pipeline/controller.py`
2. `tram/pipeline/executor.py`
3. `tram/persistence/db.py`
4. `tram/api/app.py`
5. `tram/api/routers/pipelines.py`
6. `tram/api/routers/schemas.py`
7. `tram/pipeline/k8s_service_manager.py`
8. `tram/agent/worker_pool.py`
9. `tram/metrics/registry.py`

That order covers the runtime, recovery, persistence, and API contract paths that can cause the most user-visible failures even when total coverage already looks acceptable.
