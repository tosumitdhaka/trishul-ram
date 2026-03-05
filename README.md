# TRAM — Trishul Real-time Adapter & Mapper

> Lightweight, container-native Python daemon that moves and transforms telecom data (PM/FM/Logs) across protocols.

**Version:** 0.7.0 | **Status:** Active development | **Python:** 3.11+

---

## What is TRAM?

TRAM is a pipeline daemon for telecom data integration. It runs as an always-on service, accepting pipeline definitions as YAML files, and executing them on a schedule (interval, cron) or continuously (stream). Each pipeline wires together:

```
Source → Deserialize → Transform chain → [per-record] → Serialize → Sinks
                                                                       ↓ (condition filter)
                                                              Per-sink transforms → Serialize → Sink
                                                                       ↓ (on any error)
                                                                      DLQ sink (JSON envelope)
```

Plugins for sources, sinks, serializers, and transforms self-register via decorators — adding a new protocol requires zero changes to the core engine.

---

## Quick Start

```bash
pip install -e ".[dev]"
tram version
tram plugins

# Validate a pipeline
tram validate pipelines/minimal.yaml

# Run once (no daemon)
tram run pipelines/minimal.yaml --dry-run

# Start the daemon
tram daemon &

# Manage pipelines via CLI
tram pipeline list
tram pipeline add pipelines/my-pipeline.yaml
tram pipeline run my-pipeline
tram pipeline history my-pipeline
tram pipeline rollback my-pipeline --version 1

# Or via REST
curl http://localhost:8765/api/health
curl http://localhost:8765/api/pipelines
curl http://localhost:8765/metrics
curl -X POST http://localhost:8765/webhooks/my-events -d '{"event":"pm"}'
```

---

## Plugin Registry (v0.6.0)

| Category | Keys |
|----------|------|
| **Sources** | `sftp`, `local`, `rest`, `kafka`, `ftp`, `s3`, `syslog`, `snmp_trap`, `snmp_poll`, `mqtt`, `amqp`, `nats`, `gnmi`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `webhook`, `websocket`, `elasticsearch`, `prometheus_rw` |
| **Sinks** | `sftp`, `local`, `rest`, `kafka`, `opensearch`, `ftp`, `ves`, `s3`, `snmp_trap`, `mqtt`, `amqp`, `nats`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `websocket`, `elasticsearch` |
| **Serializers** | `json`, `csv`, `xml`, `avro`, `parquet`, `msgpack`, `protobuf` |
| **Transforms** | `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`, `flatten`, `timestamp_normalize`, `aggregate`, `enrich`, `explode`, `deduplicate`, `regex_extract`, `template`, `mask`, `validate`, `sort`, `limit`, `jmespath`, `unnest` |

Optional extras: `pip install tram[kafka]` · `pip install tram[opensearch]` · `pip install tram[elasticsearch]` · `pip install tram[metrics]`

---

## Pipeline YAML

### Basic (single sink)

```yaml
version: "1"
pipeline:
  name: pm-ingest
  schedule:
    type: interval
    interval_seconds: 300

  source:
    type: sftp
    host: ${NE_SFTP_HOST}
    username: ${NE_SFTP_USER}
    password: ${NE_SFTP_PASS}
    remote_path: /export/pm/
    file_pattern: "*.csv"
    move_after_read: /export/pm/processed/

  serializer_in:
    type: csv

  transforms:
    - type: rename
      fields: {ne_id: network_element_id}
    - type: cast
      fields: {rx_bytes: int}
    - type: add_field
      fields: {rx_mbps: "rx_bytes / 1000000"}
    - type: filter
      condition: "rx_mbps > 0"

  serializer_out:
    type: json

  sink:                        # legacy single-sink (auto-wrapped)
    type: sftp
    host: ${MED_SFTP_HOST}
    username: ${MED_SFTP_USER}
    password: ${MED_SFTP_PASS}
    remote_path: /ingest/pm/
```

### Multi-sink with conditional routing + per-sink transforms (v0.5.0 / v0.6.0)

```yaml
  sinks:
    - type: kafka
      brokers: [kafka:9092]
      topic: pm-all             # no condition = catch-all
    - type: opensearch
      hosts: [http://os:9200]
      index: pm-critical
      condition: "rx_mbps > 500"   # only high-traffic records
      transforms:                  # per-sink transform chain (v0.6.0)
        - type: drop
          fields: [raw_bytes]
    - type: local
      path: /tmp/overflow
      condition: "rx_mbps <= 0"    # zero-traffic anomalies

  rate_limit_rps: 1000            # token-bucket, across all sinks

  # Dead-letter queue (v0.6.0) — captures parse/transform/sink failures
  dlq:
    type: local
    path: /tmp/dlq
```

### Alert rules (v0.6.0)

```yaml
  alerts:
    - name: high-error-rate
      condition: "error_rate > 0.1"
      action: webhook
      webhook_url: "https://hooks.slack.com/..."
      cooldown_seconds: 300

    - name: pipeline-failed
      condition: "failed"
      action: email
      email_to: "ops@example.com"
      subject: "TRAM Alert: {pipeline} failed"
      cooldown_seconds: 600
```

All `${VAR}` and `${VAR:-default}` placeholders are resolved from environment at load time.

---

## v0.7.0 Features

| Feature | Description |
|---------|-------------|
| **External DB support** | `TRAM_DB_URL` (SQLAlchemy URL) — SQLite (default), PostgreSQL, MySQL/MariaDB; `pip install tram[postgresql\|mysql]` |
| **Node identity** | `TRAM_NODE_ID` stored in every run — `node_id` column in `run_history` for multi-node tracing |
| **`dlq_count` persisted** | `RunResult.dlq_count` carried through to DB; `tram_dlq_total` Prometheus counter |
| **Graceful shutdown** | SIGTERM → SIGINT bridge; `TRAM_SHUTDOWN_TIMEOUT_SECONDS` drain wait; stream threads joined |
| **Readiness DB check** | `GET /api/ready` returns 503 when DB is unreachable |
| **Run history pagination** | `?offset=N&from_dt=ISO` on `GET /api/runs` |
| **Schema migrations** | Idempotent at startup — v0.6.0 SQLite databases upgraded automatically |

## v0.6.0 Features

| Feature | Description |
|---------|-------------|
| **Dead-Letter Queue** | `dlq: <sink>` on pipeline config; failed records (parse/transform/sink) written as JSON envelopes |
| **Per-sink transforms** | Each sink has its own `transforms:` chain, applied after global transforms + condition filter |
| **Alert rules** | `alerts:` list on pipeline; simpleeval conditions; webhook (httpx) or email (smtplib) actions with cooldown |
| **Helm chart** | Production-ready `helm/` with PVC, ConfigMap pipelines, envSecret, Prometheus annotations |
| **GitHub Actions** | `ci.yml` (ruff + pytest on PR/push); `release.yml` (multi-arch Docker + Helm OCI on `v*` tags) |

## v0.5.0 Features

| Feature | Description |
|---------|-------------|
| **Conditional routing** | Per-sink `condition:` expression; records fan-out to matched sinks |
| **Rate limiting** | `rate_limit_rps:` in pipeline config; token-bucket implementation |
| **SQLite persistence** | Run history + pipeline versions survive daemon restarts (`~/.tram/tram.db`) |
| **Prometheus metrics** | `GET /metrics` — counters for records in/out/skipped, errors, chunk duration |
| **Webhook source** | Receives HTTP POSTs at `/webhooks/{path}` on the daemon port |
| **WebSocket** | `websocket` source (consumer) + sink (producer) |
| **Elasticsearch** | `elasticsearch` source (scroll) + sink (bulk API) |
| **Prometheus remote_write** | `prometheus_rw` source decodes Snappy+protobuf WriteRequests |
| **Schema Registry** | Avro + Protobuf serializers support Confluent/Apicurio magic-byte framing |
| **Pipeline versioning** | Auto-saved on register; `tram pipeline history` / `rollback` |

---

## Docker

```bash
cp .env.example .env       # fill in credentials
docker compose up
curl http://localhost:8765/api/ready
```

## Kubernetes (Helm)

```bash
# Install from OCI registry
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --set image.repository=ghcr.io/OWNER/tram \
  --set image.tag=0.6.0

# With inline pipeline + alert SMTP config
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --set-file "pipelines.pm-ingest\.yaml=./pipelines/pm-ingest.yaml" \
  --set env.TRAM_SMTP_HOST=smtp.example.com \
  --set envSecret.TRAM_SMTP_PASS.secretName=tram-smtp \
  --set envSecret.TRAM_SMTP_PASS.secretKey=password
```

> **v0.6.0 runs as a single replica (standalone daemon).** Multi-replica clustering is planned for a future release.

---

## REST API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Liveness probe |
| GET | `/api/ready` | Readiness probe |
| GET | `/api/plugins` | Registered plugin keys |
| GET | `/api/pipelines` | List all pipelines |
| POST | `/api/pipelines` | Register pipeline (YAML body) |
| POST | `/api/pipelines/{name}/run` | Trigger immediate run |
| GET | `/api/pipelines/{name}/versions` | List saved versions |
| POST | `/api/pipelines/{name}/rollback?version=N` | Restore a version |
| POST | `/webhooks/{path}` | Ingest HTTP payload to webhook source |
| GET | `/metrics` | Prometheus metrics (text/plain) |
| GET | `/api/runs` | Run history (`?pipeline=&status=&limit=&offset=&from_dt=`) |

Full reference: [`docs/api.md`](docs/api.md)

---

## Project Layout

```
tram/
├── core/             exceptions, context (dlq_count), config, logging
├── interfaces/       BaseSource, BaseSink, BaseTransform, BaseSerializer
├── registry/         @register_* decorators + lookup
├── models/           Pydantic v2 pipeline schema (dlq, per-sink transforms, AlertRuleConfig)
├── serializers/      json, csv, xml, avro, parquet, msgpack, protobuf
├── transforms/       20 transforms
├── connectors/       22 source + 19 sink connectors
├── persistence/      SQLite (run_history + pipeline_versions + alert_state)
├── metrics/          Prometheus metrics registry
├── schema_registry/  Confluent/Apicurio client + magic-byte helpers
├── alerts/           AlertEvaluator (webhook + email actions, cooldown)
├── pipeline/         loader, executor (DLQ + per-sink transforms), manager
├── scheduler/        APScheduler (batch) + threads (stream)
├── api/              FastAPI routers (health, pipelines, runs, webhooks, metrics)
├── daemon/           uvicorn server entrypoint
└── cli/              Typer CLI
helm/                 Helm chart (Deployment, Service, ConfigMap, PVC, HPA, SA)
.github/workflows/    ci.yml (test) + release.yml (Docker + Helm OCI publish)
```

---

## Documentation

| Doc | Contents |
|-----|----------|
| [`docs/architecture.md`](docs/architecture.md) | Design, data flow, execution modes |
| [`docs/connectors.md`](docs/connectors.md) | All connectors + how to add new ones |
| [`docs/transforms.md`](docs/transforms.md) | All 20 transforms + expression syntax |
| [`docs/api.md`](docs/api.md) | REST API reference |
| [`docs/deployment.md`](docs/deployment.md) | Docker, k8s, env vars |

---

## Development

```bash
pip install -e ".[dev]"
pytest tests/unit/         # 431 unit tests (no network)
pytest tests/integration/  # 3 integration tests (mocked SFTP)
pytest tests/             # all 434 tests
```

---

## Changelog

See [`CHANGELOG.md`](CHANGELOG.md).
