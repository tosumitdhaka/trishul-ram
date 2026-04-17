# v1.3.0 Design: Broadcast Streams & Push-Source Scaling

**Status:** Design / Pre-implementation  
**Target version:** v1.3.0  
**Author:** Sumit Dhaka  
**Scope:** Manager+worker cluster mode only (standalone unaffected)

---

## Problem Statement

TRAM v1.2.x has an architectural gap for push-source pipelines (`webhook`,
`prometheus_rw`, `syslog`, `snmp_trap`) in manager+worker mode:

1. The manager dispatches a stream pipeline to **one** worker. The
   `WebhookSource` registers its in-process queue on that worker. External
   systems have no stable, load-balanced entry point to reach it.
2. Running the pipeline on a single worker is a throughput bottleneck —
   a single Python process cannot sustain 1k–10k TPS for `prometheus_rw`.
3. Stream pipelines never call `_post_run_complete`, so the manager has no
   live metrics (records/s, errors, DLQ count) for running streams.
4. The manager has no API that tells operators which worker owns a pipeline
   or what its ingress endpoint is.
5. The manager Deployment + RWO PVC combination causes downtime on every
   upgrade (`strategy: Recreate`).
6. Adding a NodePort for workers currently requires manual Helm changes;
   the manager cannot provision K8s Services itself.

---

## Solution Overview

Four discrete work streams, delivered together as v1.3.0:

| # | Theme | Key artefacts |
|---|-------|---------------|
| A | Broadcast dispatch | `PipelineConfig.dispatch`, `WorkerPool.broadcast()`, webhook router on worker |
| B | Stream heartbeat | `StreamMetrics`, worker heartbeat thread, `/api/internal/heartbeat`, manager heartbeat store |
| C | Placement API | `GET /api/pipelines/{name}/placement`, `GET /api/cluster/streams` |
| D | Manager → StatefulSet | `manager-statefulset.yaml`, remove `manager-deployment.yaml` |
| E | Dynamic K8s Services | `tram/k8s/service_manager.py`, RBAC, `kubernetes:` pipeline block |

---

## A — Broadcast Dispatch

### Concept

A broadcast pipeline is dispatched to **all** healthy workers simultaneously.
Each worker runs an independent instance. A single NodePort Service in front
of the worker StatefulSet load-balances inbound traffic across them — no
proxying through the manager.

```
Prometheus (10k TPS)
        │  remote_write
        ▼
NodePort Service  ←── stable external endpoint (provisioned by E)
        │  K8s round-robin or IPVS
   ┌────┴─────┐
   │          │
worker-0    worker-1    worker-2
PrometheusRWSource  (same pipeline, independent instance)
   │          │          │
 sink       sink       sink    (each writes its own slice)
```

This is the same model as a Kafka consumer group: same logic, partitioned
load, no coordination between workers during execution.

### Constraints

Broadcast is **only valid for push-source pipeline types**:

| Source type | Broadcast allowed | Reason |
|-------------|-------------------|--------|
| `webhook` | yes | HTTP LB distributes requests |
| `prometheus_rw` | yes | HTTP LB distributes requests |
| `syslog` | yes | UDP/TCP LB distributes datagrams |
| `snmp_trap` | yes | UDP LB distributes traps |
| `kafka` | no | Kafka consumer group handles partitioning natively |
| `nats` | no | Queue groups handle fan-out natively |
| `amqp` | no | AMQP competing consumers handle load natively |
| `mqtt` | no | MQTT broker handles delivery |
| `sftp`, `s3`, `local`, `rest`, `sql`, `influxdb`, `redis` | no | Pull sources would duplicate reads |
| `websocket`, `gnmi`, `corba` | no | Single upstream connection |

The loader/linter validates this at pipeline registration time and rejects
`dispatch: broadcast` on non-push-source types (new lint rule **L006**).

### Pipeline YAML

```yaml
name: prom-ingest
dispatch: broadcast        # new field; default: "single"
schedule:
  type: stream
source:
  type: prometheus_rw
  path: prom-rw
  secret: ${PROM_SECRET}
```

### Data model changes

**`tram/models/pipeline.py`**

```python
class PipelineConfig(BaseModel):
    ...
    dispatch: Literal["single", "broadcast"] = "single"
```

**`tram/pipeline/linter.py`** — new rule L006:

```
L006  broadcast dispatch is only valid for push-source types
      (webhook, prometheus_rw, syslog, snmp_trap)
```

### WorkerPool changes

**`tram/agent/worker_pool.py`**

```python
# New: {pipeline_name: [worker_url, ...]}  (replaces single-value dict for broadcast)
self._pipeline_workers: dict[str, list[str]] = {}

def broadcast(
    self,
    run_id_prefix: str,
    pipeline_name: str,
    yaml_text: str,
    schedule_type: str,
    callback_url: str = "",
) -> list[str]:
    """Dispatch the same pipeline to all healthy workers.

    Returns list of worker URLs that accepted the run.
    Each worker gets a unique run_id: {run_id_prefix}-w{index}.
    """
    ...

def workers_for_pipeline(self, pipeline_name: str) -> list[str]:
    """Return all worker URLs currently running pipeline_name."""
    ...
```

`_pipeline_workers` replaces `_pipeline_worker` (singular). The placement
API (C) reads from this dict.

### Worker: mount webhook router

**`tram/agent/server.py`** — in `create_worker_app()`, after app creation:

```python
from tram.api.routers.webhooks import router as _webhooks_router
app.include_router(_webhooks_router)
```

This is a 2-line change. The worker's FastAPI app already runs on `:8766`;
after this change it handles `POST /webhooks/{path}` on the same port.
`_WEBHOOK_REGISTRY` is process-local, so each worker independently resolves
its registered paths — exactly what broadcast needs.

### Scheduling integration

**`tram/scheduler/scheduler.py`** (or wherever manager dispatches stream jobs)
— when scheduling a stream pipeline:

```python
if config.dispatch == "broadcast" and worker_pool is not None:
    worker_pool.broadcast(run_id, config.name, yaml_text, "stream", callback_url)
else:
    worker_pool.dispatch(run_id, config.name, yaml_text, "stream", callback_url)
```

---

## B — Stream Heartbeat

### Why per-pipeline, not per-worker

A worker can run multiple broadcast pipelines simultaneously. The manager's
placement API (C) needs per-pipeline metrics (TPS, error rate, DLQ count)
on each worker — not an aggregate blob. One heartbeat per active stream
pipeline per worker per interval gives the manager exactly that granularity.

At 3 workers × 4 pipelines × 1 heartbeat/30s = 0.4 RPS — negligible load.

### StreamMetrics dataclass

**`tram/agent/server.py`** (or new `tram/agent/metrics.py`):

```python
@dataclass
class StreamMetrics:
    records_in: int = 0
    records_out: int = 0
    records_skipped: int = 0
    dlq_count: int = 0
    error_count: int = 0
    # Rolling window: errors in the last heartbeat interval only
    errors_last_window: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False)

    def increment(self, *, records_in=0, records_out=0, skipped=0,
                  dlq=0, errors: list[str] | None = None) -> None:
        with self._lock:
            self.records_in += records_in
            self.records_out += records_out
            self.records_skipped += skipped
            self.dlq_count += dlq
            if errors:
                self.error_count += len(errors)
                self.errors_last_window.extend(errors[-10:])  # cap window

    def snapshot_and_reset_window(self) -> dict:
        """Return a snapshot; clear errors_last_window for next interval."""
        with self._lock:
            snap = {
                "records_in": self.records_in,
                "records_out": self.records_out,
                "records_skipped": self.records_skipped,
                "dlq_count": self.dlq_count,
                "error_count": self.error_count,
                "errors_last_window": list(self.errors_last_window),
            }
            self.errors_last_window.clear()
        return snap
```

`ActiveRun` gets a `metrics: StreamMetrics` field for stream-type runs.

### Executor integration

**`tram/pipeline/executor.py`** — `stream_run()` accepts an optional
`metrics: StreamMetrics | None = None` parameter. After each record batch
it calls `metrics.increment(...)`. No other changes to the executor.

### Worker heartbeat thread

**`tram/agent/server.py`** — spawned in `create_worker_app()` lifespan:

```python
def _heartbeat_loop(state: WorkerState, manager_url: str, interval: int) -> None:
    """Send one heartbeat per active stream run every `interval` seconds."""
    while not state.heartbeat_stop.is_set():
        state.heartbeat_stop.wait(interval)
        for run in state.snapshot():
            if run.schedule_type != "stream" or run.metrics is None:
                continue
            snap = run.metrics.snapshot_and_reset_window()
            payload = {
                "worker_id": state.worker_id,
                "pipeline_name": run.pipeline_name,
                "run_id": run.run_id,
                "uptime_seconds": (datetime.now(UTC) - run.started_at_dt).total_seconds(),
                "timestamp": datetime.now(UTC).isoformat(),
                **snap,
            }
            _post_heartbeat(manager_url, payload)
```

`_post_heartbeat` is a fire-and-forget httpx POST to
`{manager_url}/api/internal/heartbeat`. Errors are logged and swallowed —
a missed heartbeat is stale data, not a fatal failure.

**Configuration:**

```python
# AppConfig (tram/core/config.py)
heartbeat_interval: int   # TRAM_HEARTBEAT_INTERVAL, default 30
```

### Manager heartbeat receiver

**`tram/api/routers/internal.py`** — new endpoint:

```
POST /api/internal/heartbeat
```

Payload schema (Pydantic):
```python
class WorkerHeartbeat(BaseModel):
    worker_id: str
    pipeline_name: str
    run_id: str
    uptime_seconds: float
    timestamp: str
    records_in: int
    records_out: int
    records_skipped: int
    dlq_count: int
    error_count: int
    errors_last_window: list[str] = []
```

Manager stores heartbeats in:

```python
# tram/agent/worker_pool.py (or new tram/agent/heartbeat_store.py)
# {(pipeline_name, worker_id): WorkerHeartbeat}
self._heartbeats: dict[tuple[str, str], WorkerHeartbeat] = {}
```

Keyed by `(pipeline_name, worker_id)` so each placement has exactly one
current entry. Stale detection: entries older than `3 × heartbeat_interval`
are considered dead. The manager does **not** write heartbeats to the DB —
they are live-only telemetry. Historical throughput is already captured in
`run_history.records_in` for batch runs; for streams an operator uses
Prometheus metrics (`tram_records_*` gauges) for history.

---

## C — Placement API

### New endpoints

#### `GET /api/pipelines/{name}/placement`

Returns where a pipeline is currently running across all workers, with live
metrics from the heartbeat store and (if provisioned) the external endpoint.

**Response:**
```json
{
  "name": "prom-ingest",
  "dispatch": "broadcast",
  "schedule_type": "stream",
  "placements": [
    {
      "worker_id": "tram-worker-0",
      "worker_url": "http://tram-worker-0.tram-worker.default.svc:8766",
      "run_id": "abc123-w0",
      "status": "running",
      "started_at": "2026-04-15T06:00:00Z",
      "uptime_seconds": 14400,
      "metrics": {
        "records_in": 1800000,
        "records_out": 1799200,
        "records_skipped": 42,
        "dlq_count": 11,
        "error_count": 53,
        "records_in_per_sec": 125.0,
        "errors_last_window": []
      },
      "push_endpoint": {
        "internal": "http://tram-worker-0.tram-worker.default.svc:8766/webhooks/prom-rw",
        "external": "http://<node-ip>:32100/webhooks/prom-rw"
      }
    },
    {
      "worker_id": "tram-worker-1",
      ...
    }
  ],
  "k8s_service": "tram-prom-ingest-ingress",   // null if not provisioned
  "node_port": 32100                            // null if not provisioned
}
```

`records_in_per_sec` is derived: `records_in / uptime_seconds` (rolling
average since stream start, not instantaneous). The heartbeat's
`errors_last_window` provides the instantaneous error list.

`push_endpoint.external` is populated only if the K8s Service for this
pipeline exists (from work stream E). Otherwise `null`.

#### `GET /api/cluster/streams`

Flat list of all active stream placements across all pipelines. Used by the
manager UI "Streams" view.

```json
{
  "streams": [
    {
      "pipeline_name": "prom-ingest",
      "worker_id": "tram-worker-0",
      "run_id": "abc123-w0",
      "uptime_seconds": 14400,
      "records_in_per_sec": 125.0,
      "dlq_count": 11,
      "last_heartbeat": "2026-04-15T10:00:00Z"
    },
    ...
  ]
}
```

### Data assembly

The placement router reads from three sources:

1. `WorkerPool._pipeline_workers` — which workers are running a pipeline (set
   during dispatch/broadcast, cleared on run-complete or worker failure)
2. `WorkerPool._assignments` — run_id per worker (already exists)
3. `HeartbeatStore._heartbeats` — live metrics per (pipeline, worker)
4. `K8sServiceManager.get_service_info(pipeline_name)` — NodePort/LB details
   (returns `None` gracefully if K8s client not available)

---

## D — Manager StatefulSet

### Why

The current `manager-deployment.yaml` uses `strategy: Recreate` because the
manager needs a RWO PVC (SQLite DB + schemas + MIBs). Recreate means:

1. Old pod is terminated
2. PVC is released
3. New pod is scheduled
4. PVC is mounted
5. Service resumes

This is **guaranteed downtime** on every `helm upgrade`. StatefulSet solves
this by owning the PVC identity — the pod and its storage are a unit, and
rolling updates work correctly because K8s guarantees only one pod instance
for a given ordinal at any time.

### Changes

**Remove:** `helm/templates/manager-deployment.yaml`

**Add:** `helm/templates/manager-statefulset.yaml`

Key differences from the Deployment:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ include "tram.fullname" . }}-manager
spec:
  replicas: 1
  serviceName: {{ include "tram.fullname" . }}-manager   # headless DNS
  podManagementPolicy: OrderedReady
  updateStrategy:
    type: RollingUpdate          # replaces strategy.type: Recreate
  selector: ...
  template: ...                  # same pod spec as Deployment (no changes)
  volumeClaimTemplates:
    - metadata:
        name: manager-data       # replaces the separate PVC template
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: {{ .Values.manager.persistence.size | default "10Gi" }}
        {{- with .Values.manager.persistence.storageClass }}
        storageClassName: {{ . }}
        {{- end }}
```

**Add:** `helm/templates/manager-headless-service.yaml`

```yaml
{{- if .Values.manager.enabled }}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "tram.fullname" . }}-manager
spec:
  clusterIP: None
  selector:
    app.kubernetes.io/component: manager
  ports:
    - name: http
      port: {{ .Values.service.port }}
      targetPort: http
{{- end }}
```

**Existing ClusterIP service** (`service.yaml`) keeps selecting
`app.kubernetes.io/component: manager` — no change. Workers continue using
`http://tram:8765` (ClusterIP) for `TRAM_MANAGER_URL`. Pod identity becomes
`tram-manager-0` but that is only needed if direct pod-to-pod addressing is
required (not currently the case).

**Remove:** `helm/templates/pvc.yaml` manager-data section (PVC is now owned
by `volumeClaimTemplates`, not a separate template).

**Migration note (values.yaml):**
`manager.persistence.existingClaim` will be respected if provided — allows
upgrading from v1.2.x by pointing at the old PVC. Add a Helm note in
`NOTES.txt` explaining the one-time migration step.

---

## E — Dynamic K8s Services

### Concept

When a broadcast pipeline with a `kubernetes:` block is registered, the
manager uses the K8s API to create a NodePort (or LoadBalancer) Service
targeting the worker StatefulSet on the correct port. The manager owns the
lifecycle — it creates the Service on pipeline start and deletes it on
pipeline stop/delete.

This means operators never need to touch Helm or infra to expose a new
push-source pipeline. The manager is self-provisioning for ingress.

### Pipeline YAML

```yaml
name: prom-ingest
dispatch: broadcast
schedule:
  type: stream
source:
  type: prometheus_rw
  path: prom-rw
kubernetes:
  service_type: NodePort        # NodePort (default) | LoadBalancer | ClusterIP
  node_port: 32100              # optional; K8s assigns one if omitted
  target_port: 8766             # worker port; default: TRAM_WORKER_PORT
  annotations: {}               # passed through to the Service metadata.annotations
```

`kubernetes:` block is optional. If absent, no Service is created and the
pipeline runs in broadcast mode with workers reachable only via cluster-internal
headless DNS (sufficient for in-cluster Prometheus).

### Data model

**`tram/models/pipeline.py`**

```python
class KubernetesServiceConfig(BaseModel):
    service_type: Literal["NodePort", "LoadBalancer", "ClusterIP"] = "NodePort"
    node_port: int | None = None          # 30000–32767 or None (K8s picks)
    target_port: int | None = None        # defaults to TRAM_WORKER_PORT
    annotations: dict[str, str] = Field(default_factory=dict)


class PipelineConfig(BaseModel):
    ...
    dispatch: Literal["single", "broadcast"] = "single"
    kubernetes: KubernetesServiceConfig | None = None
```

### K8sServiceManager

**New file: `tram/k8s/service_manager.py`**

```python
class K8sServiceManager:
    """Manages K8s Services for broadcast pipeline ingress.

    Gracefully no-ops if:
    - 'kubernetes' extra is not installed
    - Not running inside a K8s cluster (no in-cluster config)
    - RBAC does not allow Service mutations (logs warning, continues)
    """

    def __init__(self, namespace: str, release_name: str) -> None: ...

    def ensure_service(
        self,
        pipeline_name: str,
        cfg: KubernetesServiceConfig,
        worker_selector: dict[str, str],
    ) -> dict | None:
        """Create or patch a NodePort/LB Service. Returns service info dict."""
        ...

    def delete_service(self, pipeline_name: str) -> None:
        """Delete the managed Service for this pipeline, if it exists."""
        ...

    def get_service_info(self, pipeline_name: str) -> dict | None:
        """Return {name, node_port, cluster_ip, external_ip} or None."""
        ...

    @staticmethod
    def service_name(pipeline_name: str) -> str:
        """Deterministic Service name: tram-{pipeline_name}-ingress."""
        return f"tram-{pipeline_name}-ingress"
```

Service metadata labels:
```yaml
labels:
  tram-managed: "true"
  tram-pipeline: "{pipeline_name}"
  app.kubernetes.io/managed-by: "tram"
```

This allows `kubectl get svc -l tram-managed=true` for visibility and
bulk cleanup.

### RBAC

**New: `helm/templates/rbac.yaml`** (only when `manager.enabled` and
`manager.rbac.create: true`):

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ include "tram.fullname" . }}-manager
rules:
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list", "create", "delete", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ include "tram.fullname" . }}-manager
subjects:
  - kind: ServiceAccount
    name: {{ include "tram.serviceAccountName" . }}
roleRef:
  kind: Role
  apiGroup: rbac.authorization.k8s.io
  name: {{ include "tram.fullname" . }}-manager
```

`values.yaml` addition:
```yaml
manager:
  rbac:
    create: true    # set false to manage RBAC externally
```

### Dependency

`kubernetes` Python client is an optional extra:

```toml
# pyproject.toml
[project.optional-dependencies]
k8s = ["kubernetes>=28.0,<32"]
```

`K8sServiceManager.__init__` wraps the import in a try/except and sets
`self._available = False` if the library is missing. All public methods
check `self._available` and return `None`/no-op. No crash, no required
upgrade for users who don't need dynamic Services.

### Lifecycle integration

Manager's pipeline lifecycle hooks call `K8sServiceManager`:

| Event | Action |
|-------|--------|
| Broadcast pipeline started | `ensure_service(name, cfg, worker_selector)` |
| Pipeline stopped (manual) | `delete_service(name)` |
| Pipeline deleted | `delete_service(name)` |
| Manager shutdown | No action — Services are persistent K8s objects and survive pod restart. Manager re-adopts them by label on startup via `list_services(label_selector="tram-managed=true")`. |

---

## Implementation Checklist

### A — Broadcast Dispatch

- [ ] `PipelineConfig.dispatch: Literal["single", "broadcast"] = "single"`
- [ ] `KubernetesServiceConfig` model (can be done here even if E is later)
- [ ] Linter rule L006 — reject broadcast on non-push-source types
- [ ] `WorkerPool._pipeline_workers` (list, replaces `_pipeline_worker`)
- [ ] `WorkerPool.broadcast()` — dispatch to all healthy workers
- [ ] `WorkerPool.workers_for_pipeline()` — list workers running a pipeline
- [ ] `create_worker_app()` — include `webhooks_router`
- [ ] Scheduler integration — call `broadcast()` vs `dispatch()` based on `config.dispatch`
- [ ] Unit tests: broadcast dispatch, L006 lint rule, webhook router on worker

### B — Stream Heartbeat

- [ ] `StreamMetrics` dataclass with thread-safe `increment()` and `snapshot_and_reset_window()`
- [ ] `ActiveRun.metrics: StreamMetrics | None` field
- [ ] `AppConfig.heartbeat_interval` — `TRAM_HEARTBEAT_INTERVAL`, default 30
- [ ] `executor.py` `stream_run()` — accept `metrics` param, call `metrics.increment()` per iteration
- [ ] Worker heartbeat thread — spawned in lifespan, posts per-stream-run per interval
- [ ] `POST /api/internal/heartbeat` endpoint on manager
- [ ] `HeartbeatStore` — `{(pipeline_name, worker_id): WorkerHeartbeat}` with staleness check
- [ ] `WorkerPool` or `TramServer` holds a `HeartbeatStore` instance
- [ ] Unit tests: metrics increment, heartbeat thread fires, endpoint stores data, staleness

### C — Placement API

- [ ] `GET /api/pipelines/{name}/placement` endpoint
- [ ] `GET /api/cluster/streams` endpoint
- [ ] `records_in_per_sec` derived field in response
- [ ] `push_endpoint.internal` assembled from headless DNS pattern
- [ ] `push_endpoint.external` populated from `K8sServiceManager.get_service_info()` if available
- [ ] Unit tests: placement response shape, streams list, no-worker case

### D — Manager StatefulSet

- [ ] `helm/templates/manager-statefulset.yaml` — mirrors Deployment pod spec with `volumeClaimTemplates`
- [ ] `helm/templates/manager-headless-service.yaml` — headless DNS for `tram-manager-0`
- [ ] Remove `helm/templates/manager-deployment.yaml`
- [ ] `helm/templates/pvc.yaml` — remove manager-data section (now in volumeClaimTemplates)
- [ ] `helm/values.yaml` — add `manager.persistence.existingClaim` for upgrade migration
- [ ] `helm/NOTES.txt` — upgrade migration note for existing PVC
- [ ] `docs/deployment.md` — update manager architecture section

### E — Dynamic K8s Services

- [ ] `tram/k8s/__init__.py` + `tram/k8s/service_manager.py`
- [ ] `pyproject.toml` — `k8s = ["kubernetes>=28.0,<32"]` extra
- [ ] `AppConfig.k8s_namespace` — `TRAM_NAMESPACE`, default `default`
- [ ] `KubernetesServiceConfig` Pydantic model in `pipeline.py`
- [ ] Manager lifecycle hooks: call `ensure_service` on broadcast start, `delete_service` on stop/delete
- [ ] Manager startup: re-adopt existing `tram-managed=true` Services by scanning K8s
- [ ] `helm/templates/rbac.yaml` — Role + RoleBinding for Service CRUD
- [ ] `helm/values.yaml` — `manager.rbac.create: true`
- [ ] Unit tests: `K8sServiceManager` with mocked k8s client, no-op when library absent
- [ ] `docs/deployment.md` — RBAC and dynamic service documentation

---

## API Summary (new endpoints)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/pipelines/{name}/placement` | Placement detail for one pipeline |
| `GET` | `/api/cluster/streams` | All active stream placements |
| `POST` | `/api/internal/heartbeat` | Worker → Manager stream metrics heartbeat |

---

## Environment Variables (new)

| Variable | Default | Description |
|----------|---------|-------------|
| `TRAM_HEARTBEAT_INTERVAL` | `30` | Seconds between worker stream heartbeats |
| `TRAM_NAMESPACE` | `default` | K8s namespace for dynamic Service creation |

---

## Dependency Summary

| Extra | Package | Required for |
|-------|---------|-------------|
| `k8s` | `kubernetes>=28.0,<32` | Dynamic K8s Service provisioning (E) |

All other changes use only packages already present in the base install.

---

## Out of Scope for v1.3.0

- Manager HA / standby (tracked in backlog)
- Graceful worker drain (tracked in backlog)
- UI pages for placement / streams view (follow-on after API is stable)
- `LoadBalancer` service type end-to-end testing (cloud-specific; NodePort is the tested default)
- Broadcast for `syslog` and `snmp_trap` (UDP sources need a different LB approach —
  K8s Services support UDP but UDP LB behaviour varies by CNI; deferred)
