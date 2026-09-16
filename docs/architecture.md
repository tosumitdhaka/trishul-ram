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
│  dispatch_with_result() → least-loaded + round-robin tiebreaker             │
│  poll /agent/health every 10s; down only after 2 consecutive failures       │
│               │                                                              │
│  PipelineManager ── TramDB (SQLAlchemy)  ── AlertEvaluator                  │
│        │            run_history (+ node_id,   │  check(result, config)       │
│        │              dlq_count, errors_json) │  → webhook (httpx)           │
│        │            pipeline_versions         │  → email (smtplib)           │
│        │            alert_state (cooldown)    │                              │
│        │            processed_files           │                              │
│        │            queued_runs (v1.4.0)      │                              │
│        │            transform_state (v1.4.0)  │                              │
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
│  tram_mgr_dispatch_total{result} · tram_mgr_stats_missed_total (v1.4.0)    │
│  tram_mgr_queue_* · tram_mgr_reconcile_action_total (v1.4.0)               │
│  tram_transform_counter_wraps/resets_total · state_io_total (v1.4.0)      │
│  tram_transform_window_late_dropped/windows_emitted_total (v1.4.0)         │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Plugin System

Plugins self-register via decorators at import time:

```python
@register_source("kafka")
class KafkaSource(BaseSource): ...
```

The three `__init__.py` files in `connectors/`, `transforms/`, and `serializers/` import all submodules, firing decorators during package import at startup.

### Plugin Registry Keys (v1.3.0; transforms updated v1.4.0)

| Category | Count | Keys |
|----------|-------|------|
| Sources | 24 | `sftp`, `local`, `rest`, `kafka`, `ftp`, `s3`, `syslog`, `snmp_poll`, `snmp_trap`, `mqtt`, `amqp`, `nats`, `gnmi`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `webhook`, `websocket`, `elasticsearch`, `clickhouse`, `prometheus_rw`, `corba` |
| Sinks | 20 | `sftp`, `local`, `rest`, `kafka`, `opensearch`, `ftp`, `ves`, `s3`, `snmp_trap`, `mqtt`, `amqp`, `nats`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `websocket`, `elasticsearch`, `clickhouse` |
| Serializers | 12 | `json`, `ndjson`, `csv`, `xml`, `avro`, `parquet`, `msgpack`, `protobuf`, `bytes`, `text`, `asn1`, `pm_xml` |
| Transforms | 29 | `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`, `flatten`, `json_flatten`, `timestamp_normalize`, `aggregate`, `enrich`, `explode`, `deduplicate`, `regex_extract`, `template`, `mask`, `validate`, `sort`, `limit`, `jmespath`, `unnest`, `melt`, `select_from_list`, `coalesce_fields`, `project`, `inject_meta`, `hex_decode`, `counter_delta` (v1.4.0), `window_aggregate` (v1.4.0) |

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

## Thread Workers (v0.9.0; threaded path reworked v1.4.0)

`PipelineConfig.thread_workers: int = 1` — number of parallel worker threads per pipeline run.

**Batch mode** (`thread_workers > 1`): source chunks are submitted to a `ThreadPoolExecutor`, with
**in-flight chunks capped at `2 × thread_workers`** (bounded memory — GH #16's OOMKill root cause).
File sources defer their finalize (`move_after_read`, `skip_processed` marking) until their chunks
have drained, so a crash mid-run no longer loses data that was marked processed but not yet written.
`batch_size` checks are approximate across threads. Retry attempts close their sinks/DLQ/source.

**Serial batch mode with `record_chunk_size`**: the executor can ask a serializer for
`parse_chunks(data, record_chunk_size)` and process bounded decoded record windows instead of one
large in-memory list. This is the preferred path for very large file batches such as concatenated
ASN.1 BER CDR files. `asn1.split_path` (v1.4.0) splits the record list inside one decoded BER
document before chunking — on threaded runs the split is applied eagerly (bounded by the in-flight
cap, not by `record_chunk_size`).

**Stateful transforms** (`counter_delta`, `window_aggregate`) are rejected at validation when
combined with `thread_workers > 1` or in sink-level transforms — their durable state assumes
sequential per-key processing.

**Stream mode** (`thread_workers > 1`): a bounded `Queue(maxsize=thread_workers * 2)` decouples
the source producer from N worker threads, providing natural backpressure.

`PipelineRunContext` is fully thread-safe — all counter mutations are Lock-protected.

## Processed-File Tracking (v0.9.0)

`skip_processed: true` on any file/object-storage source (`sftp`, `local`, `s3`, `ftp`, `gcs`, `azure_blob`) causes the connector to skip files that have been successfully processed in a previous run.

State is persisted in the `processed_files` SQLite table, keyed by `(pipeline_name, source_key, filepath)`. `ProcessedFileTracker` is injected by `PipelineExecutor._build_source()` into the source config dict at runtime.

**File-done guards (v1.4.0)** — the `local` and `sftp` sources can additionally gate on write-in-progress files: `file_stability_seconds` (read only when size+mtime are unchanged across two scans that far apart), `file_min_age_seconds` (future-mtime tolerant), and `file_done_suffix` (collect only files renamed to a done suffix). All default off.

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
│         │  WorkerPool.dispatch_with_result()    TramDB (SQLite, RWO PVC)    │
│         │  WorkerPool.multi_dispatch()       broadcast_placements          │
│         ▼                                                              │
│  WorkerPool                                                            │
│  ├── least_loaded() + round-robin tiebreaker (batch/poll sources)      │
│  ├── multi_dispatch(count:all) → all healthy workers for push-HTTP      │
│  └── poll /agent/health every 10s; hysteresis (2 failures → down)     │
│                   │                                                    │
│  PlacementReconciler (background thread)                               │
│  ├── stale slot detection (age > 3 × TRAM_STATS_INTERVAL)             │
│  └── re-dispatch + reconciling-window timeout                          │
│  BatchReconciler (background thread)                                   │
│  ├── adopt orphaned running batch runs from worker /agent/status       │
│  ├── mark lost worker-owned batch runs failed before they stick        │
│  ├── recover count=1 streams: worker death → re-dispatch (~40-60s,     │
│  │   2-pass hysteresis); manager restart → adopt in place, never       │
│  │   double-dispatch; stale config → stop + redispatch (config_sha256) │
│  ├── drain queued manual runs when worker capacity returns (v1.4.0)   │
│  └── stop duplicate stray runs (earliest kept)                         │
│                   │                                                    │
│  FastAPI REST API + Web UI                                             │
│  POST /api/internal/run-complete  ← worker callback (X-API-Key)      │
│  POST /api/internal/pipeline-stats ← worker stats (X-API-Key)        │
│  GET/PUT /api/internal/transform-state/{pipeline} (v1.4.0)            │
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
2. Manager calls `WorkerPool.dispatch_with_result()` → picks least-loaded worker (round-robin on ties); the outcome is labeled `accepted` / `no_capacity` / `dispatch_failed` (persisted to run history + `tram_mgr_dispatch_total{result}`)
3. Controller records an active batch lease for the dispatched worker/run pair
4. Worker receives `POST /agent/run` with YAML + run_id
5. Worker syncs schemas/MIBs from manager (`GET /api/schemas`, `GET /api/mibs/{name}`)
6. For pipelines with stateful transforms, the worker GETs the pipeline's transform state from the manager (`GET /api/internal/transform-state/{pipeline}`); retries re-hydrate from the same in-run snapshot
7. Worker executes `PipelineExecutor.batch_run()` in a background thread; tracks `bytes_in`/`bytes_out`; on success PUTs the transform state back (state is only saved after a successful run)
8. Worker POSTs `run-complete` to manager: `records_in/out/skipped`, `bytes_in/bytes_out`, `error`, `errors[]` — all callbacks carry `X-API-Key`
9. Manager calls `on_worker_run_complete()` → saves to DB (including byte counters), updates pipeline state
10. If the manager restarts or the worker disappears before callback, `BatchReconciler` scans
    worker `/agent/status` to adopt surviving runs or mark lost runs failed through the same normal
    completion path

**No worker capacity** (v1.4.0): a manual run triggered with zero healthy workers is durably
queued (`queued_runs` table, `TRAM_QUEUE_MANUAL_RUNS=1` default) — the API returns
`202 {status: "queued"}`; the row survives manager restarts on an absolute TTL clock
(`TRAM_QUEUE_TTL_SECONDS`, default 900) and `BatchReconciler` drains it when capacity returns.

### Run lifecycle — multi-worker streams (`webhook`, `prometheus_rw`)

1. Pipeline controller calls `WorkerPool.multi_dispatch(count='all')` → sends `POST /agent/run` to every healthy worker
2. A placement group is created with one slot per worker; state saved to `broadcast_placements` DB table
3. Each worker runs `PipelineExecutor.stream_run()` continuously; posts periodic stats to `POST /api/internal/pipeline-stats`
4. `StatsStore` holds live per-slot stats; `PlacementReconciler` polls every `min(TRAM_STATS_INTERVAL, 10)s`
5. Stale slot (age > `3 × TRAM_STATS_INTERVAL`): reconciler re-dispatches to same worker, updates `current_run_id`
6. Reconciling-window timeout after `2 × TRAM_STATS_INTERVAL`: partial recovery → `degraded`; none → re-dispatch

### Run lifecycle — count=1 streams (v1.4.0, GH #17)

With `TRAM_STREAM_SINGLE_PLACEMENT=1` (default), all worker-mode streams route through the
placement machinery and a count=1 stream produces a durable **1-slot placement row** instead of
manager-memory-only tracking:

- **Manager restart** adopts the live run in place (zero interruption, no double-dispatch)
- **Worker death** recovers to a healthy worker within ~40-60s (2-pass liveness hysteresis);
  the reconciler increments the slot's `restart_count` and stops any duplicate stray run
- **Config drift**: the worker reports `config_sha256` in `/agent/status`; a mismatch against the
  current pipeline config stops and redispatches the stream with the fresh YAML (older agents
  without the field fail open)
- Rollback: `TRAM_STREAM_SINGLE_PLACEMENT=0` + manager restart (existing placement rows remain
  inert); kind-verified liveness, recovery, and adoption: `docs/reviews/kind-verification.md`

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

Honors `TRAM_INTERNAL_AUTH_MODE` (`/agent/health` exempt); probe paths never 401.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/agent/run` | Dispatch a pipeline run (YAML + run_id) |
| `POST` | `/agent/stop` | Signal a running pipeline to stop |
| `GET` | `/agent/status` | Active batch runs and streams, including per-run `config_sha256` (v1.4.0 — consumed by the reconciler's config-drift detection; absent means unknown, which fails open) |
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
- `queued_runs` (v1.4.0) — durably queued manual runs: stable `run_id`, YAML snapshot, status (`queued` → `dispatching` → terminal), absolute `expires_at` TTL; claim/commit/revert transitions are conditional-UPDATE fenced for single-claim
- `registered_pipelines` — the manager's durable pipeline registry (active config per pipeline); makes API updates and alert-rule edits survive manager restarts
- `settings` — key/value settings store
- `transform_state` (v1.4.0) — per-pipeline stateful-transform state blob (`counter_delta` last values, `window_aggregate` open windows) + `config_sha256`; a hash mismatch discards the state so new transform identities start fresh; `update()`/`delete()` purge the row

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

- API key auth on `/api/*` via the `X-API-Key` header (`TRAM_API_KEY`; empty = disabled) — the legacy `?api_key=` query param is removed (keys leak into logs)
- Internal machine-to-machine surfaces (`/api/internal/*` on the manager, `/agent/*` on workers) honor `TRAM_INTERNAL_AUTH_MODE` (`off | warn | enforce`, default `warn`): warn-only logs missing keys, `enforce` rejects with 401; `/agent/health` and K8s probe paths are unconditionally exempt
- Worker → manager callbacks (`run-complete`, `pipeline-stats`) send `X-API-Key` on every POST
- Webhook ingress enforces a body-size limit (`TRAM_WEBHOOK_MAX_BODY_BYTES`, default 10 MiB; 413 on excess)
- ClickHouse sink validates the `table` identifier (`db.table` accepted; quoted/bracketed names rejected)
- XML input uses `defusedxml` to prevent XXE attacks
- Expression evaluation uses `simpleeval` (safe sandbox, no builtins, no exec)
- Credentials always from environment variables, never in YAML files
- Webhook `secret` validated via `Authorization: Bearer` header (constant-time compare)
- Container runs as non-root user (uid 1000)

## Adding a New Protocol

See `docs/connectors.md` for the 3-step process.
