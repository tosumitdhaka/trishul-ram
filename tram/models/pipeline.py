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


SourceConfig = Annotated[
    Union[SFTPSourceConfig, LocalSourceConfig, RestSourceConfig, KafkaSourceConfig],
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


SinkConfig = Annotated[
    Union[SFTPSinkConfig, LocalSinkConfig, RestSinkConfig, KafkaSinkConfig, OpenSearchSinkConfig],
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


SerializerConfig = Annotated[
    Union[JsonSerializerConfig, CsvSerializerConfig, XmlSerializerConfig],
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
    sink: SinkConfig

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


class PipelineFile(BaseModel):
    """Top-level YAML wrapper — the 'pipeline:' key."""

    version: str = "1"
    pipeline: PipelineConfig
