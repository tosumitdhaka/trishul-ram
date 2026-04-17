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
  "db_engine": "sqlite",
  "db_path": "/data/tram.db",
  "scheduler": "running",
  "cluster": "manager · 3/3 workers",
  "pipelines_loaded": 3,
  "uptime": "2h 15m 30s"
}
```

`cluster` values: `"manager · N/M workers"` in manager mode, `"standalone"` in standalone mode.

### GET /api/meta
Build and version information.

```json
{"version": "1.3.0", "build_time": "2026-04-17T15:00:00+00:00", "python_version": "3.12.0"}
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

### GET /api/cluster/nodes
Worker pool status (manager mode) or standalone indicator.

**Standalone:**
```json
{"mode": "standalone", "workers": []}
```

**Manager mode:**
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

### GET /api/cluster/streams
Active stream placement and throughput summary.

```json
{
  "mode": "manager",
  "streams": [
    {
      "pipeline_name": "prom-ingest",
      "placement_group_id": "prom-ingest-20260417-ab12",
      "status": "degraded",
      "slots_total": 3,
      "slots_running": 2,
      "slots_stale": 1,
      "records_in_per_sec": 1420.5,
      "records_out_per_sec": 1420.5,
      "bytes_in_per_sec": 901120.0,
      "bytes_out_per_sec": 901120.0,
      "slots": []
    }
  ]
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
Register a new pipeline. Body: raw YAML text (`Content-Type: text/plain` or `application/yaml`) or JSON with `yaml_text` field.

```bash
curl -X POST http://localhost:8765/api/pipelines \
  -H "Content-Type: text/plain" \
  --data-binary @my-pipeline.yaml
```

Response: `201 Created` — pipeline state dict.

Auto-saves a pipeline version to SQLite and auto-starts if `enabled: true` and schedule is not `manual`.

### GET /api/pipelines/{name}
Get pipeline config and live status.

### GET /api/pipelines/{name}/placement
Per-slot placement view for an active broadcast stream.

Returns `404` when the pipeline exists but has no active broadcast placement.

```json
{
  "pipeline_name": "prom-ingest",
  "placement_group_id": "prom-ingest-20260417-ab12",
  "status": "running",
  "slots": [
    {
      "slot_id": "prom-ingest-20260417-ab12-w0",
      "worker_url": "http://trishul-ram-worker-0.trishul-ram-worker.default.svc.cluster.local:8766",
      "current_run_id": "prom-ingest-20260417-ab12-w0",
      "status": "running",
      "stats": {
        "records_in_per_sec": 710.2,
        "records_out_per_sec": 710.2,
        "bytes_in_per_sec": 450560.0,
        "bytes_out_per_sec": 450560.0
      }
    }
  ]
}
```

### PUT /api/pipelines/{name}
Update/replace a registered pipeline's YAML config in-place (v1.0.4). Stops the pipeline, re-registers with the new config, and restarts it if `enabled: true`. Body: raw YAML text (`Content-Type: application/yaml` or `text/plain`).

```bash
curl -X PUT http://localhost:8765/api/pipelines/pm-ingest \
  -H "Content-Type: application/yaml" \
  --data-binary @pm-ingest-updated.yaml
```

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

## Pipeline Dry-Run (v1.0.7)

### POST /api/pipelines/dry-run
Validate and parse a pipeline YAML without registering it. Instantiates all transforms and connectors to catch config errors early.

Body: raw YAML text (`Content-Type: text/plain` or `application/yaml`).

```bash
curl -X POST http://localhost:8765/api/pipelines/dry-run \
  -H "Content-Type: text/plain" \
  --data-binary @my-pipeline.yaml
```

Response `200` (valid):
```json
{"valid": true, "issues": []}
```

Response `200` (invalid — always 200, check `valid` field):
```json
{"valid": false, "issues": ["serializer_in: unknown type 'xtf'"]
}
```

---

## Pipeline Templates (v1.1.0)

### GET /api/templates
List all bundled pipeline YAML templates from the `pipelines/` directory.

```json
[
  {"name": "kafka-to-opensearch", "description": "...", "yaml": "pipeline:\n  name: ..."},
  {"name": "snmp-poll-ifmib-to-influxdb", "description": "...", "yaml": "..."}
]
```

---

## Pipeline Alerts (v1.0.0 / UI v1.1.0)

Alert rules evaluate simpleeval expressions after every batch run and fire webhook or email actions.

### GET /api/pipelines/{name}/alerts
List alert rules for a pipeline.

```json
[
  {
    "name": "high-error-rate",
    "condition": "error_rate > 0.05",
    "action": "webhook",
    "webhook_url": "https://hooks.example.com/alert",
    "cooldown_seconds": 300
  }
]
```

### POST /api/pipelines/{name}/alerts
Add a new alert rule.

```json
{
  "name": "low-output",
  "condition": "records_out < 10",
  "action": "email",
  "email_to": "ops@example.com",
  "subject": "Low output on pm-ingest",
  "cooldown_seconds": 600
}
```

### PUT /api/pipelines/{name}/alerts/{rule_name}
Update an existing alert rule. Body: same structure as POST.

### DELETE /api/pipelines/{name}/alerts/{rule_name}
Delete an alert rule. Returns `204 No Content`.

Alert condition variables: `records_in`, `records_out`, `records_skipped`, `error_rate`, `status`, `failed`, `duration_seconds`.

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
    "error": null,
    "errors": ["Records skipped — no sink wrote successfully (condition filtered all records)"]
  }
]
```

- `error` — top-level fatal error string if the whole run crashed; `null` on success
- `errors` — per-record error/skip-reason messages accumulated during the run; non-empty even on `status: "success"` when individual records were skipped or failed with `on_error: continue`

### GET /api/runs/{run_id}
Get a single run result.

---

## Live Stats (v1.1.0)

### GET /api/stats
Per-pipeline aggregated stats for the last hour (records in/out, error rate, avg duration). Used by the Live Metrics Dashboard.

```json
[
  {
    "pipeline": "pm-ingest",
    "records_in": 45000,
    "records_out": 44823,
    "error_rate": 0.004,
    "avg_duration_seconds": 1.23,
    "run_count": 60
  }
]
```

---

## Authentication (v1.0.0)

When `TRAM_API_KEY` is set (or `apiKey` in Helm values), all `/api/*` requests must include the key:

```bash
# Header (preferred)
curl -H "X-API-Key: mysecret" http://localhost:8765/api/pipelines

# Query param (convenience)
curl "http://localhost:8765/api/pipelines?api_key=mysecret"
```

Exempt paths (always unauthenticated): `/api/health`, `/api/ready`, `/metrics`, `/webhooks/*`, `/api/auth/login`

Returns `401 Unauthorized` when the key is missing or wrong.

## Browser Authentication (v1.0.8)

Set `TRAM_AUTH_USERS` (comma-separated `username:password` pairs) to bootstrap browser login. If `TRAM_DB_URL` is configured, changed passwords are stored in the `user_passwords` table and continue to work even after `TRAM_AUTH_USERS` is removed. Machine clients continue to use `X-API-Key`; browser users get 8-hour session tokens.

### POST /api/auth/login
Authenticate with username and password. Verification prefers the DB-stored hash when present; otherwise it falls back to `TRAM_AUTH_USERS`.

```bash
curl -X POST http://localhost:8765/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "secret"}'
```

Response:
```json
{"token": "eyJ...", "username": "admin"}
```

Use the token as `Authorization: Bearer <token>` on subsequent requests.

### GET /api/auth/me
Returns the currently authenticated user from the Bearer token. Returns `401` if unauthenticated. Works for both env-bootstrapped and DB-backed browser auth.

```json
{"username": "admin"}
```

### POST /api/auth/change-password (v1.1.0)
Change the password for the currently authenticated user. Requires `TRAM_DB_URL`. New passwords are stored as `scrypt$<salt>$<digest>` hashes in the `user_passwords` DB table, persist across restarts, and override `TRAM_AUTH_USERS` for that user.

```bash
curl -X POST http://localhost:8765/api/auth/change-password \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"current_password": "old", "new_password": "new-secure-pass"}'
```

Response `200`:
```json
{"ok": true, "username": "admin"}
```

---

## Connector Test (v1.1.0)

### POST /api/connectors/test
Test connectivity for a single connector config. Returns whether the connection succeeded and any error message.

```bash
curl -X POST http://localhost:8765/api/connectors/test \
  -H "Content-Type: application/json" \
  -d '{"type": "kafka", "bootstrap_servers": "kafka:9092", "topic": "test"}'
```

Response:
```json
{"ok": true, "latency_ms": 12, "error": null}
```

On failure: `{"ok": false, "latency_ms": null, "error": "Connection refused"}`

### POST /api/connectors/test-pipeline
Test all source and sink connectors declared in a pipeline YAML. Returns per-connector results.

```bash
curl -X POST http://localhost:8765/api/connectors/test-pipeline \
  -H "Content-Type: text/plain" \
  --data-binary @my-pipeline.yaml
```

Response:
```json
{
  "source": {"type": "sftp", "ok": true, "latency_ms": 45, "error": null},
  "sinks": [
    {"type": "kafka", "ok": false, "latency_ms": null, "error": "Connection refused"}
  ]
}
```

---

## AI Assist (v1.1.0)

### GET /api/ai/status
Returns AI configuration and availability.

```json
{"available": true, "provider": "anthropic", "model": "claude-sonnet-4-6"}
```

Returns `{"available": false}` when `TRAM_AI_API_KEY` is not set.

### POST /api/ai/suggest
Generate or explain a pipeline YAML using AI.

| Field | Description |
|-------|-------------|
| `mode` | `"generate"` — create a new pipeline from a description; `"explain"` — explain existing YAML |
| `prompt` | Natural language description (for `generate`) or question (for `explain`) |
| `yaml` | Existing pipeline YAML (required for `explain` mode) |

```bash
# Generate
curl -X POST http://localhost:8765/api/ai/suggest \
  -H "Content-Type: application/json" \
  -d '{"mode": "generate", "prompt": "Poll SNMP IF-MIB every 60s and write to InfluxDB"}'

# Explain
curl -X POST http://localhost:8765/api/ai/suggest \
  -H "Content-Type: application/json" \
  -d '{"mode": "explain", "prompt": "What does this do?", "yaml": "pipeline:\n  name: ..."}'
```

Response:
```json
{"result": "pipeline:\n  name: snmp-to-influxdb\n  ..."}
```

Configure via env vars:

| Env Var | Description |
|---------|-------------|
| `TRAM_AI_API_KEY` | API key for the AI provider |
| `TRAM_AI_PROVIDER` | `openai` or `anthropic` (default: `openai`) |
| `TRAM_AI_MODEL` | Model name (default: `gpt-4o` for OpenAI, `claude-sonnet-4-6` for Anthropic) |
| `TRAM_AI_BASE_URL` | Custom base URL (e.g. for Ollama or Azure OpenAI) |

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

## SNMP MIBs (v1.0.3)

Manages compiled pysnmp MIB `.py` files in `TRAM_MIB_DIR` (default `/mibs`).
Standard MIBs (`IF-MIB`, `ENTITY-MIB`, `HOST-RESOURCES-MIB`, `IP-MIB`, `TCP-MIB`, `UDP-MIB`, `IANAifType-MIB`) are pre-compiled in the Docker image.

### GET /api/mibs
List all compiled MIB modules in `TRAM_MIB_DIR`.

```json
[
  {"name": "IF-MIB", "file": "IF-MIB.py", "size_bytes": 14823},
  {"name": "ENTITY-MIB", "file": "ENTITY-MIB.py", "size_bytes": 22104}
]
```

### POST /api/mibs/upload
Upload a raw `.mib` text file and compile it. Requires `tram[mib]`; returns `501` if not installed.

```bash
curl -X POST http://localhost:8765/api/mibs/upload \
  -F "file=@MY-CUSTOM-MIB.mib"
```

Response:
```json
{"compiled": ["MY-CUSTOM-MIB"], "mib_dir": "/mibs", "results": {"MY-CUSTOM-MIB": "compiled"}}
```

### POST /api/mibs/download
Download and compile MIB modules by name from `mibs.pysnmp.com`. Requires internet access and `tram[mib]`.

```bash
curl -X POST http://localhost:8765/api/mibs/download \
  -H "Content-Type: application/json" \
  -d '{"names": ["CISCO-ENTITY-FRU-CONTROL-MIB", "CISCO-TC-MIB"]}'
```

### DELETE /api/mibs/{name}
Delete a compiled MIB module from `TRAM_MIB_DIR`.

```bash
curl -X DELETE http://localhost:8765/api/mibs/MY-CUSTOM-MIB
```

---

## Schemas (v1.0.3)

Manages serialization schema files (`.proto`, `.avsc`, `.json`, `.xsd`, `.yaml`, `.yml`)
in `TRAM_SCHEMA_DIR` (default `/schemas`). No compilation — files are stored as-is.
Pipeline executors compile or read them at run time.

### GET /api/schemas
List all schema files under `TRAM_SCHEMA_DIR` recursively.

```json
[
  {
    "path": "cisco/GenericRecord.proto",
    "type": "protobuf",
    "size_bytes": 3421,
    "schema_file": "/schemas/cisco/GenericRecord.proto"
  },
  {
    "path": "events.avsc",
    "type": "avro",
    "size_bytes": 892,
    "schema_file": "/schemas/events.avsc"
  }
]
```

`schema_file` is the absolute path ready to paste into a pipeline `schema_file:` field.

`type` is inferred from the extension: `protobuf`, `avro`, `json`, `xml`, `yaml`, `other`.

### GET /api/schemas/{filepath}
Return the raw text content of a schema file. `filepath` is relative to `TRAM_SCHEMA_DIR`.

```bash
curl http://localhost:8765/api/schemas/cisco/GenericRecord.proto
```

Returns `404` if not found, `400` if the path escapes `TRAM_SCHEMA_DIR`.

### POST /api/schemas/upload
Upload a schema file. Accepts `.proto`, `.avsc`, `.json`, `.xsd`, `.yaml`, `.yml`.
Returns `400` for other extensions.

| Query param | Description |
|-------------|-------------|
| `subdir` | Optional subdirectory within `TRAM_SCHEMA_DIR` (e.g. `cisco`). Must not contain `..`. |

Upload all Cisco EMS proto files to a shared subdirectory:

```bash
for f in *.proto; do
  curl -F "file=@$f" \
    "http://localhost:8765/api/schemas/upload?subdir=cisco"
done
```

Response:
```json
{
  "path": "cisco/GenericRecord.proto",
  "type": "protobuf",
  "size_bytes": 3421,
  "schema_file": "/schemas/cisco/GenericRecord.proto",
  "schema_dir": "/schemas"
}
```

### DELETE /api/schemas/{filepath}
Delete a schema file. `filepath` is relative to `TRAM_SCHEMA_DIR`.

```bash
curl -X DELETE http://localhost:8765/api/schemas/cisco/GenericRecord.proto
```

Returns `404` if not found, `400` on path-traversal attempt.

---

## Schema Registry Proxy (v1.0.4)

Transparent reverse proxy to an external Confluent-compatible schema registry (e.g. Confluent Schema Registry, Apicurio Registry). Enabled by setting `TRAM_SCHEMA_REGISTRY_URL`.

All HTTP methods (`GET`, `POST`, `PUT`, `DELETE`, `PATCH`) are proxied. Request headers, query params, and body are forwarded as-is. This lets UI tools and serializer clients reach the external registry through a single origin (TRAM).

```bash
# List subjects
curl http://localhost:8765/api/schemas/registry/subjects

# Get latest schema for a subject
curl http://localhost:8765/api/schemas/registry/subjects/device-event-value/versions/latest

# Register a new schema version
curl -X POST http://localhost:8765/api/schemas/registry/subjects/device-event-value/versions \
  -H "Content-Type: application/vnd.schemaregistry.v1+json" \
  -d '{"schema": "{\"type\":\"record\",\"name\":\"DeviceEvent\",\"fields\":[]}"}'
```

Returns `503 Service Unavailable` when `TRAM_SCHEMA_REGISTRY_URL` is not set.
Returns `502 Bad Gateway` when the upstream registry is unreachable.

**Configuration:**

| Env Var | Description |
|---------|-------------|
| `TRAM_SCHEMA_REGISTRY_URL` | Base URL of the external registry (e.g. `http://schema-registry:8081`) |

**Serializer auto-fallback** — when `TRAM_SCHEMA_REGISTRY_URL` is set, Avro and Protobuf serializers automatically use it as their registry URL without requiring `schema_registry_url:` in pipeline YAML. Pipeline-level `schema_registry_url:` overrides the env default per-pipeline.

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
