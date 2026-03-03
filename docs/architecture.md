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
Source → (bytes, meta) → Deserializer → list[dict] → Transforms → Serializer → bytes → Sinks
```

Each sink may have an optional `condition:` expression. Records are fan-out-routed: all sinks with a matching condition (or no condition) receive a separately serialized copy.

Every record is a plain Python `dict`. This universal in-memory representation allows transforms to be protocol-agnostic.

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
│  │  ┌────────────────────┐  │   │  /webhooks/{path}   /metrics             │ │
│  │  │  ThreadPool        │  │   └──────────────────────────────────────────┘ │
│  │  │  (stream)          │  │                                               │
│  │  └────────────────────┘  │                                               │
│  └──────────────────────────┘                                               │
│               │                                                              │
│         PipelineManager ──── TramDB (SQLite)                                 │
│               │               run_history + pipeline_versions                │
│         PipelineExecutor                                                     │
│         ┌────┴──────────────────────┐                                       │
│         │                           │                                        │
│    batch_run()                 stream_run()                                  │
│         │                           │                                        │
│   _build_source()            _build_source()                                 │
│   _build_sinks()             _build_sinks()    ← list of (sink, condition)  │
│   _filter_by_condition()     _filter_by_condition()                          │
│   _rate_limit()              _rate_limit()                                   │
│                                                                              │
│         Metrics (prometheus_client or no-ops)                                │
│         tram_records_in/out/skipped/errors + chunk duration histogram        │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Plugin System

Plugins self-register via decorators at import time:

```python
@register_source("kafka")
class KafkaSource(BaseSource): ...
```

The three `__init__.py` files in `connectors/`, `transforms/`, and `serializers/` import all submodules, firing decorators during package import at startup.

### Plugin Registry Keys (v0.5.0)

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

## Multi-Sink Routing

```
records = serializer_in.parse(raw)
records = transforms.apply(records)

for sink, condition in sinks:
    subset = filter_by_condition(records, condition) if condition else records
    if subset:
        serialized = serializer_out.serialize(subset)
        rate_limit()   # if rate_limit_rps configured
        sink.write(serialized, meta)
```

- No condition = catch-all (receives all records)
- A record can be written to multiple sinks simultaneously
- Empty subset → sink is skipped entirely

## Rate Limiting

Token-bucket algorithm on `PipelineExecutor`. One token consumed per sink write. Tokens refill at `rate_limit_rps` per second. Blocks (sleeps) when bucket is empty.

## Persistence (SQLite)

`TramDB` at `~/.tram/tram.db` (or `$TRAM_DB_PATH`):
- `run_history` — every `RunResult` saved by `PipelineManager.record_run()`
- `pipeline_versions` — every YAML registered, auto-incremented version number

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
