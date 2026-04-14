---
layout: default
title: TRAM Documentation
---

# TRAM Documentation

**Trishul Real-time Aggregation & Mediation**

Lightweight, container-native Python daemon for telecom data pipeline orchestration.

**Version:** 1.2.1 | **Status:** Production-ready | **Python:** 3.11+

---

## Quick Links

- [GitHub Repository](https://github.com/tosumitdhaka/trishul-ram)
- [Docker Image](https://ghcr.io/tosumitdhaka/trishul-ram)
- [Helm Chart](https://ghcr.io/tosumitdhaka/charts/trishul-ram)

---

## Core Documentation

### Getting Started

- **[Architecture](architecture.md)** - System design, data flow, execution modes, cluster coordination
- **[Deployment Guide](deployment.md)** - Docker, Kubernetes, TLS, environment variables

### Features & Configuration

- **[Connectors](connectors.md)** - All sources and sinks (SFTP, Kafka, REST, SNMP, ClickHouse, etc.)
- **[Transforms](transforms.md)** - All transforms and expression syntax
- **[API Reference](api.md)** - REST API endpoints, authentication, rate limiting

### Design & Roadmap

- **[Pipeline Controller Design](pipeline-controller-design.md)** - PipelineController architecture, state machine, DB schema, cluster ownership (implemented in v1.1.4)

---

## Quick Start

### Local Development

```bash
pip install -e ".[dev]"
tram version
tram plugins

# Validate a pipeline
tram validate pipelines/minimal.yaml

# Run the daemon
tram daemon &

# Open web UI
open http://localhost:8765/ui/
```

### Docker

```bash
docker pull ghcr.io/tosumitdhaka/trishul-ram:1.2.1
docker compose up
curl http://localhost:8765/api/ready
```

### Kubernetes (Helm)

```bash
# Standalone mode (SQLite, single pod)
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set image.tag=1.2.1

# Manager + Worker mode (3 workers, dedicated worker image)
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set image.tag=1.2.1 \
  --set manager.enabled=true \
  --set worker.replicas=3 \
  --set worker.image.repository=trishul-ram-worker \
  --set worker.image.tag=1.2.1 \
  --set apiKey=mysecret
```

---

## Plugin Registry

| Category | Count | Examples |
|----------|-------|----------|
| **Sources** | 24 | sftp, kafka, rest, snmp_poll, snmp_trap, syslog, webhook, mqtt, amqp, nats, gnmi, sql, clickhouse, influxdb, corba, websocket, prometheus_rw |
| **Sinks** | 20 | sftp, kafka, rest, opensearch, snmp_trap, mqtt, amqp, nats, sql, clickhouse, influxdb, ves, websocket, elasticsearch |
| **Serializers** | 12 | json, ndjson, csv, xml, avro, parquet, protobuf, msgpack, bytes, text, asn1, pm_xml |
| **Transforms** | 21 | rename, cast, filter, aggregate, jmespath, flatten, explode, melt, deduplicate, mask, validate, template, enrich |

---

## Key Features

### Pipeline Management
- YAML-based pipeline definitions with `${ENV_VAR}` substitution
- Hot-reload via REST API or file watcher
- Versioning and rollback support
- Pipeline templates library

### Execution Modes
- **Batch**: Interval, cron, or manual trigger
- **Stream**: Continuous processing (Kafka, NATS, webhooks)
- **Parallel workers**: Multi-threaded processing per pipeline

### Observability
- Live metrics dashboard (records, errors, latency)
- Prometheus metrics export
- OpenTelemetry tracing
- Per-pipeline run history

### Reliability
- Dead Letter Queue (DLQ) for failed records
- Per-sink retry with exponential backoff
- Circuit breakers for failing sinks
- Rate limiting per pipeline

### Web UI
- Bootstrap 5 responsive interface
- Pipeline wizard with AI assist
- Live metrics and sparklines
- Alert rules management
- MIB and schema file management

### Cluster Mode / Manager + Worker (v1.2.0)
- **Manager** Deployment — scheduler, DB, and UI; dispatches runs to workers via HTTP
- **Worker** StatefulSet — stateless executors; receive run request, POST result back to manager
- Dedicated `Dockerfile.worker` — lighter image without apscheduler/sqlalchemy/UI
- Round-robin + least-loaded dispatch across workers
- `TRAM_MODE=manager` / `TRAM_MODE=worker` — SQLite on manager PVC is sufficient (single writer)
- `PipelineController` — unified pipeline lifecycle authority; 4-state machine (`scheduled`, `running`, `stopped`, `error`)

### Security
- API key authentication
- Browser session authentication (HMAC tokens, shared secret via `tram-auth` K8s secret)
- TLS/HTTPS support
- Rate limiting per IP

---

## Architecture Overview

```
Source → Deserialize → Transform → [per-sink routing] → Serialize → Sink
                                            ↓
                                    (on any error)
                                            ↓
                                        DLQ sink
```

**Plugin-based architecture:**
- All connectors, transforms, and serializers are plugins
- Self-register via decorators at import time
- Zero core engine changes to add new plugins

---

## Documentation Structure

```
docs/
├── index.md                      # This page
├── architecture.md               # System design, data flow, manager+worker
├── api.md                        # REST API reference
├── connectors.md                 # All sources and sinks
├── deployment.md                 # Docker, k8s, environment variables
├── transforms.md                 # Transform reference
└── pipeline-controller-design.md # PipelineController design (v1.1.4)
```

---

## Support & Contributing

- **Issues**: [GitHub Issues](https://github.com/tosumitdhaka/trishul-ram/issues)
- **Discussions**: [GitHub Discussions](https://github.com/tosumitdhaka/trishul-ram/discussions)
- **License**: Apache 2.0

---

## Version History

See [CHANGELOG.md](../CHANGELOG.md) for detailed release notes.

**Current Release:** v1.2.1 (2026-04-14)
- Per-record `errors` list propagated through worker callback chain; skip reasons visible in run history UI
- `PipelineRunContext.note_skip()` — appends skip reason to errors without double-counting `records_skipped`
- Manager health poll logs silenced: single `Worker pool: N/M healthy` summary line on change only
- `/api/ready` returns `cluster` field: `"manager · N/M workers"` or `"standalone"`
- Round-robin + least-loaded worker dispatch; `assigned_pipelines` shown on Workers page
- Dashboard: Start / Stop / Download buttons; Run Now moved to detail page only
- Full CSS variable coverage (light/dark mode) across all UI pages

**v1.2.0** (2026-04-10)
- Manager + Worker mode (`TRAM_MODE=manager/worker`) — replaces shared-DB cluster model
- `Dockerfile.worker` — dedicated worker image without scheduler/DB/UI
- Helm: manager Deployment + worker StatefulSet; SQLite on RWO PVC (no PostgreSQL required)
- `WorkerPool` — health polling, dispatch, run-complete callback handling

**v1.1.4**
- `PipelineController` — unified pipeline lifecycle authority; 4-state machine
- `melt` transform; SNMP fixes; DB-backed `runtime_status`

---

*Last updated: 2026-04-14*
