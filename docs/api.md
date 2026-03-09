# TRAM REST API Reference

Base URL: `http://localhost:8765` (configurable via `TRAM_HOST`/`TRAM_PORT`)

All responses are JSON unless noted. Errors return `{"detail": "message"}`.

---

## Health & Meta

### GET /api/health
Liveness probe. Returns 200 immediately if daemon is running.

```json
{"status": "ok"}
```

### GET /api/ready
Readiness probe. Returns 200 once startup is complete, 503 if DB or scheduler is unavailable.

```json
{
  "status": "ready",
  "db": "ok",
  "scheduler": "running",
  "cluster": "disabled",
  "pipelines_loaded": 3
}
```

### GET /api/meta
Build and version information.

```json
{"version": "1.0.2", "python_version": "3.12.0"}
```

### GET /api/plugins
All registered plugin keys by category.

```json
{
  "sources": ["kafka", "webhook", "websocket", "..."],
  "sinks": ["kafka", "opensearch", "elasticsearch", "..."],
  "serializers": ["json", "csv", "xml", "avro", "parquet", "msgpack", "protobuf"],
  "transforms": ["rename", "cast", "filter", "..."]
}
```

---

## Pipelines

### GET /api/pipelines
List all registered pipelines with live status.

```json
[
  {
    "name": "pm-ingest",
    "enabled": true,
    "status": "running",
    "schedule_type": "interval",
    "last_run": "2026-03-03T12:00:00Z",
    "last_run_status": "success"
  }
]
```

### POST /api/pipelines
Register a new pipeline. Body: raw YAML text (`Content-Type: text/plain`) or JSON with `yaml_text` field.

```bash
curl -X POST http://localhost:8765/api/pipelines \
  -H "Content-Type: text/plain" \
  --data-binary @my-pipeline.yaml
```

Response: `201 Created` — pipeline state dict.

Auto-saves a pipeline version to SQLite and auto-starts if `enabled: true` and schedule is not `manual`.

### GET /api/pipelines/{name}
Get pipeline config and live status.

### DELETE /api/pipelines/{name}
Deregister pipeline (stops it first). Returns `204 No Content`.

### POST /api/pipelines/{name}/start
Start scheduling or stream execution.

### POST /api/pipelines/{name}/stop
Stop pipeline gracefully.

### POST /api/pipelines/{name}/run
Trigger one immediate batch run (not valid for stream pipelines).

```json
{"name": "pm-ingest", "status": "triggered"}
```

### POST /api/pipelines/reload
Re-scan `TRAM_PIPELINE_DIR`, reload all YAML files.

```json
{"reloaded": 3, "total": 3}
```

---

## Pipeline Versioning (v0.5.0)

### GET /api/pipelines/{name}/versions
List all saved versions for a pipeline (requires SQLite persistence).

```json
[
  {"id": 2, "name": "pm-ingest", "version": 2, "created_at": "2026-03-03T12:05:00Z", "is_active": 1},
  {"id": 1, "name": "pm-ingest", "version": 1, "created_at": "2026-03-03T12:00:00Z", "is_active": 0}
]
```

### POST /api/pipelines/{name}/rollback?version=N
Restore pipeline to a previously saved version. Stops if running, reloads config, restarts if enabled.

```json
{
  "name": "pm-ingest",
  "status": "stopped",
  "rolled_back_to_version": 1
}
```

---

## Runs

### GET /api/runs
Run history. Query params:

| Param | Default | Description |
|-------|---------|-------------|
| `pipeline` | — | Filter by pipeline name |
| `limit` | 100 | Max records to return |
| `status` | — | Filter: `success` \| `failed` \| `partial` |
| `offset` | 0 | Pagination offset (v0.7.0) |
| `from_dt` | — | ISO8601 lower bound on `started_at` (v0.7.0) |
| `format` | — | Set to `csv` to get `text/csv` export (v1.0.0) |

With SQLite/DB persistence, run history survives daemon restarts.

```json
[
  {
    "run_id": "abc12345",
    "pipeline": "pm-ingest",
    "node_id": "tram-0",
    "status": "success",
    "started_at": "2026-03-03T12:00:00Z",
    "finished_at": "2026-03-03T12:00:05Z",
    "records_in": 1500,
    "records_out": 1487,
    "records_skipped": 13,
    "dlq_count": 0,
    "error": null
  }
]
```

### GET /api/runs/{run_id}
Get a single run result.

---

## Authentication (v1.0.0)

When `TRAM_API_KEY` is set (or `apiKey` in Helm values), all `/api/*` requests must include the key:

```bash
# Header (preferred)
curl -H "X-API-Key: mysecret" http://localhost:8765/api/pipelines

# Query param (convenience)
curl "http://localhost:8765/api/pipelines?api_key=mysecret"
```

Exempt paths (always unauthenticated): `/api/health`, `/api/ready`, `/metrics`, `/webhooks/*`

Returns `401 Unauthorized` when the key is missing or wrong.

---

## Webhooks (v0.5.0)

### POST /webhooks/{path}
Forward a raw HTTP POST body to a registered `webhook` source pipeline.

- Returns `202 Accepted` if queued successfully
- Returns `404 Not Found` if no source is registered for `{path}`
- Returns `401 Unauthorized` if the source has a `secret` configured and the `Authorization: Bearer <token>` header doesn't match
- Returns `503 Service Unavailable` if the queue is full

```bash
curl -X POST http://localhost:8765/webhooks/my-events \
  -H "Content-Type: application/json" \
  -d '{"ne_id": "node-1", "severity": 2}'
```

To enable: add a pipeline with `source.type: webhook` and `source.path: my-events`.

---

## Metrics (v0.5.0)

### GET /metrics
Prometheus metrics in text exposition format (`text/plain; version=0.0.4`).

Returns `503` with JSON error if `prometheus_client` is not installed.

```
# HELP tram_records_in_total Total records read from source
# TYPE tram_records_in_total counter
tram_records_in_total{pipeline="pm-ingest"} 45000.0

# HELP tram_records_out_total Total records written to sink
# TYPE tram_records_out_total counter
tram_records_out_total{pipeline="pm-ingest"} 44823.0

# HELP tram_records_skipped_total Total records skipped
# TYPE tram_records_skipped_total counter
tram_records_skipped_total{pipeline="pm-ingest"} 177.0

# HELP tram_errors_total Total processing errors
# TYPE tram_errors_total counter
tram_errors_total{pipeline="pm-ingest"} 0.0

# HELP tram_chunk_duration_seconds Time spent processing one chunk
# TYPE tram_chunk_duration_seconds histogram
tram_chunk_duration_seconds_bucket{le="0.01",pipeline="pm-ingest"} 120.0
...
```

---

## Daemon

### GET /api/daemon/status
Scheduler state, active streams, next scheduled runs.

### POST /api/daemon/stop
Graceful shutdown.

---

## Error Responses

| Code | Meaning |
|------|---------|
| 400 | Invalid pipeline YAML or config |
| 401 | Missing/invalid `X-API-Key` header or query param (v1.0.0); or missing/invalid `Authorization: Bearer` for webhook secret |
| 404 | Pipeline, run, or webhook path not found |
| 409 | Pipeline already registered |
| 422 | Pydantic validation error |
| 429 | Rate limit exceeded (v1.0.0) — retry after the `TRAM_RATE_LIMIT_WINDOW` window resets |
| 503 | DB unavailable (readiness check); `prometheus_client` not installed (`/metrics`) |
