"""Common helpers for append/rolling file sinks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class FilePartState:
    part_index: int
    opened_at: datetime
    records_written: int = 0
    bytes_written: int = 0


def format_part_index(part_index: int, max_index: int) -> str:
    width = max(1, len(str(max_index)))
    return f"{part_index:0{width}d}"


def render_filename(
    template: str,
    *,
    opened_at: datetime,
    part_index: int,
    max_index: int,
    meta: dict,
) -> str:
    epoch = int(opened_at.timestamp())
    epoch_m = int(opened_at.timestamp() * 1000)
    part = format_part_index(part_index, max_index)
    return template.format(
        pipeline=meta.get("pipeline_name", "tram"),
        timestamp=opened_at.strftime("%Y%m%dT%H%M%S"),
        epoch=epoch,
        epoch_m=epoch_m,
        part=part,
        index=part,
        run_timestamp=meta.get("run_timestamp", ""),
        run_id=meta.get("run_id", ""),
        source_filename=meta.get("source_filename", "data"),
    )


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
