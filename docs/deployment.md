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
| `TRAM_CLUSTER_ENABLED` | `false` | Enable cluster mode (requires external DB) |
| `TRAM_HEARTBEAT_SECONDS` | `10` | Seconds between node heartbeats in cluster mode |
| `TRAM_NODE_TTL_SECONDS` | `30` | Seconds before a silent node is marked dead |

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
| `image.tag` | `"0.8.0"` | Image tag |
| `replicaCount` | `1` | Replicas — set >1 with `clusterMode.enabled: true` |
| `clusterMode.enabled` | `false` | Deploy StatefulSet instead of Deployment for clustering |
| `persistence.enabled` | `true` | Mount SQLite PVC at `/data` (standalone) or ignored in cluster mode |
| `persistence.size` | `1Gi` | PVC size |
| `persistence.accessMode` | `ReadWriteOnce` | PVC access mode |
| `env` | `{}` | Plain env vars |
| `envSecret` | `{}` | Env vars from Secret (`secretName`/`secretKey`) |
| `pipelines` | `{}` | Pipeline YAMLs mounted as ConfigMap at `/pipelines` |
| `podAnnotations` | `{}` | e.g. `prometheus.io/scrape: "true"` |

### Standalone (default)

```bash
helm install tram oci://ghcr.io/OWNER/charts/tram \
  --namespace tram --create-namespace \
  --set image.tag=0.8.0
```

A single `Deployment` + PVC runs the full daemon. SQLite stores run history and pipeline versions locally.

### Cluster mode (v0.8.0)

Cluster mode deploys a `StatefulSet` where every pod automatically discovers peers via a shared external database and partitions pipelines via consistent hashing — no external coordinator required.

**Prerequisites**: PostgreSQL (recommended) or MariaDB accessible from the cluster.

```bash
# Create a Secret for DB credentials
kubectl create secret generic tram-db \
  --namespace tram \
  --from-literal=url='postgresql+psycopg2://tram:secret@postgres:5432/tramdb'

helm install tram oci://ghcr.io/OWNER/charts/tram \
  --namespace tram --create-namespace \
  --set image.tag=0.8.0 \
  --set clusterMode.enabled=true \
  --set replicaCount=3 \
  --set envSecret.TRAM_DB_URL.secretName=tram-db \
  --set envSecret.TRAM_DB_URL.secretKey=url \
  --set persistence.enabled=false
```

Each pod (`tram-0`, `tram-1`, `tram-2`) registers in the shared DB, sends heartbeats, and owns pipelines computed by:

```
sha1(pipeline_name) % live_node_count == my_sorted_position
```

When a node fails, the remaining nodes detect it (after `TRAM_NODE_TTL_SECONDS`) and absorb its pipelines automatically. No manual intervention required.

Check cluster state:

```bash
kubectl exec -n tram tram-0 -- curl -s http://localhost:8765/api/cluster/nodes | jq .
```

### Scale up / scale down

```bash
# Scale out
kubectl scale statefulset tram -n tram --replicas=5

# Scale in — surviving nodes absorb released pipelines within TRAM_NODE_TTL_SECONDS
kubectl scale statefulset tram -n tram --replicas=2
```

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

TRAM v0.8.0 supports two deployment modes:

- **Standalone** (`clusterMode.enabled: false`, default) — single `Deployment` replica with local SQLite. Zero configuration overhead.
- **Cluster** (`clusterMode.enabled: true`) — `StatefulSet` with N replicas sharing an external PostgreSQL or MariaDB database. Pipelines are distributed automatically via consistent hashing. No external coordinator (ZooKeeper, etcd) required.
