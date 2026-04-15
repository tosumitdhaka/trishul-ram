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

## v1.2.3 — Connector Fixes: Kafka, SNMP, ASN1

- [ ] Kafka source — test and fix real-behavior edge cases (reconnect, offset commit, consumer group)
- [ ] Kafka sink — test and fix producer error handling, retry, serializer integration
- [ ] SNMP poll source — test OID resolution, walk vs get, SNMPv3 USM, yield_rows behavior
- [ ] SNMP trap source — test trap reception, MIB decoding, v3 encrypted traps
- [ ] SNMP trap sink — test varbind construction, v1/v2c/v3 send paths
- [ ] ASN1 serializer — test encode/decode round-trip, error handling on malformed input

---

## v1.2.4 — Connector Fixes: OpenSearch, ClickHouse, InfluxDB

- [ ] OpenSearch sink — test bulk write, index template, auth, retry on 429
- [ ] ClickHouse source/sink — test query execution, batch insert, type coercion
- [ ] InfluxDB source/sink — test line protocol, bucket/org resolution, token auth

---

## v1.2.5 — Connector Fixes: REST, Webhook, gNMI

- [ ] REST source/sink — test auth types (basic, bearer, apikey), pagination, retry, SSL verify
- [ ] Webhook source — test payload parsing, path routing, concurrent requests
- [ ] gNMI source — test subscription modes (ONCE, POLL, STREAM), path encoding, TLS

---

## v1.2.6 — Connector Fixes: SFTP, FTP, S3

- [ ] SFTP source/sink — test file glob, move-after-read, skip_processed, key auth
- [ ] FTP source/sink — test passive mode, directory listing, file write
- [ ] S3 source/sink — test bucket/prefix, multipart upload, credential chain

---

## v1.2.7 — Connector Fixes: MQTT, AMQP, NATS

- [ ] MQTT source/sink — test QoS levels, reconnect, topic wildcards
- [ ] AMQP source/sink — test exchange/queue binding, ack/nack, prefetch
- [ ] NATS source/sink — test subject routing, JetStream, reconnect

---

## Backlog (unversioned)

Features and issues not yet assigned to a release:

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
