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
| `TRAM_STATE_DIR` | *(none)* | Optional: persist run history to this directory |
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
  -e TRAM_PIPELINE_DIR=/pipelines \
  -e NE_SFTP_HOST=10.0.0.1 \
  tram:latest
```

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
        - name: NE_SFTP_HOST
          valueFrom:
            secretKeyRef:
              name: tram-secrets
              key: ne-sftp-host
        volumeMounts:
        - name: pipelines
          mountPath: /pipelines
          readOnly: true
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
```

### ConfigMap for pipelines

```bash
kubectl create configmap tram-pipelines \
  --from-file=./pipelines/
```

## Pipeline Variables

Pipeline YAMLs support `${VAR:-default}` syntax:

```yaml
source:
  host: ${NE_SFTP_HOST}          # required — error if not set
  port: ${NE_SFTP_PORT:-22}      # optional — defaults to 22
```

This allows a single YAML to work across environments by only changing env vars.

## Logging

TRAM outputs structured JSON logs to stdout:

```json
{"timestamp": "2024-01-01T00:00:00Z", "level": "INFO", "logger": "tram.pipeline.executor", "message": "Run started", "pipeline": "pm-ingest", "run_id": "..."}
```

Configure with a log aggregator (Filebeat, Fluentd, Vector) to forward to Elasticsearch/OpenSearch/Loki.

Set `TRAM_LOG_FORMAT=text` for human-readable output during development.

## Security

- Run as non-root user `tram` (uid 1000) in container
- Credentials **always** via env vars — never hardcode in YAML files
- XML input parsed with `defusedxml` to prevent XXE attacks
- Expression evaluation uses `simpleeval` sandbox — no code execution risk
- Private keys mounted as read-only secrets

## Scaling

TRAM is designed as a single-process daemon. For horizontal scaling:

1. Deploy multiple instances, each with a subset of pipelines
2. Use different `TRAM_PIPELINE_DIR` volumes per instance
3. Coordinate via external scheduler (Airflow, Argo Workflows) calling the REST API

Stream pipelines (Kafka consumers) support consumer group-based scaling natively.
