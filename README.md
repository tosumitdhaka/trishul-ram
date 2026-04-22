# TRAM — 🔱 Trishul Real-time Aggregation & Mediation

> A production-ready pipeline daemon for telecom data integration.
> Define your data flows in YAML. TRAM runs them — on a schedule, continuously, or on demand.

[![License](https://img.shields.io/github/license/tosumitdhaka/trishul-ram?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square)](https://www.python.org/)
[![GHCR](https://img.shields.io/badge/Container-GHCR-blue?style=flat-square&logo=github)](https://github.com/tosumitdhaka?tab=packages&repo_name=trishul-ram)
[![CI](https://img.shields.io/github/actions/workflow/status/tosumitdhaka/trishul-ram/ci.yml?style=flat-square&label=CI)](https://github.com/tosumitdhaka/trishul-ram/actions)

---

## What problem does it solve?

Telecom networks produce data in dozens of formats across dozens of protocols — PM counters over SFTP, fault events over SNMP, streaming telemetry over gNMI, logs over syslog, CDRs over CORBA. Getting that data into your analytics stack (OpenSearch, Kafka, InfluxDB, ClickHouse, S3) typically means writing and maintaining bespoke glue scripts for each source-sink pair.

**TRAM replaces that glue.** You write a pipeline YAML that says "poll this SFTP path every 5 minutes, parse the CSV, normalize the fields, and publish to Kafka" — TRAM handles the scheduling, error handling, retries, dead-lettering, and observability.

---

## Use Cases

### PM Data Collection (Performance Management)

Poll NE SFTP servers for PM CSV/XML exports, normalize field names, filter low-quality rows, and forward to Kafka or OpenSearch for real-time dashboards.

```yaml
source:
  type: sftp
  host: ${NE_HOST}
  remote_path: /export/pm/
  file_pattern: "A*.csv"

serializer_in:
  type: csv

transforms:
  - type: rename
    fields: {measObjLdn: cell_id, pmRrcConnMax: rrc_conn_max}
  - type: cast
    fields: {rrc_conn_max: int}
  - type: filter
    condition: "rrc_conn_max >= 0"

sinks:
  - type: kafka
    topic: pm-raw
  - type: opensearch
    index: pm-hourly
    condition: "rrc_conn_max > 1000"   # only high-load cells to OS

schedule:
  type: interval
  interval_seconds: 300
```

### Fault Event Mediation (SNMP Traps → Ticketing)

Receive SNMP traps from network elements, enrich them with MIB OID resolution, apply severity mapping, and push to a REST-based ticketing system.
This works in standalone mode today; in manager mode, `snmp_trap` is intentionally blocked in v1.3.0 until the UDP push-source path lands.

```yaml
source:
  type: snmp_trap
  host: 0.0.0.0
  port: 162
  mib_dirs: [/data/mibs]
  mib_modules: [IF-MIB, ENTITY-MIB]
  resolve_oids: true

transforms:
  - type: value_map
    field: severity
    mapping: {1: critical, 2: major, 3: minor, 4: warning}
  - type: add_field
    fields: {source_system: tram, region: ${REGION}}

sinks:
  - type: rest
    url: ${TICKET_API}/events
    method: POST
    condition: "severity in ['critical', 'major']"
  - type: kafka
    topic: fm-all    # all severities to Kafka for archival

schedule:
  type: stream       # continuous listener
```

### Streaming Telemetry (gNMI → InfluxDB)

Subscribe to gNMI telemetry from routers, decode Protobuf, and write time-series metrics to InfluxDB.

```yaml
source:
  type: gnmi
  host: ${ROUTER_HOST}
  port: 57400
  tls: true
  username: ${GNMI_USER}
  password: ${GNMI_PASS}
  subscriptions:
    - path: /interfaces/interface/state/counters
      mode: stream

serializer_in:
  type: protobuf
  schema_file: /schemas/gnmi.proto
  message_class: Notification

transforms:
  - type: jmespath
    fields:
      iface: path
      rx: val.in_octets
      tx: val.out_octets
  - type: cast
    fields: {rx: int, tx: int}

sinks:
  - type: influxdb
    url: ${INFLUX_URL}
    token: ${INFLUX_TOKEN}
    org: ${INFLUX_ORG}
    bucket: telemetry
    measurement: interface_counters

schedule:
  type: stream
```

### Log Aggregation (Syslog → OpenSearch)

Collect syslog from network nodes, parse structured fields, mask sensitive data, and index into OpenSearch.
This works in standalone mode today; in manager mode, `syslog` is intentionally blocked in v1.3.0 until the UDP push-source path lands.

```yaml
source:
  type: syslog
  host: 0.0.0.0
  port: 514

transforms:
  - type: regex_extract
    field: message
    pattern: "(?P<facility>\\w+)\\[(?P<pid>\\d+)\\]: (?P<body>.*)"
  - type: mask
    fields: [password, secret, community_string]
  - type: timestamp_normalize
    fields: [timestamp]
    output_format: iso

sinks:
  - type: opensearch
    hosts: [${OS_HOST}]
    index: "syslog-{facility}"

schedule:
  type: stream
```

### Multi-format Protocol Mediation (CORBA → Kafka)

Read PM data exposed over a CORBA Itf-N interface (3GPP, Ericsson ENM, Nokia NetAct), convert to JSON, and publish to Kafka for downstream consumers.

```yaml
source:
  type: corba
  ior: ${CORBA_IOR}
  operation: getPMData
  args: {granularity: 15min}

serializer_in:
  type: asn1

transforms:
  - type: flatten
  - type: rename
    fields: {measValue: value, measType: counter}

sinks:
  - type: kafka
    brokers: [${KAFKA_BROKERS}]
    topic: pm-corba

schedule:
  type: interval
  interval_seconds: 900
```

---

## How It Works

```
Source ──► Deserialize ──► Transforms ──► ┬──► condition? ──► per-sink transforms ──► Serialize ──► Sink A
                                          ├──► condition? ──► per-sink transforms ──► Serialize ──► Sink B
                                          └──► (on any error) ──────────────────────────────────► DLQ
```

- **Sources** emit raw bytes (files, network streams, API responses)
- **Deserializers** decode to `list[dict]` (CSV, JSON, XML, Avro, Protobuf, PM-XML, …)
- **Transforms** are applied per-record — one bad record goes to DLQ, others continue
- **Condition filters** route records to specific sinks
- **Serializers** re-encode per sink (Avro to Kafka, JSON to SFTP, CSV to S3 — from one pipeline)
- **Schedules** are `interval`, `cron`, `manual`, or `stream` (continuous)

All plugins (sources, sinks, transforms, serializers) self-register via decorators — adding a new protocol requires zero changes to the core engine.

---

## Quick Start

```bash
pip install tram
tram version
tram plugins                          # list all registered connectors

# Scaffold a new pipeline
tram pipeline init pm-ingest --output pipelines/pm-ingest.yaml

# Validate before running
tram validate pipelines/pm-ingest.yaml

# Test without side effects
tram run pipelines/pm-ingest.yaml --dry-run

# Start the daemon
tram daemon &
open http://localhost:8765/ui/

# Manage pipelines
tram pipeline add pipelines/pm-ingest.yaml
tram pipeline run pm-ingest
tram pipeline history pm-ingest
```

Or with Docker:

```bash
docker compose up
curl http://localhost:8765/api/ready
```

---

## Deployment Modes

| Mode | When to use |
|------|-------------|
| **Standalone** (default) | Single pod — scheduler + DB + UI + execution. Simplest setup. |
| **Manager + Worker** | Scale execution horizontally. Manager runs as a single-replica StatefulSet; workers are stateless executors with internal agent `:8766` and a published ingress service targeting worker `:8767` for `/webhooks/*`. |

```bash
# Standalone (Helm)
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set image.tag=latest

# Manager + Worker (3 workers)
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set image.tag=latest \
  --set manager.enabled=true \
  --set worker.replicas=3 \
  --set apiKey=mysecret
```

Quick-start examples use `latest`. For production deployments, pin a specific release tag in Helm values.

---

## Plugin Registry

| Category | Count | Keys |
|----------|-------|------|
| **Sources** | 24 | `sftp` `kafka` `rest` `snmp_trap` `snmp_poll` `syslog` `gnmi` `corba` `nats` `mqtt` `amqp` `websocket` `sql` `clickhouse` `influxdb` `redis` `s3` `gcs` `azure_blob` `elasticsearch` `prometheus_rw` `webhook` `local` `ftp` |
| **Sinks** | 20 | `sftp` `kafka` `rest` `opensearch` `snmp_trap` `mqtt` `amqp` `nats` `sql` `clickhouse` `influxdb` `redis` `s3` `gcs` `azure_blob` `websocket` `elasticsearch` `ves` `local` `ftp` |
| **Serializers** | 12 | `json` `ndjson` `csv` `xml` `avro` `parquet` `protobuf` `msgpack` `bytes` `text` `asn1` `pm_xml` |
| **Transforms** | 23 | `rename` `cast` `add_field` `drop` `filter` `value_map` `flatten` `json_flatten` `explode` `melt` `aggregate` `enrich` `deduplicate` `regex_extract` `template` `mask` `validate` `sort` `limit` `jmespath` `unnest` `timestamp_normalize` `hex_decode` |

Install only what you need:

```bash
pip install tram[kafka,snmp,avro]          # Kafka + SNMP + Avro
pip install tram[manager,kafka,opensearch] # manager mode with common connectors
pip install tram[all]                      # everything (except corba — system package)
```

---

## Key Capabilities

- **Hot-reload** — update pipeline YAML via API or file watcher; no restart needed
- **Broadcast push streams** — `webhook` and `prometheus_rw` scale across all healthy workers in manager mode
- **Pipeline versioning** — every update saved; one-command rollback to any previous version
- **AI-assisted authoring** — `POST /api/ai/suggest` generates or explains pipeline YAML (Anthropic / OpenAI / local LLM)
- **Dead Letter Queue** — failed records wrapped in a JSON envelope and routed to a configurable DLQ sink
- **Per-sink routing** — condition expressions, independent retry/circuit-breaker, and separate serializer per sink
- **Observability** — Prometheus metrics, OpenTelemetry tracing, live web dashboard, per-run history
- **Security** — API key auth, browser session auth (HMAC tokens), TLS, rate limiting per IP
- **Schema management** — upload `.proto`, `.avsc`, `.xsd` files at runtime; Confluent/Apicurio schema registry integration

---

## Documentation

| Doc | Contents |
|-----|----------|
| [Architecture](docs/architecture.md) | System design, execution modes, manager+worker internals |
| [Connectors](docs/connectors.md) | All sources and sinks — config reference, SNMPv3, retry/circuit-breaker |
| [Transforms](docs/transforms.md) | All 23 transforms and condition expression syntax |
| [API Reference](docs/api.md) | REST API endpoints, authentication, rate limiting |
| [Deployment](docs/deployment.md) | Docker, Kubernetes/Helm, TLS, environment variables |
| [Roadmap](docs/roadmap.md) | Planned features and known issues |
| [Changelog](docs/changelog.md) | Full release history |

---

## Development

```bash
pip install -e ".[dev]"
pytest tests/unit/        # 1,250 unit tests — no network required
pytest tests/integration/ # integration tests (SFTP, Kafka, schema registry)
ruff check .              # lint
```

---

<div align="center">

**Made with 🔱 by [Sumit Dhaka](https://github.com/tosumitdhaka)**

[![GitHub](https://img.shields.io/badge/GitHub-tosumitdhaka-181717?style=for-the-badge&logo=github)](https://github.com/tosumitdhaka)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0077B5?style=for-the-badge&logo=linkedin)](https://www.linkedin.com/in/sumit-dhaka-a5a796b3/)

*If this project helps you, please consider [starring it](https://github.com/tosumitdhaka/trishul-ram).*

</div>
