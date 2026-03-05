# Changelog

All notable changes to TRAM are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [0.6.0] — 2026-03-05

### Added

**Dead-Letter Queue (DLQ)**
- `PipelineConfig.dlq: Optional[SinkConfig]` — any sink type can serve as DLQ; receives failed records as JSON envelopes
- Envelope schema: `{_error, _stage, _pipeline, _run_id, _timestamp, record, raw}` where `raw` (base64) is only present for parse-stage failures
- Three failure stages captured: `parse` (serializer_in failed), `transform` (global or per-sink transform raised), `sink` (sink.write() raised)
- Per-record transform isolation: global transforms applied record-by-record; a single bad record no longer aborts the entire chunk
- DLQ write errors are logged and swallowed — never propagate to main pipeline
- `PipelineRunContext.dlq_count` tracks how many records were DLQ'd in a run

**Per-Sink Transform Chains**
- Each sink config gains `transforms: list[TransformConfig]` (default empty)
- Applied **after** global pipeline transforms and **after** condition filtering, **before** serializing for that specific sink
- Sink transforms are independent: different sinks can reshape the same records differently
- Sink transform failures route to DLQ (if configured) and skip that sink; other sinks continue
- `_build_sinks()` now returns `list[tuple[BaseSink, condition, list[BaseTransform]]]`

**Alert Rules**
- `AlertRuleConfig` model: `condition` (simpleeval), `action` (webhook|email), `webhook_url`, `email_to`, `subject`, `cooldown_seconds` (default 300)
- `PipelineConfig.alerts: list[AlertRuleConfig]`
- `AlertEvaluator` in `tram/alerts/evaluator.py` — evaluated after every batch run
- Alert condition namespace: `records_in`, `records_out`, `records_skipped`, `error_rate`, `status`, `failed`, `duration_seconds`
- Cooldown persisted in new SQLite `alert_state` table — survives daemon restarts
- Webhook action: `httpx.POST` with full run payload; email action: `smtplib` STARTTLS
- SMTP configured via env vars: `TRAM_SMTP_HOST/PORT/USER/PASS/TLS/FROM`
- All action errors logged and swallowed
- `PipelineManager` accepts `alert_evaluator: AlertEvaluator | None`; `AlertEvaluator(db=db)` instantiated in `create_app()`

**Helm Chart** (`helm/`)
- `Chart.yaml` — apiVersion v2, version 0.6.0
- `values.yaml` — image, replicaCount (fixed at 1), service, persistence (SQLite PVC), env, envSecret, pipelines ConfigMap, resources, nodeSelector, tolerations, affinity, podAnnotations, serviceAccount
- Templates: `deployment.yaml`, `service.yaml`, `configmap.yaml` (pipeline YAMLs), `pvc.yaml`, `serviceaccount.yaml`, `_helpers.tpl`, `NOTES.txt`
- v0.6.0 is a standalone single-replica deployment; multi-replica clustering is planned for a future release

**GitHub Actions**
- `.github/workflows/ci.yml` — triggers on push to `main`/`develop` and all PRs; runs ruff + pytest on Python 3.11 and 3.12
- `.github/workflows/release.yml` — triggers on `v*` tags; builds multi-arch Docker image (linux/amd64 + linux/arm64) → `ghcr.io/{owner}/tram:{semver}`; packages + pushes Helm chart → `oci://ghcr.io/{owner}/charts/tram`

**SQLite**
- New `alert_state` table: `(pipeline_name, rule_name, last_alerted_at)` primary key
- `TramDB.get_alert_cooldown()` / `set_alert_cooldown()` methods

**Tests** — 35 new tests (test_dlq.py ×11, test_sink_transforms.py ×8, test_alerts.py ×16); **406 total, all passing**

### Changed
- `tram/models/pipeline.py` — Transforms section moved before Sinks section to avoid Pydantic v2 forward-reference issues with `list[TransformConfig]` on sink classes
- `_build_sinks()` return type widened to 3-tuple `(BaseSink, condition | None, list[BaseTransform])`

---

## [0.5.0] — 2026-03-03

### Added

**Conditional Multi-Sink Routing**
- `sinks: list[SinkConfig]` replaces `sink: SinkConfig` (backward compat: singular `sink:` auto-wrapped by model_validator)
- Per-sink `condition: Optional[str]` — simpleeval expression evaluated per record; sink is skipped if no records match
- Catch-all sink (no condition) receives all records
- `rate_limit_rps: Optional[float]` on `PipelineConfig` — token-bucket rate limiter across all sink writes

**SQLite Persistence** (`tram/persistence/db.py`)
- `TramDB` wraps `sqlite3`; DB at `~/.tram/tram.db` (or `$TRAM_DB_PATH`)
- Tables: `run_history` (persists `RunResult`), `pipeline_versions` (auto-saved on register)
- `PipelineManager` accepts `db: TramDB | None`; `record_run()` persists to SQLite; `get_runs()` queries SQLite
- API: `GET /api/pipelines/{name}/versions`, `POST /api/pipelines/{name}/rollback?version=N`
- CLI: `tram pipeline history <name>`, `tram pipeline rollback <name> --version N`

**Prometheus Metrics** (`tram/metrics/registry.py`)
- Counters: `tram_records_in_total`, `tram_records_out_total`, `tram_records_skipped_total`, `tram_errors_total` (labeled by `pipeline`)
- Histogram: `tram_chunk_duration_seconds`
- All metrics are no-ops when `prometheus_client` is not installed
- `GET /metrics` endpoint (503 if not installed)
- New optional extra: `pip install tram[metrics]`

**Webhook Source** (`tram/connectors/webhook/source.py`)
- `@register_source("webhook")` — receives HTTP POSTs forwarded from `/webhooks/{path}` on the daemon port
- Module-level `_WEBHOOK_REGISTRY` bridges FastAPI router → source generator
- Optional `secret` for `Authorization: Bearer` validation
- New API router: `POST /webhooks/{path}` → 202 Accepted / 404 / 401

**WebSocket Connector** (`tram/connectors/websocket/`)
- `@register_source("websocket")` — background thread + asyncio loop + SimpleQueue bridge; auto-reconnect
- `@register_sink("websocket")` — `asyncio.run()` connect/send/close per write
- Optional dep: `websockets>=12.0`; new extra `pip install tram[websocket]`

**Elasticsearch Connector** (`tram/connectors/elasticsearch/`)
- `@register_source("elasticsearch")` — search + scroll API
- `@register_sink("elasticsearch")` — `helpers.bulk()` with `index_template` token substitution
- Optional dep: `elasticsearch>=8.0`; new extra `pip install tram[elasticsearch]`

**Prometheus Remote-Write Source** (`tram/connectors/prometheus_rw/source.py`)
- `@register_source("prometheus_rw")` — Snappy-decompress + protobuf `WriteRequest` → `list[dict]`
- Reuses WebhookSource global registry (path-routed via daemon)
- Optional dep: `protobuf>=4.25`, `python-snappy>=0.7`; new extra `pip install tram[prometheus_rw]`

**Schema Registry** (`tram/schema_registry/client.py`)
- `SchemaRegistryClient` — Confluent-compatible REST API (also Apicurio); in-memory cache by schema_id
- `encode_with_magic(schema_id, payload)` / `decode_magic(data)` — Confluent magic-byte `\x00` + 4-byte BE ID framing
- Avro serializer gains `schema_registry_url/subject/id` + `use_magic_bytes` config
- Protobuf serializer gains same registry config

**New Pydantic Models**
- Sources: `WebhookSourceConfig`, `WebSocketSourceConfig`, `ElasticsearchSourceConfig`, `PrometheusRWSourceConfig`
- Sinks: `WebSocketSinkConfig`, `ElasticsearchSinkConfig`
- Serializers: `AvroSerializerConfig` and `ProtobufSerializerConfig` extended with registry fields

**Tests** — 49 new tests; **371 total, all passing**

---

## [0.4.0] — 2026-03-03

### Added

**New Serializers**
- `avro` — fastavro read/write; requires `pip install tram[avro]`
- `parquet` — pyarrow read/write; requires `pip install tram[parquet]`
- `msgpack` — msgpack pack/unpack; requires `pip install tram[msgpack_ser]`
- `protobuf` — runtime .proto compilation via grpcio-tools; length-delimited framing; requires `pip install tram[protobuf_ser]`

**New Source Connectors**
- `mqtt` — paho-mqtt subscriber; TLS support; reconnect on drop
- `amqp` — pika consumer; prefetch, auto-ack configurable
- `nats` — nats-py subscriber; queue groups; credentials file
- `gnmi` — pygnmi subscription (telemetry streaming)
- `sql` — SQLAlchemy; chunked reads
- `influxdb` — influxdb-client Flux query
- `redis` — list LPOP or stream XREAD modes
- `gcs` — google-cloud-storage; blob listing + streaming
- `azure_blob` — azure-storage-blob; container listing + streaming

**New Sink Connectors**
- `amqp` — pika publisher to exchange/routing-key
- `nats` — nats-py publisher
- `sql` — SQLAlchemy insert/upsert
- `influxdb` — line-protocol write
- `redis` — list RPUSH, pubsub PUBLISH, or stream XADD
- `gcs` — google-cloud-storage blob upload
- `azure_blob` — azure-storage-blob upload

**New Transforms**
- `explode` — expand a list field into multiple rows
- `deduplicate` — remove duplicate rows by key fields
- `regex_extract` — extract named capture groups from a string field
- `template` — render Jinja-style `{field}` string templates
- `mask` — redact, hash, or partial-mask sensitive fields
- `validate` — schema validation with `on_invalid: drop|raise`
- `sort` — sort records by field list
- `limit` — keep only first N records
- `jmespath` — JMESPath field extraction
- `unnest` — lift a nested dict field to top level

**Tests** — 322 total, all passing

---

## [0.3.0] — 2026-03-03

### Added

**New Connectors**
- `ftp` source + sink — ftplib; move/delete after read; passive mode
- `s3` source + sink — boto3; endpoint_url override for S3-compatible stores
- `syslog` source — UDP/TCP listener; RFC 3164/5424 parsing
- `snmp_trap` source + sink — pysnmp trap receiver / sender
- `snmp_poll` source — GET/WALK OID polling
- `ves` sink — ONAP VES event batch sender; auth types: none/basic/bearer
- `opensearch` source (scroll) added alongside existing sink

**Tests** — 198 total, all passing

---

## [0.2.0] — 2026-03-03

### Added

**New Transforms**
- `flatten` — recursive dict flattening with configurable `separator`, `max_depth`, and `prefix`
- `timestamp_normalize` — normalizes heterogeneous timestamps to UTC ISO-8601
- `aggregate` — groupby + sum/avg/min/max/count/first/last
- `enrich` — left-join records with a static CSV or JSON lookup file

**New Connectors**
- `local` source + sink — reads/writes local filesystem files
- `rest` source + sink — HTTP polling source and POST/PUT sink (httpx)
- `kafka` source + sink — KafkaConsumer/Producer; SASL/SSL support
- `opensearch` sink — bulk-indexes records via opensearch-py

**Tests** — 124 total, all passing

---

## [0.1.0] — 2026-03-03

### Added

**Core**
- `tram.core.exceptions` — `TramError` hierarchy
- `tram.core.context` — `PipelineRunContext` + `RunResult` + `RunStatus`
- `tram.core.config` — `AppConfig` from environment variables
- `tram.core.log_config` — JSON-structured logging

**Plugin Interfaces** — `BaseSource`, `BaseSink`, `BaseTransform`, `BaseSerializer`

**Plugin Registry** — `@register_*` decorators + `get_*()` lookups + `list_plugins()`

**Pydantic Models** — `PipelineConfig` with discriminated unions; `ScheduleConfig`

**Serializers** — `json`, `csv`, `xml`

**Transforms** — `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`

**Connectors** — `sftp` source + sink

**Pipeline Engine** — `loader.py`, `executor.py` (batch/stream/dry-run), `manager.py`

**Scheduler** — `TramScheduler` (APScheduler batch + threads stream)

**REST API** — FastAPI on port 8765; health, pipelines, runs, daemon endpoints

**CLI** — Typer; direct + daemon-proxy commands

**Tests** — 69 total, all passing
