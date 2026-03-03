# TRAM Architecture

## Overview

TRAM (Trishul Real-time Adapter & Mapper) is a lightweight, container-native Python daemon that moves and transforms telecom data (PM/FM/Logs) across protocols.

## Design Principles

1. **12-Factor App** — all configuration from environment variables, logs to stdout
2. **Plugin-first** — every connector, transform, and serializer is a plugin registered by decorator
3. **Pipeline-as-code** — YAML defines the data flow; no code changes for new pipelines
4. **Always-on daemon** — pipelines managed at runtime via REST or CLI
5. **Two execution modes** — batch (finite, interval/cron/manual) and stream (infinite, Kafka/NATS)

## Data Flow

```
Source → (bytes, meta) → Deserializer → list[dict] → Transforms → Serializer → bytes → Sink
```

Every record is a plain Python `dict`. This universal in-memory representation allows transforms to be protocol-agnostic.

## Component Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                         TramServer (daemon)                         │
│  ┌─────────────────────────┐  ┌──────────────────────────────────┐  │
│  │      TramScheduler      │  │         FastAPI (REST)           │  │
│  │  ┌───────────────────┐  │  │  /api/health  /api/pipelines     │  │
│  │  │  APScheduler      │  │  │  /api/runs    /api/plugins       │  │
│  │  │  (batch/cron)     │  │  └──────────────────────────────────┘  │
│  │  └───────────────────┘  │                                         │
│  │  ┌───────────────────┐  │                                         │
│  │  │  ThreadPool       │  │                                         │
│  │  │  (stream)         │  │                                         │
│  │  └───────────────────┘  │                                         │
│  └─────────────────────────┘                                         │
│              │                                                       │
│         PipelineManager                                              │
│              │                                                       │
│         PipelineExecutor                                             │
│         ┌───┴─────────────┐                                          │
│         │                 │                                          │
│    batch_run()       stream_run()                                    │
└─────────────────────────────────────────────────────────────────────┘
```

## Plugin System

Plugins self-register via decorators at import time:

```python
@register_source("sftp")
class SFTPSource(BaseSource): ...
```

The three `__init__.py` files in `connectors/`, `transforms/`, and `serializers/` import all submodules, firing decorators during package import at startup.

### Plugin Registry Keys

| Category | Keys (v1) |
|----------|-----------|
| Sources | `sftp` |
| Sinks | `sftp` |
| Serializers | `json`, `csv`, `xml` |
| Transforms | `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter` |

## Execution Modes

### Batch Mode
- Source yields a finite set of `(bytes, meta)` tuples
- APScheduler drives execution on interval/cron
- Each run produces a `RunResult` with run_id, timestamps, record counts
- Manual trigger via `POST /api/pipelines/{name}/run`

### Stream Mode
- Source is an infinite generator (Kafka consumer, SNMP trap receiver, etc.)
- Runs in a dedicated thread per pipeline
- Stopped only by `POST /api/pipelines/{name}/stop` or daemon shutdown
- Emits continuous metrics rather than discrete RunResults

## Error Handling

Per-pipeline `on_error` policy:
- `continue` — log error, skip record, continue
- `abort` — raise exception, mark run failed, stop
- `retry` — retry entire run up to `retry_count` times with `retry_delay_seconds` backoff

## State Management

State is optional and in-memory by default. Set `TRAM_STATE_DIR` to persist run history to disk as JSON files.

## Security

- XML input uses `defusedxml` to prevent XXE attacks
- Expression evaluation uses `simpleeval` (safe sandbox, no builtins)
- Credentials always from environment variables, never in YAML files
- Container runs as non-root user (uid 1000)

## Adding a New Protocol

See `docs/connectors.md` for the 3-step process.
