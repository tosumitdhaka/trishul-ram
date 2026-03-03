# TRAM REST API Reference

Base URL: `http://localhost:8765` (configurable via `TRAM_HOST`/`TRAM_PORT`)

All responses are JSON. Errors return `{"detail": "message"}`.

## Health & Meta

### GET /api/health
Liveness probe. Returns 200 immediately if daemon is running.

```json
{"status": "ok"}
```

### GET /api/ready
Readiness probe. Returns 200 if daemon is ready to process pipelines. Returns 503 if still starting.

```json
{"status": "ready", "pipelines_loaded": 3}
```

### GET /api/meta
Build and version information.

```json
{
  "version": "0.1.0",
  "build_time": "2024-01-01T00:00:00Z",
  "python_version": "3.11.0"
}
```

### GET /api/plugins
All registered plugin keys by category.

```json
{
  "sources": ["sftp"],
  "sinks": ["sftp"],
  "serializers": ["json", "csv", "xml"],
  "transforms": ["rename", "cast", "add_field", "drop", "value_map", "filter"]
}
```

## Pipelines

### GET /api/pipelines
List all registered pipelines with their current status.

```json
[
  {
    "name": "pm-ingest",
    "enabled": true,
    "status": "running",
    "schedule_type": "interval",
    "next_run": "2024-01-01T00:05:00Z",
    "last_run": "2024-01-01T00:00:00Z",
    "last_run_status": "success"
  }
]
```

### POST /api/pipelines
Register a new pipeline. Body: raw YAML text (Content-Type: text/plain) or JSON config.

```bash
curl -X POST http://localhost:8765/api/pipelines \
  -H "Content-Type: text/plain" \
  --data-binary @my-pipeline.yaml
```

Response: `201 Created` with pipeline config.

### GET /api/pipelines/{name}
Get pipeline config and live status.

### DELETE /api/pipelines/{name}
Deregister pipeline (stops it first). Returns `204 No Content`.

### POST /api/pipelines/{name}/start
Start scheduling or stream execution.

### POST /api/pipelines/{name}/stop
Stop pipeline gracefully (drains in-flight records).

### POST /api/pipelines/{name}/run
Trigger one immediate run (batch mode only). Returns run_id.

```json
{"run_id": "pm-ingest-20240101-000000-abc123"}
```

### POST /api/pipelines/reload
Re-scan `TRAM_PIPELINE_DIR`, reload all YAML files. Useful when pipeline dir is updated.

## Runs

### GET /api/runs
Run history. Query params: `pipeline` (filter by name), `limit` (default 100), `status` (success/failed/running).

```json
[
  {
    "run_id": "pm-ingest-20240101-000000-abc123",
    "pipeline": "pm-ingest",
    "status": "success",
    "started_at": "2024-01-01T00:00:00Z",
    "finished_at": "2024-01-01T00:00:05Z",
    "records_in": 1500,
    "records_out": 1487,
    "records_skipped": 13,
    "error": null
  }
]
```

### GET /api/runs/{run_id}
Get single run result.

## Daemon

### GET /api/daemon/status
Scheduler state, active streams, next scheduled runs.

```json
{
  "scheduler_running": true,
  "active_streams": ["fm-stream"],
  "scheduled_jobs": [
    {
      "pipeline": "pm-ingest",
      "next_run": "2024-01-01T00:05:00Z"
    }
  ]
}
```

### POST /api/daemon/stop
Graceful shutdown. Drains in-flight pipelines, then exits.

## Error Responses

| Code | Meaning |
|------|---------|
| 400 | Invalid pipeline YAML or config |
| 404 | Pipeline or run not found |
| 409 | Pipeline already registered (use reload) |
| 422 | Validation error (Pydantic) |
| 503 | Daemon not ready |
