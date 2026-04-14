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
│  │  │  (stream)          │  │   └──────────────────────────────────────────┘ │
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

### Plugin Registry Keys (v1.2.1)

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

**Batch mode** (`thread_workers > 1`): source chunks are submitted to a `ThreadPoolExecutor`. N chunks process concurrently. `batch_size` checks are approximate across threads.

**Stream mode** (`thread_workers > 1`): a bounded `Queue(maxsize=thread_workers * 2)` decouples the source producer from N worker threads, providing natural backpressure.

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

Cooldown is persisted in `alert_state` SQLite table so it survives daemon restarts.

## Cluster Mode / Manager + Worker (v1.2.0)

TRAM v1.2.0 replaces the previous shared-DB cluster model with a dedicated **manager + worker** architecture. Set `TRAM_MODE` to choose the deployment shape.

### Deployment modes

| `TRAM_MODE` | Role | Runs |
|-------------|------|------|
| `standalone` (default) | All-in-one: scheduler + DB + UI + executor | Single pod |
| `manager` | Scheduler + DB + UI; dispatches runs to workers | Deployment (1 replica) |
| `worker` | Stateless executor; no DB, no scheduler, no UI | StatefulSet (N replicas) |

### Architecture diagram

```
┌───────────────────────────────────────────────────────────────────────┐
│  Manager pod (Deployment, 1 replica)                                   │
│                                                                        │
│  PipelineController ──── APScheduler ──── PipelineManager             │
│         │                                      │                       │
│         │  POST /agent/run                   TramDB (SQLite, RWO PVC) │
│         ▼                                                              │
│  WorkerPool                                                            │
│  ├── least_loaded() + round-robin tiebreaker                           │
│  ├── _pipeline_worker: {pipeline → worker_url}                        │
│  └── poll /agent/health every 10s → single summary log on change      │
│                   │                                                    │
│  FastAPI REST API + Web UI                                             │
│  POST /api/internal/run-complete  ← worker callback                   │
└──────────────────────────┬────────────────────────────────────────────┘
                           │  HTTP dispatch
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
    │  worker-0   │ │  worker-1   │ │  worker-2   │
    │  :8766      │ │  :8766      │ │  :8766      │
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
    └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
           └───────────────┼───────────────┘
                           │ POST /api/internal/run-complete
                           ▼
                    Manager (callback)
```

### Run lifecycle

1. APScheduler fires → `PipelineController._run_batch()`
2. Manager calls `WorkerPool.dispatch()` → picks least-loaded worker (round-robin on ties)
3. Worker receives `POST /agent/run` with YAML + run_id
4. Worker syncs schemas/MIBs from manager (`GET /api/schemas`, `GET /api/mibs/{name}`)
5. Worker executes `PipelineExecutor.batch_run()` in a background thread
6. Worker POSTs `run-complete` to manager: `records_in/out/skipped`, `error`, `errors[]`
7. Manager calls `on_worker_run_complete()` → saves to DB, updates pipeline state

### Worker discovery

Workers are resolved from Kubernetes headless DNS:

```
http://<release>-worker-N.<release>-worker.<namespace>.svc.cluster.local:8766
```

Controlled by `TRAM_WORKER_REPLICAS`, `TRAM_WORKER_SERVICE`, `TRAM_WORKER_NAMESPACE`, `TRAM_WORKER_PORT`. Can also be set explicitly via `TRAM_WORKERS=http://w0:8766,http://w1:8766`.

### Worker agent API (`:8766`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/agent/run` | Dispatch a pipeline run (YAML + run_id) |
| `POST` | `/agent/stop` | Signal a running pipeline to stop |
| `GET` | `/agent/status` | Active batch runs and streams |
| `GET` | `/agent/health` | Liveness + active_runs + running_pipelines |

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
- `run_history` — every `RunResult`; includes `node_id`, `dlq_count`, `records_skipped`, `errors_json`
- `pipeline_versions` — every YAML registered; UUID primary key
- `alert_state` — last-alerted timestamp per `(pipeline_name, rule_name)`
- `processed_files` — `(pipeline_name, source_key, filepath, processed_at)`; used by `skip_processed` to make file-source runs idempotent
- `user_passwords` — bcrypt-hashed passwords for browser auth (override `TRAM_AUTH_USERS`)

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
