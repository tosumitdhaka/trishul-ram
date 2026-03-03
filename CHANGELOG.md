# Changelog

All notable changes to TRAM are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

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
