# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TRAM (Trishul Real-time Aggregation & Mediation) is a production-ready Python daemon for telecom data pipeline orchestration. It runs as an always-on service that executes pipeline definitions (YAML) on schedules (interval/cron/manual) or continuously (stream mode for Kafka/NATS/webhooks).

**Tech stack:** Python 3.11+, FastAPI, Pydantic v2, APScheduler, SQLAlchemy Core, Bootstrap 5 UI

**Deployment modes** (v1.2.0):
- `standalone` — single StatefulSet pod; all-in-one (scheduler + DB + UI). Default.
- `manager` — Deployment that owns scheduling, DB, and UI; dispatches runs to workers.
- `worker` — StatefulSet pods that execute pipelines and POST results back to manager. No DB, no UI.

## Development Commands

```bash
# Installation
pip install -e ".[dev,manager]"            # base + manager (apscheduler, sqlalchemy) + dev tools
pip install -e ".[dev,manager,snmp,avro,kafka]"   # with specific extras

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

**Runtime state:** SQLite (default) or PostgreSQL via SQLAlchemy Core — manager-only in v1.2.0
- `run_history` — each pipeline execution with metrics (records_in/out/skipped, errors_json, dlq_count, node_id)
- `pipeline_versions` — YAML snapshots for rollback
- `processed_files` — idempotent file tracking when `skip_processed: true`
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
│   ├── controller.py     # PipelineController — lifecycle authority, state machine, restart
│   └── linter.py         # Pipeline lint rules (L001-L005)
├── scheduler/
│   └── scheduler.py      # TramScheduler — APScheduler wrapper + stream thread pool
├── agent/
│   ├── server.py         # WorkerAgent — FastAPI on :8766 (run/stop/status/health)
│   ├── worker_pool.py    # WorkerPool — health polling, least-loaded dispatch, round-robin
│   └── assets.py         # sync_assets() — pull schemas/MIBs from manager before each run
├── connectors/           # 24 sources + 20 sinks (sftp, kafka, rest, s3, opensearch, ...)
├── transforms/           # 21 transforms (rename, cast, filter, aggregate, jmespath, melt, ...)
├── serializers/          # json, csv, xml, avro, parquet, protobuf, msgpack, ndjson
├── persistence/
│   ├── db.py             # TramDB (SQLAlchemy Core)
│   └── file_tracker.py   # ProcessedFileTracker (skip_processed)
├── cluster/              # Legacy: NodeRegistry + ClusterCoordinator (pre-v1.2.0 HA model)
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

helm/                     # Helm chart (standalone + manager+worker mode, TLS, UI service)
├── templates/
│   ├── statefulset.yaml         # standalone StatefulSet (skipped when manager.enabled=true)
│   ├── manager-deployment.yaml  # manager Deployment (only when manager.enabled=true)
│   ├── worker-statefulset.yaml  # worker StatefulSet (only when manager.enabled=true)
│   ├── worker-headless-service.yaml  # headless DNS for workers
│   └── service-ui.yaml          # optional separate UI service
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

### Manager + Worker mode (v1.2.0) — the new cluster mode

Set `TRAM_MODE=manager` on the manager pod and `TRAM_MODE=worker` on worker pods.

- Manager is the sole scheduler and DB writer — SQLite on a RWO PVC is sufficient
- Workers are stateless: receive a run request (HTTP POST), execute the pipeline, POST result back to manager via `TRAM_MANAGER_URL`
- Worker import isolation: `server.py` branches on `TRAM_MODE=worker` **before** importing `create_app`, so workers never import `apscheduler` or `sqlalchemy`
- `tram[manager]` extra must be installed on manager; workers only need `tram[worker,...]`
- **Dispatch**: `WorkerPool.least_loaded()` picks the worker with fewest active runs; round-robin tiebreaker among equals
- **Asset sync**: workers call `sync_assets()` before each run — pulls schemas + MIBs from manager via `GET /api/schemas` and `GET /api/mibs/{name}`; requires `TRAM_API_KEY` on both sides
- **Error propagation**: `RunResult.errors` (per-record skip reasons + transform/sink errors) flows from executor → `_post_run_complete` → `RunCompletePayload.errors` → `on_worker_run_complete` → `run_history.errors_json`
- **Restart**: `POST /api/pipelines/{name}/restart` stops active execution and immediately reschedules; works for batch and stream

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
- `TRAM_MODE` — `standalone` (default) | `manager` | `worker`
- `TRAM_DB_URL` — SQLite (default: `sqlite:////data/tram.db`) or PostgreSQL connection string
- `TRAM_API_KEY` — enables API key auth (X-API-Key header)
- `TRAM_LOG_LEVEL` — DEBUG, INFO, WARNING, ERROR
- `TRAM_AUTH_USERS` — `username:password` pairs for browser auth (comma-separated)
- `TRAM_AUTH_SECRET` — HMAC secret for session tokens
- `TRAM_MANAGER_URL` — manager base URL used by workers for callbacks (e.g. `http://tram:8765`)
- `TRAM_WORKER_REPLICAS` / `TRAM_WORKER_SERVICE` / `TRAM_WORKER_PORT` — manager → worker routing

## Versioning

- **Single source of truth:** `pyproject.toml` `version` field
- Release workflow patches version from git tag
- `tram.__version__` loaded via `importlib.metadata`
- All references (README, CHANGELOG, Helm chart, UI) must match `pyproject.toml`
