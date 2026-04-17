# TRAM Roadmap

Planned features and known issues. Items are assigned to a version once scope is confirmed;
unconfirmed work lives in the backlog at the bottom.

---

## v1.2.3 — SNMP Poll v3 Validation & ASN.1 Decode Hardening

- [ ] SNMP poll source — validate SNMPv3 USM on real-device GET and WALK; keep existing walk / yield_rows coverage green
- [x] ASN.1 serializer — decode-path coverage and wording updated for explicit decode-only behavior
- [ ] SNMP trap source — deferred; blocked in manager-worker mode by the push-source architecture gap tracked in issue #11
- [ ] SNMP trap sink — deferred until a reachable real receiver/test target is available

---

## v1.3.0 — Broadcast Streams & Push-Source Scaling

> Full design: [`docs/design-v1.3.0-broadcast-streams.md`](design-v1.3.0-broadcast-streams.md)
> Skips v1.2.4–v1.2.7 due to severity of the push-source architecture gap (issue #11).

### A — Broadcast Dispatch
- [ ] `PipelineConfig.dispatch: "single" | "broadcast"` field
- [ ] Linter rule L006 — reject broadcast on non-push-source types
- [ ] `WorkerPool.broadcast()` — dispatch same pipeline to all healthy workers
- [ ] Mount `/webhooks` router on worker FastAPI app

### B — Stream Heartbeat
- [ ] `StreamMetrics` dataclass — thread-safe per-run counters
- [ ] Worker heartbeat thread — per-stream-run POST to manager every `TRAM_HEARTBEAT_INTERVAL` (default 30s)
- [ ] `POST /api/internal/heartbeat` — manager receives and stores live stream metrics
- [ ] `HeartbeatStore` — in-memory `{(pipeline, worker): heartbeat}` with staleness detection

### C — Placement API
- [ ] `GET /api/pipelines/{name}/placement` — which workers, run IDs, live metrics, ingress URLs
- [ ] `GET /api/cluster/streams` — flat list of all active stream placements

### D — Manager StatefulSet
- [ ] Replace `manager-deployment.yaml` with `manager-statefulset.yaml` + `volumeClaimTemplates`
- [ ] Add `manager-headless-service.yaml` for stable pod DNS
- [ ] Remove separate manager PVC template; add `existingClaim` migration path

### E — Dynamic K8s Services
- [ ] `tram/k8s/service_manager.py` — create/delete NodePort Services via K8s API
- [ ] `kubernetes:` pipeline block (`service_type`, `node_port`, `target_port`)
- [ ] RBAC Role + RoleBinding for Service CRUD (`helm/templates/rbac.yaml`)
- [ ] `tram[k8s]` optional extra (`kubernetes>=28.0,<32`)
- [ ] Manager re-adopts `tram-managed=true` Services on startup

### F — `source_stem` / `source_suffix` filename tokens (issue #9)
- [ ] Add `{source_stem}` and `{source_suffix}` to `filename_template.format()` in all 6 file-based sinks (`local`, `sftp`, `ftp`, `s3`, `gcs`, `azure_blob`)
- [ ] Update docstrings and `docs/connectors.md`
- [ ] Unit tests for both tokens

### G — Migrate SNMP from pysnmp-lextudio to pysnmp 7.x (issue #10)
- [ ] `pyproject.toml`: `snmp` extra → `pysnmp>=7.1,<8`; `mib` extra → `pysmi>=1.1,<2`
- [ ] Update `pysnmp.hlapi.asyncio` imports to `pysnmp.hlapi.v3arch.asyncio` in snmp source/sink
- [ ] Remove `pytest-cov<5` workaround (pysnmp-lextudio 6.2.x bug, gone in 7.x)
- [ ] SNMP unit tests pass against pysnmp 7.x; lab GET/WALK validation

### H — Alert cooldown on confirmed delivery only (issue #3)
- [ ] `_fire_webhook()` calls `raise_for_status()`; returns `True`/`False`
- [ ] `_fire_email()` returns `True`/`False`
- [ ] `_set_cooldown()` called only on `True`
- [ ] Tests: HTTP 500, connection error, SMTP failure → cooldown not set; HTTP 200 → cooldown set

### I — Webhook source validation (carried from deferred v1.2.5)
- [ ] Webhook source — test payload parsing, path routing, concurrent requests (now directly exercised by broadcast integration tests)

---

## Backlog (unversioned)

Features and issues not yet assigned to a release:

### Connector Fixes (deferred from v1.2.4–v1.2.7)
- [ ] **Kafka source** — real-behavior edge cases: reconnect, offset commit, consumer group
- [ ] **Kafka sink** — producer error handling, retry, serializer integration
- [ ] **OpenSearch sink** — bulk write, index template, auth, retry on 429
- [ ] **ClickHouse source/sink** — query execution, batch insert, type coercion
- [ ] **InfluxDB source/sink** — line protocol, bucket/org resolution, token auth
- [ ] **REST source/sink** — auth types (basic, bearer, apikey), pagination, retry, SSL verify
- [ ] **gNMI source** — subscription modes (ONCE, POLL, STREAM), path encoding, TLS
- [ ] **SFTP source/sink** — file glob, move-after-read, skip_processed, key auth
- [ ] **FTP source/sink** — passive mode, directory listing, file write
- [ ] **S3 source/sink** — bucket/prefix, multipart upload, credential chain
- [ ] **MQTT source/sink** — QoS levels, reconnect, topic wildcards
- [ ] **AMQP source/sink** — exchange/queue binding, ack/nack, prefetch
- [ ] **NATS source/sink** — subject routing, JetStream, reconnect

### Operations & Observability
- [ ] **Pipeline cloning** — copy a pipeline as a new one with a name prompt in the UI
- [ ] **Scheduled alert evaluation** — cron-based alert checks independent of pipeline runs
- [ ] **Dead-letter queue viewer** — browse and replay DLQ records via the UI
- [ ] **Per-sink record counts** — run metrics broken down per sink
- [ ] **Pipeline dependency graph** — visualize pipeline chains when pipeline A feeds pipeline B
- [ ] **Bulk actions** — start/stop/delete multiple pipelines at once from the list view
- [ ] **Live log streaming** — WebSocket tail of log output for running stream pipelines
- [ ] **Node health detail page** — per-worker pipeline assignments and load in manager mode

### Security & Multi-tenancy
- [ ] **Role-based access** — read-only vs admin token scopes (viewer/operator/admin)
- [ ] **Per-pipeline API key scoping** — restrict a key to specific pipelines
- [ ] **Key upload API** — `POST /api/keys/upload` / `GET /api/keys` / `DELETE /api/keys/<name>`; stored on shared PVC under `/data/keys/`
- [ ] **Audit log** — record who triggered, modified, or deleted pipelines

### New Connectors & Serializers
- [ ] **SMTP sink** — outbound email delivery (alerts, reports)
- [ ] **gRPC sink** — generic gRPC unary call sink
- [ ] **Syslog sink** — forward records to remote syslog (RFC 5424)
- [ ] **Kafka schema registry** — full Avro + Protobuf with Confluent wire format in both source and sink
- [ ] **PM-XML source** — ingest 3GPP TS 32.435 PM XML files natively (currently serializer-only)

### Infrastructure
- [ ] **Manager HA** — standby manager with DB-backed leader election; promotes on leader failure
- [ ] **Graceful worker drain** — `POST /api/workers/{id}/drain`; Helm pre-stop hook wires drain before pod termination
- [ ] **Coverage target increase** — raise CI coverage threshold from 60% to 75%

---

## Released

| Version | Theme |
|---------|-------|
| v1.2.2 | Stability & polish; CI fixes; docs alignment |
| v1.2.1 | Worker callback chain; run metrics propagation; dashboard UX |
| v1.2.0 | Manager + Worker cluster architecture |
| v1.1.4 | PipelineController; melt transform; SNMP fixes |
| v1.1.1 | YAML diff modal; enrich missing file; template fixes |
| v1.1.0 | Pipeline Wizard; Live Metrics; Alert Rules UI; AI Assist; Connector Test |
| v1.0.8/9 | Browser auth; PostgreSQL subchart; shared RWX storage |
| v1.0.7 | Bootstrap 5 web UI; dedicated K8s UI service |
| v1.0.6 | ndjson serializer; per-sink serializer_out |
| v1.0.4 | Schema registry; ClickHouse connector; pipeline update API |
| v1.0.3 | MIB management API; schema management API; standard MIBs in image |
| v1.0.2 | SNMPv3 USM (auth/priv) |
| v1.0.1 | SNMP poll yield_rows; dynamic version |
| v1.0.0 | API key auth; rate limiting; TLS; per-sink retry; circuit breaker; OTel |
| v0.9.0 | Thread workers; batch_size; DLQ; CORBA source; processed-file tracking |
