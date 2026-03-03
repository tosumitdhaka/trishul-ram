# TRAM — Trishul Real-time Adapter & Mapper

> Lightweight, container-native Python daemon that moves and transforms telecom data (PM/FM/Logs) across protocols.

**Version:** 0.2.0 | **Status:** Active development | **Python:** 3.11+

---

## What is TRAM?

TRAM is a pipeline daemon for telecom data integration. It runs as an always-on service, accepting pipeline definitions as YAML files, and executing them on a schedule (interval, cron) or continuously (stream). Each pipeline wires together:

```
Source → Deserialize → Transform chain → Serialize → Sink
```

Plugins for sources, sinks, serializers, and transforms self-register via decorators — adding a new protocol (Kafka, OpenSearch, VES) requires zero changes to the core engine.

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

# Or via REST
curl http://localhost:8765/api/health
curl http://localhost:8765/api/pipelines
```

---

## Plugin Registry (v0.2.0)

| Category | Keys |
|----------|------|
| Sources | `sftp`, `local`, `rest`, `kafka` |
| Sinks | `sftp`, `local`, `rest`, `kafka`, `opensearch` |
| Serializers | `json`, `csv`, `xml` |
| Transforms | `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`, `flatten`, `timestamp_normalize`, `aggregate`, `enrich` |

Optional extras: `pip install tram[kafka]` · `pip install tram[opensearch]`

---

## Pipeline YAML

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
    indent: 2

  sink:
    type: sftp
    host: ${MED_SFTP_HOST}
    username: ${MED_SFTP_USER}
    password: ${MED_SFTP_PASS}
    remote_path: /ingest/pm/
    filename_template: "pm_{pipeline}_{timestamp}.json"
```

All `${VAR}` and `${VAR:-default}` placeholders are resolved from environment variables at load time.

---

## Docker

```bash
cp .env.example .env       # fill in credentials
docker compose up
curl http://localhost:8765/api/ready
```

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
| GET | `/api/runs` | Run history |

Full reference: [`docs/api.md`](docs/api.md)

---

## Adding a New Connector

Three steps, zero core changes. See [`docs/connectors.md`](docs/connectors.md).

```python
# 1. Create the class
@register_source("kafka")
class KafkaSource(BaseSource):
    def read(self):          # infinite generator for stream mode
        for msg in consumer:
            yield msg.value, {"topic": msg.topic}

# 2. Import it in connectors/__init__.py
from tram.connectors.kafka import source  # one line

# 3. Done — pipeline YAML immediately supports: source.type: kafka
```

---

## Project Layout

```
tram/
├── core/           exceptions, context, config, logging
├── interfaces/     BaseSource, BaseSink, BaseTransform, BaseSerializer
├── registry/       @register_* decorators + lookup
├── models/         Pydantic v2 pipeline schema
├── serializers/    json, csv, xml
├── transforms/     rename, cast, add_field, drop, value_map, filter
├── connectors/     sftp/source, sftp/sink
├── pipeline/       loader (YAML→config), executor (batch+stream), manager
├── scheduler/      APScheduler (batch) + threads (stream)
├── api/            FastAPI routers (health, pipelines, runs)
├── daemon/         uvicorn server entrypoint
└── cli/            Typer CLI (direct + daemon-proxy commands)
```

---

## Documentation

| Doc | Contents |
|-----|----------|
| [`docs/architecture.md`](docs/architecture.md) | Design, data flow, execution modes |
| [`docs/connectors.md`](docs/connectors.md) | How to add new connectors |
| [`docs/transforms.md`](docs/transforms.md) | All transforms + expression syntax |
| [`docs/api.md`](docs/api.md) | REST API reference |
| [`docs/deployment.md`](docs/deployment.md) | Docker, k8s, env vars |

---

## Development

```bash
pip install -e ".[dev]"
pytest tests/unit/         # 66 unit tests (no network)
pytest tests/integration/  # 3 integration tests (mocked SFTP)
pytest tests/             # all 69 tests
```

---

## Changelog

See [`CHANGELOG.md`](CHANGELOG.md).
