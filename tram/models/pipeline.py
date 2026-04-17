"""Pydantic v2 models for pipeline configuration."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

# ── Schedule ───────────────────────────────────────────────────────────────


class ScheduleConfig(BaseModel):
    type: Literal["interval", "cron", "stream", "manual"] = "manual"
    interval_seconds: int | None = None
    cron: str | None = None

    @model_validator(mode="after")
    def check_schedule_params(self) -> ScheduleConfig:
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
    password: str | None = None
    private_key_path: str | None = None
    remote_path: str
    file_pattern: str = "*"
    move_after_read: str | None = None
    delete_after_read: bool = False
    skip_processed: bool = False   # track processed files in DB; skip on re-run
    read_chunk_bytes: int = 0      # 0 = read all at once; >0 = stream in chunks

    @model_validator(mode="after")
    def check_auth(self) -> SFTPSourceConfig:
        if self.password is None and self.private_key_path is None:
            raise ValueError("Either password or private_key_path must be provided")
        return self


class LocalSourceConfig(BaseModel):
    type: Literal["local"]
    path: str
    file_pattern: str = "*"
    move_after_read: str | None = None
    delete_after_read: bool = False
    recursive: bool = False
    skip_processed: bool = False


class RestSourceConfig(BaseModel):
    type: Literal["rest"]
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    body: Any | None = None
    auth_type: Literal["none", "basic", "bearer", "apikey"] = "none"
    username: str | None = None
    password: str | None = None
    token: str | None = None
    api_key: str | None = None
    api_key_header: str = "X-API-Key"
    timeout: int = 30
    response_path: str | None = None
    paginate: bool = False
    page_param: str = "offset"
    page_size: int = 100
    total_path: str | None = None
    verify_ssl: bool = True


class KafkaSourceConfig(BaseModel):
    type: Literal["kafka"]
    brokers: list[str]
    topic: str | list[str]
    group_id: str | None = None   # None → use pipeline name at runtime
    auto_offset_reset: Literal["latest", "earliest"] = "latest"
    enable_auto_commit: bool = True
    max_poll_records: int = 500
    session_timeout_ms: int = 30000
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str | None = None
    sasl_username: str | None = None
    sasl_password: str | None = None
    ssl_cafile: str | None = None
    reconnect_delay_seconds: float = 5.0
    max_reconnect_attempts: int = 0   # 0 = infinite


class FtpSourceConfig(BaseModel):
    type: Literal["ftp"]
    host: str
    port: int = 21
    username: str
    password: str
    remote_path: str = "/"
    file_pattern: str = "*"
    move_after_read: str | None = None
    delete_after_read: bool = False
    passive: bool = True
    skip_processed: bool = False


class S3SourceConfig(BaseModel):
    type: Literal["s3"]
    bucket: str
    prefix: str = ""
    file_pattern: str = "*"
    endpoint_url: str | None = None
    region_name: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    move_after_read: str | None = None
    delete_after_read: bool = False
    skip_processed: bool = False
    read_chunk_bytes: int = 0      # 0 = read all at once; >0 = stream in chunks


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
    mib_dirs: list[str] = Field(default_factory=list)
    mib_modules: list[str] = Field(default_factory=list)
    resolve_oids: bool = True
    # SNMPv3 USM (used when version="3"; trap decoding is best-effort for v3)
    security_name: str = ""
    auth_protocol: str = "SHA"
    auth_key: str | None = None
    priv_protocol: str = "AES128"
    priv_key: str | None = None
    context_name: str = ""


class SnmpPollSourceConfig(BaseModel):
    type: Literal["snmp_poll"]
    host: str
    port: int = 161
    community: str = "public"
    version: str = "2c"
    oids: list[str]
    operation: str = "get"
    mib_dirs: list[str] = Field(default_factory=list)
    mib_modules: list[str] = Field(default_factory=list)
    resolve_oids: bool = True
    yield_rows: bool = False   # True → yield one record per table row (WALK only)
    index_depth: int = 0       # 0=split on first dot (auto, for resolved names);
                               # >0=last N OID components form the row index
    classify: bool = False     # True → replace flat fields with _metrics/_labels dicts
    # SNMPv3 USM (used when version="3")
    security_name: str = ""
    auth_protocol: str = "SHA"        # MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512
    auth_key: str | None = None    # auth passphrase; None → noAuthNoPriv
    priv_protocol: str = "AES128"     # DES | 3DES | AES | AES128 | AES192 | AES256
    priv_key: str | None = None    # priv passphrase; None → authNoPriv
    context_name: str = ""            # SNMPv3 contextName (usually empty)


class MqttSourceConfig(BaseModel):
    type: Literal["mqtt"]
    host: str
    port: int = 1883
    topic: str
    qos: int = 0
    client_id: str = ""
    username: str | None = None
    password: str | None = None
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
    queue_group: str | None = None   # None → use pipeline name at runtime; "" → broadcast
    credentials_file: str | None = None
    max_reconnect_attempts: int = -1   # -1 = infinite
    reconnect_time_wait: float = 2.0


class GnmiSourceConfig(BaseModel):
    type: Literal["gnmi"]
    host: str
    port: int = 57400
    username: str = ""
    password: str = ""
    tls: bool = True
    tls_ca: str | None = None
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
    password: str | None = None
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
    service_account_json: str | None = None
    move_after_read: str | None = None
    delete_after_read: bool = False
    skip_processed: bool = False


class AzureBlobSourceConfig(BaseModel):
    type: Literal["azure_blob"]
    connection_string: str | None = None
    account_name: str | None = None
    account_key: str | None = None
    container: str
    prefix: str = ""
    file_pattern: str = "*"
    move_after_read: str | None = None
    delete_after_read: bool = False
    skip_processed: bool = False


# v0.5.0 new sources


class WebhookSourceConfig(BaseModel):
    type: Literal["webhook"]
    path: str
    secret: str | None = None
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
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    ca_certs: str | None = None
    verify_certs: bool = True


class PrometheusRWSourceConfig(BaseModel):
    type: Literal["prometheus_rw"]
    path: str = "prom-rw"
    secret: str | None = None


class ClickHouseSourceConfig(BaseModel):
    type: Literal["clickhouse"]
    host: str = "localhost"
    port: int = 9000
    database: str = "default"
    username: str = "default"
    password: str = ""
    query: str
    params: dict[str, Any] = Field(default_factory=dict)
    chunk_size: int = 0
    secure: bool = False
    verify: bool = True
    connect_timeout: int = 10
    send_receive_timeout: int = 300


class CorbaSourceConfig(BaseModel):
    """CORBA source — calls a remote CORBA operation via DII (no compiled stubs needed).

    Requires omniORBpy: ``pip install tram[corba]``

    Config keys:
        ior              (str, optional)  Direct IOR string (mutually exclusive with naming_service)
        naming_service   (str, optional)  corbaloc URI e.g. "corbaloc:iiop:host:2809/NameService"
        object_name      (str, optional)  Path in NamingService e.g. "PM/PMCollect" (used with naming_service)
        operation        (str, required)  CORBA operation name to invoke
        args             (list, default [])  Positional arguments (simple Python scalars)
        timeout_seconds  (int, default 30)  ORB request timeout
        skip_processed   (bool, default False)  Skip invocations already recorded in DB
    """
    type: Literal["corba"]
    ior: str | None = None
    naming_service: str | None = None
    object_name: str | None = None
    operation: str
    args: list = Field(default_factory=list)
    timeout_seconds: int = 30
    skip_processed: bool = False

    @model_validator(mode="after")
    def check_endpoint(self) -> CorbaSourceConfig:
        if not self.ior and not self.naming_service:
            raise ValueError("Either 'ior' or 'naming_service' must be provided")
        return self


SourceConfig = Annotated[
    SFTPSourceConfig | LocalSourceConfig | RestSourceConfig | KafkaSourceConfig | FtpSourceConfig | S3SourceConfig | SyslogSourceConfig | SnmpTrapSourceConfig | SnmpPollSourceConfig | MqttSourceConfig | AmqpSourceConfig | NatsSourceConfig | GnmiSourceConfig | SqlSourceConfig | ClickHouseSourceConfig | InfluxDbSourceConfig | RedisSourceConfig | GcsSourceConfig | AzureBlobSourceConfig | WebhookSourceConfig | WebSocketSourceConfig | ElasticsearchSourceConfig | PrometheusRWSourceConfig | CorbaSourceConfig,
    Field(discriminator="type"),
]


# ── Transforms ─────────────────────────────────────────────────────────────
# (defined before Sinks so sink classes can reference TransformConfig)


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
    default: Any | None = None


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
    input_format: str | None = None
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
    lookup_key: str | None = None
    add_fields: list[str] | None = None
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
    destination: str | None = None
    on_no_match: Literal["keep", "null", "drop"] = "keep"


class InjectMetaTransformConfig(BaseModel):
    type: Literal["inject_meta"]
    fields: dict[str, str] = Field(default_factory=dict)
    include_all: bool = False
    prefix: str = ""
    on_missing: Literal["skip", "null"] = "skip"


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
    RenameTransformConfig | CastTransformConfig | AddFieldTransformConfig | DropTransformConfig | ValueMapTransformConfig | FilterTransformConfig | FlattenTransformConfig | TimestampNormalizeTransformConfig | AggregateTransformConfig | EnrichTransformConfig | ExplodeTransformConfig | DeduplicateTransformConfig | RegexExtractTransformConfig | InjectMetaTransformConfig | TemplateTransformConfig | MaskTransformConfig | ValidateTransformConfig | SortTransformConfig | LimitTransformConfig | JmesPathExtractTransformConfig | UnnestTransformConfig,
    Field(discriminator="type"),
]


# ── Sinks ──────────────────────────────────────────────────────────────────


class SFTPSinkConfig(BaseModel):
    type: Literal["sftp"]
    host: str
    port: int = 22
    username: str
    password: str | None = None
    private_key_path: str | None = None
    remote_path: str
    filename_template: str = "{pipeline}_{timestamp}.bin"
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global

    @model_validator(mode="after")
    def check_auth(self) -> SFTPSinkConfig:
        if self.password is None and self.private_key_path is None:
            raise ValueError("Either password or private_key_path must be provided")
        return self


class LocalSinkConfig(BaseModel):
    type: Literal["local"]
    path: str
    filename_template: str = "{pipeline}_{timestamp}.bin"
    overwrite: bool = True
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class RestSinkConfig(BaseModel):
    type: Literal["rest"]
    url: str
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    content_type: str = "application/json"
    auth_type: Literal["none", "basic", "bearer", "apikey"] = "none"
    username: str | None = None
    password: str | None = None
    token: str | None = None
    api_key: str | None = None
    api_key_header: str = "X-API-Key"
    timeout: int = 30
    verify_ssl: bool = True
    expected_status: list[int] = Field(default_factory=lambda: [200, 201, 202, 204])
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class KafkaSinkConfig(BaseModel):
    type: Literal["kafka"]
    brokers: list[str]
    topic: str
    key_field: str | None = None
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str | None = None
    sasl_username: str | None = None
    sasl_password: str | None = None
    ssl_cafile: str | None = None
    acks: str | int = "all"
    compression_type: str | None = None
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class OpenSearchSinkConfig(BaseModel):
    type: Literal["opensearch"]
    hosts: list[str]
    index: str
    id_field: str | None = None
    pipeline: str | None = None
    username: str | None = None
    password: str | None = None
    verify_ssl: bool = True
    use_ssl: bool = False
    timeout: int = 30
    chunk_size: int = 500
    refresh: str = "false"
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class FtpSinkConfig(BaseModel):
    type: Literal["ftp"]
    host: str
    port: int = 21
    username: str
    password: str
    remote_path: str = "/"
    filename_template: str = "{pipeline}_{timestamp}.bin"
    passive: bool = True
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


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
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class S3SinkConfig(BaseModel):
    type: Literal["s3"]
    bucket: str
    key_template: str = "{pipeline}_{timestamp}.bin"
    endpoint_url: str | None = None
    region_name: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    content_type: str = "application/json"
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class VarbindConfig(BaseModel):
    """Explicit varbind specification for SNMP trap sink."""

    oid: str
    value_field: str
    type: Literal["Integer32", "OctetString", "Counter32", "Gauge32", "TimeTicks"] = "OctetString"


class SnmpTrapSinkConfig(BaseModel):
    type: Literal["snmp_trap"]
    host: str
    port: int = 162
    community: str = "public"
    version: str = "2c"
    trap_oid: str = Field(
        default="1.3.6.1.4.1.0",
        validation_alias=AliasChoices("trap_oid", "enterprise_oid"),
    )
    mib_dirs: list[str] = Field(default_factory=list)
    mib_modules: list[str] = Field(default_factory=list)
    varbinds: list[VarbindConfig] = Field(default_factory=list)
    # SNMPv3 USM (used when version="3")
    security_name: str = ""
    auth_protocol: str = "SHA"
    auth_key: str | None = None
    priv_protocol: str = "AES128"
    priv_key: str | None = None
    context_name: str = ""
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class MqttSinkConfig(BaseModel):
    type: Literal["mqtt"]
    host: str
    port: int = 1883
    topic: str
    qos: int = 0
    retain: bool = False
    username: str | None = None
    password: str | None = None
    tls: bool = False
    client_id: str = ""
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class AmqpSinkConfig(BaseModel):
    type: Literal["amqp"]
    url: str = "amqp://guest:guest@localhost:5672/"
    exchange: str = ""
    routing_key: str = ""
    content_type: str = "application/json"
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class NatsSinkConfig(BaseModel):
    type: Literal["nats"]
    servers: list[str] = Field(default_factory=lambda: ["nats://localhost:4222"])
    subject: str
    credentials_file: str | None = None
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class SqlSinkConfig(BaseModel):
    type: Literal["sql"]
    connection_url: str
    table: str
    mode: Literal["insert", "upsert"] = "insert"
    upsert_keys: list[str] = Field(default_factory=list)
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class InfluxDbSinkConfig(BaseModel):
    type: Literal["influxdb"]
    url: str
    token: str
    org: str
    bucket: str
    measurement: str
    tag_fields: list[str] = Field(default_factory=list)
    timestamp_field: str | None = None
    precision: Literal["ns", "us", "ms", "s"] = "ns"
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class RedisSinkConfig(BaseModel):
    type: Literal["redis"]
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    mode: Literal["list", "pubsub", "stream"] = "list"
    key: str
    max_len: int | None = None
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class GcsSinkConfig(BaseModel):
    type: Literal["gcs"]
    bucket: str
    blob_template: str = "{pipeline}_{timestamp}.bin"
    service_account_json: str | None = None
    content_type: str = "application/json"
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class AzureBlobSinkConfig(BaseModel):
    type: Literal["azure_blob"]
    connection_string: str | None = None
    account_name: str | None = None
    account_key: str | None = None
    container: str
    blob_template: str = "{pipeline}_{timestamp}.bin"
    content_type: str = "application/json"
    overwrite: bool = True
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


# v0.5.0 new sinks


class WebSocketSinkConfig(BaseModel):
    type: Literal["websocket"]
    url: str
    extra_headers: dict[str, str] = Field(default_factory=dict)
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class ElasticsearchSinkConfig(BaseModel):
    type: Literal["elasticsearch"]
    hosts: list[str]
    index_template: str
    id_field: str | None = None
    chunk_size: int = 500
    refresh: str = "false"
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    ca_certs: str | None = None
    pipeline: str | None = None
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global


class ClickHouseSinkConfig(BaseModel):
    type: Literal["clickhouse"]
    host: str = "localhost"
    port: int = 9000
    database: str = "default"
    username: str = "default"
    password: str = ""
    table: str
    secure: bool = False
    verify: bool = True
    connect_timeout: int = 10
    send_receive_timeout: int = 300
    condition: str | None = None
    transforms: list[TransformConfig] = Field(default_factory=list)
    retry_count: int = 0
    retry_delay_seconds: float = 1.0
    circuit_breaker_threshold: int = 0
    serializer_out: SerializerConfig | None = None  # per-sink override; None = use global
    # Batching — accumulate rows before flushing to prevent ClickHouse "too many parts"
    batch_size: int = 5000
    batch_timeout_seconds: float = 2.0
    batch_flush_on_stop: bool = True


SinkConfig = Annotated[
    SFTPSinkConfig | LocalSinkConfig | RestSinkConfig | KafkaSinkConfig | OpenSearchSinkConfig | FtpSinkConfig | VesSinkConfig | S3SinkConfig | SnmpTrapSinkConfig | MqttSinkConfig | AmqpSinkConfig | NatsSinkConfig | SqlSinkConfig | ClickHouseSinkConfig | InfluxDbSinkConfig | RedisSinkConfig | GcsSinkConfig | AzureBlobSinkConfig | WebSocketSinkConfig | ElasticsearchSinkConfig,
    Field(discriminator="type"),
]


# ── Serializers ────────────────────────────────────────────────────────────


class JsonSerializerConfig(BaseModel):
    type: Literal["json"]
    indent: int | None = None
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
    avro_schema: str | None = Field(default=None, alias="schema")
    schema_file: str | None = None
    schema_registry_url: str | None = None
    schema_registry_subject: str | None = None
    schema_registry_id: int | None = None
    use_magic_bytes: bool = True

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def check_schema(self) -> AvroSerializerConfig:
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
    framing: Literal["length_delimited", "none"] = "length_delimited"
    schema_registry_url: str | None = None
    schema_registry_subject: str | None = None
    schema_registry_id: int | None = None
    use_magic_bytes: bool = True


class ParquetSerializerConfig(BaseModel):
    type: Literal["parquet"]
    compression: Literal["snappy", "gzip", "brotli", "none"] = "snappy"


class MsgpackSerializerConfig(BaseModel):
    type: Literal["msgpack"]


class NdjsonSerializerConfig(BaseModel):
    type: Literal["ndjson"]
    ensure_ascii: bool = True
    strict: bool = False
    newline: str = "\n"


class BytesSerializerConfig(BaseModel):
    type: Literal["bytes"]
    encoding: Literal["base64", "hex", "none"] = "base64"


class TextSerializerConfig(BaseModel):
    type: Literal["text"]
    encoding: str = "utf-8"
    skip_empty: bool = True
    line_field: str = "_line"
    include_line_num: bool = True
    newline: str = "\n"


class Asn1SerializerConfig(BaseModel):
    type: Literal["asn1"]
    schema_file: str
    message_class: str
    encoding: Literal["ber", "der", "per", "uper", "xer", "jer"] = "ber"


class PmXmlSerializerConfig(BaseModel):
    type: Literal["pm_xml"]
    encoding: str = "utf-8"
    add_managed_element: bool = True
    add_duration: bool = False
    numeric_values: bool = True


SerializerConfig = Annotated[
    JsonSerializerConfig | CsvSerializerConfig | XmlSerializerConfig | AvroSerializerConfig | ProtobufSerializerConfig | ParquetSerializerConfig | MsgpackSerializerConfig | NdjsonSerializerConfig | BytesSerializerConfig | TextSerializerConfig | Asn1SerializerConfig | PmXmlSerializerConfig,
    Field(discriminator="type"),
]


# Rebuild all sink config models now that SerializerConfig is fully defined.
# Sink configs reference SerializerConfig (for per-sink serializer_out) but are
# defined before it in the file. Pydantic v2 requires model_rebuild() after all
# forward-referenced types are available.
_SINK_CONFIG_CLASSES = [
    SFTPSinkConfig, LocalSinkConfig, RestSinkConfig, KafkaSinkConfig,
    OpenSearchSinkConfig, FtpSinkConfig, VesSinkConfig, S3SinkConfig,
    SnmpTrapSinkConfig, MqttSinkConfig, AmqpSinkConfig, NatsSinkConfig,
    SqlSinkConfig, ClickHouseSinkConfig, InfluxDbSinkConfig, RedisSinkConfig,
    GcsSinkConfig, AzureBlobSinkConfig, WebSocketSinkConfig, ElasticsearchSinkConfig,
]
for _cls in _SINK_CONFIG_CLASSES:
    _cls.model_rebuild()


# ── Alert Rules ────────────────────────────────────────────────────────────


class AlertRuleConfig(BaseModel):
    name: str = ""
    condition: str
    action: Literal["webhook", "email"]
    webhook_url: str | None = None
    email_to: str | None = None
    subject: str = "TRAM Alert: {pipeline}"
    cooldown_seconds: int = 300

    @model_validator(mode="after")
    def check_target(self) -> AlertRuleConfig:
        if self.action == "webhook" and not self.webhook_url:
            raise ValueError("webhook_url required when action=webhook")
        if self.action == "email" and not self.email_to:
            raise ValueError("email_to required when action=email")
        return self


# ── Pipeline (top-level) ───────────────────────────────────────────────────


class WorkersConfig(BaseModel):
    count: int | Literal["all"] | None = None
    worker_ids: list[str] | None = Field(
        default=None,
        alias="list",
        validation_alias=AliasChoices("list", "worker_ids"),
        serialization_alias="list",
    )

    @model_validator(mode="after")
    def validate_workers(self) -> WorkersConfig:
        if self.count is not None and self.worker_ids is not None:
            raise ValueError("workers.count and workers.list are mutually exclusive")
        if isinstance(self.count, int) and self.count < 1:
            raise ValueError("workers.count must be >= 1")
        if self.worker_ids is not None:
            if not self.worker_ids:
                raise ValueError("workers.list must not be empty")
            if len(self.worker_ids) != len(set(self.worker_ids)):
                raise ValueError("workers.list must not contain duplicate worker IDs")
        return self


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
    serializer_out: SerializerConfig | None = None  # None → defaults to json at runtime
    sinks: list[SinkConfig] = Field(default_factory=list, min_length=0)

    # Backward compat: singular `sink` auto-wrapped into `sinks`
    sink: SinkConfig | None = Field(default=None, exclude=True)

    # Dead-letter queue
    dlq: SinkConfig | None = None

    # Rate limiting
    rate_limit_rps: float | None = None

    # Parallelism
    thread_workers: int = 1   # intra-node worker threads per pipeline run
    parallel_sinks: bool = False   # fan-out sink writes concurrently
    workers: WorkersConfig | None = None

    # Batch size cap (max records to process per batch run; None = unlimited)
    batch_size: int | None = None

    # Error handling
    on_error: Literal["continue", "abort", "retry", "dlq"] = "continue"
    retry_count: int = 3
    retry_delay_seconds: int = 10

    # Alert rules
    alerts: list[AlertRuleConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_on_error_dlq(self) -> PipelineConfig:
        if self.on_error == "dlq" and self.dlq is None:
            raise ValueError("on_error='dlq' requires a 'dlq' sink to be configured")
        return self

    @model_validator(mode="after")
    def apply_workers_default(self) -> PipelineConfig:
        if self.workers is None:
            if self.source.type in ("webhook", "prometheus_rw"):
                self.workers = WorkersConfig(count="all")
            else:
                self.workers = WorkersConfig(count=1)
        return self

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
    def normalise_sinks(self) -> PipelineConfig:
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
