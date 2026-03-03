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
| `TRAM_DB_PATH` | `~/.tram/tram.db` | SQLite path for run history + pipeline versions |
| `TRAM_API_URL` | `http://localhost:8765` | Daemon URL used by CLI proxy commands |
| `TRAM_LOG_LEVEL` | `INFO` | Log level: DEBUG, INFO, WARNING, ERROR |
| `TRAM_LOG_FORMAT` | `json` | Log format: `json` or `text` |
| `TRAM_WORKERS` | `1` | Uvicorn worker count |
| `TRAM_RELOAD_ON_START` | `true` | Auto-load pipelines from TRAM_PIPELINE_DIR at startup |

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

## Kubernetes

### Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tram
spec:
  replicas: 1
  template:
    spec:
      containers:
      - name: tram
        image: tram:latest
        ports:
        - containerPort: 8765
        env:
        - name: TRAM_PIPELINE_DIR
          value: /pipelines
        - name: TRAM_DB_PATH
          value: /data/tram.db
        - name: NE_SFTP_HOST
          valueFrom:
            secretKeyRef:
              name: tram-secrets
              key: ne-sftp-host
        volumeMounts:
        - name: pipelines
          mountPath: /pipelines
          readOnly: true
        - name: tram-data
          mountPath: /data
        livenessProbe:
          httpGet:
            path: /api/health
            port: 8765
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /api/ready
            port: 8765
          initialDelaySeconds: 5
          periodSeconds: 10
      volumes:
      - name: pipelines
        configMap:
          name: tram-pipelines
      - name: tram-data
        persistentVolumeClaim:
          claimName: tram-data-pvc
```

### ConfigMap for pipelines

```bash
kubectl create configmap tram-pipelines --from-file=./pipelines/
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

TRAM is designed as a single-process daemon. For horizontal scaling:

1. Deploy multiple instances, each with a subset of pipelines
2. Use different `TRAM_PIPELINE_DIR` per instance
3. Coordinate via external scheduler (Airflow, Argo Workflows) calling the REST API

Stream pipelines (Kafka consumers) support consumer group-based scaling natively.
