# TRAM v1.2.0 Roadmap

> **Status:** Planning  
> **Base:** v1.1.4 (PipelineController, melt transform, cluster sync, SNMP fixes)  
> **Theme:** Manager + Worker cluster architecture + feature backlog from earlier planning

---

## Primary Feature: Manager + Worker Architecture

The fundamental distributed-state problem in the N-equal-peer cluster model makes every
new cluster feature harder than it should be. v1.2.0 resolves this at the architecture level
while remaining 100% backward compatible for users.

### The v1.1.x problem

Every TRAM pod runs the same code: serves UI, hosts REST API, manages scheduling, executes
pipelines, and maintains in-memory state. This creates an inherent tension:

- **N independent in-memory state stores** — each pod has its own `PipelineState.status`.
  Load balancer routes requests to different pods. Status flickering results.
- **Distributed consensus without a coordinator** — pods agree on ownership via consistent
  hashing + DB columns. Every topology change triggers rebalance loops, cooling periods,
  voluntary drain, and stale-run recovery. Each with its own edge cases.
- **The fix stack in v1.1.4** — `PipelineController`, `_may_schedule()`, `claim_run()` CAS,
  sticky `owner_node`, `_seen_nodes`, `_sync_from_db` stopped-flag detection, DB
  `runtime_status` overlay. It works, but ~900 lines of coordination code sits atop a model
  that does not naturally support consistent state.

### The v1.2.0 answer

Separate **orchestration** from **execution**:

- **`tram-manager`** — single-replica Deployment. Owns all state, hosts UI, makes all
  scheduling decisions. One pod = one in-memory state = no flickering, ever.
- **`tram-worker`** — StatefulSet replicas. Execute pipelines when told to, report results.
  No scheduler, no UI, no registry, no state. ~200 lines of agent API.

### Architecture

```
┌─────────────────────────────────────────────────────┐
│                   tram-manager                       │
│              (Deployment, replicas: 1)               │
│                                                      │
│  ┌──────────────┐  ┌────────────────┐  ┌──────────┐ │
│  │  Pipeline    │  │   Scheduler    │  │    UI    │ │
│  │  Registry    │  │  (APScheduler) │  │  /api/*  │ │
│  │  (in-memory) │  │  + DB persist  │  │  /ui/*   │ │
│  └──────┬───────┘  └────────┬───────┘  └──────────┘ │
│         └──────────┬────────┘                        │
│                    │  HTTP (internal)                 │
└────────────────────┼─────────────────────────────────┘
                     │  K8s headless service DNS
     ┌───────────────┼───────────────────┐
     ▼               ▼                   ▼
 tram-worker-0   tram-worker-1       tram-worker-2
 ┌────────────┐  ┌────────────┐      ┌────────────┐
 │ Agent API  │  │ Agent API  │      │ Agent API  │
 │ /run       │  │ /run       │      │ /run       │
 │ /stop      │  │ /stop      │      │ /stop      │
 │ /status    │  │ /status    │      │ /status    │
 │ /health    │  │ /health    │      │ /health    │
 └────────────┘  └────────────┘      └────────────┘
 (PipelineExecutor only — no scheduling, no registry)
```

### Data flow

```
User → manager REST API
  manager → decides which worker (least-loaded, sticky assignment)
  manager → POST worker-n/agent/run  {pipeline_config, run_id}
  worker  → executes batch_run() / stream_run()
  worker  → POST manager/api/internal/run-complete  {run_id, result}
  manager → updates registry + DB
```

### Worker discovery

Via K8s headless service DNS — no service discovery library needed:
```
tram-worker-{n}.tram-worker.<namespace>.svc.cluster.local:8766
```
Manager probes `/agent/health` on startup and on 10s poll.

### Component responsibilities

**`tram-manager`**
- Pipeline registry (in-memory, single authoritative copy)
- REST API (`/api/*`) — all current endpoints unchanged
- UI (`/ui/*`) — served by manager only
- Scheduling (APScheduler runs in manager process)
- Worker assignment (`least_loaded_worker()`)
- DB persistence (run_history, pipeline_versions, alerts)
- Status authority — manager's in-memory state IS the truth

**`tram-worker`** (~200 lines)
- `POST /agent/run` — receive config + run_id, execute, report back
- `POST /agent/stop` — stop a running batch/stream
- `GET  /agent/status` — return active jobs
- `GET  /agent/health` — liveness/readiness probe

### Failure modes

| Event | Behaviour |
|-------|-----------|
| Manager pod restarts | ~15s API/UI downtime; workers keep running active jobs; manager reloads from DB |
| Worker pod crashes | Manager detects via `/agent/health` timeout; marks run as error; reassigns to healthy worker |
| Worker scales out | Manager detects new DNS entry; assigns unowned pipelines |
| Worker scales in | Manager reassigns pipelines from draining worker |

### Internal agent API

```
POST /agent/run      {pipeline_name, yaml_text, run_id, schedule_type}
POST /agent/stop     {pipeline_name, run_id}
GET  /agent/status   → {running: [...], streams: [...]}
GET  /agent/health   → {ok: true, worker_id: "tram-worker-1"}

POST /api/internal/run-complete   (worker → manager)
     {run_id, pipeline_name, status, records_in, records_out, error}
```

### Helm chart changes

```yaml
# New: manager Deployment
tram-manager:
  kind: Deployment
  replicas: 1
  ports: [8765]

# Renamed: worker StatefulSet (was trishul-ram)
tram-worker:
  kind: StatefulSet
  replicas: 3
  ports: [8766]   # agent port only

# New: headless service for worker DNS
tram-worker-headless:
  kind: Service
  clusterIP: None
```

### Code removals (simplification)

| Removed | Reason |
|---------|--------|
| `_rebalance_loop()` + `_rebalance()` | No peer-to-peer topology negotiation |
| `_sync_from_db()` loop | Manager is the only writer |
| `ClusterCoordinator` | Replaced by simple worker health probing |
| `NodeRegistry` / `node_registry` table | Workers register directly with manager |
| `_may_schedule()` guards 3+4 | No cross-pod execution race |
| `claim_run()` CAS | Manager assigns; only one worker executes |
| `_seen_nodes`, cooling period | No rebalance triggers needed |
| `owner_node`, `runtime_status`, `status_node`, `status_updated` DB columns | Manager is state authority |

**Net:** ~600 lines deleted, ~200 lines worker agent added = **~400 lines removed**.

### Migration from v1.1.x cluster mode

```bash
helm upgrade trishul-ram ./helm \
  --set manager.enabled=true \
  --set worker.replicas=3
```

Existing run_history, pipeline_versions, alert_state preserved. Standalone users
(`TRAM_CLUSTER_MODE` unset): zero change, single-pod mode continues working.

### Implementation phases

**Phase 1 — Worker agent**
- `tram/agent/server.py` — FastAPI app on `:8766`
- `TRAM_MODE=worker` suppresses scheduler, registry, UI
- Workers POST run-complete callback to manager

**Phase 2 — Manager worker-dispatch**
- Manager replaces `_thread_pool.submit(_run_batch)` with `_dispatch_to_worker()`
- Manager tracks `{run_id: worker_url}` for stop routing
- Manager polls `/agent/health` every 10s

**Phase 3 — Cleanup**
- Remove coordinator, rebalance loop, sync loop, node_registry
- Remove DB columns no longer needed
- Replace `TRAM_CLUSTER_MODE` with `TRAM_MODE=manager|worker|standalone`
- Update Helm chart

**Phase 4 — Release**
- Update docs, bump version, CHANGELOG entry

---

## Backend Features

- **Pipeline cloning** — copy pipeline as new with name prompt
- **Per-sink record counts** — run-level metrics broken down by sink
- **Scheduled alert evaluation** — cron-based alert checks, not just post-run
- **Dead-letter queue viewer** — browse and replay DLQ records via the UI
- **Pipeline dependency graph** — visualize chain when pipeline A feeds pipeline B

## UI / UX

- **Bulk actions** — start/stop/delete multiple pipelines at once
- **Live log streaming** — WebSocket tail for running stream pipelines
- **Dark/light theme toggle**
- **Node health detail page** — per-node pipeline assignments and load (manager view)

## Cluster / Operations

- **Graceful drain** — mark node as draining, rebalance ownership before shutdown
- **Manager HA (stretch goal)** — standby manager with DB-backed leader election;
  only leader schedules; standby serves read-only API and takes over on leader failure

## Connectors

- **SMTP sink** — outbound email delivery
- **gRPC sink** — generic gRPC unary call sink
- **Syslog sink** — forward records to remote syslog server (RFC 5424)
- **Kafka schema registry** — Avro with schema ID framing (Confluent wire format)

## Testing / Quality

- **Test coverage to 75%** — Tier 3 unit tests for executor, persistence DB, pipeline
  watcher, health/runs/metrics routers; currently at 69% (threshold: 60%)

## Security

- **Role-based access** — read-only vs admin token scopes
- **Per-pipeline API key scoping** — restrict a key to specific pipelines
- **Key upload API** — `POST /api/keys/upload`, `GET /api/keys`, `DELETE /api/keys/<name>`;
  stores files on shared RWX PVC under `/data/keys/`; UI shows dropdown of uploaded keys
  alongside the free-text `private_key_path` field in connector forms

---

## What Does NOT Change

- `PipelineExecutor` (`batch_run`, `stream_run`, `dry_run`) — untouched
- All connectors, transforms, serializers — untouched
- External REST API (`/api/*`) — 100% backward compatible
- Pipeline YAML format — unchanged
- `PipelineState` object and `PipelineManager` registry — kept (used by manager)
- Auth, rate limiting, metrics, OpenTelemetry — unchanged

---

*Created: 2026-04-09*  
*Status: Planning — implementation begins after v1.1.4 changes are validated*
