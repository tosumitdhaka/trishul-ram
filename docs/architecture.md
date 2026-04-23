# TRAM Architecture

## Overview

TRAM (Trishul Real-time Aggregation & Mediation) is a lightweight, container-native Python daemon that moves and transforms telecom data (PM/FM/Logs) across protocols.

## Design Principles

1. **12-Factor App** — all configuration from environment variables, logs to stdout
2. **Plugin-first** — every connector, transform, and serializer is a plugin registered by decorator
3. **Pipeline-as-code** — YAML defines the data flow; no code changes for new pipelines
4. **Always-on daemon** — pipelines managed at runtime via REST or CLI
5. **Two execution modes** — batch (finite, interval/cron/manual) and stream (infinite, Kafka/NATS/webhook)

## Data Flow

```
Source → (bytes, meta) → Deserializer → list[dict] → Global Transforms (per-record)
                                                                │
                                             ┌─────────────────┴──────────────────┐
                                        [parse error]                       [transform error]
                                             │                                     │
                                          DLQ sink                            DLQ sink
                                       (stage=parse)                    (stage=transform)

                               list[dict] (surviving records)
                                             │
                                  ┌──────────┴──────────┐
                                  │    For each sink:    │
                                  │  condition filter    │
                                  │  per-sink transforms │──── [transform error] → DLQ sink
                                  │  serializer_out      │
                                  │  sink.write()        │──── [write error]     → DLQ sink
                                  └──────────────────────┘
```

Every record is a plain Python `dict`. Global transforms apply per-record so a single bad record cannot abort the whole chunk. Each sink can apply its own transform chain independently.

## Component Map

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              TramServer (daemon)                              │
│                                                                              │
│  ┌──────────────────────────┐   ┌──────────────────────────────────────────┐ │
│  │   PipelineController     │   │            FastAPI (REST API)            │ │
│  │  ┌────────────────────┐  │   │  /api/health    /api/pipelines           │ │
│  │  │  APScheduler       │  │   │  /api/runs      /api/plugins             │ │
│  │  │  (batch/cron)      │  │   │  /api/pipelines/{name}/versions          │ │
│  │  └────────────────────┘  │   │  /api/pipelines/{name}/rollback          │ │
│  │  ┌────────────────────┐  │   │  /api/cluster/nodes                      │ │
│  │  │  ThreadPool        │  │   │  /webhooks/{path}   /metrics             │ │
│  │  │  (batch runs)      │  │   └──────────────────────────────────────────┘ │
│  │  └────────────────────┘  │                                               │
│  └──────────────────────────┘                                               │
│               │                                                              │
│  WorkerPool  (manager mode only)                                             │
│  least_loaded() dispatch + round-robin tiebreaker                            │
│  poll /agent/health every 10s → single summary log on change                │
│               │                                                              │
│  PipelineManager ── TramDB (SQLAlchemy)  ── AlertEvaluator                  │
│        │            run_history (+ node_id,   │  check(result, config)       │
│        │              dlq_count, errors_json) │  → webhook (httpx)           │
│        │            pipeline_versions         │  → email (smtplib)           │
│        │            alert_state (cooldown)    │                              │
│        │            processed_files           │                              │
│        │                                                                     │
│  PipelineExecutor                                                            │
│  ┌─────┴──────────────────────┐                                              │
│  │                            │                                              │
│  batch_run()             stream_run()                                        │
│  │                            │                                              │
│  _build_source()         _build_source()                                     │
│  _build_sinks()          _build_sinks()  ← list of (sink, cond, transforms) │
│  _build_dlq_sink()       _build_dlq_sink()                                   │
│  _filter_by_condition()  _filter_by_condition()                              │
│  _rate_limit()           _rate_limit()                                       │
│                                                                              │
│  Metrics (prometheus_client or no-ops)                                       │
│  tram_records_in/out/skipped/errors/dlq_total + chunk duration histogram     │
│  tram_kafka_consumer_lag{pipeline,topic,partition} (v1.0.0)                  │
│  tram_stream_queue_depth{pipeline} (v1.0.0)                                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Plugin System

Plugins self-register via decorators at import time:

```python
@register_source("kafka")
class KafkaSource(BaseSource): ...
```

The three `__init__.py` files in `connectors/`, `transforms/`, and `serializers/` import all submodules, firing decorators during package import at startup.

### Plugin Registry Keys (v1.3.0)

| Category | Count | Keys |
|----------|-------|------|
| Sources | 24 | `sftp`, `local`, `rest`, `kafka`, `ftp`, `s3`, `syslog`, `snmp_poll`, `snmp_trap`, `mqtt`, `amqp`, `nats`, `gnmi`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `webhook`, `websocket`, `elasticsearch`, `clickhouse`, `prometheus_rw`, `corba` |
| Sinks | 20 | `sftp`, `local`, `rest`, `kafka`, `opensearch`, `ftp`, `ves`, `s3`, `snmp_trap`, `mqtt`, `amqp`, `nats`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `websocket`, `elasticsearch`, `clickhouse` |
| Serializers | 12 | `json`, `ndjson`, `csv`, `xml`, `avro`, `parquet`, `msgpack`, `protobuf`, `bytes`, `text`, `asn1`, `pm_xml` |
| Transforms | 21 | `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`, `flatten`, `timestamp_normalize`, `aggregate`, `enrich`, `explode`, `deduplicate`, `regex_extract`, `template`, `mask`, `validate`, `sort`, `limit`, `jmespath`, `unnest`, `melt` |

## Execution Modes

### Batch Mode
- Source yields a finite set of `(bytes, meta)` tuples
- APScheduler drives execution on interval/cron
- Each run produces a `RunResult` saved to SQLite (if configured)
- Manual trigger via `POST /api/pipelines/{name}/run`

### Stream Mode
- Source is an infinite generator (Kafka consumer, SNMP trap receiver, webhook, etc.)
- Runs in a dedicated thread per pipeline
- Stopped only by `POST /api/pipelines/{name}/stop` or daemon shutdown

## Multi-Sink Routing + Per-Sink Transforms

```python
records = serializer_in.parse(raw)          # parse error → DLQ (stage=parse)

surviving = []
for record in records:
    try:
        r = [record]
        for t in global_transforms:
            r = t.apply(r)                  # transform error → DLQ (stage=transform)
        surviving.extend(r)
    except Exception:
        dlq_sink.write(envelope)

for sink, condition, sink_transforms in sinks:
    subset = filter_by_condition(records, condition) if condition else records
    if not subset:
        continue

    for t in sink_transforms:              # per-sink transforms
        subset = t.apply(subset)           # error → DLQ (stage=transform)

    serialized = serializer_out.serialize(subset)
    rate_limit()                            # if rate_limit_rps configured
    try:
        sink.write(serialized, meta)
    except Exception:
        dlq_sink.write(envelope)           # error → DLQ (stage=sink)
```

- No condition = catch-all (receives all records)
- A record can be written to multiple sinks simultaneously
- Each sink's transform chain is independent — different sinks can reshape the same records differently
- Empty subset or failed sink transform → sink is skipped; other sinks continue

## Rate Limiting

Token-bucket algorithm on `PipelineExecutor`. One token consumed per sink write. Tokens refill at `rate_limit_rps` per second. Blocks (sleeps) when bucket is empty.

## Thread Workers (v0.9.0)

`PipelineConfig.thread_workers: int = 1` — number of parallel worker threads per pipeline run.

**Batch mode** (`thread_workers > 1`): source chunks are submitted to a `ThreadPoolExecutor`. N
chunks process concurrently. `batch_size` checks are approximate across threads.

**Serial batch mode with `record_chunk_size`**: the executor can ask a serializer for
`parse_chunks(data, record_chunk_size)` and process bounded decoded record windows instead of one
large in-memory list. This is the preferred path for very large file batches such as concatenated
ASN.1 BER CDR files.

**Current scope note:** threaded batch execution still uses the eager parse path in `v1.3.2`;
bounded record chunking is implemented in the serial batch path.

**Stream mode** (`thread_workers > 1`): a bounded `Queue(maxsize=thread_workers * 2)` decouples
the source producer from N worker threads, providing natural backpressure.

`PipelineRunContext` is fully thread-safe — all counter mutations are Lock-protected.

## Processed-File Tracking (v0.9.0)

`skip_processed: true` on any file/object-storage source (`sftp`, `local`, `s3`, `ftp`, `gcs`, `azure_blob`) causes the connector to skip files that have been successfully processed in a previous run.

State is persisted in the `processed_files` SQLite table, keyed by `(pipeline_name, source_key, filepath)`. `ProcessedFileTracker` is injected by `PipelineExecutor._build_source()` into the source config dict at runtime.

## Dead-Letter Queue (DLQ)

`PipelineConfig.dlq: Optional[SinkConfig]` — any sink type (typically `local` or `kafka`).

When configured, failed records are written as JSON envelopes:

```json
{
  "_error":     "ValueError: cannot cast 'N/A' to int",
  "_stage":     "transform",
  "_pipeline":  "pm-ingest",
  "_run_id":    "abc12345",
  "_timestamp": "2026-03-05T12:00:00+00:00",
  "record":     {"ne_id": "NE-01", "rx_bytes": "N/A"},
  "raw":        null
}
```

`raw` (base64) is only set when `_stage == "parse"`. DLQ write failures are logged and swallowed.

## Alert Rules

`AlertEvaluator.check(result, config)` is called by `PipelineManager.record_run()` after every batch run. Condition variables:

| Variable | Type | Description |
|----------|------|-------------|
| `records_in` | int | Records read from source |
| `records_out` | int | Records written to at least one sink |
| `records_skipped` | int | Records filtered or failed |
| `error_rate` | float | `records_skipped / records_in` (0 if no records) |
| `status` | str | `"success"` \| `"failed"` \| `"aborted"` |
| `failed` | bool | Shorthand for `status == "failed"` |
| `duration_seconds` | float | Wall time of the run |

Cooldown is only started after a confirmed successful delivery: `_fire_webhook()` and `_fire_email()` return `True`/`False`, and `_set_cooldown()` is called only on `True`. An HTTP 5xx, connection error, or SMTP failure does not silence the rule.

Cooldown state is persisted in `alert_state` SQLite table so it survives daemon restarts.

## Cluster Mode / Manager + Worker (v1.2.0)

TRAM v1.2.0 replaces the previous shared-DB cluster model with a dedicated **manager + worker** architecture. Set `TRAM_MODE` to choose the deployment shape.

### Deployment modes

| `TRAM_MODE` | Role | Runs |
|-------------|------|------|
| `standalone` (default) | All-in-one: scheduler + DB + UI + executor | StatefulSet (1 replica) |
| `manager` | Scheduler + DB + UI; dispatches runs to workers | StatefulSet (1 replica) |
| `worker` | Stateless executor; no DB, no scheduler, no UI | StatefulSet (N replicas) |

### Architecture diagram

```
┌───────────────────────────────────────────────────────────────────────┐
│  Manager pod (StatefulSet, 1 replica)                                  │
│                                                                        │
│  PipelineController ──── APScheduler ──── PipelineManager             │
│         │                                      │                       │
│         │  WorkerPool.dispatch()             TramDB (SQLite, RWO PVC) │
│         │  WorkerPool.multi_dispatch()       broadcast_placements      │
│         ▼                                                              │
│  WorkerPool                                                            │
│  ├── least_loaded() + round-robin tiebreaker (batch/poll sources)      │
│  ├── multi_dispatch(count:all) → all healthy workers for push-HTTP      │
│  └── poll /agent/health every 10s → single summary log on change      │
│                   │                                                    │
│  PlacementReconciler (background thread)                               │
│  ├── stale slot detection (age > 3 × TRAM_STATS_INTERVAL)             │
│  └── re-dispatch + reconciling-window timeout                          │
│  BatchReconciler (background thread)                                   │
│  ├── adopt orphaned running batch runs from worker /agent/status       │
│  └── mark lost worker-owned batch runs failed before they stick        │
│                   │                                                    │
│  FastAPI REST API + Web UI                                             │
│  POST /api/internal/run-complete  ← worker callback                   │
│  POST /api/internal/pipeline-stats ← worker stats                     │
│  GET  /api/pipelines/{name}/placement                                  │
│  GET  /api/cluster/streams                                             │
└──────────────────────────┬────────────────────────────────────────────┘
                           │  HTTP dispatch
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
    │  worker-0   │ │  worker-1   │ │  worker-2   │
    │  :8766      │ │  :8766      │ │  :8766      │
    │  :8767      │ │  :8767      │ │  :8767      │
    │ (ingress)   │ │ (ingress)   │ │ (ingress)   │
    │             │ │             │ │             │
    │ WorkerAgent │ │ WorkerAgent │ │ WorkerAgent │
    │ (FastAPI)   │ │ (FastAPI)   │ │ (FastAPI)   │
    │             │ │             │ │             │
    │ sync assets │ │ sync assets │ │ sync assets │
    │ (schemas,   │ │ (schemas,   │ │ (schemas,   │
    │  MIBs)      │ │  MIBs)      │ │  MIBs)      │
    │             │ │             │ │             │
    │ PipelineExe │ │ PipelineExe │ │ PipelineExe │
    │ cutor       │ │ cutor       │ │ cutor       │
    │ + stats     │ │ + stats     │ │ + stats     │
    └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
           └───────────────┼───────────────┘
                           │ POST /api/internal/run-complete
                           │ POST /api/internal/pipeline-stats
                           ▼
                    Manager (callbacks)
```

### Run lifecycle — batch/poll

1. APScheduler fires → `PipelineController._run_batch()`
2. Manager calls `WorkerPool.dispatch()` → picks least-loaded worker (round-robin on ties)
3. Controller records an active batch lease for the dispatched worker/run pair
4. Worker receives `POST /agent/run` with YAML + run_id
5. Worker syncs schemas/MIBs from manager (`GET /api/schemas`, `GET /api/mibs/{name}`)
6. Worker executes `PipelineExecutor.batch_run()` in a background thread; tracks `bytes_in`/`bytes_out`
7. Worker POSTs `run-complete` to manager: `records_in/out/skipped`, `bytes_in/bytes_out`, `error`, `errors[]`
8. Manager calls `on_worker_run_complete()` → saves to DB (including byte counters), updates pipeline state
9. If the manager restarts or the worker disappears before callback, `BatchReconciler` scans
   worker `/agent/status` to adopt surviving runs or mark lost runs failed through the same normal
   completion path

### Run lifecycle — multi-worker streams (`webhook`, `prometheus_rw`)

1. Pipeline controller calls `WorkerPool.multi_dispatch(count='all')` → sends `POST /agent/run` to every healthy worker
2. A placement group is created with one slot per worker; state saved to `broadcast_placements` DB table
3. Each worker runs `PipelineExecutor.stream_run()` continuously; posts periodic stats to `POST /api/internal/pipeline-stats`
4. `StatsStore` holds live per-slot stats; `PlacementReconciler` polls every `min(TRAM_STATS_INTERVAL, 10)s`
5. Stale slot (age > `3 × TRAM_STATS_INTERVAL`): reconciler re-dispatches to same worker, updates `current_run_id`
6. Reconciling-window timeout after `2 × TRAM_STATS_INTERVAL`: partial recovery → `degraded`; none → re-dispatch

### Worker discovery

Workers are resolved from Kubernetes headless DNS:

```
http://<release>-worker-N.<release>-worker.<namespace>.svc.cluster.local:8766
```

Controlled by `TRAM_WORKER_REPLICAS`, `TRAM_WORKER_SERVICE`, `TRAM_WORKER_NAMESPACE`, `TRAM_WORKER_PORT`. Can also be set explicitly via `TRAM_WORKER_URLS=http://w0:8766,http://w1:8766`.

### Worker ports

Workers listen on two ports:

- **`:8766`** — internal agent API (manager-to-worker dispatch, health, status)
- **`:8767`** — ingress-only webhook receiver (`/webhooks/*`); reachable from outside the cluster

Both threads start together; if either exits the pod sends `SIGTERM` to itself so Kubernetes restarts it. The composite `GET /agent/health` endpoint returns `ok: false` when the ingress thread has died.

### Worker agent API (`:8766`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/agent/run` | Dispatch a pipeline run (YAML + run_id) |
| `POST` | `/agent/stop` | Signal a running pipeline to stop |
| `GET` | `/agent/status` | Active batch runs and streams |
| `GET` | `/agent/health` | Liveness + active_runs + running_pipelines + ingress_up |

### Worker ingress API (`:8767`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhooks/{path}` | Forward push traffic to the registered `webhook` or `prometheus_rw` source queue |

### `GET /api/cluster/nodes` (manager)

```json
{
  "mode": "manager",
  "workers": [
    {
      "url": "http://trishul-ram-worker-0...:8766",
      "ok": true,
      "active_runs": 0,
      "running_pipelines": [],
      "assigned_pipelines": ["snmp_ifmib_to_sftp"]
    }
  ]
}
```

`ok` is `false` when the worker agent thread is dead or the composite worker health check fails. The manager-facing node view does not expose a separate `ingress_up` field; ingress health is folded into `ok`.

### Standalone fallback

`TRAM_MODE=standalone` (default): no `WorkerPool` is created; the manager executes pipelines in-process via `PipelineExecutor` directly. SQLite is sufficient.

## Persistence (SQLAlchemy Core — v0.7.0)

`TramDB` uses **SQLAlchemy Core** so any backend is supported:

| Backend | `TRAM_DB_URL` example |
|---------|----------------------|
| SQLite (default) | `sqlite:////data/tram.db` or leave unset |
| PostgreSQL | `postgresql+psycopg2://user:pass@host/db` |
| MySQL | `mysql+pymysql://user:pass@host/db` |

Tables:
- `run_history` — every `RunResult`; includes `node_id`, `dlq_count`, `records_skipped`, `errors_json`, `bytes_in`, `bytes_out`
- `pipeline_versions` — every YAML registered; UUID primary key
- `alert_state` — last-alerted timestamp per `(pipeline_name, rule_name)`
- `processed_files` — `(pipeline_name, source_key, filepath, processed_at)`; used by `skip_processed` to make file-source runs idempotent
- `user_passwords` — scrypt-hashed passwords for browser auth (override `TRAM_AUTH_USERS` bootstrap values)
- `broadcast_placements` — active multi-worker placement groups; persists `slots_json` (including mutable `current_run_id` per slot) so the manager can reconcile after restart

**Schema migrations**: `_create_tables()` runs `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` guards at startup. Existing databases from v0.6.0 are upgraded automatically.

**Node identity**: `TRAM_NODE_ID` (defaults to hostname) is stored in each `run_history` row — essential for diagnosing which instance produced which runs in multi-node deployments.

## Webhook Bridge

`WebhookSource` registers a `queue.SimpleQueue` in the global `_WEBHOOK_REGISTRY` dict. The FastAPI `/webhooks/{path}` router puts `(body, meta)` into the queue. The source generator yields from it.

This bridge makes the daemon's HTTP port a synchronous input channel for any HTTP-speaking system (Filebeat HTTP output, Prometheus remote_write, custom agents).

## Error Handling

Per-pipeline `on_error` policy:
- `continue` — log error, skip record/chunk, continue
- `abort` — raise exception, mark run failed, stop
- `retry` — retry entire run up to `retry_count` times with `retry_delay_seconds` backoff
- `dlq` — route ALL failures (parse/transform/sink) to the DLQ sink (requires `dlq:` to be configured)

### Per-record error tracking (v1.2.1)

`PipelineRunContext` accumulates per-record errors throughout a run:

| Method | Effect |
|--------|--------|
| `record_error(msg)` | Appends `msg` to `ctx.errors`; increments `records_skipped` by 1 |
| `note_skip(msg)` | Appends `msg` to `ctx.errors` only — no counter change (used when skip already counted) |

When no sink writes a batch of records (all conditions filtered them out, or all sinks failed/circuit-open), the executor calls `ctx.note_skip("Records skipped — no sink wrote successfully ...")` and logs a WARNING. This means skip reasons are visible in the run detail in the UI.

In manager+worker mode the full `errors` list is sent in the worker callback payload and stored in `run_history.errors_json` so it survives across the HTTP boundary.

## Security

- XML input uses `defusedxml` to prevent XXE attacks
- Expression evaluation uses `simpleeval` (safe sandbox, no builtins, no exec)
- Credentials always from environment variables, never in YAML files
- Webhook `secret` validated via `Authorization: Bearer` header
- Container runs as non-root user (uid 1000)

## Adding a New Protocol

See `docs/connectors.md` for the 3-step process.
