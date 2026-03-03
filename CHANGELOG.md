# Changelog

All notable changes to TRAM are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [0.1.0] — 2026-03-03

### Added

**Core**
- `tram.core.exceptions` — `TramError` hierarchy: `SourceError`, `SinkError`, `TransformError`, `SerializerError`, `ConfigError`, `PluginNotFoundError`, `PipelineAlreadyExistsError`, `PipelineNotFoundError`
- `tram.core.context` — `PipelineRunContext` (mutable, per-run state) and `RunResult` (immutable result) dataclasses with `RunStatus` enum
- `tram.core.config` — `AppConfig` loaded entirely from environment variables (12-factor)
- `tram.core.log_config` — `setup_logging()` with JSON-structured output (`JsonFormatter`) for container log aggregators; text format for local dev

**Plugin Interfaces**
- `BaseSource` — ABC: `read() → Iterator[(bytes, meta)]`
- `BaseSink` — ABC: `write(bytes, meta)`
- `BaseTransform` — ABC: `apply(list[dict]) → list[dict]`
- `BaseSerializer` — ABC: `parse(bytes) → list[dict]`, `serialize(list[dict]) → bytes`

**Plugin Registry**
- `@register_source/sink/transform/serializer` class decorators
- `get_source/sink/transform/serializer(key)` lookup with `PluginNotFoundError` on miss
- `list_plugins()` — returns all registered keys by category

**Pydantic Models**
- `PipelineConfig` — full pipeline schema with discriminated unions for `SourceConfig`, `SinkConfig`, `SerializerConfig`, `TransformConfig`
- `ScheduleConfig` — `interval` / `cron` / `stream` / `manual` with validation
- Pipeline name validation (slug: alphanumeric + hyphens/underscores)

**Serializers**
- `json` — parse JSON array or object; serialize with optional indent
- `csv` — parse with header/delimiter/quotechar options; handles UTF-8 BOM
- `xml` — parse with `defusedxml` (XXE-safe); serialize with `lxml`

**Transforms**
- `rename` — rename fields by mapping
- `cast` — convert to `str / int / float / bool / datetime`; bool accepts `true/1/yes/on`
- `add_field` — computed fields via `simpleeval` safe sandbox; supports `round`, `abs`, ternary, etc.
- `drop` — remove fields by name list
- `value_map` — lookup-table value replacement with optional default
- `filter` — row filter using `simpleeval` condition expressions

**Connectors**
- `sftp` source — paramiko; `listdir` + `read` + optional `move_after_read` or `delete_after_read`
- `sftp` sink — paramiko; writes with `filename_template` tokens: `{pipeline}`, `{timestamp}`, `{source_filename}`

**Pipeline Engine**
- `loader.py` — YAML loader with `${VAR:-default}` env substitution; `scan_pipeline_dir()` for bulk loading
- `executor.py` — `batch_run()` (finite, returns `RunResult`), `stream_run()` (infinite, stop event), `dry_run()` (no I/O validation)
- `manager.py` — in-memory registry of `PipelineState`; run history (last 500 per pipeline); thread-safe CRUD

**Scheduler**
- `TramScheduler` — APScheduler `BackgroundScheduler` for interval/cron pipelines; dedicated threads for stream pipelines
- `start/stop_pipeline()` and `trigger_run()` for runtime management
- `get_status()` — returns scheduler state, active streams, next scheduled runs

**REST API** (FastAPI, port 8765)
- `GET /api/health` — liveness probe
- `GET /api/ready` — readiness probe with pipeline count
- `GET /api/meta` — version + build info
- `GET /api/plugins` — registered plugin keys by category
- `GET /api/pipelines` — list all pipelines + status
- `POST /api/pipelines` — register from YAML body
- `GET/DELETE /api/pipelines/{name}` — get or deregister
- `POST /api/pipelines/{name}/start|stop|run` — lifecycle control
- `POST /api/pipelines/reload` — rescan `TRAM_PIPELINE_DIR`
- `GET /api/runs` — run history (filterable by pipeline, status, limit)
- `GET /api/runs/{run_id}` — single run result
- `GET /api/daemon/status` — scheduler state
- `POST /api/daemon/stop` — graceful shutdown

**Daemon**
- `TramServer` — starts `TramScheduler` + uvicorn in one process; lifespan loads pipelines and starts scheduler on startup, drains on shutdown

**CLI** (Typer)
- Direct: `tram version`, `tram plugins`, `tram validate <file>`, `tram run <file> [--dry-run]`, `tram daemon`
- Proxy: `tram pipeline list/add/remove/start/stop/run/status/reload`
- Proxy: `tram runs [--pipeline] [--limit]`, `tram runs get <run_id>`
- All proxy commands read `TRAM_API_URL` env var (default `http://localhost:8765`)

**Container**
- Multi-stage `Dockerfile` — builder stage produces wheel; runtime stage installs as non-root user `tram` (uid 1000)
- `docker-compose.yml` — volume-mounted pipeline dir, persistent state volume, full env var passthrough
- `.env.example` — all supported env vars with defaults and comments

**Examples**
- `pipelines/example_sftp_to_sftp.yaml` — full PM pipeline (CSV→transforms→JSON, interval schedule)
- `pipelines/minimal.yaml` — minimal JSON pass-through, manual trigger

**Documentation**
- `docs/architecture.md` — design, data flow, component map, execution modes, error handling
- `docs/connectors.md` — 3-step guide to adding new connectors; full interface examples
- `docs/transforms.md` — all transform options, expression syntax, ordering tips
- `docs/api.md` — full REST API reference with request/response examples
- `docs/deployment.md` — Docker, Kubernetes, env vars, logging, security, scaling

**Tests**
- 66 unit tests (`tests/unit/`) — registry, loader, all transforms, all serializers, executor (batch, empty, error handling, transform chain)
- 3 integration tests (`tests/integration/`) — full SFTP pipeline with mocked paramiko; transform chain correctness; empty source handling
- Total: **69 tests, all passing**
