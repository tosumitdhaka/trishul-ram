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
{"version": "1.4.0", "build_time": "2026-05-01T12:00:00+00:00", "python_version": "3.13.0"}
```

### GET /api/plugins
All registered plugin keys by category, plus per-plugin UI metadata and the
schema identity.

```json
{
  "sources": ["kafka", "webhook", "websocket", "..."],
  "sinks": ["kafka", "opensearch", "elasticsearch", "..."],
  "serializers": ["json", "csv", "xml", "avro", "parquet", "msgpack", "protobuf"],
  "transforms": ["rename", "cast", "filter", "..."],
  "details": { "sources": [...], "sinks": [...], "serializers": [...], "transforms": [...] },
  "schema_mismatch": { "sources": {}, "sinks": {}, "serializers": {}, "transforms": {} },
  "schema_version": "eecd1712ea4d"
}
```

- `details` — per-plugin descriptors (`name`, `class_name`, `summary`,
  `required_fields`, `common_optional_fields`, `fields`, `field_count`).
- `schema_mismatch` — registry↔union cross-check per category (Issue #24):
  `union_only` lists types in the Pydantic union with no registered class
  (validation passes, runtime `PluginNotFoundError`); `registry_only` lists
  registered types missing from the union (fails Pydantic validation, AI
  context renders them as "(no schema available)"). `{}` means in sync.
- `schema_version` — first 12 hex chars of the sha256 over the canonical JSON
  of `SCHEMA_FIELDS`; the identity token for the schema this payload was
  derived from (equality check only — not a semantic version).

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
      "target_count": "all",
      "started_at": "2026-04-17T15:00:00+00:00",
      "slot_count": 3,
      "active_slots": 2,
      "records_in": 7102,
      "records_out": 7102,
      "records_skipped": 0,
      "dlq_count": 0,
      "error_count": 1,
      "bytes_in": 4505600,
      "bytes_out": 4505600,
      "records_in_per_sec": 1420.4,
      "records_out_per_sec": 1420.4,
      "bytes_in_per_sec": 901120.0,
      "bytes_out_per_sec": 901120.0,
      "slots": [
        {
          "worker_index": 0,
          "worker_id": "tram-worker-0",
          "worker_url": "http://trishul-ram-worker-0.trishul-ram-worker.default.svc.cluster.local:8766",
          "run_id_prefix": "prom-ingest-20260417-ab12-w0",
          "current_run_id": "prom-ingest-20260417-ab12-w0-r1",
          "status": "running",
          "restart_count": 1,
          "stats": {
            "schedule_type": "stream",
            "timestamp": "2026-04-17T15:00:05+00:00",
            "uptime_seconds": 5.0,
            "records_in": 3551,
            "records_out": 3551,
            "records_skipped": 0,
            "dlq_count": 0,
            "error_count": 0,
            "bytes_in": 2252800,
            "bytes_out": 2252800,
            "errors_last_window": [],
            "stale": false,
            "records_in_per_sec": 710.2,
            "records_out_per_sec": 710.2,
            "bytes_in_per_sec": 450560.0,
            "bytes_out_per_sec": 450560.0
          }
        },
        {
          "worker_index": 1,
          "worker_id": "tram-worker-1",
          "worker_url": "http://trishul-ram-worker-1.trishul-ram-worker.default.svc.cluster.local:8766",
          "run_id_prefix": "prom-ingest-20260417-ab12-w1",
          "current_run_id": "prom-ingest-20260417-ab12-w1-r0",
          "status": "stale",
          "restart_count": 0,
          "stats": {
            "schedule_type": "stream",
            "timestamp": "2026-04-17T14:58:00+00:00",
            "uptime_seconds": 5.0,
            "records_in": 3551,
            "records_out": 3551,
            "records_skipped": 0,
            "dlq_count": 0,
            "error_count": 1,
            "bytes_in": 2252800,
            "bytes_out": 2252800,
            "errors_last_window": [
              "timeout talking to sink"
            ],
            "stale": true,
            "records_in_per_sec": 0.0,
            "records_out_per_sec": 0.0,
            "bytes_in_per_sec": 0.0,
            "bytes_out_per_sec": 0.0
          }
        }
      ]
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
Per-slot placement view for an active multi-worker stream, or a synthetic single-slot view
for an active standalone stream pipeline (v1.3.2+).

Returns `404` when: the pipeline has no active multi-worker placement (manager mode), or the
stream is not yet reporting stats / has stopped (standalone mode), or the pipeline is not a
stream type.

```json
{
  "pipeline_name": "prom-ingest",
  "placement_group_id": "prom-ingest-20260417-ab12",
  "status": "running",
  "target_count": "all",
  "started_at": "2026-04-17T15:00:00+00:00",
  "slot_count": 1,
  "active_slots": 1,
  "records_in": 7102,
  "records_out": 7102,
  "records_skipped": 0,
  "dlq_count": 0,
  "error_count": 0,
  "bytes_in": 4505600,
  "bytes_out": 4505600,
  "records_in_per_sec": 710.2,
  "records_out_per_sec": 710.2,
  "bytes_in_per_sec": 450560.0,
  "bytes_out_per_sec": 450560.0,
  "slots": [
    {
      "worker_index": 0,
      "worker_id": "tram-worker-0",
      "worker_url": "http://trishul-ram-worker-0.trishul-ram-worker.default.svc.cluster.local:8766",
      "run_id_prefix": "prom-ingest-20260417-ab12-w0",
      "current_run_id": "prom-ingest-20260417-ab12-w0-r1",
      "status": "running",
      "restart_count": 0,
      "stats": {
        "schedule_type": "stream",
        "timestamp": "2026-04-17T15:00:05+00:00",
        "uptime_seconds": 10.0,
        "records_in": 7102,
        "records_out": 7102,
        "records_skipped": 0,
        "dlq_count": 0,
        "error_count": 0,
        "bytes_in": 4505600,
        "bytes_out": 4505600,
        "errors_last_window": [],
        "stale": false,
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

**Queued response (v1.4.0)** — in manager+worker mode with no healthy workers and queued runs enabled (`TRAM_QUEUE_MANUAL_RUNS=1`, the default), the run is durably queued instead of failing: it survives manager restarts and is dispatched automatically when worker capacity returns. The response is `202 Accepted` with the stable run_id and absolute TTL:

```json
{"name": "pm-ingest", "status": "queued", "run_id": "…", "expires_at": "…"}
```

**Flush runs (v1.4.0)** — `?flush=true` makes stateful transforms emit their open windows as partials (`window_complete: false`) and clear them from the saved state. The flag is not carried through the queue: a queued flush run executes as a normal run when capacity returns — re-issue `?flush=true` once capacity is back to flush.

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
| `status` | — | Filter: `success` \| `failed` \| `aborted` |
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
    "bytes_in": 2359296,
    "bytes_out": 2341888,
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

### GET /api/runs/count (v1.4.3)
Total run count for the current list filters — the honest-pagination
companion to `GET /api/runs` (the UI pill shows "showing N of M" and drives
exact load-more). Query params mirror the listing:

| Param | Default | Description |
|-------|---------|-------------|
| `pipeline` | — | Filter by pipeline name |
| `status` | — | Filter: `success` \| `failed` \| `aborted` \| `queued` |
| `from_dt` | — | ISO8601 lower bound on `started_at` |

```json
{"total": 42}
```

- `total` counts every run-history row matching the filters plus each queued
  run matching them — the listing merges queued rows the same way, so the
  count always equals the listing's total across pages (each queued run is
  counted once).
- Returns `{"total": null}` when no persistence (DB) is configured — the UI
  falls back to its has-more heuristic there.
- Registered before `GET /api/runs/{run_id}`, so the literal path segment
  `count` is never captured as a run id.

## Internal Transform State (v1.4.0)

Durable per-pipeline state for stateful transforms (`counter_delta`, `window_aggregate`). In manager+worker mode the worker GETs the state at run start and PUTs it back only after a successful run (retries re-hydrate from the same in-run snapshot); in standalone mode the state lives in the local `transform_state` table. Requires `TRAM_STATEFUL_TRANSFORMS=1` (default); `0` disables both the transforms and these endpoints (404). `update()`/`delete()` on the pipeline purge the row; a config-hash mismatch discards the stored state so the new transform identities start fresh.

### GET /api/internal/transform-state/{pipeline}
Returns the persisted state blob and its config hash.

```json
{"pipeline": "pm-counters", "state": {"counter_delta:0": {"…identity…": {"v": 1500, "t": 1789534920.0}}}, "config_sha256": "1c3036a4cf884027"}
```

### PUT /api/internal/transform-state/{pipeline}
Replaces the state. Bodies over `TRAM_STATE_MAX_BYTES` (default 20 MiB) are rejected with `413`.

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

When `TRAM_API_KEY` is set (or `apiKey` in Helm values), all protected `/api/*` requests must include the key via the `X-API-Key` header:

```bash
curl -H "X-API-Key: mysecret" http://localhost:8765/api/pipelines
```

The `?api_key=` query param is removed — keys in URLs end up in access/proxy logs and browser history.

Exempt paths (always unauthenticated): `/api/health`, `/api/ready`, `/agent/health`, `/metrics`, `/`, `/api/auth/login`, `/favicon.ico`, `/docs`, `/redoc`, `/openapi.json`, and the `/webhooks/*` and `/ui` prefixes.

Internal machine-to-machine surfaces (`/api/internal/*` on the manager, `/agent/*` on workers) honor `TRAM_INTERNAL_AUTH_MODE`:

- `off` — requests pass through with no check and no log
- `warn` (default) — missing/invalid keys are logged at WARNING but requests are still served
- `enforce` — missing/invalid keys are rejected with `401`; requires `TRAM_API_KEY` to be set (without a key configured, internal surfaces pass through)

An invalid `TRAM_INTERNAL_AUTH_MODE` value is logged at WARNING and falls back to `warn`.

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

## Connector schema (v1.4.3, Issue #24)

### GET /api/config/schema
Backend-generated connector schema metadata for UI-driven forms, derived at
import time from the Pydantic plugin models. The response is the descriptor
payload (categories → type → `{fields: [...]}`) plus a sibling
`schema_version` key — consumers that index the known category names
(`sources`, `sinks`, `serializers`, `transforms`) are unaffected.

Each field descriptor: `name`, `type`, `kind` (`text`/`select`/`boolean`/
`integer`/`number`/`list`/`map`/`complex`), `choices`, `required`, `default`,
`secret` (name heuristic: password/token/secret), `multiline`.

```json
{
  "sources": { "sftp": { "fields": [ {"name": "host", "type": "str", "kind": "text", "required": true, "secret": false, ...} ] } },
  "sinks": {},
  "serializers": { "json": { "fields": [ ... ] } },
  "transforms": {},
  "schema_version": "eecd1712ea4d"
}
```

`schema_version` is the first 12 hex chars of the sha256 over the canonical
JSON of the underlying `SCHEMA_FIELDS` cache — the identity token for this
schema (equality check only, not a semantic version). A long-lived UI tab can
compare it across polls to detect a manager upgrade underneath it (hash
mismatch) instead of rendering forms from a stale schema.

---

## AI Assist (v1.1.0)

AI assist is configured either via `TRAM_AI_*` env vars or through the Settings
page, which persists to the DB (DB values override env vars).

### GET /api/ai/status
Returns whether AI assist is enabled (an API key is configured), which
provider/model would be used, and the schema identity token the AI prompts
are built against (Issue #24).

```json
{"enabled": true, "provider": "anthropic", "model": "claude-haiku-4-5-20251001", "schema_version": "eecd1712ea4d"}
```

Returns `{"enabled": false, "provider": null, "model": null, "schema_version": "eecd1712ea4d"}`
when no API key is configured — `schema_version` is always present.

### GET /api/ai/config
Returns the current AI configuration. The API key is never returned — only
whether one is set, a masked hint, and its source (`db` or `env`).

```json
{
  "provider": "anthropic",
  "api_key_set": true,
  "api_key_hint": "…abcd",
  "model": "",
  "base_url": "",
  "source": "db"
}
```

### POST /api/ai/config
Persist AI configuration to the DB (overrides env vars). Each field is
three-state: **absent or blank** = no change, **a value** = set, **explicit
`null`** = clear (deletes the stored setting, reverting to the env-var /
default). In particular a blank `api_key` never clears a stored key, while
`"api_key": null` deliberately does.

| Field | Description |
|-------|-------------|
| `provider` | `"anthropic"`, `"openai"`, or `"bedrock"` (rejected with 400 if unknown); `null` clears it |
| `api_key` | API key for the provider; `null` clears it |
| `model` | Model name (blank = provider default); `null` clears it |
| `base_url` | Optional endpoint override (required for `bedrock`); `null` clears it |

```bash
curl -X POST http://localhost:8765/api/ai/config \
  -H "Content-Type: application/json" \
  -d '{"provider": "openai", "api_key": "sk-…", "model": "gpt-4o-mini"}'

# Deliberately clear the stored API key:
curl -X POST http://localhost:8765/api/ai/config \
  -H "Content-Type: application/json" \
  -d '{"api_key": null}'
```

Response:
```json
{"ok": true}
```

Errors: `503` when no database is available, `400` for an unknown `provider`,
an invalid `base_url` scheme, or an allowlist violation.

### POST /api/ai/test
Send a minimal probe prompt to verify the provider configuration.

Response:
```json
{"ok": true, "reply": "OK", "provider": "openai", "model": "gpt-4o-mini"}
```

Errors: `503` when AI is not configured, `502` when the provider call fails
(e.g. bad key, connection error).

### POST /api/ai/suggest
Generate, explain, fix, or modify pipeline YAML. Returns `503` when AI is not
configured, `502` when the provider call fails, and `400` for an unknown mode.

For `generate`/`fix`/`modify`, the response carries `valid` and `issues` in
addition to the YAML: the model output is validated server-side with the same
`yaml.safe_load` + `load_pipeline_from_yaml` check the dry-run endpoint uses.
`valid` is `false` when the YAML fails to parse/validate (errors in `issues`),
or when the provider reported output truncation (`stop_reason`/`finish_reason`
of `max_tokens`/`length` — a warning is appended to `issues`). The raw YAML is
always returned so the editor can still show it.

For `explain`/`fix`/`modify`, secret fields in the incoming YAML (per the
schema metadata: field names containing `password`/`token`/`secret`) are masked
with `***redacted***` before the prompt is sent to the provider — the operator's
pipeline on disk is never modified. `${VAR}` environment references are left
intact (the loader substitutes them at runtime, so they are not secrets).

#### mode: `generate`
Create a new pipeline from a description.

| Field | Description |
|-------|-------------|
| `mode` | `"generate"` |
| `prompt` | Natural-language description of the pipeline |
| `plugins` | Optional `{sources, sinks, transforms, serializers}` type lists to scope the schema context |

```bash
curl -X POST http://localhost:8765/api/ai/suggest \
  -H "Content-Type: application/json" \
  -d '{"mode": "generate", "prompt": "Poll SNMP IF-MIB every 60s and write to InfluxDB"}'
```

Response:
```json
{
  "yaml": "name: snmp-to-influxdb\nschedule:\n  ...",
  "valid": true,
  "issues": []
}
```

#### mode: `explain`
Explain a dry-run error for existing YAML.

| Field | Description |
|-------|-------------|
| `mode` | `"explain"` |
| `yaml` | The pipeline YAML that failed |
| `error` | The dry-run error message |

Response:
```json
{"explanation": "The source is missing a serializer_in ..."}
```

#### mode: `fix`
Return corrected YAML for a pipeline that failed dry-run.

| Field | Description |
|-------|-------------|
| `mode` | `"fix"` |
| `yaml` | The pipeline YAML to fix |
| `error` | The dry-run error to resolve |
| `plugins` | Optional type lists (as in `generate`) |

Response:
```json
{
  "yaml": "name: fixed-pipe\nschedule:\n  ...",
  "valid": false,
  "issues": ["Pipeline validation error:\n..."]
}
```

#### mode: `modify`
Modify existing YAML per an instruction.

| Field | Description |
|-------|-------------|
| `mode` | `"modify"` |
| `yaml` | The pipeline YAML to modify |
| `instruction` | What to change |
| `plugins` | Optional type lists (as in `generate`) |

Response:
```json
{
  "yaml": "name: modified-pipe\nschedule:\n  ...",
  "valid": true,
  "issues": []
}
```

Configure via env vars:

| Env Var | Description |
|---------|-------------|
| `TRAM_AI_API_KEY` | API key for the AI provider |
| `TRAM_AI_PROVIDER` | `anthropic`, `openai`, or `bedrock` (default: `anthropic`) |
| `TRAM_AI_MODEL` | Model name (defaults: `claude-haiku-4-5-20251001` for Anthropic, `gpt-4o-mini` for OpenAI, `us.anthropic.claude-sonnet-4-6` for Bedrock) |
| `TRAM_AI_BASE_URL` | Custom base URL (honored for Anthropic/OpenAI, required for Bedrock) |

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
| 401 | Missing/invalid `X-API-Key` header (the legacy `?api_key=` query param was removed in v1.4.0); or missing/invalid `Authorization: Bearer` for webhook secret |
| 404 | Pipeline, run, or webhook path not found |
| 409 | Pipeline already registered |
| 422 | Pydantic validation error |
| 429 | Rate limit exceeded (v1.0.0) — retry after the `TRAM_RATE_LIMIT_WINDOW` window resets |
| 503 | DB unavailable (readiness check); `prometheus_client` not installed (`/metrics`) |
