# TRAM — Trishul Real-time Adapter & Mapper

> Lightweight, container-native Python daemon that moves and transforms telecom data (PM/FM/Logs) across protocols.

**Version:** 0.9.0 | **Status:** Active development | **Python:** 3.11+

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

## Plugin Registry (v0.9.0)

| Category | Keys |
|----------|------|
| **Sources** | `sftp`, `local`, `rest`, `kafka`, `ftp`, `s3`, `syslog`, `snmp_trap`, `snmp_poll`, `mqtt`, `amqp`, `nats`, `gnmi`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `webhook`, `websocket`, `elasticsearch`, `prometheus_rw`, `corba` |
| **Sinks** | `sftp`, `local`, `rest`, `kafka`, `opensearch`, `ftp`, `ves`, `s3`, `snmp_trap`, `mqtt`, `amqp`, `nats`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `websocket`, `elasticsearch` |
| **Serializers** | `json`, `csv`, `xml`, `avro`, `parquet`, `msgpack`, `protobuf` |
| **Transforms** | `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`, `flatten`, `timestamp_normalize`, `aggregate`, `enrich`, `explode`, `deduplicate`, `regex_extract`, `template`, `mask`, `validate`, `sort`, `limit`, `jmespath`, `unnest` |

Optional extras: `pip install tram[kafka]` · `pip install tram[opensearch]` · `pip install tram[elasticsearch]` · `pip install tram[metrics]` · `pip install tram[corba]`

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

### Multi-sink with conditional routing + per-sink transforms

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

### v0.9.0 — parallel workers, batch cap, skip_processed, CORBA

```yaml
  # Parallelism and throughput control (v0.9.0)
  thread_workers: 4           # process 4 chunks in parallel (batch + stream)
  batch_size: 10000           # cap records per run (nil = unlimited)
  on_error: dlq               # explicit: route all failures to DLQ (requires dlq:)

  # Skip files already processed in this pipeline (v0.9.0)
  source:
    type: sftp
    host: ${NE_SFTP_HOST}
    remote_path: /export/pm/
    file_pattern: "*.csv"
    skip_processed: true      # idempotent; uses SQLite to track processed paths

  # CORBA source — DII, no IDL stubs required (v0.9.0)
  # source:
  #   type: corba
  #   naming_service: "corbaloc:iiop:192.168.1.1:2809/NameService"
  #   object_name: "PM/PMCollect"
  #   operation: getPMData
  #   args: ["ManagedElement=1", 900]
  #   skip_processed: true
```

### Alert rules

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

## v0.9.0 Features

| Feature | Description |
|---------|-------------|
| **`thread_workers`** | Intra-node parallel chunk processing — `thread_workers: N` in pipeline config; works for both batch and stream modes |
| **`batch_size` cap** | Stop reading after N records per run — useful for catch-up scenarios and rate-controlled ingestion |
| **`on_error: dlq`** | Explicit mode routing all failures (parse/transform/sink) to the DLQ sink; model validates that `dlq:` is configured |
| **CORBA source** | DII-based connector using `omniORBpy` — no pre-compiled IDL stubs; covers 3GPP Itf-N, TMN X.700, Ericsson ENM, Nokia NetAct |
| **Processed-file tracking** | `skip_processed: true` on file/object-storage sources — pipeline + source-key + path stored in SQLite; re-runs are idempotent |
| **Helm ConfigMap checksum** | Pod template annotation `checksum/config` auto-triggers rolling restart when any pipeline YAML changes on `helm upgrade` |
| **Kafka group_id isolation** | `group_id` defaults to pipeline name (was hardcoded `"tram"`) — each pipeline gets its own consumer group out of the box |
| **NATS queue_group isolation** | `queue_group` defaults to pipeline name; empty string `""` = explicit broadcast; `null` was the previous default (broadcast on all nodes) |

## v0.8.1 Bug Fixes

| Fix | Description |
|-----|-------------|
| **Kafka group isolation** | `KafkaSourceConfig.group_id: Optional[str] = None` — defaults to pipeline name at runtime |
| **NATS queue_group** | `NatsSourceConfig.queue_group: Optional[str] = None` — defaults to pipeline name; `""` = opt-in broadcast |
| **Kafka explicit commit** | `consumer.commit()` called before `consumer.close()` to avoid re-delivering tail records |
| **Helm image.tag** | Stale default `0.6.0` corrected to track current release |

## v0.8.0 Features

| Feature | Description |
|---------|-------------|
| **Cluster mode** | Self-organizing multi-node deployment — `TRAM_CLUSTER_ENABLED=true`; nodes hash-ring partition pipelines |
| **StatefulSet Helm** | Chart migrated from Deployment to StatefulSet; headless service for stable `tram-N` pod identities |
| **Cluster REST API** | `GET /api/cluster/nodes` returns live topology; `TRAM_NODE_ID` from pod name |
| **Rebalance** | `ClusterCoordinator` heartbeat + partition rebalance loop; pipelines migrate on node join/leave |

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
# Standalone (default)
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --set image.tag=0.9.0

# Cluster mode (3-replica StatefulSet + external PostgreSQL)
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --set image.tag=0.9.0 \
  --set clusterMode.enabled=true \
  --set replicaCount=3 \
  --set envSecret.TRAM_DB_URL.secretName=tram-db \
  --set envSecret.TRAM_DB_URL.secretKey=url \
  --set persistence.enabled=false
```

> **v0.8.0+**: always a `StatefulSet` — `replicaCount=1` for standalone (stable `tram-0` identity, PVC per pod), `replicaCount=N` + `clusterMode.enabled=true` for self-organizing cluster. See [`docs/deployment.md`](docs/deployment.md) for full setup.

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
| GET | `/api/cluster/nodes` | Cluster topology (node_id, position, live peers) |

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
├── connectors/       23 source + 19 sink connectors (incl. corba source)
├── cluster/          NodeRegistry + ClusterCoordinator (v0.8.0 cluster mode)
├── persistence/      TramDB (SQLAlchemy Core): run_history, pipeline_versions, alert_state, node_registry, processed_files; ProcessedFileTracker
├── metrics/          Prometheus metrics registry
├── schema_registry/  Confluent/Apicurio client + magic-byte helpers
├── alerts/           AlertEvaluator (webhook + email actions, cooldown)
├── pipeline/         loader, executor (DLQ + per-sink transforms), manager
├── scheduler/        APScheduler (batch) + threads (stream) + rebalance loop
├── api/              FastAPI routers (health, pipelines, runs, webhooks, metrics, cluster)
├── daemon/           uvicorn server entrypoint
└── cli/              Typer CLI
helm/                 Helm chart (StatefulSet, Service, Headless Service, ConfigMap, SA, volumeClaimTemplates)
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
pytest tests/unit/         # 535 unit tests (no network)
pytest tests/integration/  # 3 integration tests (mocked SFTP)
pytest tests/             # all 538 tests
```

---

## Changelog

See [`CHANGELOG.md`](CHANGELOG.md).
