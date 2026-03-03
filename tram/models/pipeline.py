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


SourceConfig = Annotated[
    Union[SFTPSourceConfig],
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


SinkConfig = Annotated[
    Union[SFTPSinkConfig],
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
    fields: dict[str, str]  # field_name → expression


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


TransformConfig = Annotated[
    Union[
        RenameTransformConfig,
        CastTransformConfig,
        AddFieldTransformConfig,
        DropTransformConfig,
        ValueMapTransformConfig,
        FilterTransformConfig,
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
