"""Common helpers for append/rolling file sinks."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from string import Formatter

_FORMATTER = Formatter()
_ROLLING_TOKENS = {"timestamp", "epoch", "epoch_m", "part", "index"}


@dataclass
class FilePartState:
    part_index: int
    opened_at: datetime
    records_written: int = 0
    bytes_written: int = 0


def format_part_index(part_index: int, max_index: int) -> str:
    width = max(1, len(str(max_index)))
    return f"{part_index:0{width}d}"


def _basename(path: str) -> str:
    normalized = str(path).replace("\\", "/").rstrip("/")
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1]


def _resolve_source_filename(meta: dict) -> str:
    filename = str(meta.get("source_filename", "") or "").strip()
    if filename:
        return filename
    source_path = str(meta.get("source_path", "") or "").strip()
    basename = _basename(source_path)
    return basename or "data"


def build_filename_vars(
    *,
    opened_at: datetime,
    part_index: int,
    max_index: int,
    meta: dict,
) -> dict[str, str | int]:
    source_filename = _resolve_source_filename(meta)
    source_path = str(meta.get("source_path", "") or source_filename)
    source_name = Path(source_filename)
    epoch = int(opened_at.timestamp())
    epoch_m = int(opened_at.timestamp() * 1000)
    part = format_part_index(part_index, max_index)
    return {
        "pipeline": meta.get("pipeline_name", "tram"),
        "timestamp": opened_at.strftime("%Y%m%dT%H%M%S"),
        "epoch": epoch,
        "epoch_m": epoch_m,
        "part": part,
        "index": part,
        "run_timestamp": meta.get("run_timestamp", ""),
        "run_id": meta.get("run_id", ""),
        "source_filename": source_filename,
        "source_stem": source_name.stem or source_filename,
        "source_suffix": source_name.suffix,
        "source_path": source_path,
    }


def _resolve_field_path(values: Mapping[str, object] | object, path: str) -> str:
    if isinstance(values, Mapping):
        direct = values.get(path)
        if direct not in (None, ""):
            return str(direct)
        current: object = values
        for segment in path.split("."):
            if not isinstance(current, Mapping) or segment not in current:
                return "unknown"
            current = current[segment]
        if current in (None, ""):
            return "unknown"
        return str(current)
    return "unknown"


def extract_field_paths(template: str) -> list[str]:
    paths: list[str] = []
    for _, field_name, _, _ in _FORMATTER.parse(template):
        if field_name and field_name.startswith("field.") and field_name[6:] not in paths:
            paths.append(field_name[6:])
    return paths


def file_state_key(template: str, *, meta: dict) -> tuple[tuple[str, str], ...]:
    base_vars = build_filename_vars(
        opened_at=utc_now(),
        part_index=1,
        max_index=1,
        meta=meta,
    )
    field_values = meta.get("field_values", {})
    parts: list[tuple[str, str]] = []
    for _, field_name, _, _ in _FORMATTER.parse(template):
        if not field_name or field_name in _ROLLING_TOKENS:
            continue
        if field_name.startswith("field."):
            parts.append((field_name, _resolve_field_path(field_values, field_name[6:])))
            continue
        if field_name in base_vars:
            parts.append((field_name, str(base_vars[field_name])))
    return tuple(parts)


def _resolve_template_value(
    field_name: str,
    *,
    base_vars: dict[str, str | int],
    field_values: Mapping[str, object] | object,
) -> str | int:
    if field_name.startswith("field."):
        return _resolve_field_path(field_values, field_name[6:])
    if field_name in base_vars:
        return base_vars[field_name]
    raise KeyError(field_name)


def render_filename(
    template: str,
    *,
    opened_at: datetime,
    part_index: int,
    max_index: int,
    meta: dict,
) -> str:
    base_vars = build_filename_vars(
        opened_at=opened_at,
        part_index=part_index,
        max_index=max_index,
        meta=meta,
    )
    field_values = meta.get("field_values", {})
    parts: list[str] = []
    for literal_text, field_name, format_spec, conversion in _FORMATTER.parse(template):
        parts.append(literal_text)
        if field_name is None:
            continue
        value = _resolve_template_value(
            field_name,
            base_vars=base_vars,
            field_values=field_values,
        )
        if conversion == "r":
            value = repr(value)
        elif conversion == "a":
            value = ascii(value)
        elif conversion == "s":
            value = str(value)
        if format_spec:
            parts.append(format(value, format_spec))
        else:
            parts.append(str(value))
    return "".join(parts)


def ensure_rolling_token(template: str, *, logger: logging.Logger, sink_name: str) -> str:
    if "{part}" in template or "{index}" in template or "{epoch_m}" in template:
        return template
    path = Path(template)
    if path.suffix:
        updated = f"{path.stem}_{{part}}{path.suffix}"
    else:
        updated = f"{template}_{{part}}"
    logger.warning(
        "%s rolling sink template lacks a strong uniqueness token; "
        "auto-appending _{part} to avoid collisions",
        sink_name,
        extra={"original_template": template, "effective_template": updated},
    )
    return updated


def should_roll(
    state: FilePartState | None,
    *,
    now: datetime,
    incoming_records: int,
    incoming_bytes: int,
    max_records: int | None,
    max_time: int | None,
    max_bytes: int | None,
) -> bool:
    if state is None:
        return False
    if max_time is not None and (now - state.opened_at).total_seconds() >= max_time:
        return True
    if max_records is not None and state.records_written > 0 and state.records_written + incoming_records > max_records:
        return True
    if max_bytes is not None and state.bytes_written > 0 and state.bytes_written + incoming_bytes > max_bytes:
        return True
    return False


def prepare_payload_for_append(
    data: bytes,
    *,
    serializer_type: str,
    serializer_config: dict,
    is_new_file: bool,
) -> bytes:
    if not data:
        return data

    if serializer_type == "csv" and not is_new_file and serializer_config.get("has_header", True):
        lines = data.splitlines(keepends=True)
        if len(lines) <= 1:
            return b""
        data = b"".join(lines[1:])

    if serializer_type == "ndjson":
        newline = str(serializer_config.get("newline", "\n")).encode("utf-8")
        if data and not data.endswith(newline):
            data += newline

    return data


def utc_now() -> datetime:
    return datetime.now(UTC)
