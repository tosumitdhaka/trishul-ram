---
layout: default
title: TRAM Documentation
---

# TRAM Documentation

**Trishul Real-time Aggregation & Mediation**

Lightweight, container-native Python daemon for telecom data pipeline orchestration.

**Version:** 1.1.3 | **Status:** Production-ready | **Python:** 3.11+

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

- **[Connectors](connectors.md)** - All 24 sources and 20 sinks (SFTP, Kafka, REST, SNMP, etc.)
- **[Transforms](transforms.md)** - All 20 transforms and expression syntax
- **[API Reference](api.md)** - REST API endpoints, authentication, rate limiting

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
docker pull ghcr.io/tosumitdhaka/trishul-ram:1.1.3
docker compose up
curl http://localhost:8765/api/ready
```

### Kubernetes (Helm)

```bash
# Standalone mode
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set image.tag=1.1.3

# Cluster mode (3 replicas + PostgreSQL)
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set image.tag=1.1.3 \
  --set clusterMode.enabled=true \
  --set replicaCount=3
```

---

## Plugin Registry

| Category | Count | Examples |
|----------|-------|----------|
| **Sources** | 24 | sftp, kafka, rest, snmp_poll, snmp_trap, syslog, webhook, mqtt, amqp, nats, gnmi, sql, clickhouse, influxdb, corba, websocket, prometheus_rw |
| **Sinks** | 20 | sftp, kafka, rest, opensearch, snmp_trap, mqtt, amqp, nats, sql, clickhouse, influxdb, ves, websocket, elasticsearch |
| **Serializers** | 12 | json, ndjson, csv, xml, avro, parquet, protobuf, msgpack, bytes, text, asn1, pm_xml |
| **Transforms** | 20 | rename, cast, filter, aggregate, jmespath, flatten, explode, deduplicate, mask, validate, template, enrich |

---

## Project Roadmaps

- [v1.2.0 Roadmap](roadmap_1.2.0.md) - ClickHouse batching, UI enhancements
- [v1.1.0 Planning](roadmap_1.1.0/) - Feature specifications (wizard, metrics, alerts)
- [v1.1.0 Plan](v1.1.0-plan.md) - Original implementation plan

---

## Key Features

### Pipeline Management
- YAML-based pipeline definitions
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

### Cluster Mode
- Multi-node StatefulSet deployment
- Consistent hashing for pipeline distribution
- Automatic rebalancing on node join/leave
- Shared PostgreSQL state

### Security
- API key authentication
- Browser session authentication (HMAC tokens)
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
├── index.md           # This page
├── architecture.md    # System design and data flow
├── api.md            # REST API reference
├── connectors.md     # All sources and sinks
├── deployment.md     # Docker, k8s, environment variables
├── transforms.md     # Transform reference
└── roadmap_*.md      # Version roadmaps
```

---

## Support & Contributing

- **Issues**: [GitHub Issues](https://github.com/tosumitdhaka/trishul-ram/issues)
- **Discussions**: [GitHub Discussions](https://github.com/tosumitdhaka/trishul-ram/discussions)
- **License**: Apache 2.0

---

## Version History

See [CHANGELOG.md](../CHANGELOG.md) for detailed release notes.

**Current Release:** v1.1.3
- Comprehensive documentation (CLAUDE.md, CHECKLIST.md)
- Enhanced .env.example with all 50+ variables
- PM XML serializer
- 846 tests passing, 69% coverage
- Zero lint errors

---

*Last updated: 2026-04-03*
