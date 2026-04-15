# TRAM Roadmap

Planned features and known issues. Items are assigned to a version once scope is confirmed;
unconfirmed work lives in the backlog at the bottom.

---

## v1.2.2 — Stability & Polish

- [x] Unit coverage raised to 77% (1,250 passing tests)
- [x] Fix `AttributeError` in `cli/main.py` when unpacking `(config, raw_yaml)` tuple from `load_pipeline()`
- [x] Fix `PipelineAlreadyExistsError` in `watcher/pipeline_watcher.py` on hot-reload
- [x] Align `docs/api.md` response shapes (dry-run, connector-test, change-password) with implementation
- [x] Fix `on_error` valid values in `docs/connectors.md` (`continue | abort | retry | dlq`)
- [x] Remove `omniORBpy` from the `all` pip extra — it is a system package, not a PyPI wheel (CI was failing)

---

## v1.3.0 — Operations & Observability

- [ ] **Pipeline cloning** — copy a pipeline as a new one with a name prompt in the UI
- [ ] **Scheduled alert evaluation** — cron-based alert checks independent of pipeline runs
- [ ] **Dead-letter queue viewer** — browse and replay DLQ records via the UI
- [ ] **Per-sink record counts** — run metrics broken down per sink
- [ ] **Pipeline dependency graph** — visualize pipeline chains when pipeline A feeds pipeline B
- [ ] **Bulk actions** — start/stop/delete multiple pipelines at once from the list view
- [ ] **Live log streaming** — WebSocket tail of log output for running stream pipelines
- [ ] **Node health detail page** — per-worker pipeline assignments and load in manager mode

---

## v1.4.0 — Security & Multi-tenancy

- [ ] **Role-based access** — read-only vs admin token scopes (viewer/operator/admin)
- [ ] **Per-pipeline API key scoping** — restrict a key to specific pipelines
- [ ] **Key upload API** — `POST /api/keys/upload` / `GET /api/keys` / `DELETE /api/keys/<name>`; stored on shared PVC under `/data/keys/`; UI shows key dropdown in connector forms
- [ ] **Audit log** — record who triggered, modified, or deleted pipelines

---

## v1.5.0 — Connectors & Serializers

- [ ] **SMTP sink** — outbound email delivery (alerts, reports)
- [ ] **gRPC sink** — generic gRPC unary call sink
- [ ] **Syslog sink** — forward records to remote syslog (RFC 5424)
- [ ] **Kafka schema registry** — full Avro + Protobuf with Confluent wire format in both source and sink

---

## Backlog (unversioned)

Features and issues not yet assigned to a release:

- [ ] **Manager HA** — standby manager with DB-backed leader election; standby serves read-only API and promotes on leader failure
- [ ] **Graceful worker drain** — `POST /api/workers/{id}/drain`; Helm pre-stop hook wires drain before pod termination
- [ ] **Per-connector integration tests** — dedicated test suite for each source/sink (Kafka, OpenSearch, SFTP, S3, ClickHouse, SNMP) run against real services in CI
- [ ] **PM-XML source** — ingest 3GPP TS 32.435 PM XML files natively (currently serializer-only)
- [ ] **Dark/light theme toggle** — user-controlled, persisted in `localStorage`
- [ ] **Coverage target increase** — raise CI coverage threshold from 60% to 75%

---

## Released

| Version | Theme |
|---------|-------|
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
