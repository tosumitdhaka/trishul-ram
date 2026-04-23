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
| `TRAM_API_KEY` | _(empty)_ | API key for request authentication; empty = auth disabled |
| `TRAM_AUTH_USERS` | _(empty)_ | Comma-separated `user:password` pairs for browser UI login (v1.0.8); issues 8-hour HMAC session tokens; coexists with `TRAM_API_KEY` |
| `TRAM_AUTH_SECRET` | _(random)_ | Shared HMAC signing secret for session tokens (v1.0.8); **required in cluster mode** — without a shared secret each pod signs tokens independently and cross-pod requests return 401 |
| `TRAM_RATE_LIMIT` | `0` | Max requests per minute per IP for `/api/*`; 0 = disabled |
| `TRAM_RATE_LIMIT_WINDOW` | `60` | Sliding window in seconds for rate limiting |
| `TRAM_TLS_CERTFILE` | _(empty)_ | Path to TLS certificate file for HTTPS |
| `TRAM_TLS_KEYFILE` | _(empty)_ | Path to TLS key file for HTTPS |
| `TRAM_OTEL_ENDPOINT` | _(empty)_ | OTLP gRPC endpoint (e.g. `http://jaeger:4317`) for OpenTelemetry traces |
| `TRAM_OTEL_SERVICE` | `tram` | Service name reported to OTel collector |
| `TRAM_WATCH_PIPELINES` | `false` | Watch `TRAM_PIPELINE_DIR` for YAML changes and auto-reload pipelines |
| `TRAM_MIB_DIR` | `/mibs` | Directory containing compiled pysnmp MIB `.py` files; standard MIBs baked into Docker image at build time (v1.0.3) |
| `TRAM_SCHEMA_DIR` | `/schemas` | Directory containing serialization schema files (`.proto`, `.avsc`, `.asn`, etc.); managed via `POST /api/schemas/upload` (v1.0.3) |
| `TRAM_SCHEMA_REGISTRY_URL` | _(empty)_ | Base URL of an external Confluent-compatible schema registry; enables `/api/schemas/registry/*` proxy and serves as the default `schema_registry_url` for Avro/Protobuf serializers (v1.0.4) |
| `TRAM_SCHEMA_REGISTRY_USERNAME` | _(empty)_ | Basic-auth username for the external schema registry; used as default when not set in pipeline YAML (v1.0.4) |
| `TRAM_SCHEMA_REGISTRY_PASSWORD` | _(empty)_ | Basic-auth password for the external schema registry; used as default when not set in pipeline YAML (v1.0.4) |
| `TRAM_UI_DIR` | `/ui` | Directory containing built tram-ui static assets; set to empty string to disable the web UI without rebuilding the image (v1.0.8) |
| `TRAM_TEMPLATES_DIR` | `/tram-templates` | Directory containing bundled pipeline templates served by `/api/templates` (v1.1.0) |
| `TRAM_MODE` | `standalone` | Deployment mode: `standalone` \| `manager` \| `worker` (v1.2.0) |
| `TRAM_WORKER_URLS` | _(empty)_ | Explicit comma-separated worker agent URLs; when set, manager uses this list instead of replica-based headless DNS discovery (v1.2.0) |
| `TRAM_WORKER_REPLICAS` | `0` | Number of worker StatefulSet replicas used for headless-DNS discovery; `0` disables replica-based discovery (set on manager pod, v1.2.0) |
| `TRAM_WORKER_SERVICE` | `tram-worker` | Headless Service name used to build worker DNS addresses (v1.2.0) |
| `TRAM_WORKER_NAMESPACE` | `default` | Kubernetes namespace where worker pods run (v1.2.0) |
| `TRAM_WORKER_PORT` | `8766` | Port that worker pods listen on (v1.2.0) |
| `TRAM_WORKER_INGRESS_PORT` | `8767` | Public ingress port on worker pods for `/webhooks/*` push traffic (v1.3.0) |
| `TRAM_MANAGER_URL` | _(empty)_ | Manager base URL used by worker pods for run-complete callbacks (v1.2.0) |
| `TRAM_STATS_INTERVAL` | `30` | Seconds between worker periodic stats reports; also controls `PlacementReconciler` tick interval (`min(TRAM_STATS_INTERVAL, 10)s`) and stale-slot threshold (`3 × interval`) (v1.3.0) |

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

## Manager + Worker Mode (v1.2.0)

TRAM supports a split deployment where a single **manager** pod owns all scheduling, the database, and the UI, while one or more **worker** pods execute pipelines and return results.

| Mode | `TRAM_MODE` | Role |
|------|-------------|------|
| Standalone | `standalone` (default) | All-in-one: scheduler + DB + executor + UI on one pod |
| Manager | `manager` | Owns scheduling, DB writes, UI; dispatches run requests to workers |
| Worker | `worker` | Stateless executor; no DB, no scheduler, no UI; listens on `:8766` for `/agent/*` and `:8767` for `/webhooks/*` |

### Manager pod

```bash
TRAM_MODE=manager
TRAM_WORKER_REPLICAS=3
TRAM_WORKER_SERVICE=tram-worker        # headless Service name
TRAM_WORKER_NAMESPACE=default
TRAM_WORKER_PORT=8766
TRAM_DB_URL=postgresql+psycopg2://tram:secret@postgres:5432/tramdb
```

The manager dispatches `POST /agent/run` to each worker using Kubernetes headless DNS:
`<service>-N.<service>.<namespace>.svc.cluster.local:<port>`

Alternatively, set `TRAM_WORKER_URLS` to an explicit comma-separated list of worker agent URLs and the manager will use that list instead of headless-DNS discovery.

### Worker pod

```bash
TRAM_MODE=worker
TRAM_MANAGER_URL=http://tram:8765      # manager Service DNS or ClusterIP
TRAM_WORKER_PORT=8766                  # internal agent API
TRAM_WORKER_INGRESS_PORT=8767          # public webhook ingress
```

Workers only need `tram[worker,kafka,snmp,...]` — the `manager` extra (apscheduler, sqlalchemy) is not installed.

### Helm: manager.enabled=true

```yaml
manager:
  enabled: true
  persistence:
    enabled: true
    existingClaim: ""

worker:
  replicas: 3
  ingressPort: 8767
  ingressService:
    enabled: true
    type: NodePort
    nodePort: 30002
  resources:
    requests: {cpu: 200m, memory: 256Mi}
    limits:   {cpu: 1000m, memory: 1Gi}
```

This creates:
- `manager-statefulset.yaml` — single-replica StatefulSet for the manager pod
- `manager-headless-service.yaml` — headless Service for stable manager DNS / StatefulSet identity
- `worker-statefulset.yaml` — StatefulSet for worker pods
- `worker-headless-service.yaml` — headless Service for stable DNS
- `worker-ingress-service.yaml` — published Service for worker `/webhooks/*` ingress on `:8767`
- `service-ui.yaml` — optional separate Service for the web UI

If you are upgrading from the older manager `Deployment`, set `manager.persistence.existingClaim` to reuse the current manager PVC instead of provisioning a new one.

### Worker image

```dockerfile
# Build with Dockerfile.worker (no UI assets, no manager deps)
docker build -f Dockerfile.worker -t trishul-ram-worker:1.3.1 .
```

The worker image exposes port `8766` for the internal agent API and port `8767` for ingress-only webhook traffic. Kubernetes liveness/readiness probes stay on `/agent/health` over port `8766`.

### Worker agent endpoints

| Method | Port | Path | Description |
|--------|------|------|-------------|
| `GET` | `8766` | `/agent/health` | Composite liveness check — returns `ok: false` when either agent or ingress thread is dead; includes `ingress_up` field |
| `GET` | `8766` | `/agent/status` | Lists active batch and stream runs |
| `POST` | `8766` | `/agent/run` | Start a pipeline run (batch or stream) |
| `POST` | `8766` | `/agent/stop` | Signal a stream run to stop |
| `POST` | `8767` | `/webhooks/{path}` | Ingress-only webhook receiver for push-HTTP sources |
| `POST` | `8765` | `/api/internal/run-complete` | _(manager)_ Receive run result callback from worker |
| `POST` | `8765` | `/api/internal/pipeline-stats` | _(manager)_ Receive periodic stats from worker; batch completion sends one final report with `is_final: true` before run-complete |

## API Key Authentication (v1.0.0)

Set `TRAM_API_KEY` to a secret string to require authentication on all `/api/*` endpoints.
Clients must pass the key via:

```bash
# Header (recommended)
curl -H "X-API-Key: $TRAM_API_KEY" http://localhost:8765/api/pipelines

# Query parameter (useful for webhooks)
curl "http://localhost:8765/api/pipelines?api_key=$TRAM_API_KEY"
```

Exempt paths (no key needed): `/api/health`, `/api/ready`, `/metrics`, `/webhooks/*`, `/ui/*`, `/`

When `TRAM_API_KEY` is empty (default), all requests pass through without authentication.

## TLS / HTTPS (v1.0.0)

```bash
TRAM_TLS_CERTFILE=/certs/tls.crt
TRAM_TLS_KEYFILE=/certs/tls.key
tram daemon  # now serves HTTPS
```

**Helm** — use cert-manager or pre-existing TLS secret:
```yaml
tls:
  enabled: true
  secretName: tram-tls        # Kubernetes secret with tls.crt / tls.key
  certManagerIssuer: letsencrypt-prod  # optional: cert-manager ClusterIssuer
```

## Pipeline File Watcher (v1.0.0)

Enable auto-reload when pipeline YAML files change:

```bash
TRAM_WATCH_PIPELINES=true tram daemon
```

- New/modified YAML files in `TRAM_PIPELINE_DIR` trigger reload
- Deleted YAML files deregister the pipeline
- Requires: `pip install tram[watch]` (watchdog)

## OpenTelemetry Tracing (v1.0.0)

```bash
pip install tram[otel]
TRAM_OTEL_ENDPOINT=http://jaeger:4317 tram daemon
```

Traces are emitted for each `batch_run()` execution with `pipeline`, `run_id`, `records_in`, `records_out` attributes.
Compatible with Jaeger, Grafana Tempo, and any OTLP-compatible backend.

## v1.0.0 Pipeline Config Fields

These fields are set per-pipeline in the pipeline YAML (not environment variables):

| Field | Default | Description |
|-------|---------|-------------|
| `thread_workers` | `1` | Worker threads for parallel chunk processing (batch + stream) |
| `batch_size` | _(none)_ | Stop after N records per run; useful for controlled catch-up |
| `on_error` | `continue` | Error policy: `continue` \| `abort` \| `retry` \| `dlq` |
| `skip_processed` | `false` | On file/object sources: skip files already processed |
| `parallel_sinks` | `false` | Fan out to all sinks concurrently via thread pool |

### Per-sink reliability fields (v1.0.0)

Add to any `sink:` block:

```yaml
sink:
  type: kafka
  # ... existing fields ...
  retry_count: 3              # retry up to 3 times on failure
  retry_delay_seconds: 1.0   # base delay; doubles each attempt (exponential back-off)
  circuit_breaker_threshold: 5  # skip sink for 60s after 5 consecutive failures
```

## SNMP MIB Management (v1.0.3)

The Docker image includes pre-compiled versions of the most commonly needed SNMP MIBs:
`IF-MIB`, `ENTITY-MIB`, `HOST-RESOURCES-MIB`, `IP-MIB`, `TCP-MIB`, `UDP-MIB`, `IANAifType-MIB`.

SNMP connectors automatically look for MIBs in `TRAM_MIB_DIR` (`/mibs`) without any pipeline-level configuration.

**Add more MIBs at runtime:**

```bash
# Via CLI (requires tram[mib])
tram mib download CISCO-ENTITY-FRU-CONTROL-MIB --out /mibs

# Via REST API
curl -X POST http://localhost:8765/api/mibs/download \
  -H "Content-Type: application/json" \
  -d '{"names": ["CISCO-ENTITY-FRU-CONTROL-MIB"]}'

# Upload a local .mib file
curl -X POST http://localhost:8765/api/mibs/upload \
  -F "file=@/path/to/MY-CUSTOM-MIB.mib"

# List compiled MIBs
curl http://localhost:8765/api/mibs
```

**Compile a directory of .mib files:**

```bash
tram mib compile /path/to/vendor-mibs/ --out /mibs
```

**Air-gapped environments** — copy pre-compiled MIB `.py` files into the image:

```dockerfile
FROM ghcr.io/tosumitdhaka/trishul-ram:1.3.1
COPY compiled-mibs/*.py /mibs/
```

## Schema Management (v1.0.3)

Serialization schemas (Protobuf `.proto`, Avro `.avsc`, JSON Schema `.json`, XML Schema `.xsd`)
are stored in `TRAM_SCHEMA_DIR` (`/schemas`). Reference them in pipeline YAML:

```yaml
serializer_in:
  type: protobuf
  schema_file: /schemas/cisco/GenericRecord.proto
  message_class: PerformanceMonitoringMessage
  framing: none
```

**Upload schemas via REST API:**

```bash
# Upload a single file
curl -X POST http://localhost:8765/api/schemas/upload \
  -F "file=@GenericRecord.proto"

# Upload all Cisco EMS proto files to a subdirectory (imports resolve correctly)
for f in *.proto; do
  curl -F "file=@$f" \
    "http://localhost:8765/api/schemas/upload?subdir=cisco"
done

# List all schemas
curl http://localhost:8765/api/schemas

# Read a schema file
curl http://localhost:8765/api/schemas/cisco/GenericRecord.proto

# Delete a schema
curl -X DELETE http://localhost:8765/api/schemas/cisco/GenericRecord.proto
```

**Accepted extensions:** `.proto`, `.avsc`, `.json`, `.xsd`, `.yaml`, `.yml`

**Mount host directory** for development (read-write):

```bash
docker run -v ./schemas:/schemas tram:1.3.1
```

## Schema Registry Integration (v1.0.4)

Set `TRAM_SCHEMA_REGISTRY_URL` once to enable both the proxy endpoint and automatic serializer client fallback:

```bash
TRAM_SCHEMA_REGISTRY_URL=http://schema-registry:8081 tram daemon
```

With this set:
- `ANY /api/schemas/registry/{path}` proxies to `http://schema-registry:8081/{path}` — UI tools can reach the registry through TRAM as a single origin
- Avro and Protobuf serializers use `http://schema-registry:8081` as the default registry URL — no `schema_registry_url:` needed in pipeline YAML
- Per-pipeline override still works: `schema_registry_url: http://other-registry:8081` in a pipeline's `serializer_in:` or `serializer_out:` block takes precedence

**With authentication:**

```bash
TRAM_SCHEMA_REGISTRY_URL=https://schema-registry:8081
TRAM_SCHEMA_REGISTRY_USERNAME=myuser
TRAM_SCHEMA_REGISTRY_PASSWORD=mypassword
```

**In docker-compose:**

```yaml
environment:
  TRAM_SCHEMA_REGISTRY_URL: http://schema-registry:8081
  TRAM_SCHEMA_REGISTRY_USERNAME: ${SR_USER:-}
  TRAM_SCHEMA_REGISTRY_PASSWORD: ${SR_PASS:-}
```

## Web UI (v1.0.8)

The `tram-ui` Bootstrap 5 SPA is built into the Docker image at `/ui` and served by the daemon at `http://<host>:8765/ui/`. Navigating to `/` redirects there automatically.

```bash
# Local development
tram daemon &
open http://localhost:8765/ui/

# Disable UI serving without rebuilding the image
TRAM_UI_DIR="" tram daemon
```

### UI pages

| Page | Description |
|------|-------------|
| Dashboard | Stat cards, active pipelines, recent runs |
| Pipelines | Full list with search/filter, start/stop/run/edit/delete |
| Run History | Filterable table with expandable error rows, CSV export |
| Pipeline Detail | Summary cards, run history, Versions tab, Config tab, rollback |
| Pipeline Editor | YAML editor, dry-run, save (create/update) |
| Schemas | Upload (drag-and-drop), list, delete |
| MIB Modules | Upload, bulk download from mibs.pysnmp.com, delete |
| Cluster | Node accordion with status and pipeline assignments |
| Plugins | Registered sources/sinks/serializers/transforms |
| Settings | Connection config, daemon status, Reload Pipelines |

### Kubernetes — UI Service

When `ui.enabled=true` (default), the Helm chart creates a dedicated `{release}-ui` Service:

```
tram        ClusterIP  :8765  ← API traffic (restrict via NetworkPolicy)
tram-ui     ClusterIP  :80    ← UI traffic  (expose via Ingress)
```

Both target the same pod on port 8765. The split lets you:
- Attach an Ingress rule only to `tram-ui`
- Apply a NetworkPolicy that allows internal services to reach `tram` but routes browsers to `tram-ui`

```bash
# Access the UI locally
kubectl port-forward svc/tram-ui -n tram 8080:80
open http://localhost:8080/ui/

# Expose via Ingress
kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: tram-ui
  namespace: tram
spec:
  rules:
  - host: tram.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: tram-release-ui
            port:
              number: 80
EOF
```

The UI reads `TRAM_BASE_URL` from `localStorage` (set on the Settings page) to know where the API is. Default is `http://localhost:8765`.

## Docker

### Build and run

```bash
docker build -t tram:latest .
docker run -p 8765:8765 \
  -v ./pipelines:/pipelines:ro \
  -v ./schemas:/schemas \
  -v tram-data:/data \
  -e TRAM_DB_URL=sqlite:////data/tram.db \
  -e TRAM_PIPELINE_DIR=/pipelines \
  -e NE_SFTP_HOST=10.0.0.1 \
  tram:latest
```

Mount a volume at `/data` (or set `TRAM_DB_URL`) to persist run history and pipeline versions across container restarts.

### Installed extras in the default image

The default `tram:1.3.1` image installs (`clickhouse` added in v1.0.4):

`kafka`, `opensearch`, `snmp`, `avro`, `protobuf_ser`, `msgpack_ser`, `mqtt`, `amqp`, `nats`,
`gnmi`, `jmespath`, `sql`, `influxdb`, `redis`, `websocket`, `elasticsearch`, `metrics`,
`prometheus_rw`, `corba`, `mib`, `watch`, `postgresql`, `mysql`

`corba` (`omniORBpy`) is included — the image pre-installs the required omniORB runtime libraries
(`libomniorb4-2`, `libomnithread4`) so the pre-built PyPI wheel installs without a source build.

The following extras are **excluded by default** to keep the image lean. Extend with a custom layer:

| Extra | Reason excluded | ~Size |
|-------|----------------|-------|
| `parquet` | pyarrow is large | ~150 MB |
| `s3` | boto3/botocore | ~60 MB |
| `gcs` | google-cloud-storage + deps | ~50 MB |
| `azure` | azure-storage-blob + SDK | ~30 MB |
| `otel` | only needed when `TRAM_OTEL_ENDPOINT` is set; no-op fallback when absent | ~15 MB |

```dockerfile
FROM ghcr.io/tosumitdhaka/trishul-ram:1.3.1
RUN pip install "tram[parquet,s3,gcs,azure,otel]"
```

### docker-compose

```bash
cp .env.example .env
# Edit .env with your credentials
docker compose up
```

## Kubernetes — Helm (recommended)

TRAM ships a production-ready Helm chart in `helm/`. Published to GHCR OCI on every release tag.

### Install

Quick-start examples below use `latest`. For production, pin `image.tag` and worker image tags to a specific release such as `1.3.2`.

```bash
# Add chart from OCI registry
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --namespace tram --create-namespace \
  --set image.tag=latest

# Mount pipelines from local files
helm upgrade tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set-file "pipelines.pm-ingest\.yaml=./pipelines/pm-ingest.yaml"

# Inject SFTP credentials from a Kubernetes Secret
helm upgrade tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --set envSecret.NE_SFTP_PASS.secretName=ne-creds \
  --set envSecret.NE_SFTP_PASS.secretKey=password
```

### Key values

| Value | Default | Description |
|-------|---------|-------------|
| `image.repository` | `ghcr.io/tosumitdhaka/trishul-ram` | Docker image repository |
| `image.tag | "1.3.1"` | Image tag |
| `replicaCount` | `1` | Replicas for the standalone StatefulSet; not used when `manager.enabled=true` |
| `manager.enabled` | `false` | `true` = manager+worker mode (manager StatefulSet + worker StatefulSet); `false` = standalone StatefulSet |
| `worker.replicas` | `3` | Number of worker StatefulSet replicas (only when `manager.enabled=true`) |
| `manager.persistence.enabled` | `true` | RWO PVC for manager pod (SQLite DB, schemas, MIBs) — recommended in manager mode |
| `manager.persistence.existingClaim` | `""` | Reuse an existing manager PVC during Deployment → StatefulSet upgrades; skips `volumeClaimTemplates` when set |
| `worker.ingressPort` | `8767` | Worker public ingress port for `/webhooks/*`; `TRAM_WORKER_INGRESS_PORT` is set from this value |
| `worker.ingressService.enabled` | `true` | Create a separate published Service for worker `/webhooks/*` ingress in manager mode |
| `worker.ingressService.type` | `NodePort` | Service type for published worker ingress (`NodePort`, `ClusterIP`, or `LoadBalancer`) |
| `worker.ingressService.port` | `8767` | Service port for worker ingress |
| `worker.ingressService.nodePort` | `30002` | Fixed NodePort for worker ingress when `type=NodePort`; set null to let Kubernetes assign one |
| Pipeline `kubernetes.service_type` | `NodePort` | Per-pipeline dedicated Service exposure for active `webhook` / `prometheus_rw` streams; requires image built with `tram[k8s]` |
| `persistence.enabled` | `true` | Provision a per-pod RWO PVC via `volumeClaimTemplates` mounted at `/data`; auto-sets `TRAM_DB_URL=sqlite:////data/tram.db`, `TRAM_SCHEMA_DIR=/data/schemas`, `TRAM_MIB_DIR=/data/mibs`; disable in cluster mode when using `sharedStorage` |
| `persistence.size` | `1Gi` | PVC size per pod (standalone mode only) |
| `persistence.accessMode` | `ReadWriteOnce` | PVC access mode (standalone mode only) |
| `sharedStorage.enabled` | `false` | Provision a single shared `ReadWriteMany` PVC (`data-<release>`) mounted at `/data` on every pod (v1.0.9); schemas/MIBs uploaded via the UI are visible to all replicas immediately; requires a RWX StorageClass |
| `sharedStorage.size` | `2Gi` | Shared PVC size |
| `sharedStorage.storageClass` | `""` | RWX StorageClass: `nfs-rwx` (kind), `efs-sc` (AWS), `azurefile` (Azure), `filestore-rwx` (GKE), `longhorn-rwx` |
| `schemaRegistry.url` | `""` | External registry URL; injects `TRAM_SCHEMA_REGISTRY_URL` — enables proxy + serializer default (v1.0.4) |
| `schemaRegistry.username` | `""` | Registry basic-auth username; prefer `envSecret` in production |
| `schemaRegistry.password` | `""` | Registry basic-auth password; prefer `envSecret` in production |
| `service.snmpTrapPorts` | `[]` | List of UDP ports to expose for `snmp_trap` sources (e.g. `[1162, 1163]`); each entry creates one Service port + containerPort; requires `helm upgrade` to add/remove |
| `ui.enabled` | `true` | Serve tram-ui static assets at `/ui`; set to `false` to disable without rebuilding the image (injects `TRAM_UI_DIR=""`) |
| `apiKey` | `""` | API key (X-API-Key header / `api_key` query param) for machine clients; empty = disabled |
| `authUsers` | `""` | Comma-separated `user:password` pairs for browser login bootstrap; with `TRAM_DB_URL`, changed passwords are stored as scrypt hashes in `user_passwords` and override the env value |
| `postgresql.enabled` | `false` | Deploy Bitnami PostgreSQL subchart and auto-wire `TRAM_DB_URL` (v1.0.8) |
| `postgresql.auth.username` | `tram` | PostgreSQL username |
| `postgresql.auth.password` | `tram` | PostgreSQL password (use external secret for production) |
| `postgresql.auth.database` | `tram` | PostgreSQL database name |
| `nameOverride` | `""` | Override the chart name portion of resource names |
| `fullnameOverride` | `""` | Fully override the resource name prefix |
| `env` | `{}` | Plain env vars |
| `envSecret` | `{}` | Env vars from Secret (`secretName`/`secretKey`) |
| `pipelines` | `{}` | Pipeline YAMLs mounted as ConfigMap at `/pipelines` |
| `podAnnotations` | `{}` | e.g. `prometheus.io/scrape: "true"` |

### Standalone (default)

```bash
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --namespace tram --create-namespace \
  --set image.tag=1.3.1
```

A single-replica `StatefulSet` with pod name `tram-0` runs the full daemon. A `PersistentVolumeClaim` (`data-tram-0`) is auto-provisioned via `volumeClaimTemplates` and mounted at `/data`. SQLite run history, API-uploaded schemas (`/data/schemas`), and runtime MIBs (`/data/mibs`) all share this single PVC and survive pod restarts. Standard MIBs baked into the image at `/mibs` remain available alongside any runtime-downloaded ones.

### Manager + Worker mode (v1.2.0)

Manager+Worker mode replaces the old v0.8.0 consistent-hashing cluster model. A single manager `StatefulSet` owns scheduling, the SQLite database, and the Web UI. A worker `StatefulSet` (N replicas) receives run requests over the internal agent API, exposes a separate ingress-only port for push traffic, executes pipelines statelessly, and POSTs results back to the manager.

SQLite on a `ReadWriteOnce` PVC is sufficient — only one manager pod ever writes to it.

```bash
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --namespace tram --create-namespace \
  --set image.tag=1.3.1 \
  --set manager.enabled=true \
  --set worker.replicas=3 \
  --set worker.image.repository=trishul-ram-worker \
  --set worker.image.tag=1.3.1 \
  --set apiKey=mysecret
```

This creates:
- `tram-manager` StatefulSet (1 replica, port 8765) — scheduler + DB + UI
- `tram-manager` headless Service — stable manager pod DNS for the StatefulSet
- `tram-worker` StatefulSet (3 replicas, agent port 8766, ingress port 8767) — stateless executors
- `tram-worker` headless Service — stable DNS `tram-worker-N.tram-worker.<ns>.svc.cluster.local`
- `tram-worker-ingress` Service — published ingress entrypoint for worker `/webhooks/*` traffic

If you are upgrading an existing release that used a manager `Deployment`, reuse the current PVC:

```bash
helm upgrade tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --namespace tram \
  --set manager.enabled=true \
  --set manager.persistence.existingClaim=manager-data-tram
```

Check cluster state:

```bash
kubectl exec -n tram pod/tram-manager-0 -- \
  curl -s -H "X-API-Key: mysecret" http://localhost:8765/api/cluster/nodes | jq .
```

### PostgreSQL subchart (v1.0.8)

PostgreSQL is **optional** in manager+worker mode — SQLite on the manager's RWO PVC is the recommended default. Enable the PostgreSQL subchart only when you need external tooling to query run history directly, or when planning future manager HA failover:

```bash
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --namespace tram --create-namespace \
  --set image.tag=1.3.1 \
  --set manager.enabled=true \
  --set worker.replicas=3 \
  --set postgresql.enabled=true
```

For production with an existing external database, set `TRAM_DB_URL` via `envSecret` instead of the subchart.

> **Note:** For production, use an external managed PostgreSQL and set `TRAM_DB_URL` via `envSecret`.

### Shared RWX storage for schemas and MIBs (v1.0.9)

In standalone mode, enable `sharedStorage` to provision a single `ReadWriteMany` PVC that every replica mounts at `/data`, so schemas/MIBs uploaded via the UI are visible to all pods immediately.

In manager+worker mode the manager's RWO PVC already holds schemas and MIBs — workers sync them at run time via `GET /api/schemas` and `GET /api/mibs/{name}`. Only enable `sharedStorage` in manager mode if workers must read schema/MIB files directly at runtime (e.g. Avro/Protobuf pipelines referencing `/data/schemas`).

**For kind clusters** — deploy the bundled NFS Ganesha provisioner first:

```bash
# Pull and load image into kind (substitute your cluster name)
docker pull registry.k8s.io/sig-storage/nfs-provisioner:v4.0.8
docker save registry.k8s.io/sig-storage/nfs-provisioner:v4.0.8 \
  | docker exec -i <kind-node> ctr --namespace=k8s.io images import -

kubectl apply -f ~/kind/nfs-provisioner.yaml
kubectl rollout status deploy/nfs-provisioner -n nfs-provisioner
```

Then install/upgrade TRAM with shared storage enabled:

```bash
helm upgrade trishul-ram helm/ \
  --namespace trishul-ram \
  --set image.tag=1.3.1 \
  --set manager.enabled=true \
  --set worker.replicas=3 \
  --set manager.persistence.enabled=true \
  --set sharedStorage.enabled=true \
  --set sharedStorage.storageClass=nfs-rwx
```

This creates a single RWX PVC for shared schemas/MIBs backed by NFS. In manager+worker mode the manager still keeps SQLite on its own RWO PVC; the shared RWX volume is only for assets that must be visible across pods.

**For production clouds** — use the platform RWX StorageClass directly:

| Platform | StorageClass |
|----------|-------------|
| AWS EKS (EFS) | `efs-sc` |
| Azure AKS | `azurefile` |
| GKE (Filestore) | `filestore-rwx` |
| Longhorn | `longhorn-rwx` |

> **Note:** `sharedStorage.enabled=true` does not replace manager persistence. In manager+worker mode, keep `manager.persistence.enabled=true` for SQLite and use the RWX volume only when workers must read shared schema/MIB files directly.

### Scale up / scale down

**Standalone** — scaling is not supported; the standalone StatefulSet is always a single replica.

**Manager + Worker** — scale the worker StatefulSet:

```bash
# Scale out workers
kubectl scale statefulset tram-worker -n tram --replicas=5

# Scale in workers — the manager's WorkerPool health poll detects the change within 10s
kubectl scale statefulset tram-worker -n tram --replicas=2
```

Or via Helm (preferred — keeps values.yaml in sync):

```bash
helm upgrade tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --namespace tram \
  --reuse-values \
  --set worker.replicas=5
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
        image: ghcr.io/tosumitdhaka/trishul-ram:1.3.1
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
          httpGet: {path: /api/health, port: 8765}
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet: {path: /api/ready, port: 8765}
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
| `tram_dlq_total` | Counter | Records sent to dead-letter queue |
| `tram_chunk_duration_seconds` | Histogram | Chunk processing time |
| `tram_kafka_consumer_lag` | Gauge | Kafka consumer lag per topic+partition (v1.0.0) |
| `tram_stream_queue_depth` | Gauge | Internal stream queue depth per pipeline (v1.0.0) |

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

TRAM supports two Kubernetes deployment shapes controlled by `manager.enabled`:

- **Standalone** (`manager.enabled: false`, default) — a single-replica `StatefulSet` (`tram-0`) with local SQLite via auto-provisioned PVC `data-tram-0`. Zero configuration overhead.
- **Manager + Worker** (`manager.enabled: true`) — a manager `StatefulSet` (1 replica, port 8765) for scheduling/DB/UI, and a worker `StatefulSet` (N replicas, agent port 8766, ingress port 8767) for stateless execution. SQLite on the manager's RWO PVC; no PostgreSQL required.

Using `StatefulSet` for standalone, manager, and worker pods ensures stable pod identity, consistent `TRAM_NODE_ID` across restarts, and proper PVC affinity so the data volume follows the pod when it reschedules.
