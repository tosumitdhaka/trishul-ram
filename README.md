# TRAM — Trishul Real-time Adapter & Mapper

> Lightweight, container-native Python daemon that moves and transforms telecom data (PM/FM/Logs) across protocols.

**Version:** 1.0.3 | **Status:** Production-ready | **Python:** 3.11+

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

# Scaffold a new pipeline
tram pipeline init my-pipeline --source sftp --sink local

# Validate a pipeline (includes lint checks)
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
curl http://localhost:8765/api/ready
curl http://localhost:8765/api/pipelines
curl http://localhost:8765/metrics

# API key authentication (v1.0.0)
TRAM_API_KEY=secret tram daemon &
curl -H "X-API-Key: secret" http://localhost:8765/api/pipelines

# Webhook ingestion
curl -X POST http://localhost:8765/webhooks/my-events -d '{"event":"pm"}'

# MIB management (v1.0.3)
tram mib compile IF-MIB.mib --out /mibs/compiled/
tram mib download CISCO-ENTITY-FRU-CONTROL-MIB --out /mibs/
curl http://localhost:8765/api/mibs
curl -F "file=@MY-CUSTOM-MIB.mib" http://localhost:8765/api/mibs/upload

# Schema management (v1.0.3)
curl http://localhost:8765/api/schemas
curl -F "file=@GenericRecord.proto" "http://localhost:8765/api/schemas/upload?subdir=cisco"
curl http://localhost:8765/api/schemas/cisco/GenericRecord.proto
```

---

## Plugin Registry (v1.0.3)

| Category | Keys |
|----------|------|
| **Sources** | `sftp`, `local`, `rest`, `kafka`, `ftp`, `s3`, `syslog`, `snmp_trap`, `snmp_poll`, `mqtt`, `amqp`, `nats`, `gnmi`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `webhook`, `websocket`, `elasticsearch`, `prometheus_rw`, `corba` |
| **Sinks** | `sftp`, `local`, `rest`, `kafka`, `opensearch`, `ftp`, `ves`, `s3`, `snmp_trap`, `mqtt`, `amqp`, `nats`, `sql`, `influxdb`, `redis`, `gcs`, `azure_blob`, `websocket`, `elasticsearch` |
| **Serializers** | `json`, `csv`, `xml`, `avro`, `parquet`, `msgpack`, `protobuf` |
| **Transforms** | `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`, `flatten`, `timestamp_normalize`, `aggregate`, `enrich`, `explode`, `deduplicate`, `regex_extract`, `template`, `mask`, `validate`, `sort`, `limit`, `jmespath`, `unnest` |

Optional extras: `pip install tram[kafka]` · `pip install tram[snmp]` · `pip install tram[mib]` · `pip install tram[otel]` · `pip install tram[watch]` · `pip install tram[metrics]` · `pip install tram[corba]` · `pip install tram[all]`

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

  sink:
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
      retry_count: 3            # per-sink retry (v1.0.0)
      circuit_breaker_threshold: 5
    - type: opensearch
      hosts: [http://os:9200]
      index: pm-critical
      condition: "rx_mbps > 500"
      transforms:
        - type: drop
          fields: [raw_bytes]
    - type: local
      path: /tmp/overflow
      condition: "rx_mbps <= 0"

  parallel_sinks: true          # fan-out to all sinks concurrently (v1.0.0)
  rate_limit_rps: 1000

  dlq:
    type: local
    path: /tmp/dlq
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

## v1.0.3 Features

| Feature | Description |
|---------|-------------|
| **SNMP MIB management API** | `GET/POST /api/mibs/upload`, `POST /api/mibs/download`, `DELETE /api/mibs/{name}` — list, upload, download, and delete compiled MIB modules at runtime |
| **Schema management API** | `GET/POST /api/schemas/upload`, `GET /api/schemas/{path}`, `DELETE /api/schemas/{path}` — manage `.proto`, `.avsc`, `.json`, `.xsd`, `.yaml` schema files; optional `?subdir=` grouping |
| **Standard MIBs in image** | IF-MIB, ENTITY-MIB, HOST-RESOURCES-MIB, IP-MIB, TCP-MIB, UDP-MIB, IANAifType-MIB baked into Docker image at build time |
| **`tram mib download`** | CLI command to download + compile MIBs from mibs.pysnmp.com |
| **`tram mib compile` dirs** | `tram mib compile <dir>` compiles all `.mib` files in a directory |
| **All connectors in image** | Docker image now installs every connector/serializer/observability extra (all except `corba` which requires a source build) |
| **Single PVC for all data** | Helm chart uses one `/data` PVC for SQLite, schemas (`/data/schemas`), and MIBs (`/data/mibs`); no separate PVCs needed |

## v1.0.2 Features

| Feature | Description |
|---------|-------------|
| **SNMPv3 USM** | Full USM auth/priv on `snmp_poll` source, `snmp_trap` sink; `security_name`, `auth_protocol` (MD5/SHA/SHA256/SHA512), `auth_key`, `priv_protocol` (DES/AES128/AES192/AES256), `priv_key`, `context_name` |
| **SNMP trap v3 config** | v3 fields stored on `snmp_trap` source; trap *receiving* is best-effort (encrypted v3 falls back to raw hex; full USM receive planned) |

## v1.0.1 Features

| Feature | Description |
|---------|-------------|
| **SNMP poll `yield_rows`** | `yield_rows: true` yields one record per WALK table row; `index_depth` controls index split (0=auto on first dot, N=last N components for composite keys) |
| **`_polled_at` timestamp** | Every SNMP poll record contains `_polled_at` (UTC ISO8601) and `_index`/`_index_parts` when using `yield_rows` |
| **Dynamic version** | `tram.__version__` from `importlib.metadata`; `pyproject.toml` is the single version source; release workflow patches it from git tag |

## v1.0.0 Features

| Feature | Description |
|---------|-------------|
| **API key auth** | `TRAM_API_KEY` env var — `X-API-Key` header or `?api_key=` query param; exempt paths: health, ready, metrics, webhooks |
| **Rate limiting** | `TRAM_RATE_LIMIT` (req/min per IP), `TRAM_RATE_LIMIT_WINDOW`; 429 with `Retry-After` header |
| **TLS** | `TRAM_TLS_CERTFILE`/`TRAM_TLS_KEYFILE` — passed to uvicorn; Helm `tls:` section for cert-manager or manual secret |
| **Per-sink retry** | `retry_count`, `retry_delay_seconds` on every sink; exponential backoff + jitter |
| **Circuit breaker** | `circuit_breaker_threshold` — sink bypassed 60s after N consecutive failures |
| **Parallel sinks** | `parallel_sinks: true` on pipeline — all sinks written concurrently via ThreadPoolExecutor |
| **SNMP MIB integration** | `mib_dirs`, `mib_modules`, `resolve_oids` on snmp sources/sink; `VarbindConfig` for explicit trap varbinds; `tram mib compile` CLI |
| **OpenTelemetry** | `TRAM_OTEL_ENDPOINT` + `TRAM_OTEL_SERVICE`; batch_run and sink writes traced; `pip install tram[otel]` |
| **Kafka consumer lag** | `tram_kafka_consumer_lag{pipeline,topic,partition}` Prometheus gauge |
| **Stream queue depth** | `tram_stream_queue_depth{pipeline}` Prometheus gauge |
| **CSV run export** | `GET /api/runs?format=csv` downloads run history as CSV |
| **Enhanced readiness** | `/api/ready` returns `{db, scheduler, cluster, pipelines_loaded}` |
| **Pipeline linter** | `tram validate` runs 5 lint rules (L001–L005) after schema validation |
| **File watcher** | `TRAM_WATCH_PIPELINES=true` hot-reloads pipeline YAMLs on change; `pip install tram[watch]` |
| **`tram pipeline init`** | Scaffolds minimal valid pipeline YAML to stdout or file |
| **Kafka reconnect** | `reconnect_delay_seconds`, `max_reconnect_attempts` on `kafka` source |
| **NATS reconnect** | `max_reconnect_attempts`, `reconnect_time_wait` on `nats` source |

## v0.9.0 Features

| Feature | Description |
|---------|-------------|
| **`thread_workers`** | Parallel chunk processing per pipeline; works for batch + stream |
| **`batch_size` cap** | Stop reading after N records per run |
| **`on_error: dlq`** | Route all failures to DLQ sink |
| **CORBA source** | DII-based, no IDL stubs; covers 3GPP Itf-N, TMN X.700, Ericsson ENM, Nokia NetAct |
| **Processed-file tracking** | `skip_processed: true` — idempotent re-runs via SQLite |

---

## Docker

```bash
cp .env.example .env       # fill in credentials
docker compose up
curl http://localhost:8765/api/ready
```

The default image includes **all connector, serializer, and observability extras** (Kafka, S3, MQTT, AMQP, NATS, gNMI, Redis, InfluxDB, GCS, Azure Blob, OpenSearch, Elasticsearch, Avro, Protobuf, Parquet, MsgPack, Prometheus, OpenTelemetry, watchdog, and more). Only `corba` is excluded (requires a source build — extend with a custom `FROM` layer).

## Kubernetes (Helm)

```bash
# Standalone (default) — single PVC at /data holds SQLite DB, schemas, and MIBs
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --set image.tag=1.0.3

# With API key authentication
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --set image.tag=1.0.3 \
  --set apiKey=mysecret

# Cluster mode (3-replica StatefulSet + external PostgreSQL)
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --set image.tag=1.0.3 \
  --set clusterMode.enabled=true \
  --set replicaCount=3 \
  --set envSecret.TRAM_DB_URL.secretName=tram-db \
  --set envSecret.TRAM_DB_URL.secretKey=url

# TLS (cert-manager)
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --set tls.enabled=true \
  --set tls.certManagerIssuer=letsencrypt-prod
```

> Always a `StatefulSet` — `replicaCount=1` standalone, `replicaCount=N` + `clusterMode.enabled=true` cluster. See [`docs/deployment.md`](docs/deployment.md) for full setup.

---

## REST API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Liveness probe |
| GET | `/api/ready` | Readiness probe (db/scheduler/cluster status) |
| GET | `/api/meta` | Version info |
| GET | `/api/plugins` | Registered plugin keys |
| GET | `/api/pipelines` | List all pipelines |
| POST | `/api/pipelines` | Register pipeline (YAML body) |
| POST | `/api/pipelines/{name}/run` | Trigger immediate run |
| GET | `/api/pipelines/{name}/versions` | List saved versions |
| POST | `/api/pipelines/{name}/rollback?version=N` | Restore a version |
| POST | `/webhooks/{path}` | Ingest HTTP payload to webhook source |
| GET | `/metrics` | Prometheus metrics (text/plain) |
| GET | `/api/runs` | Run history (`?pipeline=&status=&limit=&offset=&from_dt=&format=csv`) |
| GET | `/api/cluster/nodes` | Cluster topology |
| GET | `/api/daemon/status` | Scheduler state, active streams |
| GET | `/api/mibs` | List compiled MIB modules |
| POST | `/api/mibs/upload` | Upload + compile a `.mib` file |
| POST | `/api/mibs/download` | Download + compile MIBs from mibs.pysnmp.com |
| DELETE | `/api/mibs/{name}` | Delete a compiled MIB module |
| GET | `/api/schemas` | List all schema files |
| GET | `/api/schemas/{path}` | Read raw schema file content |
| POST | `/api/schemas/upload` | Upload a schema file (optional `?subdir=`) |
| DELETE | `/api/schemas/{path}` | Delete a schema file |

All `/api/*` endpoints require `X-API-Key` header when `TRAM_API_KEY` is set. Health, ready, metrics, and webhooks are always exempt.

Full reference: [`docs/api.md`](docs/api.md)

---

## Project Layout

```
tram/
├── core/             exceptions, context (dlq_count), config, logging
├── interfaces/       BaseSource, BaseSink, BaseTransform, BaseSerializer
├── registry/         @register_* decorators + lookup
├── models/           Pydantic v2 pipeline schema
├── serializers/      json, csv, xml, avro, parquet, msgpack, protobuf
├── transforms/       20 transforms
├── connectors/       23 source + 19 sink connectors
├── cluster/          NodeRegistry + ClusterCoordinator (v0.8.0)
├── persistence/      TramDB (SQLAlchemy Core) + ProcessedFileTracker
├── metrics/          Prometheus metrics registry (incl. Kafka lag, stream depth)
├── telemetry/        OpenTelemetry tracing (v1.0.0)
├── watcher/          Pipeline file watcher (v1.0.0)
├── schema_registry/  Confluent/Apicurio client + magic-byte helpers
├── alerts/           AlertEvaluator (webhook + email, cooldown)
├── pipeline/         loader, executor, manager, linter (v1.0.0)
├── scheduler/        APScheduler (batch) + threads (stream) + rebalance loop
├── api/              FastAPI routers + middleware (auth, rate-limit)
├── daemon/           uvicorn server entrypoint (TLS support)
└── cli/              Typer CLI (pipeline init, mib compile, validate)
helm/                 Helm chart (StatefulSet, TLS, apiKey, cluster mode)
.github/workflows/    ci.yml (lint + unit + integration tests) + release.yml
```

---

## Documentation

| Doc | Contents |
|-----|----------|
| [`docs/architecture.md`](docs/architecture.md) | Design, data flow, execution modes, cluster |
| [`docs/connectors.md`](docs/connectors.md) | All connectors, SNMP MIB, SNMPv3, retry/circuit-breaker |
| [`docs/transforms.md`](docs/transforms.md) | All 20 transforms + expression syntax |
| [`docs/api.md`](docs/api.md) | REST API reference, auth, rate limiting |
| [`docs/deployment.md`](docs/deployment.md) | Docker, k8s, TLS, OTel, env vars |

---

## Development

```bash
pip install -e ".[dev]"
pytest tests/unit/         # 633 unit tests (no network required)
pytest tests/integration/  # 44 tests (2 skipped when pysnmp not installed)
pytest tests/              # all 677 tests
```

---

## Changelog

See [`CHANGELOG.md`](CHANGELOG.md).
