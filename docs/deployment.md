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
| `TRAM_PIPELINE_SYNC_INTERVAL` | `30` | Seconds between DB polls for API-registered pipelines (v1.1.2); all cluster pods converge within this interval when a pipeline is added or deleted via the API |
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
FROM ghcr.io/tosumitdhaka/trishul-ram:1.1.3
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
docker run -v ./schemas:/schemas tram:1.1.3
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

The default `tram:1.1.3` image installs (`clickhouse` added in v1.0.4):

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
FROM ghcr.io/tosumitdhaka/trishul-ram:1.1.3
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

```bash
# Add chart from OCI registry
helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --namespace tram --create-namespace \
  --set image.tag=1.1.3

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
| `image.tag | "1.1.3"` | Image tag |
| `replicaCount` | `1` | Replicas — `1` = standalone, `N` = cluster |
| `clusterMode.enabled` | `false` | Activate cluster mode (sets `TRAM_CLUSTER_ENABLED`, requires external DB) |
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
| `authUsers` | `""` | Comma-separated `user:password` pairs for browser UI login (v1.0.8); use `envSecret.TRAM_AUTH_USERS` for production |
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
  --set image.tag=1.1.3
```

A single-replica `StatefulSet` with pod name `tram-0` runs the full daemon. A `PersistentVolumeClaim` (`data-tram-0`) is auto-provisioned via `volumeClaimTemplates` and mounted at `/data`. SQLite run history, API-uploaded schemas (`/data/schemas`), and runtime MIBs (`/data/mibs`) all share this single PVC and survive pod restarts. Standard MIBs baked into the image at `/mibs` remain available alongside any runtime-downloaded ones.

### Cluster mode (v0.8.0)

Cluster mode deploys a `StatefulSet` where every pod automatically discovers peers via a shared external database and partitions pipelines via consistent hashing — no external coordinator required.

**Prerequisites**: PostgreSQL (recommended) or MariaDB accessible from the cluster.

```bash
# Create a Secret for DB credentials
kubectl create secret generic tram-db \
  --namespace tram \
  --from-literal=url='postgresql+psycopg2://tram:secret@postgres:5432/tramdb'

helm install tram oci://ghcr.io/tosumitdhaka/charts/trishul-ram \
  --namespace tram --create-namespace \
  --set image.tag=1.1.3 \
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

### PostgreSQL subchart (v1.0.8)

For a self-contained cluster deployment on Kubernetes (e.g. kind/minikube) without a separate database server, use the bundled Bitnami PostgreSQL subchart:

```bash
helm install trishul-ram helm/ \
  --namespace trishul-ram --create-namespace \
  --set image.tag=1.1.3 \
  --set replicaCount=3 \
  --set clusterMode.enabled=true \
  --set postgresql.enabled=true
```

This deploys a `trishul-ram-postgresql` StatefulSet alongside TRAM and automatically sets:

```
TRAM_DB_URL=postgresql+psycopg2://tram:tram@trishul-ram-postgresql/tram
TRAM_CLUSTER_ENABLED=true
```

> **Note:** For production, use an external managed PostgreSQL and set `TRAM_DB_URL` via `envSecret` instead.

### Shared RWX storage for schemas and MIBs (v1.0.9)

In cluster mode, schemas and MIBs uploaded via the UI are stored per-pod by default (each pod has its own `/data` volume). Enable `sharedStorage` to provision a single `ReadWriteMany` PVC (`data-<release>`) that every pod mounts at `/data`, making uploads visible cluster-wide immediately.

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
  --set image.tag=1.1.3 \
  --set replicaCount=3 \
  --set clusterMode.enabled=true \
  --set postgresql.enabled=true \
  --set persistence.enabled=false \
  --set sharedStorage.enabled=true \
  --set sharedStorage.storageClass=nfs-rwx
```

This creates a single PVC `data-trishul-ram` (2 Gi, RWX) backed by NFS. All pods mount it at `/data`; `TRAM_SCHEMA_DIR=/data/schemas` and `TRAM_MIB_DIR=/data/mibs` are set automatically.

**For production clouds** — use the platform RWX StorageClass directly:

| Platform | StorageClass |
|----------|-------------|
| AWS EKS (EFS) | `efs-sc` |
| Azure AKS | `azurefile` |
| GKE (Filestore) | `filestore-rwx` |
| Longhorn | `longhorn-rwx` |

> **Note:** `persistence.enabled` should be `false` in cluster mode when `sharedStorage.enabled=true` — PostgreSQL handles the database and the shared PVC handles schemas/MIBs; per-pod RWO PVCs would be unused overhead.

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
        image: ghcr.io/tosumitdhaka/trishul-ram:1.1.3
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

TRAM always deploys as a `StatefulSet` (pod names `tram-0`, `tram-1`, …). Scale is controlled by `replicaCount` and `clusterMode.enabled`:

- **Standalone** (`replicaCount: 1`, default) — single pod `tram-0` with local SQLite via auto-provisioned PVC `data-tram-0`. Zero configuration overhead.
- **Cluster** (`replicaCount: N`, `clusterMode.enabled: true`) — N pods sharing an external PostgreSQL or MariaDB database. Pipelines distributed automatically via consistent hashing. No external coordinator required.

Using a `StatefulSet` in both modes ensures stable pod identity (`tram-0` always stays `tram-0`), consistent `TRAM_NODE_ID` across restarts, and proper PVC affinity so the data volume follows the pod when it reschedules.
