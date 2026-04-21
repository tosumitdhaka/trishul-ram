# v1.3.2 Design Plan: Metrics/Stats Parity & UDP Multi-Worker Streams

**Status:** Draft for review
**Target version:** v1.3.2
**Branch:** observer
**Scope:** Standalone and manager modes. Worker mode is execution-only; it already reports stats upstream.

---

## Goal

v1.3.2 closes three observability gaps deferred from v1.3.0/v1.3.1:

1. **Standalone live stats parity (A)** — standalone mode exposes live stats for active stream
   pipelines via `StatsStore` and the existing `GET /api/cluster/streams` surface, instead of
   only DB run-history and process-local Prometheus metrics.
2. **Manager operational metrics (B)** — Prometheus series on the manager process for dispatch
   decisions, reconciler actions, worker health, and callback receipt.
3. **UDP multi-worker streams (C)** — lift the L008 linter block on `syslog` and `snmp_trap`
   sources in manager mode; provision K8s Services for UDP multi-worker streams via the same
   generic `kubernetes:` block used by HTTP sources (`kubernetes: enabled: true` required in
   manager mode for all UDP push sources); validate the UDP path end-to-end in kind.

This release does **not** contain:

- UI revalidation (v1.3.3)
- Pipeline cloning, bulk actions, or live log streaming (backlog)
- Connector hardening (backlog)

---

## Design Decisions

### 1. Standalone stats: stream-only, in-process loop, no HTTP hop

`create_app()` already creates `StatsStore` unconditionally and wires it into `controller` and
`app.state.stats_store`. The gap is that standalone `_stream_worker()` calls
`executor.stream_run()` with no `stats=` argument, so `PipelineStats` is never created and
`StatsStore` always stays empty.

**Scope: stream pipelines only.** Batch runs in standalone mode are finite and synchronous —
they complete in seconds to minutes and their final counts are already written to `run_history`
via `manager.record_run()`. There is no operational benefit to making batch runs visible in
`StatsStore` (no load scoring, no placement reconciler). Mid-run batch visibility is deferred.

**In-process update, not HTTP.** The standalone stats loop constructs `PipelineStatsPayload`
directly from `PipelineStats.snapshot_and_reset_window()` and calls `stats_store.update()`
in-process. No HTTP call, no `/api/internal/pipeline-stats` route involved.

**Removal is explicit, never via `is_final`.** The `is_final` flag is a worker-to-manager HTTP
protocol convention. Its only consumer is the `POST /api/internal/pipeline-stats` HTTP handler
in `tram/api/routers/internal.py`, which calls `store.remove(run_id)` when `is_final=True`.
There is no local consumer of `is_final` in the standalone process. Calling
`stats_store.update(payload)` with `is_final=True` in-process would leave completed entries in
the store until the 3× interval stale timeout — it does not trigger removal.

The standalone path therefore uses two explicit calls:
- The stats loop always calls `stats_store.update(payload)` with `is_final=False`.
- `_stream_worker()` calls `stats_store.remove(run_id)` directly in its `finally` block when
  the stream exits.

**Approximate parity, not structural parity.** `GET /api/cluster/streams` in standalone reuses
the existing `build_cluster_streams()` fallback in `_stream_views.py`, which surfaces
`StatsStore.all_active()` entries that are not associated with a placement group. This path
already exists and requires no changes — but it produces synthetic views that are structurally
different from manager-mode placements in three ways:

1. `placement_group_id` is `null` — no persisted group identity, no controller-owned slot.
2. `started_at` is `null` — no placement-backed timestamp; only stats timestamps are available.
3. No reconciliation: a standalone stream that stops updating will age out of `all_active()`
   after `3 × interval` seconds, but there is no reconciler to re-dispatch it.

The roadmap goal of "same live stats model" refers to counters and rates being live and
observable, not to structural placement parity. True structural parity would require the
controller to own synthetic placement objects — that is explicitly deferred beyond v1.3.2.

### 2. Manager metrics: instrument the server side, not the client side

The manager process owns `POST /api/internal/run-complete` and
`POST /api/internal/pipeline-stats`. Counters incremented in these handlers are genuinely
manager-side metrics. They record what the manager actually received, which is operationally
useful (e.g., "how many run-completes have I processed today?").

**Do NOT instrument `_post_run_complete()` or `_post_stats()` in `tram/agent/server.py`.**
Those functions execute inside the worker process. Any counters placed there appear on
worker `/metrics`, not manager `/metrics`. They are the wrong instrumentation point for the
stated requirement.

There is no way for the manager to directly observe callback failures that occurred inside a
worker process. The inferred count `tram_mgr_dispatch_total{result="accepted"} -
tram_mgr_run_complete_received_total` is the closest approximation for batch dispatches. The
design acknowledges this gap explicitly and does not claim a direct "callback failures" counter.

### 3. Terminology: "broadcast" is a placement word, not a data word

The v1.3.0 feature is called "broadcast streams" and the placement table is named
`broadcast_placements`. "Broadcast" was chosen to describe the placement act — the pipeline
config is dispatched to all healthy workers simultaneously. It has no meaning about data routing.

In practice "broadcast" reads as a data-routing term to most engineers: "every packet goes to
every recipient." Combined with `count: all`, the feature name implies data is being duplicated
across workers. It is not.

**Decision:** rename the user-facing feature to "multi-worker streams" in v1.3.2.

What changes:
- This document title and all docs going forward use "multi-worker streams"
- `GET /api/cluster/streams` response field names stay as-is (no API break)
- Log messages: "broadcast stream dispatched" → "multi-worker stream dispatched"
- `roadmap.md` and `architecture.md` updated in this release

What does **not** change:
- `broadcast_placements` DB table name — internal, no user-visible surface, migration cost
  not worth it
- `placement_group_id` field — already in the API, renaming would be a breaking change
- `workers.count: all` YAML syntax — `all` and `each` mean the same thing to a user; no gain
- Internal code symbols (`_broadcast_placements`, `redispatch_broadcast_slot`, etc.) — internal

### 4. UDP multi-worker streams: shared ingress vs. per-slot endpoints

`workers.count: all` for `syslog` and `snmp_trap` means N workers each independently run a
pipeline instance. **No packet is replicated** — each packet goes to exactly one worker.

**For `count: all` + stateless sinks (Kafka, OpenSearch, REST, etc.)** a shared UDP NodePort
Service works correctly. Kube-proxy ECMP hashes per flow (src_ip + src_port + dst_ip +
dst_port), so each upstream sender tends to consistently land on one worker, and it doesn't
matter which worker processes which trap as long as one does. This is semantically equivalent
to how HTTP push sources work today with a shared LB — the distribution is automatic and no
per-pod addressing is needed.

**Targeted Services are needed in two cases:**

1. **`workers.list` (constrained backend set):** Only specific workers are dispatched. A
   Service selecting all workers would route traffic to non-participating pods. The
   implementation uses one Service with manual Endpoints pointing to only the listed workers.
   This constrains the backend set but does not guarantee a specific sender always reaches a
   specific worker — kube-proxy ECMP still distributes flows among the listed backends. True
   per-worker sender pinning (SNMP manager X always → `worker-0`) is not achievable with a
   Service; that would require the SNMP manager to send directly to the pod IP. This
   requirement is out of scope for v1.3.2.

2. **`count: N` (partial placement):** Only N of the available workers are dispatched. A
   Service selecting all workers would route to non-participating pods. Same manual Endpoints
   approach as `workers.list`.

**Design choice for v1.3.2:** Require `kubernetes: enabled: true` for all UDP push sources in
manager mode. There is no pre-existing worker-targeting UDP Service in the chart: the static
`worker-ingress-service.yaml` is TCP-only (`:8767`), and `service.yaml` selects the manager
pod in manager mode. A "shared NodePort for `count: all` with no kubernetes block" would have
no routing path to workers without introducing new Helm infrastructure. Rather than add that
infrastructure, the design reuses the same pipeline-specific Service mechanism already built
for HTTP `workers.list`: L012 is an **error** (not a warning) that blocks UDP push sources in
manager mode when `kubernetes: enabled: true` is absent, regardless of `count` value.

`count: all` + `kubernetes: enabled: true` gets a Service with a broad label selector (all
worker pods as backends). kube-proxy ECMP provides natural distribution. This is the
recommended model for `snmp_trap → kafka` and similar stateless-sink pipelines.

`count: N` and `workers.list` get a Service with manual Endpoints targeting only the
dispatched workers — the same path used by HTTP `workers.list` today.

---

## Workstream A — Standalone Live Stats Parity

### Objectives

- `GET /api/cluster/streams` returns live stats for active stream pipelines in standalone mode.
- `GET /api/pipelines/{name}/placement` returns a synthetic single-slot view for an active
  standalone stream (acknowledged approximation: `placement_group_id: null`,
  `started_at: null`). All other cases — non-stream pipelines, stopped streams, manager mode
  with no placement — keep existing `404` behavior.
- Stale detection works identically: entries age out after `3 × interval` seconds if the loop
  stops updating.
- All changes are gated on `self._worker_pool is None` — zero effect in manager mode.

### Runtime model

```
standalone stream pipeline running
  │
  ├─ controller._stream_worker(config, stop_event)
  │     run_id = str(uuid4())
  │     stats = PipelineStats(run_id=run_id, pipeline_name=config.name, schedule_type="stream")
  │     local_run = _LocalRun(run_id, pipeline_name, "stream", started_at=now, stats=stats)
  │     _local_active_stats[run_id] = local_run           ← protected by _local_stats_lock
  │     executor.stream_run(config, stop_event, stats=stats)
  │     [on exit/finally]
  │         stats_store.remove(run_id)                    ← explicit removal, no is_final
  │         _local_active_stats.pop(run_id)
  │
  └─ controller._local_stats_loop  [daemon thread, standalone only]
        every stats_interval seconds:
          with _local_stats_lock:
              runs = list(_local_active_stats.items())
          for run_id, local_run in runs:
              payload = PipelineStatsPayload(
                  worker_id=node_id,
                  pipeline_name=local_run.pipeline_name,
                  run_id=run_id,
                  schedule_type="stream",
                  uptime_seconds=(now - local_run.started_at).total_seconds(),
                  timestamp=now,
                  is_final=False,
                  **local_run.stats.snapshot_and_reset_window(),
              )
              stats_store.update(payload)      ← in-process, no HTTP
```

**Batch runs are excluded from StatsStore entirely.** Their final counts reach `run_history`
via `manager.record_run()` as before. No `stats_store.update()` or `stats_store.remove()` call
is made for batch runs — not even with `is_final=True`. That flag has no local consumer and
would leave orphaned entries.

### New data structures on PipelineController

```python
@dataclass
class _LocalRun:
    run_id: str
    pipeline_name: str
    schedule_type: str
    started_at: datetime
    stats: PipelineStats

# on PipelineController:
_local_active_stats: dict[str, _LocalRun]     # guarded by _local_stats_lock
_local_stats_lock: threading.Lock
_local_stats_stop: threading.Event
```

`_local_active_stats` uses its own lock, separate from `_stream_threads`, because APScheduler
and the stats loop access different state.

### `run_id` threading through `_run_batch`

Currently, the APScheduler path in `_run_batch` only passes `run_id` to the executor when
called from `trigger_run()`. In the standalone local path, `run_id` is generated inside
`PipelineRunContext` if not supplied, making it unobservable to the controller. A one-line
fix: always generate `run_id = str(uuid.uuid4())` in `_run_batch` before calling the executor.
This has no observable effect on existing behavior and enables future batch stats work.

### Placement endpoint behavior in standalone

`GET /api/pipelines/{name}/placement` currently returns `404` when no placement exists. The
v1.3.2 standalone path adds one new case only — active streams — and keeps all other cases as
`404` to avoid broadening the API contract:

- **Standalone, stream pipeline, active entry in `stats_store.for_pipeline(name)`:** return a
  synthetic single-slot view with `placement_group_id: null`, `started_at: null`,
  `status: "running"`, `target_count: 1`, one slot with live stats.
- **All other cases** (pipeline not found, pipeline not stream type, stream not yet reporting,
  stream stopped, manager mode with no placement): keep existing `404` behavior unchanged.

The synthetic view is documented as an approximation — it has no slot-identity stability and
no reconciliation backing. It exists solely to make `GET /api/cluster/streams` meaningful in
standalone; the placement endpoint is a secondary convenience.

### Invariants

- Stats loop starts only when `self._worker_pool is None` (standalone).
- Loop is stopped via `_local_stats_stop.set()` before `controller.stop()` joins threads.
- `stats_store.remove(run_id)` is called in the `finally` block of `_stream_worker` — always
  executes even on exception.
- Loop is daemon=True, name=`tram-local-stats`.

### Code changes

| File | Change |
|------|--------|
| `tram/pipeline/controller.py` | Add `_LocalRun` dataclass; `_local_active_stats`, `_local_stats_lock`, `_local_stats_stop`; `_emit_local_stats_once()`; `_local_stats_loop()` thread target; start/stop in `start()`/`stop()` guarded by `self._worker_pool is None`; `_stream_worker()` creates and registers `_LocalRun`, removes on exit; `_run_batch()` always generates `run_id` |
| `tram/api/routers/pipelines.py` | `GET /{name}/placement`: add standalone stream synthetic view; all other cases keep existing `404` behavior |

No changes needed to: `executor.py`, `stats_store.py`, `_stream_views.py`, `internal.py`,
`health.py` — the existing `build_cluster_streams()` fallback already handles non-placement
entries from `stats_store.all_active()`.

### Tests

| Test file | Cases |
|-----------|-------|
| `tests/unit/test_controller_standalone_stats.py` (new) | `_stream_worker` registers `_LocalRun`; `_emit_local_stats_once()` calls `stats_store.update()` with correct payload fields; stream exit calls `stats_store.remove()`; `_local_stats_loop` exits on stop event; loop not started when `worker_pool is not None` |
| `tests/unit/test_pipelines_router.py` (extend) | Placement endpoint: standalone stream active → synthetic single-slot; standalone stream stopped → `404`; non-stream pipeline → `404` |

### Acceptance criteria

- `tram daemon` (standalone): start a webhook stream pipeline; send at least one HTTP request
  into the webhook endpoint to produce traffic; poll `GET /api/cluster/streams` within 45 s;
  verify the pipeline appears in the `streams` list with non-zero `records_in`.
- `GET /api/pipelines/{name}/placement` returns `{"status": "running", "slots": [...]}` with
  `slot_count: 1` and `stale: false` stats.
- Stop the pipeline; entry is absent from `GET /api/cluster/streams` **immediately** (the
  `finally` block in `_stream_worker()` calls `stats_store.remove(run_id)` explicitly — no
  waiting for the 3 × interval stale timeout). If the entry persists after a clean stop, that
  is a regression in explicit removal, not expected behavior.
- All existing unit tests pass unchanged.

---

## Workstream B — Manager Operational Metrics

### Objectives

- Prometheus `/metrics` on the manager exposes dispatch decisions, reconciler actions, worker
  health, and callback receipt counters.
- All series use the same no-op fallback as existing metrics when `prometheus_client` is absent.
- Note in the metrics endpoint docstring that these are process-local; worker-side execution
  metrics (`tram_records_*`, `tram_chunk_duration_seconds`, etc.) require scraping worker pods.

### New Prometheus series

| Name | Type | Labels | Description |
|------|------|--------|-------------|
| `tram_mgr_dispatch_total` | Counter | `pipeline`, `result` (`accepted`/`no_workers`) | Pipeline dispatch attempts by manager |
| `tram_mgr_redispatch_total` | Counter | `pipeline` | Reconciler-triggered re-dispatches |
| `tram_mgr_reconcile_action_total` | Counter | `pipeline`, `action` (`mark_stale`/`redispatch`/`resolve_running`) | Reconciler per-slot actions |
| `tram_mgr_placement_status` | Gauge | `pipeline`, `status` (`running`/`degraded`/`reconciling`/`error`) | 1 when placement is in that status, 0 otherwise |
| `tram_mgr_worker_healthy` | Gauge | — | Currently healthy worker count |
| `tram_mgr_worker_total` | Gauge | — | Total configured worker count |
| `tram_mgr_run_complete_received_total` | Counter | `pipeline`, `status` | Run-complete callbacks received at manager |
| `tram_mgr_pipeline_stats_received_total` | Counter | — | Pipeline-stats callbacks received at manager |

`tram_mgr_placement_status` is a per-label gauge (1 = active, 0 = inactive). When a placement
transitions from `degraded` to `running`, the `degraded` label is set to 0 and `running` to 1.

**Scope narrowing from roadmap:** The v1.3.2 roadmap item says "callback failures." This plan
narrows that to **callback receipt counters plus inferred loss**. There is no direct way for
the manager to count failures that occurred inside a worker process. What this release
delivers: `tram_mgr_run_complete_received_total` counts what the manager actually received.
Operators infer failures as `tram_mgr_dispatch_total{result="accepted"} -
tram_mgr_run_complete_received_total` for completed batch runs. This gap is documented in
the metrics description; it is a known boundary, not an oversight. The roadmap wording has
been updated to match.

### Instrumentation points

| Call site | Series |
|-----------|--------|
| `controller._run_batch()` after `worker_pool.dispatch()` — accepted | `tram_mgr_dispatch_total{result="accepted"}` |
| `controller._run_batch()` when dispatch returns `None` | `tram_mgr_dispatch_total{result="no_workers"}` |
| `controller._start_stream()` single-dispatch path — accepted | `tram_mgr_dispatch_total{result="accepted"}` |
| `controller._start_stream()` multi-dispatch path — once per accepted slot | `tram_mgr_dispatch_total{result="accepted"}` (N increments for N slots; counts individual worker dispatches, not logical placement decisions) |
| `controller._update_broadcast_placement_status()` | `tram_mgr_placement_status` (set 1 for new status, 0 for all others for that pipeline) |
| `reconciler.run_once()` — `slot["status"]` set to `"stale"` | `tram_mgr_reconcile_action_total{action="mark_stale"}` |
| `reconciler.run_once()` — `redispatch_broadcast_slot()` returns `True` | `tram_mgr_redispatch_total`, `tram_mgr_reconcile_action_total{action="redispatch"}` |
| `reconciler.run_once()` — slot transitions to `"running"` | `tram_mgr_reconcile_action_total{action="resolve_running"}` |
| `WorkerPool._poll_all()` (at the end of each full poll cycle) | `tram_mgr_worker_healthy`, `tram_mgr_worker_total` |
| `internal.run_complete()` handler | `tram_mgr_run_complete_received_total{pipeline, status}` |
| `internal.pipeline_stats()` handler | `tram_mgr_pipeline_stats_received_total` |

All instrumentation points are on the manager process. No worker-side code is touched.

### Code changes

| File | Change |
|------|--------|
| `tram/metrics/registry.py` | Add 8 new `tram_mgr_*` series with `_NoOp*` fallbacks |
| `tram/pipeline/controller.py` | Increment `tram_mgr_dispatch_total`, `tram_mgr_placement_status` |
| `tram/agent/reconciler.py` | Increment `tram_mgr_redispatch_total`, `tram_mgr_reconcile_action_total` |
| `tram/agent/worker_pool.py` | Set `tram_mgr_worker_healthy`, `tram_mgr_worker_total` after health poll |
| `tram/api/routers/internal.py` | Increment `tram_mgr_run_complete_received_total`, `tram_mgr_pipeline_stats_received_total` |
| `tram/api/routers/metrics_router.py` | Add docstring noting process-locality |

### Tests

| Test file | Cases |
|-----------|-------|
| `tests/unit/test_metrics_registry.py` (extend) | All new series importable; no-op when `prometheus_client` absent |
| `tests/unit/test_reconciler.py` (extend) | Stale detection increments `mark_stale`; re-dispatch increments `redispatch` and `tram_mgr_redispatch_total` |
| `tests/unit/test_controller_metrics.py` (new) | Batch dispatch accepted → `result="accepted"`; no-worker path → `result="no_workers"`; placement status gauge transitions correctly |

### Acceptance criteria

- `tram daemon` (manager mode, kind cluster): dispatch a batch pipeline; `curl /metrics` shows
  `tram_mgr_dispatch_total{result="accepted"}` and `tram_mgr_worker_healthy` / `tram_mgr_worker_total`.
  Labeled series that require specific events (`tram_mgr_redispatch_total`,
  `tram_mgr_run_complete_received_total`, etc.) will not appear until those events are
  exercised — verify each by triggering the relevant path explicitly:
  - Run a batch pipeline to completion → `tram_mgr_run_complete_received_total` appears.
  - Start a stream pipeline → `tram_mgr_pipeline_stats_received_total` appears after first stats interval.
  - Kill a worker and wait one reconcile cycle → `tram_mgr_redispatch_total` and
    `tram_mgr_placement_status{status="degraded"}` appear.
- With `prometheus_client` uninstalled: daemon starts without error; no `tram_mgr_*` series
  in response body.

---

## Workstream C — UDP Multi-Worker Streams

### Objectives

- Lift the L008 linter block: `syslog` and `snmp_trap` sources work with `workers.count: all`,
  `count: N`, and `workers.list` in manager mode when `kubernetes: enabled: true` is set.
- Replace L008 with L012 (error): UDP push sources in manager mode **require**
  `kubernetes: enabled: true`. Without it there is no worker-targeting UDP ingress path in the
  chart. This applies to all placement types (`count: all`, `count: N`, `workers.list`).
- Fix `count: N` Service over-selection — a pre-existing gap affecting both HTTP and UDP push
  sources, and also fix the matching L006 contract: currently L006 blocks `count: N` even with
  a kubernetes block, but the over-selection bug is being fixed here, so L006 should allow
  `count: N` + `kubernetes: enabled: true`.
- Extend `is_eligible` to allow UDP push sources to use a kubernetes Service.
- End-to-end UDP path validated in kind before merge.

### Semantics of `workers.count: all` for UDP sources

`workers.count: all` means: instantiate the pipeline on every healthy worker. Each worker runs
its own independent pipeline instance. **No packet is replicated.**

All UDP push sources in manager mode require `kubernetes: enabled: true`. The chart provides
no pre-existing worker-targeting UDP ingress; the per-pipeline Service created by
`KubernetesServiceManager` is the only ingress path. L012 enforces this as an error.

**`count: all` + `kubernetes: enabled: true`:** The Service uses a broad selector targeting all
worker pods. Kube-proxy ECMP hashes per flow (src_ip + src_port → one backend), so each
sender consistently lands on one worker. For `snmp_trap → kafka` this is the correct and
recommended model.

**`count: N` or `workers.list` + `kubernetes: enabled: true`:** Only specific workers are
dispatched. The Service uses manual Endpoints targeting only the dispatched worker pods — the
same path used today by HTTP `workers.list`.

### The `count: N` over-selection fix (applies to HTTP and UDP equally)

**Current behaviour:** `_build_selector()` always returns a broad label selector matching all
worker pods — regardless of placement type. For `workers.list` this is already corrected by
manual Endpoints (`_uses_manual_endpoints` returns True when `config.workers.worker_ids` is
set). For `count: N` the Service over-selects.

**Fix:** `ensure_service()` gains an optional `dispatched_worker_ids: list[str] | None`
parameter. The controller derives these from placement slots for `count: N` and passes them
through. `_uses_manual_endpoints()` returns True when `dispatched_worker_ids` is provided,
triggering the same manual Endpoints path used by `workers.list`.

```python
# In controller._activate_kubernetes_service:
dispatched_worker_ids = self._get_dispatched_worker_ids(config.name)
self._kubernetes_service_manager.ensure_service(config, dispatched_worker_ids=dispatched_worker_ids)

def _get_dispatched_worker_ids(self, pipeline_name: str) -> list[str] | None:
    """Return worker_ids for count:N placements only. None for count:all and workers.list."""
    placement_group_id = self._active_placement_group.get(pipeline_name)
    if placement_group_id is None:
        return None
    placement = self._broadcast_placements.get(placement_group_id)
    if placement is None:
        return None
    target_count = placement.get("target_count")
    if target_count == "all":
        return None  # shared selector is correct for count:all
    state = self.manager.get(pipeline_name)
    if state.config.workers and state.config.workers.worker_ids is not None:
        return None  # workers.list already uses config.workers.worker_ids path
    return [slot["worker_id"] for slot in placement["slots"] if slot.get("worker_id")]
```

```python
# In KubernetesServiceManager:
def _uses_manual_endpoints(
    self, config: PipelineConfig, dispatched_worker_ids: list[str] | None = None
) -> bool:
    if self._mode != "manager":
        return False
    if config.workers is not None and config.workers.worker_ids is not None:
        return True  # workers.list path
    return dispatched_worker_ids is not None  # count:N path

def _listed_worker_ids(
    self, config: PipelineConfig, dispatched_worker_ids: list[str] | None = None
) -> list[str]:
    if config.workers is not None and config.workers.worker_ids is not None:
        return list(config.workers.worker_ids)
    return list(dispatched_worker_ids or [])
```

### UDP port binding model

HTTP push sources (`webhook`, `prometheus_rw`) share a single ingress listener on port `:8767`
and are routed by path (`/webhooks/path-a`). UDP sources work differently: each `syslog` or
`snmp_trap` pipeline binds its own raw socket directly on the pod using the `source.port`
value from the pipeline YAML. There is no shared UDP multiplexer.

Consequences:
- Two UDP pipelines on the same worker pod **must use different `source.port` values** — the
  second bind will fail with `Address already in use`. This is operator responsibility; the
  linter cannot catch runtime placement conflicts.
- Helm does not need UDP container port declarations — `containerPort` in the pod spec is
  informational only and not required for Service routing.
- Privileged ports (< 1024, i.e., 162/514) require `NET_BIND_SERVICE` capability. Use
  non-privileged ports (≥ 1024) in kind/dev. Production clusters may need explicit
  security context configuration.

### Generic `kubernetes:` config block

Rather than separate HTTP and UDP models, `KubernetesServiceConfig` is extended to be fully
generic. Protocol is auto-derived from source type — never user-configured. Port fields are
optional overrides.

**Updated `KubernetesServiceConfig` model:**

```python
class KubernetesServiceConfig(BaseModel):
    enabled: bool = True
    service_type: Literal["ClusterIP", "NodePort", "LoadBalancer"] = "NodePort"
    # Service cluster-side port. Default: target_port.
    port: int | None = None
    # Pod-side target port. Default: source.port (UDP) or worker ingress port (HTTP).
    target_port: int | None = None
    # Only valid when service_type=NodePort. Omit for auto-allocation.
    node_port: int | None = None
    # Only valid when service_type=LoadBalancer.
    load_balancer_ip: str | None = None
    # Merged into Service metadata.annotations.
    annotations: dict[str, str] = Field(default_factory=dict)
    # Optional name override (DNS-1123, max 63 chars).
    service_name: str = ""
```

**Example YAML — UDP snmp_trap pipeline, count:2:**

```yaml
source:
  type: snmp_trap
  port: 1162

kubernetes:
  enabled: true
  service_type: NodePort
  port: 1162          # Service cluster-side port (same as pod port here)
  target_port: 1162   # Explicit override; default would also resolve to source.port=1162
  annotations:
    metallb.universe.tf/address-pool: trap-pool

workers:
  count: 2
```

**Example YAML — HTTP webhook pipeline, LoadBalancer:**

```yaml
source:
  type: webhook
  path: /events

kubernetes:
  enabled: true
  service_type: LoadBalancer
  port: 80            # Service cluster-side port
                      # target_port defaults to 8767 (shared worker ingress)
  load_balancer_ip: 10.0.0.50
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: nlb
```

**`_resolve_target_port()` in `KubernetesServiceManager`:**

```python
UDP_PUSH_SOURCES = {"syslog", "snmp_trap"}
UDP_PORT_DEFAULTS = {"snmp_trap": 162, "syslog": 514}

def _resolve_target_port(self, config: PipelineConfig) -> int:
    if config.kubernetes.target_port is not None:
        return config.kubernetes.target_port
    if config.source.type in UDP_PUSH_SOURCES:
        return getattr(config.source, "port", None) or UDP_PORT_DEFAULTS[config.source.type]
    return self._worker_ingress_port if self._mode == "manager" else self._standalone_port

def _resolve_protocol(self, config: PipelineConfig) -> str:
    return "UDP" if config.source.type in UDP_PUSH_SOURCES else "TCP"

def _resolve_service_port(self, config: PipelineConfig) -> int:
    if config.kubernetes.port is not None:
        return config.kubernetes.port
    return self._resolve_target_port(config)
```

**`_build_service_body()` additions:**

```python
target_port = self._resolve_target_port(config)
service_port = self._resolve_service_port(config)
protocol = self._resolve_protocol(config)

port_spec = {
    "name": "traffic",
    "port": service_port,
    "protocol": protocol,
    "targetPort": target_port,
}
if config.kubernetes.service_type == "NodePort" and config.kubernetes.node_port is not None:
    port_spec["nodePort"] = config.kubernetes.node_port

spec = {"type": config.kubernetes.service_type, "ports": [port_spec]}
if config.kubernetes.service_type == "LoadBalancer" and config.kubernetes.load_balancer_ip:
    spec["loadBalancerIP"] = config.kubernetes.load_balancer_ip

metadata = {
    "name": service_name,
    "namespace": self._namespace,
    "labels": {...},
    "annotations": dict(config.kubernetes.annotations),  # user annotations merged directly
}
```

`is_eligible()` is extended to cover `ALL_PUSH_SOURCES = HTTP_PUSH_SOURCES | UDP_PUSH_SOURCES`.

The `PipelineConfig` model validator that currently rejects non-HTTP sources with a kubernetes
block is updated to accept `ALL_PUSH_SOURCES`.

### Linter rule changes

**L006 update** (HTTP push sources, `kubernetes: enabled: true` + `count: N`):

Currently L006 blocks `count: N` for HTTP push sources even when a kubernetes block is
present. The over-selection bug being fixed in this release makes `count: N` safe when
`kubernetes: enabled: true`. L006 is updated to allow `count: N` + `kubernetes: enabled: true`:

```
# Existing (unchanged): shared ingress path
no kubernetes block + workers.count: all       → valid (shared HTTP ingress; v1.3.0 contract)
no kubernetes block + workers.list: [...]      → L006 error (shared ingress cannot pin)

# Extended in v1.3.2: per-pipeline Service path
kubernetes: enabled: true + workers.count: all → no L006
kubernetes: enabled: true + workers.count: N   → no L006 (over-selection bug now fixed)
kubernetes: enabled: true + workers.list: ...  → no L006

# Still blocked in both paths:
no kubernetes block + workers.count: N         → L006 error (shared ingress selects all workers)
```

**L008 removed** — the block is lifted now that UDP push sources have a proper Service path.

**L012 added** (error, manager mode only):

> L012: UDP push source (`syslog`, `snmp_trap`) in manager mode requires
> `kubernetes: enabled: true`. There is no pre-existing worker-targeting UDP ingress in the
> Helm chart; the per-pipeline Service is the only ingress path. This applies to all
> `workers.count` values.
> Severity: Error (blocking).

L012 fires when: manager mode + UDP push source + no `kubernetes: enabled: true` block.
L012 does **not** fire for standalone mode (no Service provisioning needed).

**Note:** L011 is already taken by the risky-filename-partition rule
(`_l011_risky_filename_partition_fields` in `linter.py:260`). The new rule is L012.

### Topology after fix

**`count: all` + UDP (`kubernetes: enabled: true` required)**
```
External sender → UDP NodePort (pipeline Service, broad selector) → kube-proxy ECMP → worker-0, 1, 2
```
One Service with broad label selector, all workers as backends. Correct for stateless sinks.

**`count: N` or `workers.list` + UDP (`kubernetes: enabled: true` required)**
```
External sender → UDP NodePort (pipeline Service) → manual Endpoints → dispatched workers only
```
Same manual Endpoints mechanism already used for HTTP `workers.list`.

### kind validation checklist

**Prerequisites:**
- Manager + 3 workers deployed in kind.
- A single-broker Kafka deployment in the same kind cluster is required for steps 1–3
  (e.g., `helm install kafka oci://registry-1.docker.io/bitnamicharts/kafka --set
  replicaCount=1 --set zookeeper.enabled=false --set kraft.enabled=true`). As an alternative,
  a `file` or `rest` sink can substitute for routing-level proof (steps 1–3) if Kafka is not
  yet available; swap back to Kafka for final merge validation.

1. Register `snmp_trap → kafka` (or `snmp_trap → file`) with `workers.count: all`, **no**
   `kubernetes` block. Confirm L012 fires (error). Add `kubernetes: enabled: true`; confirm
   L012 clears and no L008.
2. Send test SNMP traps from **at least two distinct source IPs** to the cluster NodePort
   (from the pipeline Service). ECMP hashes per flow (src_ip + src_port), so a single sender
   lands on one backend consistently — multiple senders are needed to exercise distribution.
   Confirm records from both senders appear in the sink and that worker logs show each sender
   consistently reaching one worker (and no traps routed to a pod not running the pipeline).
3. Register the same pipeline with `workers.count: 2` + `kubernetes: enabled: true`. Confirm
   no L012. Confirm Service Endpoints target only the 2 dispatched workers, not all 3.
4. HTTP regression: register a webhook pipeline with `count: 2` + `kubernetes: enabled: true`;
   confirm L006 does not fire and Service Endpoints target only the 2 dispatched workers.

### Code changes

| File | Change |
|------|--------|
| `tram/models/pipeline.py` | `KubernetesServiceConfig`: add `port`, `target_port`, `load_balancer_ip`, `annotations` fields; add `ClusterIP` to `service_type` literal; update model validator to accept `ALL_PUSH_SOURCES` |
| `tram/pipeline/linter.py` | Remove L008; add L012 (error); update L006 to allow `count: N` + `kubernetes: enabled: true` |
| `tram/pipeline/k8s_service_manager.py` | Add `_resolve_target_port()`, `_resolve_protocol()`, `_resolve_service_port()`; update `_build_service_body()` to use resolved values + annotations + `loadBalancerIP`; `ensure_service(config, dispatched_worker_ids=None)`; extend `_uses_manual_endpoints` and `_listed_worker_ids` for `dispatched_worker_ids`; extend `is_eligible` to `ALL_PUSH_SOURCES`; add `UDP_PUSH_SOURCES`, `UDP_PORT_DEFAULTS` |
| `tram/pipeline/controller.py` | `_activate_kubernetes_service` calls `_get_dispatched_worker_ids` and passes to `ensure_service`; add `_get_dispatched_worker_ids()` |

No changes to: `_stream_views.py`, `db.py`, `slots_json` schema — no new slot fields needed.

### Tests

| Test file | Cases |
|-----------|-------|
| `tests/unit/test_linter.py` (extend) | L008 gone; L012 fires on UDP + manager + any `count` without `kubernetes: enabled: true`; clears when kubernetes block present; L006 no longer fires for HTTP push + `count: N` + `kubernetes: enabled: true` |
| `tests/unit/test_k8s_service_manager.py` (extend) | `ensure_service(dispatched_worker_ids=[...])` uses manual Endpoints for those ids; UDP source produces `protocol: UDP` Service with correct `targetPort`; HTTP source keeps `protocol: TCP` and `targetPort=8767`; `annotations` merged into Service metadata; `loadBalancerIP` set when `service_type=LoadBalancer`; explicit `port`/`target_port` override defaults; `is_eligible` true for UDP + HTTP sources with kubernetes block |
| `tests/unit/test_pipeline_model.py` (extend) | `KubernetesServiceConfig`: `annotations` dict accepted; `load_balancer_ip` only valid with LoadBalancer; `ClusterIP` accepted as service_type; `target_port` and `port` optional; model validator accepts UDP push sources |
| `tests/unit/test_controller_udp.py` (new) | `count: N` dispatch passes `dispatched_worker_ids` to service manager; `count: all` passes `None`; `workers.list` passes `None` (handled by config path) |
| kind validation | See checklist above (manual, gates merge of PR-C2) |

---

## Recommended PR split

| PR | Branch | Content | Merge order |
|----|--------|---------|-------------|
| PR-A | `observer-a-standalone-stats` | Workstream A: controller stats loop, `_LocalRun` dataclass, placement endpoint synthetic view | 1st |
| PR-B | `observer-b-mgr-metrics` | Workstream B: registry series + instrumentation at controller/reconciler/worker_pool/internal router | 2nd (parallel with A) |
| PR-C1 | `observer-c1-udp-model` | Workstream C: L008 removal, L012, `count: N` over-selection fix in service manager, UDP eligibility, unit tests | 3rd |
| PR-C2 | `observer-c2-udp-e2e` | Workstream C: controller `_get_dispatched_worker_ids` wiring, kind validation evidence in PR body | 4th (gated on C1) |

PR-A and PR-B share no code paths and can be reviewed in parallel. PR-C1 is a prerequisite
for C2 because the kind validation requires the full stack wired together.

---

## Open questions

1. **`tram_mgr_placement_status` cardinality:** 4 label combinations (running/degraded/
   reconciling/error) × number of active multi-worker pipelines. For deployments with >50
   pipelines this could be high. Alternative: single gauge with value 0–3 encoded as an enum.
   Confirm expected pipeline count before deciding; 50+ is unusual for v1.3.2 target deployments.

2. **Batch stats mid-run visibility (deferred):** If mid-run batch visibility is needed in
   standalone, the controller would need to snapshot periodically inside the thread pool. This
   adds complexity and is explicitly deferred. Re-evaluate in v1.3.3 after UI revalidation
   clarifies what stats the dashboard actually needs during a batch run.
