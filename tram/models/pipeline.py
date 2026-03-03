"""Pydantic v2 models for pipeline configuration."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Schedule ───────────────────────────────────────────────────────────────


class ScheduleConfig(BaseModel):
    type: Literal["interval", "cron", "stream", "manual"] = "manual"
    interval_seconds: Optional[int] = None
    cron: Optional[str] = None

    @model_validator(mode="after")
    def check_schedule_params(self) -> "ScheduleConfig":
        if self.type == "interval" and self.interval_seconds is None:
            raise ValueError("interval_seconds required when type=interval")
        if self.type == "cron" and self.cron is None:
            raise ValueError("cron expression required when type=cron")
        return self


# ── Sources ────────────────────────────────────────────────────────────────


class SFTPSourceConfig(BaseModel):
    type: Literal["sftp"]
    host: str
    port: int = 22
    username: str
    password: Optional[str] = None
    private_key_path: Optional[str] = None
    remote_path: str
    file_pattern: str = "*"
    move_after_read: Optional[str] = None
    delete_after_read: bool = False

    @model_validator(mode="after")
    def check_auth(self) -> "SFTPSourceConfig":
        if self.password is None and self.private_key_path is None:
            raise ValueError("Either password or private_key_path must be provided")
        return self


class LocalSourceConfig(BaseModel):
    type: Literal["local"]
    path: str
    file_pattern: str = "*"
    move_after_read: Optional[str] = None
    delete_after_read: bool = False
    recursive: bool = False


class RestSourceConfig(BaseModel):
    type: Literal["rest"]
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    body: Optional[Any] = None
    auth_type: Literal["none", "basic", "bearer"] = "none"
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    timeout: int = 30
    response_path: Optional[str] = None
    paginate: bool = False
    page_param: str = "offset"
    page_size: int = 100
    total_path: Optional[str] = None
    verify_ssl: bool = True


class KafkaSourceConfig(BaseModel):
    type: Literal["kafka"]
    brokers: list[str]
    topic: Union[str, list[str]]
    group_id: str = "tram"
    auto_offset_reset: Literal["latest", "earliest"] = "latest"
    enable_auto_commit: bool = True
    max_poll_records: int = 500
    session_timeout_ms: int = 30000
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: Optional[str] = None
    sasl_username: Optional[str] = None
    sasl_password: Optional[str] = None
    ssl_cafile: Optional[str] = None


class FtpSourceConfig(BaseModel):
    type: Literal["ftp"]
    host: str
    port: int = 21
    username: str
    password: str
    remote_path: str = "/"
    file_pattern: str = "*"
    move_after_read: Optional[str] = None
    delete_after_read: bool = False
    passive: bool = True


class S3SourceConfig(BaseModel):
    type: Literal["s3"]
    bucket: str
    prefix: str = ""
    file_pattern: str = "*"
    endpoint_url: Optional[str] = None
    region_name: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    move_after_read: Optional[str] = None
    delete_after_read: bool = False


class SyslogSourceConfig(BaseModel):
    type: Literal["syslog"]
    host: str = "0.0.0.0"
    port: int = 514
    protocol: str = "udp"
    buffer_size: int = 65535
    encoding: str = "utf-8"


class SnmpTrapSourceConfig(BaseModel):
    type: Literal["snmp_trap"]
    host: str = "0.0.0.0"
    port: int = 162
    community: str = "public"
    version: str = "2c"


class SnmpPollSourceConfig(BaseModel):
    type: Literal["snmp_poll"]
    host: str
    port: int = 161
    community: str = "public"
    version: str = "2c"
    oids: list[str]
    operation: str = "get"


class MqttSourceConfig(BaseModel):
    type: Literal["mqtt"]
    host: str
    port: int = 1883
    topic: str
    qos: int = 0
    client_id: str = ""
    username: Optional[str] = None
    password: Optional[str] = None
    tls: bool = False
    keepalive: int = 60


class AmqpSourceConfig(BaseModel):
    type: Literal["amqp"]
    url: str = "amqp://guest:guest@localhost:5672/"
    queue: str
    prefetch_count: int = 10
    auto_ack: bool = False


class NatsSourceConfig(BaseModel):
    type: Literal["nats"]
    servers: list[str] = Field(default_factory=lambda: ["nats://localhost:4222"])
    subject: str
    queue_group: str = ""
    credentials_file: Optional[str] = None


class GnmiSourceConfig(BaseModel):
    type: Literal["gnmi"]
    host: str
    port: int = 57400
    username: str = ""
    password: str = ""
    tls: bool = True
    tls_ca: Optional[str] = None
    subscriptions: list[dict[str, Any]] = Field(default_factory=list)


class SqlSourceConfig(BaseModel):
    type: Literal["sql"]
    connection_url: str
    query: str
    params: dict[str, Any] = Field(default_factory=dict)
    chunk_size: int = 0


class InfluxDbSourceConfig(BaseModel):
    type: Literal["influxdb"]
    url: str
    token: str
    org: str
    query: str
    timeout: int = 30


class RedisSourceConfig(BaseModel):
    type: Literal["redis"]
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    mode: Literal["list", "stream"] = "list"
    key: str
    count: int = 100
    block_ms: int = 1000
    start_id: str = "$"
    delete_after_read: bool = False


class GcsSourceConfig(BaseModel):
    type: Literal["gcs"]
    bucket: str
    prefix: str = ""
    file_pattern: str = "*"
    service_account_json: Optional[str] = None
    move_after_read: Optional[str] = None
    delete_after_read: bool = False


class AzureBlobSourceConfig(BaseModel):
    type: Literal["azure_blob"]
    connection_string: Optional[str] = None
    account_name: Optional[str] = None
    account_key: Optional[str] = None
    container: str
    prefix: str = ""
    file_pattern: str = "*"
    move_after_read: Optional[str] = None
    delete_after_read: bool = False


# v0.5.0 new sources


class WebhookSourceConfig(BaseModel):
    type: Literal["webhook"]
    path: str
    secret: Optional[str] = None
    max_queue_size: int = 1000


class WebSocketSourceConfig(BaseModel):
    type: Literal["websocket"]
    url: str
    extra_headers: dict[str, str] = Field(default_factory=dict)
    ping_interval: int = 20
    reconnect: bool = True
    reconnect_delay: int = 5


class ElasticsearchSourceConfig(BaseModel):
    type: Literal["elasticsearch"]
    hosts: list[str]
    index: str
    query: dict[str, Any] = Field(default_factory=lambda: {"match_all": {}})
    scroll: str = "2m"
    batch_size: int = 500
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    ca_certs: Optional[str] = None
    verify_certs: bool = True


class PrometheusRWSourceConfig(BaseModel):
    type: Literal["prometheus_rw"]
    path: str = "prom-rw"
    secret: Optional[str] = None


SourceConfig = Annotated[
    Union[
        SFTPSourceConfig,
        LocalSourceConfig,
        RestSourceConfig,
        KafkaSourceConfig,
        FtpSourceConfig,
        S3SourceConfig,
        SyslogSourceConfig,
        SnmpTrapSourceConfig,
        SnmpPollSourceConfig,
        MqttSourceConfig,
        AmqpSourceConfig,
        NatsSourceConfig,
        GnmiSourceConfig,
        SqlSourceConfig,
        InfluxDbSourceConfig,
        RedisSourceConfig,
        GcsSourceConfig,
        AzureBlobSourceConfig,
        WebhookSourceConfig,
        WebSocketSourceConfig,
        ElasticsearchSourceConfig,
        PrometheusRWSourceConfig,
    ],
    Field(discriminator="type"),
]


# ── Sinks ──────────────────────────────────────────────────────────────────


class SFTPSinkConfig(BaseModel):
    type: Literal["sftp"]
    host: str
    port: int = 22
    username: str
    password: Optional[str] = None
    private_key_path: Optional[str] = None
    remote_path: str
    filename_template: str = "{pipeline}_{timestamp}.bin"
    condition: Optional[str] = None

    @model_validator(mode="after")
    def check_auth(self) -> "SFTPSinkConfig":
        if self.password is None and self.private_key_path is None:
            raise ValueError("Either password or private_key_path must be provided")
        return self


class LocalSinkConfig(BaseModel):
    type: Literal["local"]
    path: str
    filename_template: str = "{pipeline}_{timestamp}.bin"
    overwrite: bool = True
    condition: Optional[str] = None


class RestSinkConfig(BaseModel):
    type: Literal["rest"]
    url: str
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    content_type: str = "application/json"
    auth_type: Literal["none", "basic", "bearer"] = "none"
    username: Optional[str] = None
    password: Optional[str] = None
    token: Optional[str] = None
    timeout: int = 30
    verify_ssl: bool = True
    expected_status: list[int] = Field(default_factory=lambda: [200, 201, 202, 204])
    condition: Optional[str] = None


class KafkaSinkConfig(BaseModel):
    type: Literal["kafka"]
    brokers: list[str]
    topic: str
    key_field: Optional[str] = None
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: Optional[str] = None
    sasl_username: Optional[str] = None
    sasl_password: Optional[str] = None
    ssl_cafile: Optional[str] = None
    acks: Union[str, int] = "all"
    compression_type: Optional[str] = None
    condition: Optional[str] = None


class OpenSearchSinkConfig(BaseModel):
    type: Literal["opensearch"]
    hosts: list[str]
    index: str
    id_field: Optional[str] = None
    pipeline: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    verify_ssl: bool = True
    use_ssl: bool = False
    timeout: int = 30
    chunk_size: int = 500
    refresh: str = "false"
    condition: Optional[str] = None


class FtpSinkConfig(BaseModel):
    type: Literal["ftp"]
    host: str
    port: int = 21
    username: str
    password: str
    remote_path: str = "/"
    filename_template: str = "{pipeline}_{timestamp}.bin"
    passive: bool = True
    condition: Optional[str] = None


class VesSinkConfig(BaseModel):
    type: Literal["ves"]
    url: str
    domain: str = "other"
    source_name: str = "tram"
    reporting_entity_name: str = "tram"
    priority: str = "Normal"
    version: str = "4.1"
    auth_type: str = "none"
    username: str = ""
    password: str = ""
    token: str = ""
    expected_status: list[int] = Field(default_factory=lambda: [202])
    condition: Optional[str] = None


class S3SinkConfig(BaseModel):
    type: Literal["s3"]
    bucket: str
    key_template: str = "{pipeline}_{timestamp}.bin"
    endpoint_url: Optional[str] = None
    region_name: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    content_type: str = "application/json"
    condition: Optional[str] = None


class SnmpTrapSinkConfig(BaseModel):
    type: Literal["snmp_trap"]
    host: str
    port: int = 162
    community: str = "public"
    version: str = "2c"
    enterprise_oid: str = "1.3.6.1.4.1.0"
    condition: Optional[str] = None


class MqttSinkConfig(BaseModel):
    type: Literal["mqtt"]
    host: str
    port: int = 1883
    topic: str
    qos: int = 0
    retain: bool = False
    username: Optional[str] = None
    password: Optional[str] = None
    tls: bool = False
    client_id: str = ""
    condition: Optional[str] = None


class AmqpSinkConfig(BaseModel):
    type: Literal["amqp"]
    url: str = "amqp://guest:guest@localhost:5672/"
    exchange: str = ""
    routing_key: str = ""
    content_type: str = "application/json"
    condition: Optional[str] = None


class NatsSinkConfig(BaseModel):
    type: Literal["nats"]
    servers: list[str] = Field(default_factory=lambda: ["nats://localhost:4222"])
    subject: str
    credentials_file: Optional[str] = None
    condition: Optional[str] = None


class SqlSinkConfig(BaseModel):
    type: Literal["sql"]
    connection_url: str
    table: str
    mode: Literal["insert", "upsert"] = "insert"
    upsert_keys: list[str] = Field(default_factory=list)
    condition: Optional[str] = None


class InfluxDbSinkConfig(BaseModel):
    type: Literal["influxdb"]
    url: str
    token: str
    org: str
    bucket: str
    measurement: str
    tag_fields: list[str] = Field(default_factory=list)
    timestamp_field: Optional[str] = None
    precision: Literal["ns", "us", "ms", "s"] = "ns"
    condition: Optional[str] = None


class RedisSinkConfig(BaseModel):
    type: Literal["redis"]
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    mode: Literal["list", "pubsub", "stream"] = "list"
    key: str
    max_len: Optional[int] = None
    condition: Optional[str] = None


class GcsSinkConfig(BaseModel):
    type: Literal["gcs"]
    bucket: str
    blob_template: str = "{pipeline}_{timestamp}.bin"
    service_account_json: Optional[str] = None
    content_type: str = "application/json"
    condition: Optional[str] = None


class AzureBlobSinkConfig(BaseModel):
    type: Literal["azure_blob"]
    connection_string: Optional[str] = None
    account_name: Optional[str] = None
    account_key: Optional[str] = None
    container: str
    blob_template: str = "{pipeline}_{timestamp}.bin"
    content_type: str = "application/json"
    overwrite: bool = True
    condition: Optional[str] = None


# v0.5.0 new sinks


class WebSocketSinkConfig(BaseModel):
    type: Literal["websocket"]
    url: str
    extra_headers: dict[str, str] = Field(default_factory=dict)
    condition: Optional[str] = None


class ElasticsearchSinkConfig(BaseModel):
    type: Literal["elasticsearch"]
    hosts: list[str]
    index_template: str
    id_field: Optional[str] = None
    chunk_size: int = 500
    refresh: str = "false"
    username: Optional[str] = None
    password: Optional[str] = None
    api_key: Optional[str] = None
    ca_certs: Optional[str] = None
    pipeline: Optional[str] = None
    condition: Optional[str] = None


SinkConfig = Annotated[
    Union[
        SFTPSinkConfig,
        LocalSinkConfig,
        RestSinkConfig,
        KafkaSinkConfig,
        OpenSearchSinkConfig,
        FtpSinkConfig,
        VesSinkConfig,
        S3SinkConfig,
        SnmpTrapSinkConfig,
        MqttSinkConfig,
        AmqpSinkConfig,
        NatsSinkConfig,
        SqlSinkConfig,
        InfluxDbSinkConfig,
        RedisSinkConfig,
        GcsSinkConfig,
        AzureBlobSinkConfig,
        WebSocketSinkConfig,
        ElasticsearchSinkConfig,
    ],
    Field(discriminator="type"),
]


# ── Serializers ────────────────────────────────────────────────────────────


class JsonSerializerConfig(BaseModel):
    type: Literal["json"]
    indent: Optional[int] = None
    ensure_ascii: bool = True


class CsvSerializerConfig(BaseModel):
    type: Literal["csv"]
    delimiter: str = ","
    has_header: bool = True
    quotechar: str = '"'


class XmlSerializerConfig(BaseModel):
    type: Literal["xml"]
    root_element: str = "records"
    record_element: str = "record"
    encoding: str = "utf-8"


class AvroSerializerConfig(BaseModel):
    type: Literal["avro"]
    avro_schema: Optional[str] = Field(default=None, alias="schema")
    schema_file: Optional[str] = None
    schema_registry_url: Optional[str] = None
    schema_registry_subject: Optional[str] = None
    schema_registry_id: Optional[int] = None
    use_magic_bytes: bool = True

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def check_schema(self) -> "AvroSerializerConfig":
        has_inline = bool(self.avro_schema or self.schema_file)
        has_registry = bool(self.schema_registry_url)
        if not has_inline and not has_registry:
            raise ValueError(
                "Avro serializer requires either 'schema'/'schema_file' or 'schema_registry_url'"
            )
        return self


class ProtobufSerializerConfig(BaseModel):
    type: Literal["protobuf"]
    schema_file: str
    message_class: str
    schema_registry_url: Optional[str] = None
    schema_registry_subject: Optional[str] = None
    schema_registry_id: Optional[int] = None
    use_magic_bytes: bool = True


class ParquetSerializerConfig(BaseModel):
    type: Literal["parquet"]
    compression: Literal["snappy", "gzip", "brotli", "none"] = "snappy"


class MsgpackSerializerConfig(BaseModel):
    type: Literal["msgpack"]


SerializerConfig = Annotated[
    Union[
        JsonSerializerConfig,
        CsvSerializerConfig,
        XmlSerializerConfig,
        AvroSerializerConfig,
        ProtobufSerializerConfig,
        ParquetSerializerConfig,
        MsgpackSerializerConfig,
    ],
    Field(discriminator="type"),
]


# ── Transforms ─────────────────────────────────────────────────────────────


class RenameTransformConfig(BaseModel):
    type: Literal["rename"]
    fields: dict[str, str]


class CastTransformConfig(BaseModel):
    type: Literal["cast"]
    fields: dict[str, Literal["str", "int", "float", "bool", "datetime"]]


class AddFieldTransformConfig(BaseModel):
    type: Literal["add_field"]
    fields: dict[str, str]


class DropTransformConfig(BaseModel):
    type: Literal["drop"]
    fields: list[str]


class ValueMapTransformConfig(BaseModel):
    type: Literal["value_map"]
    field: str
    mapping: dict[str, Any]
    default: Optional[Any] = None


class FilterTransformConfig(BaseModel):
    type: Literal["filter"]
    condition: str


class FlattenTransformConfig(BaseModel):
    type: Literal["flatten"]
    separator: str = "_"
    max_depth: int = 0
    prefix: str = ""


class TimestampNormalizeTransformConfig(BaseModel):
    type: Literal["timestamp_normalize"]
    fields: list[str]
    input_format: Optional[str] = None
    output_format: str = "iso"
    on_error: Literal["raise", "null", "keep"] = "raise"


class AggregateTransformConfig(BaseModel):
    type: Literal["aggregate"]
    group_by: list[str] = Field(default_factory=list)
    operations: dict[str, Any]


class EnrichTransformConfig(BaseModel):
    type: Literal["enrich"]
    lookup_file: str
    lookup_format: Literal["csv", "json"] = "csv"
    join_key: str
    lookup_key: Optional[str] = None
    add_fields: Optional[list[str]] = None
    prefix: str = ""
    on_miss: Literal["keep", "null_fields"] = "keep"


class ExplodeTransformConfig(BaseModel):
    type: Literal["explode"]
    field: str
    include_index: bool = False
    index_field: str = "index"
    drop_source: bool = True


class DeduplicateTransformConfig(BaseModel):
    type: Literal["deduplicate"]
    fields: list[str]
    keep: Literal["first", "last"] = "first"


class RegexExtractTransformConfig(BaseModel):
    type: Literal["regex_extract"]
    field: str
    pattern: str
    destination: Optional[str] = None
    on_no_match: Literal["keep", "null", "drop"] = "keep"


class TemplateTransformConfig(BaseModel):
    type: Literal["template"]
    fields: dict[str, str]


class MaskTransformConfig(BaseModel):
    type: Literal["mask"]
    fields: list[str]
    mode: Literal["redact", "hash", "partial"] = "redact"
    placeholder: str = "***"
    visible_start: int = 2
    visible_end: int = 2


class ValidateTransformConfig(BaseModel):
    type: Literal["validate"]
    rules: dict[str, Any]
    on_invalid: Literal["drop", "raise"] = "drop"


class SortTransformConfig(BaseModel):
    type: Literal["sort"]
    fields: list[str]
    reverse: bool = False


class LimitTransformConfig(BaseModel):
    type: Literal["limit"]
    count: int


class JmesPathExtractTransformConfig(BaseModel):
    type: Literal["jmespath"]
    fields: dict[str, str]


class UnnestTransformConfig(BaseModel):
    type: Literal["unnest"]
    field: str
    prefix: str = ""
    drop_source: bool = True
    on_non_dict: Literal["keep", "drop", "raise"] = "keep"


TransformConfig = Annotated[
    Union[
        RenameTransformConfig,
        CastTransformConfig,
        AddFieldTransformConfig,
        DropTransformConfig,
        ValueMapTransformConfig,
        FilterTransformConfig,
        FlattenTransformConfig,
        TimestampNormalizeTransformConfig,
        AggregateTransformConfig,
        EnrichTransformConfig,
        ExplodeTransformConfig,
        DeduplicateTransformConfig,
        RegexExtractTransformConfig,
        TemplateTransformConfig,
        MaskTransformConfig,
        ValidateTransformConfig,
        SortTransformConfig,
        LimitTransformConfig,
        JmesPathExtractTransformConfig,
        UnnestTransformConfig,
    ],
    Field(discriminator="type"),
]


# ── Pipeline (top-level) ───────────────────────────────────────────────────


class PipelineConfig(BaseModel):
    version: str = "1"

    # Pipeline metadata
    name: str
    description: str = ""
    enabled: bool = True

    # Execution
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    source: SourceConfig
    serializer_in: SerializerConfig
    transforms: list[TransformConfig] = Field(default_factory=list)
    serializer_out: SerializerConfig
    sinks: list[SinkConfig] = Field(default_factory=list, min_length=0)

    # Backward compat: singular `sink` auto-wrapped into `sinks`
    sink: Optional[SinkConfig] = Field(default=None, exclude=True)

    # Rate limiting
    rate_limit_rps: Optional[float] = None

    # Error handling
    on_error: Literal["continue", "abort", "retry"] = "continue"
    retry_count: int = 3
    retry_delay_seconds: int = 10

    @field_validator("name")
    @classmethod
    def name_slug(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError(
                f"Pipeline name '{v}' must contain only alphanumeric characters, hyphens, or underscores"
            )
        return v

    @model_validator(mode="after")
    def normalise_sinks(self) -> "PipelineConfig":
        """Accept legacy `sink: ...` and wrap it in `sinks` list."""
        if self.sink is not None and not self.sinks:
            self.sinks = [self.sink]
        if not self.sinks:
            raise ValueError("At least one sink must be configured (use 'sinks:' list or 'sink:')")
        return self


class PipelineFile(BaseModel):
    """Top-level YAML wrapper — the 'pipeline:' key."""

    version: str = "1"
    pipeline: PipelineConfig
