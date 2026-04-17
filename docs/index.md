---
layout: default
title: TRAM Documentation
---

# TRAM Documentation

**Trishul Real-time Aggregation & Mediation**

Lightweight, container-native Python daemon for telecom data pipeline orchestration.

**Version:** 1.3.0 | **Status:** Production-ready | **Python:** 3.11+

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
- **[Roadmap](roadmap.md)** - Planned features and version checklist

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
docker pull ghcr.io/tosumitdhaka/trishul-ram:latest
docker compose up
curl http://localhost:8765/api/ready
```

### Kubernetes (Helm)

Quick-start examples below use `latest`. For production, pin `image.tag` to a specific release such as `1.3.0`.

```bash
# Standalone mode (SQLite, single pod)
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set image.tag=latest

# Manager + Worker mode (3 workers, dedicated worker image)
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set image.tag=latest \
  --set manager.enabled=true \
  --set worker.replicas=3 \
  --set worker.image.repository=trishul-ram-worker \
  --set worker.image.tag=latest \
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

### Cluster Mode / Manager + Worker (v1.2.0+)
- **Manager** StatefulSet — scheduler, DB, and UI; dispatches runs to workers via HTTP
- **Worker** StatefulSet — stateless executors; internal agent on `:8766`, ingress-only webhook receiver on `:8767`
- Dedicated `Dockerfile.worker` — lighter image without apscheduler/sqlalchemy/UI
- Least-loaded dispatch plus broadcast placement for HTTP push streams
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
├── pipeline-controller-design.md # PipelineController design (v1.1.4)
├── roadmap.md                    # Planned features and version checklist
├── changelog.md                  # Full release history
└── checklist.md                  # Development checklist
```

---

## Support & Contributing

- **Issues**: [GitHub Issues](https://github.com/tosumitdhaka/trishul-ram/issues)
- **Discussions**: [GitHub Discussions](https://github.com/tosumitdhaka/trishul-ram/discussions)
- **License**: Apache 2.0

---

## Version History

See [changelog.md](changelog.md) for detailed release notes.

**Current Release:** v1.3.0 (2026-04-17)
- HTTP push streams (`webhook`, `prometheus_rw`) now broadcast across all healthy workers in manager mode
- Placement tracking, stale-slot reconciliation, and placement APIs are available for active broadcast streams
- Worker stats are unified across batch and stream runs, including byte counters and load-aware dispatch inputs
- Manager runs as a StatefulSet with PVC reuse support during Deployment → StatefulSet upgrades

**v1.2.0** (2026-04-10)
- Manager + Worker mode (`TRAM_MODE=manager/worker`) — replaces shared-DB cluster model
- `Dockerfile.worker` — dedicated worker image without scheduler/DB/UI
- Helm: manager/worker split introduced; SQLite on RWO PVC (no PostgreSQL required)
- `WorkerPool` — health polling, dispatch, run-complete callback handling

**v1.1.4**
- `PipelineController` — unified pipeline lifecycle authority; 4-state machine
- `melt` transform; SNMP fixes; DB-backed `runtime_status`

---

*Last updated: 2026-04-17*
