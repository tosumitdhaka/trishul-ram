# TRAM Deployment Guide

## Quick Start

```bash
pip install -e ".[dev]"
tram daemon &
tram pipeline list
curl http://localhost:8765/api/health
```

## Environment Variables

All configuration is via environment variables (12-factor).

| Variable | Default | Description |
|----------|---------|-------------|
| `TRAM_HOST` | `0.0.0.0` | API server bind address |
| `TRAM_PORT` | `8765` | API server port |
| `TRAM_PIPELINE_DIR` | `./pipelines` | Directory scanned for pipeline YAMLs at startup |
| `TRAM_DB_URL` | _(empty)_ | SQLAlchemy database URL (see below); if empty, uses `TRAM_DB_PATH` |
| `TRAM_DB_PATH` | `~/.tram/tram.db` | SQLite file path (used when `TRAM_DB_URL` is unset) |
| `TRAM_NODE_ID` | hostname | Node identifier stored in run_history for multi-node tracing |
| `TRAM_SHUTDOWN_TIMEOUT_SECONDS` | `30` | Seconds to drain in-flight runs before forced stop |
| `TRAM_API_URL` | `http://localhost:8765` | Daemon URL used by CLI proxy commands |
| `TRAM_LOG_LEVEL` | `INFO` | Log level: DEBUG, INFO, WARNING, ERROR |
| `TRAM_LOG_FORMAT` | `json` | Log format: `json` or `text` |
| `TRAM_WORKERS` | `1` | Uvicorn worker count |
| `TRAM_RELOAD_ON_START` | `true` | Auto-load pipelines from TRAM_PIPELINE_DIR at startup |
| `TRAM_SMTP_HOST` | `localhost` | SMTP host for email alert actions |
| `TRAM_SMTP_PORT` | `587` | SMTP port |
| `TRAM_SMTP_USER` | _(none)_ | SMTP username (optional) |
| `TRAM_SMTP_PASS` | _(none)_ | SMTP password (optional) |
| `TRAM_SMTP_TLS` | `true` | Use STARTTLS (`false` for plain SMTP) |
| `TRAM_SMTP_FROM` | `tram@localhost` | Sender address for alert emails |

### Database backends (v0.7.0)

```bash
# SQLite (default — zero config)
# TRAM_DB_URL not set; uses ~/.tram/tram.db

# SQLite at a custom path
TRAM_DB_URL=sqlite:////data/tram.db

# PostgreSQL (requires pip install tram[postgresql])
TRAM_DB_URL=postgresql+psycopg2://tram:secret@postgres:5432/tramdb

# MySQL / MariaDB (requires pip install tram[mysql])
TRAM_DB_URL=mysql+pymysql://tram:secret@mysql:3306/tramdb
```

Schema migrations run automatically at startup: `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` guards handle upgrades from v0.6.0 databases.

## Docker

### Build and run

```bash
docker build -t tram:latest .
docker run -p 8765:8765 \
  -v ./pipelines:/pipelines:ro \
  -v tram-data:/root/.tram \
  -e TRAM_PIPELINE_DIR=/pipelines \
  -e NE_SFTP_HOST=10.0.0.1 \
  tram:latest
```

Mount a volume at `/root/.tram` (or set `TRAM_DB_PATH`) to persist run history and pipeline versions across container restarts.

### docker-compose

```bash
cp .env.example .env
# Edit .env with your credentials
docker compose up
```

## Kubernetes — Helm (recommended)

TRAM ships a production-ready Helm chart in `helm/`. Published to GHCR OCI on every release tag.

### Install

```bash
# Add chart from OCI registry
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --namespace tram --create-namespace \
  --set image.tag=0.6.0

# Mount pipelines from local files
helm upgrade tram oci://ghcr.io/OWNER/charts/tram \
  --set-file "pipelines.pm-ingest\.yaml=./pipelines/pm-ingest.yaml"

# Inject SFTP credentials from a Kubernetes Secret
helm upgrade tram oci://ghcr.io/OWNER/charts/tram \
  --set envSecret.NE_SFTP_PASS.secretName=ne-creds \
  --set envSecret.NE_SFTP_PASS.secretKey=password
```

### Key values

| Value | Default | Description |
|-------|---------|-------------|
| `image.repository` | `ghcr.io/OWNER/tram` | Docker image repository |
| `image.tag` | `"0.6.0"` | Image tag |
| `replicaCount` | `1` | Fixed at 1 — clustering planned for a future release |
| `persistence.enabled` | `true` | Mount SQLite PVC at `/data` |
| `persistence.size` | `1Gi` | PVC size |
| `persistence.accessMode` | `ReadWriteOnce` | PVC access mode |
| `env` | `{}` | Plain env vars |
| `envSecret` | `{}` | Env vars from Secret (`secretName`/`secretKey`) |
| `pipelines` | `{}` | Pipeline YAMLs mounted as ConfigMap at `/pipelines` |
| `podAnnotations` | `{}` | e.g. `prometheus.io/scrape: "true"` |

### Scaling

TRAM v0.6.0 runs as a single-replica standalone daemon. Multi-replica clustering (StatefulSet self-organising workers or Manager+Worker split) is planned for a future release.

### Prometheus scraping

```yaml
# values.yaml
podAnnotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8765"
  prometheus.io/path: "/metrics"
```

## Kubernetes — Raw manifests

For environments without Helm, use the chart as a reference. Minimum manifests needed:

```bash
# ConfigMap — pipeline YAMLs
kubectl create configmap tram-pipelines --from-file=./pipelines/

# PVC — SQLite persistence
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: tram-data
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
EOF

# Deployment
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tram
spec:
  replicas: 1
  selector:
    matchLabels:
      app: tram
  template:
    metadata:
      labels:
        app: tram
    spec:
      containers:
      - name: tram
        image: ghcr.io/OWNER/tram:0.6.0
        command: ["tram", "daemon"]
        ports:
        - containerPort: 8765
        env:
        - name: TRAM_DB_PATH
          value: /data/tram.db
        - name: TRAM_PIPELINE_DIR
          value: /pipelines
        volumeMounts:
        - name: pipelines
          mountPath: /pipelines
          readOnly: true
        - name: data
          mountPath: /data
        livenessProbe:
          httpGet: {path: /health, port: 8765}
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet: {path: /health, port: 8765}
          initialDelaySeconds: 5
          periodSeconds: 10
      volumes:
      - name: pipelines
        configMap:
          name: tram-pipelines
      - name: data
        persistentVolumeClaim:
          claimName: tram-data
EOF
```

## Pipeline Variables

Pipeline YAMLs support `${VAR:-default}` syntax:

```yaml
source:
  host: ${NE_SFTP_HOST}          # required — error if not set
  port: ${NE_SFTP_PORT:-22}      # optional — defaults to 22
```

## Logging

TRAM outputs structured JSON logs to stdout:

```json
{"timestamp": "2026-03-03T12:00:00Z", "level": "INFO", "logger": "tram.pipeline.executor", "message": "Batch run completed", "pipeline": "pm-ingest", "run_id": "abc12345", "records_in": 1500, "records_out": 1487}
```

Configure with a log aggregator (Filebeat, Fluentd, Vector) to forward to Elasticsearch/OpenSearch/Loki.

Set `TRAM_LOG_FORMAT=text` for human-readable output during development.

## Prometheus Metrics

Install prometheus-client:

```bash
pip install tram[metrics]
```

Scrape endpoint:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: tram
    static_configs:
      - targets: ['tram:8765']
    metrics_path: /metrics
```

Available metrics (all labeled by `pipeline`):

| Metric | Type | Description |
|--------|------|-------------|
| `tram_records_in_total` | Counter | Records read from source |
| `tram_records_out_total` | Counter | Records written to sinks |
| `tram_records_skipped_total` | Counter | Records skipped (filtered or error) |
| `tram_errors_total` | Counter | Processing errors |
| `tram_chunk_duration_seconds` | Histogram | Chunk processing time |

## Webhook Integration

To receive data from Filebeat HTTP output or any HTTP client:

```yaml
# Pipeline YAML
source:
  type: webhook
  path: filebeat-events
  secret: ${WEBHOOK_SECRET}   # optional
```

Then configure Filebeat:

```yaml
# filebeat.yml
output.http:
  hosts: ["http://tram:8765"]
  path: "/webhooks/filebeat-events"
  headers:
    Authorization: "Bearer ${WEBHOOK_SECRET}"
```

## Prometheus Remote Write

To ingest Prometheus metrics:

```yaml
# Pipeline YAML
source:
  type: prometheus_rw
  path: prom-rw
```

Configure Prometheus:

```yaml
# prometheus.yml
remote_write:
  - url: http://tram:8765/webhooks/prom-rw
```

## Security

- Run as non-root user `tram` (uid 1000) in container
- Credentials **always** via env vars — never hardcode in YAML files
- XML input parsed with `defusedxml` to prevent XXE attacks
- Expression evaluation uses `simpleeval` sandbox — no code execution risk
- Private keys mounted as read-only secrets
- Webhook `secret` enforced via `Authorization: Bearer` header

## Scaling

TRAM v0.6.0 is a standalone single-replica daemon. Multi-replica clustering is planned for a future release. See the **Kubernetes — Helm** scaling note above.
