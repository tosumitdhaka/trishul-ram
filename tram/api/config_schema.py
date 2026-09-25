"""Shared backend-generated config schema metadata for UI and AI helpers."""

from __future__ import annotations

import re
import typing
from typing import get_args, get_origin

from pydantic_core import PydanticUndefined

# Fields that appear on every model and carry no connector-specific meaning
_SKIP_FIELDS = {"type", "condition", "transforms", "serializer_out"}

# Fields that are internal plumbing injected by the executor at runtime
_INTERNAL_PREFIX = "_"

# Substrings that mark a connector field as secret-bearing. Shared with the AI
# router's outbound-prompt redaction (tram/api/routers/ai.py) so the schema
# cache and prompt masking can never drift apart.
SECRET_NAME_TOKENS = ("password", "token", "secret", "api_key")


def _type_name(annotation) -> str:
    """Return a compact human-readable type string for a Pydantic field annotation."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    union_type = getattr(typing, "UnionType", None)

    if origin is typing.Union or (
        union_type is not None and isinstance(annotation, union_type)
    ):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_name(non_none[0]) + " | None"
        return " | ".join(_type_name(a) for a in non_none)

    if origin is list:
        inner = _type_name(args[0]) if args else "any"
        return f"list[{inner}]"

    if origin is dict:
        k = _type_name(args[0]) if args else "str"
        v = _type_name(args[1]) if len(args) > 1 else "any"
        return f"dict[{k}, {v}]"

    if hasattr(annotation, "__name__"):
        name = annotation.__name__
        return {"str": "str", "int": "int", "float": "float", "bool": "bool"}.get(name, name)

    if origin is typing.Literal:
        return str(args[0]) if len(args) == 1 else "|".join(str(a) for a in args)

    return str(annotation)


def _model_to_schema_lines(model_cls) -> list[str]:
    lines = []
    try:
        fields = model_cls.model_fields
    except AttributeError:
        return lines

    for name, field_info in fields.items():
        if name in _SKIP_FIELDS or name.startswith(_INTERNAL_PREFIX):
            continue

        required = field_info.is_required()
        default = field_info.default
        annotation = field_info.annotation

        type_str = _type_name(annotation) if annotation is not None else "any"
        type_str = re.sub(r"SerializerConfig \| None", "SerializerConfig", type_str)

        if required:
            lines.append(f"  {name}: {type_str}  # required")
        else:
            if default is None:
                lines.append(f"  {name}: {type_str}  # optional")
            elif isinstance(default, bool):
                lines.append(f"  {name}: {type_str} = {str(default).lower()}")
            elif isinstance(default, (int, float)):
                lines.append(f"  {name}: {type_str} = {default}")
            elif isinstance(default, str) and default:
                lines.append(f"  {name}: {type_str} = \"{default}\"")
            elif isinstance(default, list) and not default:
                pass
            elif isinstance(default, str) and not default:
                pass
            else:
                lines.append(f"  {name}: {type_str}")

    return lines


def _unwrap_optional(annotation):
    origin = get_origin(annotation)
    args = get_args(annotation)
    import types as _types

    if origin is typing.Union or (
        isinstance(annotation, _types.UnionType)
    ):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and len(non_none) != len(args):
            return non_none[0], True
    return annotation, False


def _field_kind(annotation) -> tuple[str, list[str] | None]:
    inner, _optional = _unwrap_optional(annotation)
    origin = get_origin(inner)
    args = get_args(inner)

    if origin is typing.Literal:
        return "select", [str(a) for a in args]
    if inner is bool:
        return "boolean", None
    if inner is int:
        return "integer", None
    if inner is float:
        return "number", None
    if inner is str:
        return "text", None
    if origin is list:
        item = args[0] if args else str
        item_inner, _ = _unwrap_optional(item)
        if item_inner in (str, int, float, bool):
            return "list", None
        return "complex", None
    if origin is dict:
        return "map", None
    return "complex", None


def _serialize_default(value):
    if value is None or value is PydanticUndefined:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        return value
    return str(value)


def _model_to_field_descriptors(model_cls) -> list[dict]:
    fields = []
    try:
        model_fields = model_cls.model_fields
    except AttributeError:
        return fields

    for name, field_info in model_fields.items():
        if name in _SKIP_FIELDS or name.startswith(_INTERNAL_PREFIX):
            continue
        annotation = field_info.annotation
        kind, choices = _field_kind(annotation)
        fields.append(
            {
                "name": name,
                "type": _type_name(annotation) if annotation is not None else "any",
                "kind": kind,
                "choices": choices or [],
                "required": field_info.is_required(),
                "default": _serialize_default(field_info.default),
                "secret": any(token in name for token in SECRET_NAME_TOKENS),
                "multiline": name in {"query", "body"} or name.endswith("_template"),
            }
        )
    return fields


# ── A.1: per-field descriptions ───────────────────────────────────────────────
# Operator-facing one-liners surfaced as the wizard's form help text and (via
# the schema payload and /api/plugins) the Plugins page. Wording tracks
# docs/connectors.md, docs/transforms.md, and the changelog terminology.
# Shared fields that repeat across types use the field-name map below;
# type-specific overrides live in _TYPE_FIELD_DESCRIPTIONS keyed by
# (category, type, field).

_COMMON_FIELD_DESCRIPTIONS: dict[str, str] = {
    "host": "Server hostname or IP address",
    "port": "Server port",
    "username": "Authentication username",
    "password": "Authentication password (secret)",
    "token": "Authentication token (secret)",
    "api_key": "API key (secret)",
    "api_key_header": "Request header that carries the API key",
    "url": "Endpoint URL",
    "timeout": "Request timeout in seconds",
    "headers": "Additional request headers",
    "extra_headers": "Additional headers attached to each message or handshake",
    "verify_ssl": "Verify TLS certificates on requests",
    "verify": "Verify TLS certificates when TLS is enabled",
    "tls": "Enable TLS for the connection",
    "tls_ca": "Path to the CA certificate that verifies the TLS peer",
    "community": "SNMP v1/v2c community string",
    "security_name": "SNMPv3 USM username",
    "auth_protocol": "SNMPv3 authentication protocol (MD5, SHA, SHA224, SHA256, SHA384, SHA512)",
    "auth_key": "SNMPv3 authentication passphrase (secret)",
    "priv_protocol": "SNMPv3 privacy protocol (DES, 3DES, AES, AES128, AES192, AES256)",
    "priv_key": "SNMPv3 privacy passphrase (secret)",
    "context_name": "SNMPv3 context name",
    "resolve_oids": "Resolve OIDs to symbolic names using the loaded MIBs",
    "mib_dirs": "Extra directories containing compiled MIB Python files",
    "mib_modules": "MIB module names to pre-load (e.g. IF-MIB, SNMPv2-MIB)",
    "retry_count": "Retries on write failure before the batch is marked failed (0 = no retry)",
    "retry_delay_seconds": "Base delay between retries in seconds (exponential backoff per attempt)",
    "circuit_breaker_threshold": "Open the circuit breaker after this many consecutive failures (0 = off)",
    "circuit_breaker_window_seconds": "Seconds the circuit breaker stays open before the sink is retried",
    "reconnect_delay_seconds": "Seconds between reconnect attempts",
    "max_reconnect_attempts": "Maximum reconnect attempts (0 = infinite)",
    "delete_after_read": "Delete source files after they are read successfully",
    "move_after_read": "Move processed files to this directory after reading",
    "skip_processed": "Skip files already processed (tracked in durable state)",
    "file_pattern": "Glob pattern matching the files to process",
    "encoding": "Character encoding applied to text (e.g. utf-8)",
    "bucket": "Object storage bucket name",
    "container": "Blob storage container name",
    "connection_string": "Storage connection string (secret)",
    "account_name": "Storage account name (alternative to a connection string)",
    "account_key": "Storage account key (secret)",
    "service_account_json": "Google service account JSON key (secret)",
    "endpoint_url": "Custom S3-compatible endpoint URL",
    "region_name": "AWS region the bucket lives in",
    "aws_access_key_id": "AWS access key ID",
    "aws_secret_access_key": "AWS secret access key (secret)",
    "ssl_cafile": "Path to the CA certificate file",
    "qos": "MQTT QoS level (0, 1, or 2)",
    "client_id": "MQTT client identifier",
    "keepalive": "MQTT keepalive interval in seconds",
    "servers": "Server URLs to connect to",
    "subject": "Subject to subscribe to or publish on",
    "queue_group": "Load-balancing queue group (empty string = broadcast)",
    "credentials_file": "Path to the NATS credentials file",
    "connect_timeout": "Connection timeout in seconds",
    "send_receive_timeout": "Send/receive timeout in seconds",
    "secure": "Use TLS for the connection",
    "ca_certs": "Path to CA certificates used for TLS verification",
    "verify_certs": "Verify TLS certificates on connections",
    "refresh": "Elasticsearch/OpenSearch refresh policy for writes (true, wait_for, or false)",
    "pipeline": "Ingest pipeline applied to documents before indexing",
    "id_field": "Record field used as the document ID",
    "index": "Index name (supports {pipeline}, {timestamp}, {YYYY}, {MM}, {DD} tokens)",
    "filename_template": "Output filename template; tokens: {pipeline}, {timestamp}, {epoch}, {part}/{index}, {run_id}, {source_filename}, {source_stem}, {field.*}",
    "file_mode": "Output file mode (single or rolling)",
    "max_records": "Roll to a new file part when the next write would exceed this record count",
    "max_time": "Roll to a new file part after this many seconds",
    "max_bytes": "Roll to a new file part when the byte count would exceed this",
    "max_index": "Highest file part index before the oldest part is deleted",
    "overwrite": "Overwrite an existing object or file with the same name",
    "content_type": "Content type assigned to written objects or messages",
    "passive": "Use passive FTP mode",
    "recursive": "Recursively scan subdirectories for matching files",
    "file_stability_seconds": "Require a file's size and mtime to be unchanged across two scans this many seconds apart before reading it (0 = off)",
    "file_min_age_seconds": "Ignore files younger than this many seconds (tolerates future-mtime clocks)",
    "file_done_suffix": "Only collect files renamed to this suffix; the suffix is stripped from the source filename",
    "read_chunk_bytes": "Bytes read per chunk while streaming a file",
    "method": "HTTP method",
    "body": "Request body string or dict (for POST/PUT)",
    "auth_type": "Authentication type (none, basic, bearer, or apikey)",
    "expected_status": "HTTP status codes treated as success",
    "response_path": "Dot-path into the JSON response that yields the records (e.g. data.items)",
    "paginate": "Enable offset-based pagination across pages",
    "page_param": "Query parameter name that carries the page offset",
    "page_size": "Records per page",
    "total_path": "Dot-path to the total record count in the response",
    "secret": "Bearer token that must be presented on incoming requests (secret)",
    "max_queue_size": "Maximum queued payloads before backpressure",
    "buffer_size": "Receive buffer size in bytes",
    "max_message_size": "Maximum accepted message size in bytes (oversized messages are truncated and logged)",
    "max_connections": "Maximum concurrent connections (excess connections are refused)",
    "protocol": "Transport protocol (udp or tcp)",
    "ping_interval": "WebSocket ping interval in seconds",
    "reconnect": "Automatically reconnect after a disconnect",
    "reconnect_delay": "Seconds between WebSocket reconnect attempts",
    "poll_interval_seconds": "Seconds between polls",
    "database": "Database name",
    "table": "Target table name",
    "upsert_keys": "Columns that define the upsert key for conflict resolution",
    "batch_size": "Records processed per batch operation",
    "chunk_size": "Rows/documents processed per chunk (0 = all at once)",
    "org": "InfluxDB organization name",
    "measurement": "InfluxDB measurement written to",
    "tag_fields": "Record fields written as InfluxDB tags",
    "precision": "Timestamp precision for InfluxDB writes (e.g. s, ms, us, ns)",
    "avro_schema": "Inline Avro schema JSON",
    "schema_file": "Path to the schema definition file",
    "schema_registry_url": "Confluent-compatible schema registry URL",
    "schema_registry_subject": "Schema registry subject name",
    "schema_registry_id": "Schema registry schema ID",
    "use_magic_bytes": "Expect/write the Confluent magic byte + schema ID prefix",
    "message_class": "Top-level message/type name to decode",
    "framing": "Protobuf message framing (length_delimited or none)",
    "delimiter": "Field delimiter character",
    "has_header": "Treat the first row as a header",
    "quotechar": "Quote character for quoted fields",
    "indent": "Pretty-print indent in spaces (null = compact)",
    "ensure_ascii": "Escape non-ASCII characters as \\uXXXX",
    "strict": "Raise on non-object lines; false wraps scalars and lists",
    "newline": "Line separator written between records (e.g. \\n)",
    "compression": "Compression codec (e.g. snappy, gzip, none)",
    "root_element": "XML root element name",
    "record_element": "XML element name wrapping each record",
    "skip_empty": "Skip empty lines or records",
    "line_field": "Field name that carries the raw line text",
    "include_line_num": "Add a line-number field to each output record",
    "exchange": "AMQP exchange name",
    "routing_key": "AMQP routing key",
    "queue": "AMQP queue name",
    "prefetch_count": "Messages fetched ahead per consumer",
    "auto_ack": "Acknowledge messages immediately on receipt",
    "domain": "VES event domain (e.g. fault, measurement, other)",
    "source_name": "VES source name (reportingEntityName in the event)",
    "reporting_entity_name": "VES reporting entity name",
    "priority": "VES event priority",
    "key_field": "Record field used as the Kafka message key",
    "acks": "Kafka producer acknowledgment level (e.g. all, 1, 0)",
    "compression_type": "Kafka producer compression codec (e.g. none, gzip, snappy, lz4, zstd)",
    "trap_oid": "SNMP trap OID to send",
    "varbinds": "List of varbind specifications for the trap",
    "group_by": "Fields that define the aggregation group",
    "operations": "Aggregation operations (op:field or {op, field})",
    "lookup_file": "Path to the static lookup file",
    "lookup_format": "Lookup file format (csv or json)",
    "join_key": "Record field used to join against the lookup",
    "lookup_key": "Lookup column that matches the join key",
    "add_fields": "Fields copied from the lookup row (null = add all)",
    "on_miss": "Behavior when no lookup row matches (keep or null_fields)",
    "field": "Dotted path to the target field",
    "include_index": "Add an index column to each exploded row",
    "index_field": "Field name that carries the exploded row index",
    "drop_source": "Remove the source field after the transform",
    "separator": "Separator joining flattened keys",
    "max_depth": "Maximum nesting depth (0 = unlimited)",
    "preserve_original": "Keep the original value alongside the transformed one",
    "original_suffix": "Suffix appended to the preserved original field name",
    "overrides": "Explicit per-path overrides",
    "include_all": "Copy all metadata keys instead of the fields map",
    "on_missing": "Behavior when a source/meta key is absent (skip or null)",
    "explode_paths": "Dotted paths to list fields exploded into one row per element",
    "keep_empty_rows": "Emit rows that end up empty after flattening",
    "preserve_lists": "Keep list values intact instead of flattening them",
    "zip_groups": "Align paired list fields element-wise (fields plus strict)",
    "choice_unwrap": "Unwrap ASN.1 CHOICE values (paths, mode, type_suffix, value_suffix)",
    "drop_paths": "Dotted paths removed from the final flattened keys",
    "placeholder": "Mask placeholder text",
    "visible_start": "Leading characters left visible in partial mode",
    "visible_end": "Trailing characters left visible in partial mode",
    "value_field": "Dict-valued field to pivot into one record per key/value pair",
    "label_fields": "Dict fields unnested as label columns",
    "metric_name_col": "Output column carrying the metric name",
    "metric_value_col": "Output column carrying the metric value",
    "include_only": "Only these keys are processed (empty = all)",
    "exclude": "Keys to skip",
    "pattern": "Regular expression with named capture groups",
    "destination": "Output field for the match (null = merge into the record)",
    "reverse": "Sort in descending order",
    "input_format": "Input timestamp format (null = auto-detect)",
    "output_format": "Output timestamp format (iso or a strftime pattern)",
    "source_timezone": "IANA timezone for naive input timestamps (null = UTC)",
    "on_non_dict": "Behavior when the source field is not a dict (keep, drop, or raise)",
    "rules": "Per-field validation rules",
    "on_invalid": "Behavior for records failing validation (drop or raise)",
    "mapping": "Value lookup table (source value to target value)",
    "default": "Value used when no mapping entry matches",
    "window_seconds": "Tumbling window length in seconds (epoch-aligned UTC)",
    "allowed_lateness_seconds": "How far past a window's end a record may arrive before being dropped",
    "flush_on_close": "Emit open windows as partials when the stream stops",
    "width": "Counter width (auto, 32, or 64)",
    "output": "What to emit (delta, rate, or both)",
    "keep_raw": "Keep the raw cumulative counter value alongside the delta/rate",
    "first_sample": "How the first sight of a counter series is handled (pass or drop)",
    "reset_threshold": "Wrap-corrected delta above this fraction of the width is treated as a device reset",
    "max_gap_seconds": "Optional outage guard; a gap longer than this nulls the delta/rate (null = off)",
    "key_fields": "Fields that define the series identity",
    "select": "Per-selection rules (match or first_item plus output paths)",
    "keep": "Which duplicate to keep (first or last)",
    "on_error": "Behavior on a bad record (raise, null, or keep)",
    "timestamp_field": "Record field(s) carrying the event timestamp",
    "private_key_path": "Path to the SSH private key used for authentication",
    "connection_url": "Database connection URL (SQLAlchemy format)",
    "hosts": "List of cluster hosts",
    "params": "Additional parameters passed with the request or query",
    "db": "Redis database number",
    "key": "Redis key (list or stream name)",
    "args": "Positional arguments passed to the operation",
    "timeout_seconds": "Operation timeout in seconds",
    "dedupe_window_seconds": "Time-bucket width in seconds for the skip_processed key",
    "oids": "List of OIDs or symbolic names to poll",
    "yield_rows": "Emit one record per table row (use with walk)",
    "index_depth": "0 = auto; >0 = last N OID components form the row index",
    "classify": "Split fields into _metrics and _labels (INTEGER layering)",
    "metric_patterns": "INTEGER globs that force a field to be a metric",
    "label_patterns": "INTEGER globs that force a field to be a label",
    "brokers": "Kafka bootstrap server list",
    "topic": "Topic name (or list of topics) to consume from or publish to",
    "group_id": "Kafka consumer group ID",
    "auto_offset_reset": "Where to start when no committed offset exists (latest or earliest)",
    "enable_auto_commit": "Commit offsets once per poll batch after consumption (at-least-once); true opts into at-most-once",
    "max_poll_records": "Maximum records per poll batch",
    "session_timeout_ms": "Consumer session timeout in milliseconds",
    "security_protocol": "Kafka security protocol (PLAINTEXT, SASL_PLAINTEXT, SASL_SSL, or SSL)",
    "sasl_mechanism": "Kafka SASL mechanism (PLAIN, SCRAM-SHA-256, or SCRAM-SHA-512)",
    "sasl_username": "Kafka SASL username",
    "sasl_password": "Kafka SASL password (secret)",
}

# Type-specific descriptions where the field-name meaning differs per type.
_TYPE_FIELD_DESCRIPTIONS: dict[tuple[str, str, str], str] = {
    # ── sources ─────────────────────────────────────────────────────────────
    ("source", "amqp", "url"): "AMQP connection URL (includes credentials and vhost)",
    ("source", "clickhouse", "query"): "SQL SELECT query",
    ("source", "corba", "ior"): "Direct CORBA IOR string (mutually exclusive with naming_service)",
    ("source", "corba", "naming_service"): "corbaloc: URI of the naming service",
    ("source", "corba", "object_name"): "NamingService path to the object, e.g. PM/PMCollect",
    ("source", "corba", "operation"): "CORBA operation name to invoke",
    ("source", "elasticsearch", "index"): "Index to search (supports {pipeline}, {timestamp} tokens)",
    ("source", "elasticsearch", "query"): "Elasticsearch query body (e.g. {\"match_all\": {}})",
    ("source", "elasticsearch", "scroll"): "Scroll context duration for deep pagination (e.g. 1m)",
    ("source", "ftp", "remote_path"): "Remote directory to read from",
    ("source", "gnmi", "subscriptions"): "gNMI subscription paths to stream",
    ("source", "gnmi", "subscription_mode"): "gNMI subscription mode (STREAM, ONCE, or POLL)",
    ("source", "influxdb", "query"): "Flux query string",
    ("source", "local", "path"): "Directory to scan for files",
    ("source", "nats", "reconnect_time_wait"): "Seconds to wait between reconnect attempts",
    ("source", "prometheus_rw", "path"): "URL path segment → POST /webhooks/{path}",
    ("source", "redis", "mode"): "Read mode: list (LPOP) or stream (XREAD)",
    ("source", "redis", "count"): "Maximum entries read per batch",
    ("source", "redis", "block_ms"): "Blocking read timeout in milliseconds",
    ("source", "redis", "start_id"): "Stream entry ID to start reading from (e.g. $ for new entries)",
    ("source", "redis", "delete_after_read"): "Delete entries after they are read",
    ("source", "s3", "prefix"): "Only collect objects under this key prefix",
    ("source", "gcs", "prefix"): "Only collect objects under this key prefix",
    ("source", "azure_blob", "prefix"): "Only collect blobs under this name prefix",
    ("source", "sftp", "remote_path"): "Remote directory to read from",
    ("source", "snmp_poll", "version"): "SNMP version (1, 2c, or 3)",
    ("source", "snmp_poll", "operation"): "get = exact instance; walk = subtree traversal",
    ("source", "snmp_trap", "version"): "SNMP version (1, 2c, or 3)",
    ("source", "snmp_trap", "host"): "Bind address",
    ("source", "sql", "query"): "SQL SELECT query",
    ("source", "syslog", "host"): "Bind address",
    ("source", "syslog", "encoding"): "Message decoding charset",
    ("source", "webhook", "path"): "URL path segment → POST /webhooks/{path}",
    # ── sinks ───────────────────────────────────────────────────────────────
    ("sink", "amqp", "url"): "AMQP connection URL (includes credentials and vhost)",
    ("sink", "azure_blob", "blob_template"): "Blob name template; tokens: {pipeline}, {timestamp}, {part}",
    ("sink", "clickhouse", "batch_timeout_seconds"): "Max seconds a batch waits before being flushed",
    ("sink", "clickhouse", "batch_flush_on_stop"): "Flush buffered rows when the pipeline stops",
    ("sink", "elasticsearch", "index_template"): "Index name template (supports {pipeline}, {timestamp}, {YYYY}, {MM}, {DD} tokens)",
    ("sink", "ftp", "remote_path"): "Remote directory to write to",
    ("sink", "gcs", "blob_template"): "Blob name template; tokens: {pipeline}, {timestamp}, {part}",
    ("sink", "influxdb", "bucket"): "InfluxDB bucket written to",
    ("sink", "influxdb", "timestamp_field"): "Record field written as the InfluxDB timestamp",
    ("sink", "local", "path"): "Output directory",
    ("sink", "mqtt", "retain"): "Retain the published message on the broker",
    ("sink", "opensearch", "index"): "Index name (supports {pipeline}, {timestamp}, {YYYY}, {MM}, {DD} tokens)",
    ("sink", "opensearch", "use_ssl"): "Use TLS for the connection",
    ("sink", "redis", "mode"): "Write mode: list (RPUSH) or stream (XADD)",
    ("sink", "redis", "max_len"): "Stream max length (XADD MAXLEN); 0 = unlimited",
    ("sink", "s3", "key_template"): "Object key template; tokens: {pipeline}, {timestamp}, {part}",
    ("sink", "sftp", "remote_path"): "Remote directory to write to",
    ("sink", "snmp_trap", "version"): "SNMP version (1, 2c, or 3)",
    ("sink", "snmp_trap", "host"): "Target SNMP agent hostname or IP",
    ("sink", "sql", "mode"): "Write mode: insert or upsert",
    ("sink", "ves", "version"): "VES API version",
    # ── serializers ─────────────────────────────────────────────────────────
    ("serializer", "asn1", "message_class"): "Top-level ASN.1 type name to decode",
    ("serializer", "asn1", "message_classes"): "Ordered fallback list of top-level ASN.1 types to try per record",
    ("serializer", "asn1", "encoding"): "ASN.1 encoding (ber, der, per, uper, xer, or jer)",
    ("serializer", "asn1", "split_records"): "Split concatenated top-level TLVs and decode each separately (BER)",
    ("serializer", "asn1", "split_path"): "Dot-path to the record list inside a single decoded document",
    ("serializer", "asn1", "split_path_context"): "Sibling values copied into every split record (requires split_path)",
    ("serializer", "bytes", "encoding"): "Character encoding applied to the bytes",
    ("serializer", "pm_xml", "encoding"): "File encoding",
    ("serializer", "pm_xml", "add_managed_element"): "Include the managed_element field (localDn from <managedElement>)",
    ("serializer", "pm_xml", "add_duration"): "Include the duration field (granPeriod duration attribute)",
    ("serializer", "pm_xml", "numeric_values"): "Cast counter values to float where possible; keep as strings otherwise",
    ("serializer", "protobuf", "message_class"): "Top-level message name to decode",
    ("serializer", "text", "encoding"): "Character encoding applied to the text",
    ("serializer", "xml", "encoding"): "Character encoding (e.g. utf-8)",
    # ── transforms ──────────────────────────────────────────────────────────
    ("transform", "add_field", "fields"): "Computed fields: output name to simpleeval expression using record fields as variables",
    ("transform", "cast", "fields"): "Field-to-type mapping; target types: str, int, float, bool, datetime",
    ("transform", "coalesce_fields", "fields"): "Output field to candidate source paths; first non-empty wins",
    ("transform", "counter_delta", "fields"): "Dotted paths to the cumulative counter fields",
    ("transform", "deduplicate", "fields"): "Fields whose values define a duplicate row",
    ("transform", "drop", "fields"): "Fields to remove (list, or dict of field to matching values for conditional drop)",
    ("transform", "enrich", "prefix"): "Prefix prepended to the added field names",
    ("transform", "flatten", "prefix"): "String prepended to all flattened keys",
    ("transform", "hex_decode", "mode"): "Decode mode: hex, utf8_or_hex, or latin1_or_hex",
    ("transform", "inject_meta", "fields"): "Metadata key to output field name mapping",
    ("transform", "inject_meta", "prefix"): "Prefix applied to the injected field names",
    ("transform", "jmespath", "fields"): "Output field to JMESPath expression mapping",
    ("transform", "limit", "count"): "Records kept from the start of the batch",
    ("transform", "mask", "fields"): "Fields to redact, hash, or partially mask",
    ("transform", "mask", "mode"): "Mask mode: redact, hash, or partial",
    ("transform", "project", "fields"): "Output field to source path mapping (compact or expanded form)",
    ("transform", "regex_extract", "on_no_match"): "Behavior when the pattern does not match (keep, null, or drop)",
    ("transform", "rename", "fields"): "Old field name to new field name mapping",
    ("transform", "select_from_list", "on_no_match"): "Behavior when no item matches (null_fields or raise)",
    ("transform", "sort", "fields"): "Fields to sort by, in order",
    ("transform", "template", "fields"): "Output field to {placeholder} template mapping",
    ("transform", "timestamp_normalize", "fields"): "Fields whose timestamps are normalized",
    ("transform", "unnest", "prefix"): "Prefix prepended to the lifted field names",
}


def _apply_field_descriptions() -> None:
    """Merge a ``description`` key into every SCHEMA_FIELDS field descriptor
    (A.1). Type-specific wording wins; shared fields fall back to the
    field-name map. Raises on any uncovered field so gaps fail loudly at
    import instead of shipping empty help text."""
    missing: list[str] = []
    for category, types in SCHEMA_FIELDS.items():
        for type_name, fields in types.items():
            for field in fields:
                key = (category, type_name, field["name"])
                description = (
                    _TYPE_FIELD_DESCRIPTIONS.get(key)
                    or _COMMON_FIELD_DESCRIPTIONS.get(field["name"])
                )
                if not description:
                    missing.append(f"{category}/{type_name}/{field['name']}")
                    continue
                field["description"] = description
    if missing:
        raise ValueError(
            "SCHEMA_FIELDS is missing A.1 descriptions for: "
            + ", ".join(sorted(missing))
        )


SCHEMA_LINES: dict[str, dict[str, list[str]]] = {
    "source": {},
    "sink": {},
    "serializer": {},
    "transform": {},
}
SCHEMA_FIELDS: dict[str, dict[str, list[dict]]] = {
    "source": {},
    "sink": {},
    "serializer": {},
    "transform": {},
}


def _iter_union_models(union_type) -> list:
    """Unwrap Annotated[A | B | C, ...] into a model list."""
    import types as _types

    args = get_args(union_type)
    if not args:
        return []
    inner = args[0]
    if isinstance(inner, _types.UnionType) or get_origin(inner) is typing.Union:
        return list(get_args(inner))
    return [inner]


def _build_schema_cache() -> None:
    try:
        from tram.models.pipeline import (  # noqa: PLC0415
            SerializerConfig,
            SinkConfig,
            SourceConfig,
            TransformConfig,
        )
    except ImportError:
        return

    for category, union_type in (
        ("source", SourceConfig),
        ("sink", SinkConfig),
        ("serializer", SerializerConfig),
        ("transform", TransformConfig),
    ):
        for model_cls in _iter_union_models(union_type):
            try:
                ann = model_cls.model_fields["type"].annotation
                type_args = get_args(ann)
                type_name = type_args[0] if type_args else None
                if not type_name:
                    default = model_cls.model_fields["type"].default
                    type_name = default if isinstance(default, str) else None
                if type_name:
                    SCHEMA_LINES[category][type_name] = _model_to_schema_lines(model_cls)
                    SCHEMA_FIELDS[category][type_name] = _model_to_field_descriptors(model_cls)
            except Exception:
                pass


def build_config_schema_payload() -> dict:
    """Return backend-generated config schema metadata for UI-driven forms."""

    def wrap(category: str) -> dict[str, dict]:
        return {
            type_name: {"fields": fields}
            for type_name, fields in SCHEMA_FIELDS[category].items()
        }

    return {
        "sources": wrap("source"),
        "sinks": wrap("sink"),
        "serializers": wrap("serializer"),
        "transforms": wrap("transform"),
    }


# Content-hash identity for the derived schema (Issue #24 / Option A). The
# hash is cached: SCHEMA_FIELDS is built once at import time and never
# mutated afterwards, so the computed value is stable for the process.
_schema_version_cache: str | None = None


def schema_version() -> str:
    """Return the schema identity token: the first 12 hex chars of the sha256
    over the canonical JSON of ``SCHEMA_FIELDS``
    (``json.dumps(..., sort_keys=True)``).

    This is an identity token for equality checks ("is this the schema the
    daemon is serving now?"), NOT a semantic version — a Pydantic version bump
    can change how types/defaults are rendered and rotate the hash with no
    semantic change (risk noted in docs/ideas/schema-registry-feasibility.md
    §5). Computed once and cached.
    """
    global _schema_version_cache
    if _schema_version_cache is None:
        import hashlib
        import json

        canonical = json.dumps(SCHEMA_FIELDS, sort_keys=True)
        _schema_version_cache = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return _schema_version_cache


_build_schema_cache()
_apply_field_descriptions()
