# TRAM Architecture

## Overview

TRAM (Trishul Real-time Adapter & Mapper) is a lightweight, container-native Python daemon that moves and transforms telecom data (PM/FM/Logs) across protocols.

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
│  │      TramScheduler       │   │            FastAPI (REST API)            │ │
│  │  ┌────────────────────┐  │   │  /api/health    /api/pipelines           │ │
│  │  │  APScheduler       │  │   │  /api/runs      /api/plugins             │ │
│  │  │  (batch/cron)      │  │   │  /api/pipelines/{name}/versions          │ │
│  │  └────────────────────┘  │   │  /api/pipelines/{name}/rollback          │ │
│  │  ┌────────────────────┐  │   │  /api/cluster/nodes                      │ │
│  │  │  ThreadPool        │  │   │  /webhooks/{path}   /metrics             │ │
│  │  │  (stream)          │  │   └──────────────────────────────────────────┘ │
│  │  └────────────────────┘  │                                               │
│  │  ┌────────────────────┐  │                                               │
│  │  │  Rebalance thread  │  │                                               │
│  │  │  (cluster mode)    │  │                                               │
│  │  └────────────────────┘  │                                               │
│  └──────────────────────────┘                                               │
│               │                                                              │
│  NodeRegistry ── ClusterCoordinator  (cluster mode only)                    │
│  heartbeat thread  consistent hashing                                        │
│               │                                                              │
│  PipelineManager ── TramDB (SQLAlchemy)  ── AlertEvaluator                  │
│        │            run_history (+ node_id,   │  check(result, config)       │
│        │              dlq_count)              │  → webhook (httpx)           │
│        │            pipeline_versions         │  → email (smtplib)           │
│        │            alert_state (cooldown)    │                              │
│        │            node_registry             │                              │
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
└──────────────────────────────────────────────────────────────────────────────┘
```

## Plugin System

Plugins self-register via decorators at import time:

```python
@register_source("kafka")
class KafkaSource(BaseSource): ...
```

The three `__init__.py` files in `connectors/`, `transforms/`, and `serializers/` import all submodules, firing decorators during package import at startup.

### Plugin Registry Keys (v0.8.0)

| Category | Keys |
|----------|------|
| Sources | `sftp`, `local`, `rest`, `kafka`, `ftp`, `s3`, `syslog`, `snmp_trap`, `snmp_poll`, `mqtt`, `amqp`, `nats`, `gnmi`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `webhook`, `websocket`, `elasticsearch`, `prometheus_rw` |
| Sinks | `sftp`, `local`, `rest`, `kafka`, `opensearch`, `ftp`, `ves`, `s3`, `snmp_trap`, `mqtt`, `amqp`, `nats`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `websocket`, `elasticsearch` |
| Serializers | `json`, `csv`, `xml`, `avro`, `parquet`, `msgpack`, `protobuf` |
| Transforms | `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`, `flatten`, `timestamp_normalize`, `aggregate`, `enrich`, `explode`, `deduplicate`, `regex_extract`, `template`, `mask`, `validate`, `sort`, `limit`, `jmespath`, `unnest` |

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

## Cluster Mode (v0.8.0)

TRAM v0.8.0 adds a self-organising cluster with no external coordinator. Every pod participates in lightweight leader-election-free ownership via a shared database.

### How it works

```
┌──────────────────────────────────────────────────────────────────┐
│  Kubernetes StatefulSet                                           │
│                                                                   │
│  tram-0 ─────┐                                                   │
│  tram-1 ─────┼──── node_registry table (PostgreSQL / MariaDB) ───┤
│  tram-2 ─────┘         node_id  │  last_heartbeat  │  status     │
│                                                                   │
│  Each pod:                                                        │
│    1. NodeRegistry.start() → INSERT/UPSERT own row               │
│    2. Heartbeat thread → UPDATE last_heartbeat every N seconds   │
│    3. expire_nodes()  → mark peers with old heartbeat 'dead'     │
│    4. ClusterCoordinator.refresh() → read live peers             │
│    5. owns(pipeline)  → sha1(name) % live_count == my_position  │
│    6. Rebalance loop  → start/stop local pipelines on change     │
└──────────────────────────────────────────────────────────────────┘
```

### Consistent hashing

```python
_stable_hash(name) % len(live_nodes) == my_position
```

- `_stable_hash(name)`: `sha1(name.encode())` — deterministic across all pods and restarts (unlike Python `hash()`)
- `live_nodes`: sorted by `node_id` — order is identical on every pod
- `my_position`: index of `this_node_id` in the sorted list — NOT the StatefulSet ordinal
  - Example: if `tram-1` fails, `tram-0` = position 0, `tram-2` = position 1; full coverage is maintained

### Failure recovery

When a node stops heartbeating for `TRAM_NODE_TTL_SECONDS` (default 30s), peers mark it `'dead'` via `expire_nodes()`. On the next `coordinator.refresh()` call, the rebalance loop detects the topology change and absorbs the dead node's pipelines. No human intervention required.

### Standalone fallback

- `TRAM_CLUSTER_ENABLED=false` (default): `coordinator = None`; scheduler runs all pipelines normally
- SQLite as `TRAM_DB_URL`: cluster mode is silently disabled with a warning (SQLite cannot handle concurrent multi-node writes)
- If live nodes list is empty (startup race): node owns all pipelines as a safe fallback

### API

`GET /api/cluster/nodes` — returns topology state:

```json
{
  "cluster_enabled": true,
  "node_id": "tram-1",
  "my_position": 1,
  "live_node_count": 3,
  "nodes": [
    {"node_id": "tram-0"},
    {"node_id": "tram-1"},
    {"node_id": "tram-2"}
  ]
}
```

## Persistence (SQLAlchemy Core — v0.7.0)

`TramDB` uses **SQLAlchemy Core** so any backend is supported:

| Backend | `TRAM_DB_URL` example |
|---------|----------------------|
| SQLite (default) | `sqlite:////data/tram.db` or leave unset |
| PostgreSQL | `postgresql+psycopg2://user:pass@host/db` |
| MySQL | `mysql+pymysql://user:pass@host/db` |

Tables:
- `run_history` — every `RunResult`; includes `node_id` (TRAM_NODE_ID) and `dlq_count`
- `pipeline_versions` — every YAML registered; UUID primary key
- `alert_state` — last-alerted timestamp per `(pipeline_name, rule_name)`
- `node_registry` — cluster membership: `node_id, ordinal, registered_at, last_heartbeat, status`

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

## Security

- XML input uses `defusedxml` to prevent XXE attacks
- Expression evaluation uses `simpleeval` (safe sandbox, no builtins, no exec)
- Credentials always from environment variables, never in YAML files
- Webhook `secret` validated via `Authorization: Bearer` header
- Container runs as non-root user (uid 1000)

## Adding a New Protocol

See `docs/connectors.md` for the 3-step process.
