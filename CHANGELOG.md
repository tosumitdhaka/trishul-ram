# Changelog

All notable changes to TRAM are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [0.9.0] — 2026-03-05

### Added

**`thread_workers` — intra-node parallelism**
- `PipelineConfig.thread_workers: int = 1` — number of worker threads per pipeline run
- `batch_run()`: when `thread_workers > 1`, chunks from the source are submitted to a
  `ThreadPoolExecutor(max_workers=thread_workers)` so N chunks process concurrently; single-
  threaded code path unchanged for `thread_workers=1`
- `stream_run()`: when `thread_workers > 1`, a bounded `Queue(maxsize=thread_workers * 2)`
  decouples the source producer from N worker threads, providing natural backpressure
- `PipelineRunContext` is now fully thread-safe: all counter mutations go through
  `threading.Lock`-protected helper methods (`inc_records_in`, `inc_records_out`,
  `inc_records_skipped`, `record_error`, `record_dlq`)

**`batch_size` — record cap per run**
- `PipelineConfig.batch_size: Optional[int] = None` — limits records processed per batch run
- Source read loop breaks once `ctx.records_in >= batch_size`; remaining source chunks skipped
- Works in both single-threaded and multi-threaded modes
- Useful for controlling run duration on large sources (Kafka backlog, large S3 buckets)

**`on_error: "dlq"` — explicit DLQ routing**
- `on_error` Literal extended with `"dlq"` value
- Model validator raises `ValueError` if `on_error="dlq"` is set without a `dlq` sink configured
- Runtime behavior identical to `on_error="continue"` with DLQ sink present — makes intent explicit

**Processed-file tracking**
- New DB table: `processed_files (pipeline_name, source_key, filepath, processed_at)` — PRIMARY KEY on all three name fields; indexed on `(pipeline_name, source_key)` for fast lookup
- `TramDB.is_processed(pipeline, source_key, filepath) -> bool`
- `TramDB.mark_processed(pipeline, source_key, filepath)` — dialect-aware upsert; errors logged and swallowed
- `ProcessedFileTracker` wrapper in `tram/persistence/file_tracker.py` — silences DB errors, safe for use in connectors
- `skip_processed: bool = False` added to `SFTPSourceConfig`, `LocalSourceConfig`, `S3SourceConfig`, `FtpSourceConfig`, `GcsSourceConfig`, `AzureBlobSourceConfig`
- Source connectors check `is_processed` before reading and call `mark_processed` after successful yield + `_post_read`
- `PipelineExecutor._build_source()` injects `_file_tracker` into source config dict when `file_tracker` is present on the executor
- `TramScheduler` and `create_app()` wired to create and pass `ProcessedFileTracker` when DB is available

**CORBA source connector**
- `@register_source("corba")` — DII (Dynamic Invocation Interface) mode; no pre-compiled IDL stubs required
- Supports: direct IOR (`ior:`) or NamingService resolution (`naming_service:` + `object_name:`)
- `operation:` names the CORBA operation; `args:` passes positional scalar arguments via DII
- Result normalised to `list[dict]` via `_corba_to_python()` (handles structs, nested sequences)
- `skip_processed: bool` supported via `ProcessedFileTracker` — invocation key = `operation:args_json`
- `pip install tram[corba]` (pulls `omniORBpy>=4.3`)
- `CorbaSourceConfig` in Pydantic models with `model_validator` requiring `ior` or `naming_service`
- Plugin key: `corba`

**Helm: ConfigMap checksum annotation**
- `checksum/config` annotation added to the StatefulSet pod template (when `pipelines` values are non-empty)
- Value: `sha256sum` of the rendered `configmap.yaml` — changes when any pipeline YAML changes
- Kubernetes detects the pod spec diff and triggers a rolling restart automatically on `helm upgrade`

**Tests** — 62 new tests (`test_thread_workers.py` ×13, `test_batch_size_on_error.py` ×10,
`test_processed_files.py` ×15, `test_corba_connector.py` ×24); **535 total, all passing**

### Changed
- `PipelineExecutor.__init__` gains `file_tracker: ProcessedFileTracker | None = None`
- `TramScheduler.__init__` gains `file_tracker: ProcessedFileTracker | None = None`
- `executor._build_source()` injects both `_pipeline_name` and `_file_tracker` into source config
- `tram/__init__.__version__` → `"0.9.0"`

---

## [0.8.1] — 2026-03-05

### Fixed

**Kafka consumer group isolation**
- `KafkaSourceConfig.group_id` default changed from `"tram"` (shared across every pipeline) to
  `None` — resolved at runtime to the pipeline name, giving each pipeline its own consumer group
- Pipelines that set `group_id:` explicitly in YAML are unaffected
- Added explicit `consumer.commit()` before `consumer.close()` — best-effort offset flush on clean
  shutdown (supplements `enable_auto_commit=True` timer; no-ops on abrupt kill)
- Fallback chain: explicit `group_id` → pipeline name → `"tram"` (if no pipeline name available)

**NATS queue group for cluster mode**
- `NatsSourceConfig.queue_group` default changed from `""` (broadcast — all cluster nodes receive
  every message) to `None` — resolved at runtime to the pipeline name (competing consumers, correct
  for cluster mode where the same pipeline runs on all nodes)
- `queue_group: ""` in YAML still works as an explicit broadcast opt-out
- Fallback chain: explicit `queue_group` (including `""`) → pipeline name → `""` (broadcast)

**Pipeline name injection**
- `PipelineExecutor._build_source()` now injects `_pipeline_name` into the source config dict;
  connectors can use `config.get("_pipeline_name")` as a safe default for group/queue identifiers

**Helm chart**
- `helm/values.yaml` `image.tag` corrected from `"0.6.0"` to `"0.8.1"`

**Tests** — 20 new tests (`test_kafka_connectors.py` ×16, `test_nats_connectors.py` ×5 new);
**473 total, all passing**

---

## [0.8.0] — 2026-03-05

### Added

**StatefulSet self-organizing cluster**
- `tram/cluster/registry.py` — `NodeRegistry`: registers the local node in the shared DB, runs a
  periodic heartbeat thread, expires stale peers (`status='dead'`), deregisters on clean shutdown
- `tram/cluster/coordinator.py` — `ClusterCoordinator`: caches live node topology, determines
  pipeline ownership via consistent hashing: `sha1(pipeline_name) % live_node_count == my_position`
- Ownership uses **sorted position** in live node list (not static ordinal) — handles non-sequential
  ordinals gracefully when a node fails (tram-0, tram-2 become positions 0 and 1)
- Safe fallback: if no live nodes in DB (startup race), the node owns all pipelines
- `detect_ordinal(node_id)` helper: extracts ordinal suffix from StatefulSet hostname (`tram-2` → `2`)

**DB: node_registry table**
- `node_registry` table: `node_id, ordinal, registered_at, last_heartbeat, status`
- New `TramDB` methods: `register_node()` (dialect-aware upsert), `heartbeat()`, `expire_nodes()`,
  `get_live_nodes()`, `deregister_node()`
- Cluster mode requires an external DB (`TRAM_DB_URL`); SQLite is blocked with a warning

**Cluster env vars (AppConfig)**
- `TRAM_CLUSTER_ENABLED` — enable cluster mode (default: `false`)
- `TRAM_NODE_ORDINAL` — override ordinal (default: auto-detected from hostname)
- `TRAM_HEARTBEAT_SECONDS` — heartbeat interval in seconds (default: `10`)
- `TRAM_NODE_TTL_SECONDS` — seconds before a silent node is marked dead (default: `30`)

**Scheduler: dynamic rebalance**
- `TramScheduler` gains `coordinator` and `rebalance_interval` parameters
- Ownership check in `_schedule_pipeline()` — nodes skip pipelines they don't own
- Background `tram-rebalance` thread: polls `coordinator.refresh()` every N seconds; on topology
  change calls `_rebalance()` which starts newly owned pipelines and stops released ones

**Cluster API endpoint**
- `GET /api/cluster/nodes` — returns `cluster_enabled`, `node_id`, `my_position`,
  `live_node_count`, `nodes` list; returns `{"cluster_enabled": false}` in standalone mode

**Helm: always-StatefulSet design**
- `helm/templates/statefulset.yaml` — always rendered; `replicaCount=1` standalone, `N` cluster
- `helm/templates/headless-service.yaml` — always rendered; headless Service for stable pod DNS
- `deployment.yaml` and `pvc.yaml` removed — replaced by `volumeClaimTemplates` in StatefulSet
- `volumeClaimTemplates` auto-provisions `data-tram-N` PVC per pod — survives pod restarts and
  rescheduling; PVC stays bound to the same pod across node reschedules
- `helm/values.yaml` — `clusterMode.enabled: false` controls `TRAM_CLUSTER_ENABLED` env var
- `helm/Chart.yaml` — version bumped to `0.8.0`

**Tests** — 22 new tests (`test_cluster.py`); **453 total, all passing**

### Changed
- `TramScheduler.__init__` gains optional `coordinator: ClusterCoordinator | None` and
  `rebalance_interval: int` parameters (backward compatible — defaults to standalone behaviour)
- `tram/api/app.py` wires `NodeRegistry` + `ClusterCoordinator` from `AppConfig` in lifespan
- `tram/__init__.__version__` → `"0.8.0"`

---

## [0.7.0] — 2026-03-05

### Added

**SQLAlchemy Core DB abstraction**
- `tram/persistence/db.py` rewritten on SQLAlchemy Core — any backend supported via `TRAM_DB_URL`
- SQLite (default), PostgreSQL (`tram[postgresql]`), MySQL/MariaDB (`tram[mysql]`) all work out of the box
- `TRAM_DB_URL` env var (SQLAlchemy URL); falls back to `TRAM_DB_PATH` → SQLite when unset
- Connection pooling (`pool_size=5`, `max_overflow=10`, `pool_pre_ping=True`) for non-SQLite backends
- `sqlalchemy>=2.0` added to core dependencies (was previously in `[sql]` optional only)
- New optional extras: `postgresql = ["psycopg2-binary>=2.9"]`, `mysql = ["PyMySQL>=1.1"]`

**Node identity**
- `AppConfig.node_id` — from `TRAM_NODE_ID` env (default: `socket.gethostname()`)
- `node_id` stored in every `run_history` row — essential for multi-node cluster debugging
- `TramDB(url, node_id)` constructor; node_id auto-stamped on every `save_run()`

**`dlq_count` persisted**
- `RunResult.dlq_count: int = 0` field added; `from_context()` carries it from `PipelineRunContext`
- `to_dict()` now includes `dlq_count`
- `dlq_count` column added to `run_history` table
- `tram_dlq_total` Prometheus counter (`pipeline` label) incremented on every DLQ write

**Graceful shutdown**
- `TramScheduler.stop(timeout: int = 30)` — signals all stream threads, waits for in-flight batch runs via `ThreadPoolExecutor.shutdown(wait=True)`, joins stream threads with timeout
- `TRAM_SHUTDOWN_TIMEOUT_SECONDS` env var (default `30`) wired through `AppConfig` and `lifespan`
- SIGTERM handler in `daemon/server.py` converts SIGTERM → SIGINT so uvicorn gets a clean shutdown (critical for Docker / Kubernetes PID 1)

**Readiness DB check**
- `TramDB.health_check()` executes `SELECT 1`; returns `True/False`
- `GET /api/ready` returns `503` when DB is configured but unreachable

**Run history pagination**
- `GET /api/runs` gains `offset` and `from_dt` query params
- `TramDB.get_runs(offset, from_dt)` — `OFFSET` clause + `started_at >=` filter
- `PipelineManager.get_runs()` and in-memory fallback both support new params
- `TramDB.get_run(run_id)` now queries DB directly (previously only searched in-memory deque)

**Schema migration**
- `_create_tables()` is idempotent: `CREATE TABLE IF NOT EXISTS` + `_add_column_if_missing()` helper
- Existing v0.6.0 SQLite databases upgraded automatically on first start (adds `node_id`, `dlq_count` to `run_history`)

**Tests** — 25 new tests (`test_db_v07.py` ×15, `test_config_v07.py` ×6, `test_runresult_v07.py` ×4); **431 total, all passing**

### Changed
- `TramDB.__init__` signature: `path: Path` → `url: str = "", node_id: str = ""` (uses SQLAlchemy URL)
- `pipeline_versions.id` now TEXT UUID (generated in Python); fresh databases get UUID ids; existing SQLite databases keep their integer ids (SQLite flexible typing)
- `AppConfig` gains `node_id`, `db_url`, `shutdown_timeout` fields (from env: `TRAM_NODE_ID`, `TRAM_DB_URL`, `TRAM_SHUTDOWN_TIMEOUT_SECONDS`)

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
- Templates: `statefulset.yaml`, `service.yaml`, `headless-service.yaml`, `configmap.yaml`, `serviceaccount.yaml`, `_helpers.tpl`, `NOTES.txt`
- Storage managed via `volumeClaimTemplates` (introduced in v0.8.0; v0.6.0 used `deployment.yaml` + `pvc.yaml`)

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
