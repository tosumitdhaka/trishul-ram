"""Timestamp normalize transform — converts heterogeneous timestamps to UTC ISO-8601."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

logger = logging.getLogger(__name__)

# Thresholds for unix epoch auto-detection
_SEC_MAX = 9_999_999_999        # up to year 2286 in seconds
_MS_MAX = 9_999_999_999_999     # milliseconds
_US_MAX = 9_999_999_999_999_999 # microseconds
# above _US_MAX → nanoseconds


def _parse_timestamp(val: Any, input_format: str | None) -> datetime:
    """Parse a value into a UTC-aware datetime."""
    # Already a datetime
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)

    # Numeric — unix epoch (sec / ms / us / ns auto-detect)
    if isinstance(val, (int, float)):
        return _from_unix(val)

    s = str(val).strip()

    # Numeric string
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return _from_unix(float(s))

    # Explicit format
    if input_format:
        try:
            dt = datetime.strptime(s, input_format)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError as exc:
            raise TransformError(f"Cannot parse {s!r} with format {input_format!r}: {exc}") from exc

    # Try ISO-8601 variants
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue

    # Python 3.11+ fromisoformat handles most ISO variants
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    raise TransformError(f"Cannot parse timestamp: {val!r}")


def _from_unix(val: float) -> datetime:
    absval = abs(val)
    if absval <= _SEC_MAX:
        ts_secs = val
    elif absval <= _MS_MAX:
        ts_secs = val / 1_000
    elif absval <= _US_MAX:
        ts_secs = val / 1_000_000
    else:
        ts_secs = val / 1_000_000_000
    return datetime.fromtimestamp(ts_secs, tz=timezone.utc)


@register_transform("timestamp_normalize")
class TimestampNormalizeTransform(BaseTransform):
    """Normalize timestamps in specified fields to UTC ISO-8601 strings (or datetime objects).

    Handles: unix epoch (sec/ms/us/ns auto-detect), ISO-8601 variants, custom strptime formats.

    Config keys:
        fields        (list[str], required)   Fields to normalize.
        input_format  (str, optional)         strptime format string. Auto-detected if omitted.
        output_format (str, default "iso")    "iso" | "datetime" | "epoch_s" | "epoch_ms" |
                                              "epoch_us" | "epoch_ns" | strftime format string.
                                              "iso"       → UTC ISO-8601 string (millisecond precision)
                                              "datetime"  → Python datetime object
                                              "epoch_s"   → float seconds since Unix epoch
                                              "epoch_ms"  → int milliseconds since Unix epoch
                                              "epoch_us"  → int microseconds since Unix epoch
                                              "epoch_ns"  → int nanoseconds since Unix epoch
        on_error      (str, default "raise")  "raise" | "null" | "keep"
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: list[str] = config.get("fields", [])
        if not self.fields:
            raise TransformError("timestamp_normalize: 'fields' list is required")
        self.input_format: str | None = config.get("input_format")
        self.output_format: str = config.get("output_format", "iso")
        self.on_error: str = config.get("on_error", "raise")

    def _format(self, dt: datetime) -> Any:
        if self.output_format == "epoch_s":
            return dt.timestamp()
        if self.output_format == "epoch_ms":
            return int(dt.timestamp() * 1_000)
        if self.output_format == "epoch_us":
            return int(dt.timestamp() * 1_000_000)
        if self.output_format == "epoch_ns":
            return int(dt.timestamp() * 1_000_000_000)
        if self.output_format == "iso":
            return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # millisecond precision + Z
        if self.output_format == "datetime":
            return dt
        return dt.strftime(self.output_format)

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = dict(record)
            for field in self.fields:
                if field not in new_record:
                    continue
                try:
                    dt = _parse_timestamp(new_record[field], self.input_format)
                    new_record[field] = self._format(dt)
                except TransformError as exc:
                    if self.on_error == "raise":
                        raise
                    elif self.on_error == "null":
                        new_record[field] = None
                    # "keep" → leave original value
                    else:
                        logger.debug("timestamp_normalize: keeping original value — %s", exc)
            result.append(new_record)
        return result
