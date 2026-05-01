---
layout: default
title: TRAM Documentation
---

# TRAM Documentation

**Trishul Real-time Aggregation & Mediation**

Lightweight, container-native Python daemon for telecom data pipeline orchestration.

**Version:** 1.3.3 | **Status:** Production-ready | **Python:** 3.13+

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

- **[Pipeline Controller Design](pipeline-controller-design.md)** - Historical design notes for the v1.1.x controller transition; current manager/worker architecture is documented in `architecture.md`
- **[Roadmap](roadmap.md)** - Planned features and version checklist
- **[v1.3.3 Plan](v1.3.3_plan.md)** - Completed UI revalidation, shared-system consolidation, and release-close record for `1.3.3`
- **Archive**
  - **[v1.3.2 Plan](archive/v1.3.2-plan.md)** - Archived consolidated `1.3.2` design and implementation plan covering stats parity, multi-worker UDP streams, ASN.1 flattening, CDR record shaping, and batch resilience
  - **[v1.3.1 Plan](archive/v1.3.1-plan.md)** - Archived planning document for the `1.3.1` implementation slice set
  - **[v1.3.0 Plan *Architecture Change*](archive/v1.3.0-plan.md)** - Archived design document for the `1.3.0` broadcast-streams rollout

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

Quick-start examples below use `latest`. For production, pin `image.tag` to a specific release such as `1.3.3`.

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
| **Transforms** | 27 | rename, cast, filter, aggregate, jmespath, flatten, json_flatten, explode, unnest, select_from_list, coalesce_fields, project, inject_meta, melt, deduplicate, mask, validate, template, enrich, hex_decode |

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
- Least-loaded dispatch plus multi-worker placement for HTTP push streams
- `TRAM_MODE=manager` / `TRAM_MODE=worker` — SQLite on manager PVC is sufficient (single writer)
- `PipelineController` — unified pipeline lifecycle authority; core lifecycle states are `scheduled`, `running`, `stopped`, `error`, with `degraded` / `reconciling` surfaced for multi-worker placements in v1.3.0

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
├── pipeline-controller-design.md # Historical v1.1.x controller design notes
├── roadmap.md                    # Planned features and version checklist
├── v1.3.3_plan.md                # Current 1.3.3 release record and UI validation plan
├── changelog.md                  # Full release history
├── checklist.md                  # Development checklist
└── archive/                      # Archived version-specific design and planning docs
    ├── v1.3.2-plan.md            # Archived consolidated 1.3.2 backend implementation plan
    ├── v1.3.1-plan.md            # Archived 1.3.1 implementation plan
    └── v1.3.0-plan.md            # Archived 1.3.0 implementation plan
```

---

## Support & Contributing

- **Issues**: [GitHub Issues](https://github.com/tosumitdhaka/trishul-ram/issues)
- **Discussions**: [GitHub Discussions](https://github.com/tosumitdhaka/trishul-ram/discussions)
- **License**: Apache 2.0

---

## Version History

See [changelog.md](changelog.md) for detailed release notes.

**Current Release:** v1.3.3 (2026-05-01)
- Full UI revalidation completed across Dashboard, Pipelines, Detail, Editor, Runs, Cluster, Templates, Settings, MIBs, Schemas, and Plugins in both manager and standalone mode
- Dashboard overview now uses shared records/bytes in/out cards, a bytes-processed load chart with metric toggle and bucket tooltip, contextual `Run now` handling for manual pipelines, and manual refresh
- Cluster now presents runtime state first, merged input/output traffic summaries, per-worker processed records/bytes totals, and clearer active pipeline/live metric truth
- Shell, route aliases, and shared renderers were consolidated: `#templates` now resolves through Pipelines, hash navigation preserves browser history, login boot no longer flashes unauthorized errors, and shared YAML diff/run-monitor helpers back the main flows
- Pipeline detail/version flows were tightened with version dedupe, scoped version YAML caching, rollback correctness, shared compare/view/download tooling, and visible failed-run reasons for no-worker batch dispatch
- Schemas/MIBs/template/settings surfaces were normalized onto the shared UI system, including search, theme-safe modal/result chrome, and raw-source-aware MIB management

**v1.3.2** (2026-04-21)
- Standalone live stats: local stream runs now feed `StatsStore` so `/api/pipelines/{name}/placement` returns a live single-slot view in standalone mode
- Manager operational metrics: 8 new `tram_mgr_*` Prometheus series for dispatch, redispatch, reconcile actions, placement status, worker health, and callback receipt; `/metrics` is process-local
- UDP multi-worker streams: `syslog` and `snmp_trap` sources work in manager mode with `kubernetes: enabled: true`; per-pipeline NodePort Services; `count: N` uses manual Endpoints pinned to dispatched workers; L012 enforces the kubernetes requirement
- ASN.1 decode improvements: concatenated BER multi-record split, ordered `message_classes` fallback, and the explicit `json_flatten` / `hex_decode` shaping path
- CDR shaping primitives: dotted-path support across core transforms plus `select_from_list`, `coalesce_fields`, `project`, conditional `drop`, and narrow wildcard path support for explicit LTE/SGW/PGW mediation pipelines
- Batch resilience: `BatchReconciler` adopts or clears lost worker-owned batch runs after manager restart or worker loss, reusing the normal final status path
- Large-file batch efficiency: `record_chunk_size` enables bounded serial batch decode windows, and ASN.1 BER payloads now support incremental record parsing instead of one giant in-memory decode
- Safer batch file output: serial batch `csv` / `ndjson` sinks stage temp files per source unit, publish only on successful source completion, and can optionally run post-batch heap cleanup via `post_batch_cleanup: true`

**v1.3.1** (2026-04-20)
- `workers.count: N` and `workers.list` placement behavior are now implemented for multi-worker push streams in manager mode
- Dedicated per-pipeline Kubernetes Services are available for active `webhook` and `prometheus_rw` streams, including pinned-worker `workers.list` endpoints
- File sinks support shared source filename variables, executor-side `{field.*}` partitioning, and append-mode rolling via `max_records`, `max_time`, and `max_bytes`
- SNMP connectors now target `pysnmp>=7,<8`, and the `1.3.1` pass was revalidated on a live kind cluster for placement, ingress, and scale-down recovery

**v1.2.0** (2026-04-10)
- Manager + Worker mode (`TRAM_MODE=manager/worker`) — replaces shared-DB cluster model
- `Dockerfile.worker` — dedicated worker image without scheduler/DB/UI
- Helm: manager/worker split introduced; SQLite on RWO PVC (no PostgreSQL required)
- `WorkerPool` — health polling, dispatch, run-complete callback handling

**v1.1.4**
- `PipelineController` — unified pipeline lifecycle authority; 4-state machine
- `melt` transform; SNMP fixes; DB-backed `runtime_status`

---

*Last updated: 2026-05-01*
