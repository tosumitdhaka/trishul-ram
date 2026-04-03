# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TRAM (Trishul Real-time Aggregation & Mediation) is a production-ready Python daemon for telecom data pipeline orchestration. It runs as an always-on service that executes pipeline definitions (YAML) on schedules (interval/cron/manual) or continuously (stream mode for Kafka/NATS/webhooks).

**Tech stack:** Python 3.11+, FastAPI, Pydantic v2, APScheduler, SQLAlchemy Core, Bootstrap 5 UI

## Development Commands

```bash
# Installation
pip install -e ".[dev]"                    # base + dev tools
pip install -e ".[dev,snmp,avro,kafka]"   # with specific extras

# Testing
pytest tests/unit/                        # 651 unit tests, no network
pytest tests/integration/                 # 44 integration tests
pytest tests/ --cov=tram --cov-report=term-missing  # with coverage
pytest tests/unit/test_foo.py::test_bar  # single test

# Linting
ruff check .                              # lint entire codebase
ruff check --fix .                        # auto-fix issues

# Running locally
tram version                              # verify install
tram plugins                              # list registered plugins
tram validate pipelines/foo.yaml          # validate + lint pipeline
tram run pipelines/foo.yaml --dry-run     # test without side effects
tram daemon &                             # start daemon (localhost:8765)
open http://localhost:8765/ui/            # web UI

# Pipeline management
tram pipeline list
tram pipeline add pipelines/foo.yaml
tram pipeline run foo
tram pipeline history foo

# Docker
docker compose up                         # starts daemon + PostgreSQL
curl http://localhost:8765/api/ready
```

## Core Architecture

### Plugin System (Decorator-based registration)

All sources, sinks, transforms, and serializers are plugins that self-register via decorators at import time:

```python
@register_source("kafka")
class KafkaSource(BaseSource):
    def __init__(self, config: dict): ...
    def read(self) -> Iterator[tuple[bytes, dict]]: ...
    def close(self): ...
```

**Key files:**
- `tram/registry/registry.py` — `@register_*` decorators + lookup functions
- `tram/interfaces/` — `BaseSource`, `BaseSink`, `BaseTransform`, `BaseSerializer`
- `tram/connectors/__init__.py`, `tram/transforms/__init__.py`, `tram/serializers/__init__.py` — import all plugins to trigger registration

**To add a new plugin:** create the class with decorator, add to the appropriate `__init__.py` imports (no core engine changes needed).

### Execution Modes

TRAM has two distinct execution paths:

1. **Batch mode** (`batch_run()` in `tram/pipeline/executor.py`)
   - Finite execution: read all data, transform, write, exit
   - Scheduled via APScheduler (interval/cron) or triggered manually
   - Used for: SFTP polls, SQL queries, local file reads, REST GET

2. **Stream mode** (`stream_run()` in `tram/pipeline/executor.py`)
   - Infinite loop: consume messages continuously from queue/topic
   - Runs in background thread managed by scheduler
   - Used for: Kafka, NATS, webhook listeners, MQTT, AMQP, WebSocket

**Execution flow (both modes):**
```
Source → bytes → Deserializer → list[dict] → Global Transforms (per-record)
                                                   ↓
                                    ┌──────────────┴──────────────┐
                                    │  For each sink:              │
                                    │  • condition filter          │
                                    │  • per-sink transforms       │
                                    │  • per-sink serializer_out   │
                                    │  • sink.write()              │
                                    └─────────────────────────────┘
                                                   ↓
                          [any error at any stage] → DLQ sink (JSON envelope)
```

### Data Model

**Pipeline config:** Pydantic v2 models in `tram/models/pipeline.py`
- All `${VAR}` / `${VAR:-default}` placeholders resolved from environment at load time
- Validation happens before scheduling (see `tram/pipeline/loader.py`)

**Runtime state:** SQLite (dev) or PostgreSQL (prod) via SQLAlchemy Core
- `run_history` — each pipeline execution with metrics (records_in/out/errors/dlq_count, node_id)
- `pipeline_versions` — YAML snapshots for rollback
- `processed_files` — idempotent file tracking when `skip_processed: true`
- `node_registry` — cluster membership (heartbeat, last_seen)
- `alert_state` — cooldown tracking for alert rules
- `user_passwords` — bcrypt hashed passwords for browser auth

## Directory Structure

```
tram/
├── core/                 # exceptions, logging, config (TRAM_* env vars), context
├── interfaces/           # BaseSource, BaseSink, BaseTransform, BaseSerializer
├── registry/             # @register_* decorators + get_* lookup functions
├── models/               # Pydantic v2 pipeline schema (ScheduleConfig, SourceConfig unions, etc.)
├── pipeline/
│   ├── loader.py         # YAML → PipelineConfig (env substitution, validation)
│   ├── executor.py       # batch_run(), stream_run(), dry_run() — core execution logic
│   ├── manager.py        # PipelineManager (add/remove/run/pause/list)
│   └── linter.py         # Pipeline lint rules (L001-L005)
├── scheduler/
│   └── scheduler.py      # TramScheduler — APScheduler wrapper + stream thread pool
├── connectors/           # 23 sources + 19 sinks (sftp, kafka, rest, s3, opensearch, ...)
├── transforms/           # 20 transforms (rename, cast, filter, aggregate, jmespath, ...)
├── serializers/          # json, csv, xml, avro, parquet, protobuf, msgpack, ndjson
├── persistence/
│   ├── database.py       # TramDB (SQLAlchemy Core)
│   └── file_tracker.py   # ProcessedFileTracker (skip_processed)
├── cluster/              # NodeRegistry + ClusterCoordinator (consistent hashing, rebalance)
├── alerts/               # AlertEvaluator (webhook + email, cooldown logic)
├── metrics/              # Prometheus metrics (tram_records_*, tram_kafka_consumer_lag, etc.)
├── telemetry/            # OpenTelemetry tracing (batch_run, sink writes)
├── schema_registry/      # Confluent/Apicurio client + magic-byte helpers (Avro/Protobuf)
├── api/
│   ├── middleware.py     # API key auth, rate limiting, CORS
│   └── routers/          # /pipelines, /runs, /cluster, /mibs, /schemas, /webhooks, /stats, /ai, /auth
├── daemon/
│   └── server.py         # TramServer (FastAPI + TramScheduler + optional TLS)
└── cli/
    └── main.py           # Typer CLI (pipeline, mib, validate commands)

tram-ui/                  # Bootstrap 5 SPA (built to /ui, served by FastAPI StaticFiles)
├── src/
│   ├── api.js            # REST client wrapper
│   ├── router.js         # client-side routing (hash-based)
│   └── pages/            # dashboard.js, pipelines.js, detail.js, editor.js, ...
└── dist/                 # Vite build output (copied into Docker image)

helm/                     # Helm chart (StatefulSet, TLS, cluster mode, UI service)
├── templates/
│   ├── statefulset.yaml  # always StatefulSet (replicaCount=1 standalone, N for cluster)
│   └── service-ui.yaml   # optional separate UI service
└── values.yaml           # default config

tests/
├── unit/                 # 651 tests, no external services
└── integration/          # 44 tests (SFTP, Kafka, schema registry mocks)
```

## Key Patterns & Conventions

### Adding a new connector

1. Create `tram/connectors/sources/my_source.py` or `tram/connectors/sinks/my_sink.py`
2. Define config model in `tram/models/pipeline.py` (e.g., `MySourceConfig`)
3. Implement class with `@register_source("my_source")` or `@register_sink("my_sink")`
4. Add import to `tram/connectors/__init__.py` to trigger registration
5. Add optional dependency to `pyproject.toml` if needed (e.g., `my_source = ["my-lib>=1.0"]`)

### Transform execution

Transforms are applied **per-record**, not per-chunk. A transform raising an exception for one record does not abort the entire batch — that record is routed to DLQ and others continue.

### Condition filtering

Uses `simpleeval` for safe expression evaluation. Available in conditions:
- All record fields as variables (e.g., `rx_mbps > 500`)
- Standard functions: `round`, `abs`, `int`, `float`, `str`, `len`, `min`, `max`
- Python operators: `+`, `-`, `*`, `/`, `>`, `<`, `>=`, `<=`, `==`, `!=`, `and`, `or`, `not`

### DLQ (Dead Letter Queue)

Every error (parse, transform, write) is wrapped in a JSON envelope and sent to the DLQ sink:
```json
{
  "error": "Error message",
  "stage": "transform|write|parse",
  "record": {...},
  "timestamp": "2024-01-01T00:00:00Z"
}
```

### Cluster mode

When `TRAM_CLUSTER_MODE=true` + external PostgreSQL:
- Each node heartbeats to `node_registry` table
- `ClusterCoordinator` assigns pipelines via consistent hashing
- Rebalance thread redistributes pipelines when nodes join/leave
- Non-owner nodes set pipeline `status=scheduled` but don't execute

### Pipeline versioning

Every update saves the previous YAML to `pipeline_versions` table. Rollback loads the specified version and re-registers the pipeline.

### Serializer resolution

1. Per-sink `serializer_out` (if specified)
2. Global pipeline `serializer_out` (if specified)
3. Default: `json`

## Testing Strategy

- **Unit tests** (`tests/unit/`) mock all I/O — connectors, database, HTTP calls
- **Integration tests** (`tests/integration/`) use real local services (test SFTP server, embedded Kafka)
- Coverage target: 60% minimum (CI enforces this)
- Test file naming: `test_<module>.py` mirrors source structure

## Common Development Pitfalls

1. **Forgetting to import new plugins in `__init__.py`** — decorator never fires, plugin not registered
2. **Using `git add -A` to stage changes** — can accidentally commit `.env` or large test files
3. **Not reading existing pipelines before suggesting changes** — always validate YAML structure
4. **Modifying core engine for new connector types** — extend via plugins instead
5. **Testing DLQ logic without setting `dlq.type`** — DLQ is optional; set it explicitly in test pipelines

## Environment Variables

All runtime config via `TRAM_*` env vars (see `.env.example`). Critical ones:
- `TRAM_DB_URL` — SQLite (default) or PostgreSQL connection string
- `TRAM_API_KEY` — enables API key auth (X-API-Key header)
- `TRAM_CLUSTER_MODE` — enables cluster coordination (requires PostgreSQL)
- `TRAM_LOG_LEVEL` — DEBUG, INFO, WARNING, ERROR
- `TRAM_AUTH_USERS` — bcrypt password hashes for browser auth (user:hash,user2:hash2)
- `TRAM_AUTH_SECRET` — HMAC secret for session tokens

## Versioning

- **Single source of truth:** `pyproject.toml` `version` field
- Release workflow patches version from git tag
- `tram.__version__` loaded via `importlib.metadata`
- All references (README, CHANGELOG, Helm chart, UI) must match `pyproject.toml`
