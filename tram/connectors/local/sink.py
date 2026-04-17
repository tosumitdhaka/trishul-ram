"""Local filesystem sink connector."""

from __future__ import annotations

import logging
from pathlib import Path

from tram.connectors.file_sink_common import (
    FilePartState,
    ensure_rolling_token,
    prepare_payload_for_append,
    render_filename,
    should_roll,
    utc_now,
)
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("local")
class LocalSink(BaseSink):
    """Write data to a local directory.

    Config keys:
        path               (str, required)   Directory to write to (created if absent).
        filename_template  (str, optional)   Filename template. Tokens: {pipeline},
                                             {timestamp}, {source_filename}.
                                             Default: "{pipeline}_{timestamp}.bin"
        overwrite          (bool, default True)  Overwrite existing files in single mode.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.path = Path(config["path"])
        self.filename_template: str = config.get(
            "filename_template", "{pipeline}_{timestamp}.bin"
        )
        self.overwrite: bool = bool(config.get("overwrite", True))
        self.file_mode: str = str(config.get("file_mode", "append"))
        self.max_records: int | None = config.get("max_records")
        self.max_time: int | None = config.get("max_time")
        self.max_bytes: int | None = config.get("max_bytes")
        self.max_index: int = int(config.get("max_index", 99999))
        if self.file_mode == "append" and any(
            value is not None for value in (self.max_records, self.max_time, self.max_bytes)
        ):
            self.filename_template = ensure_rolling_token(
                self.filename_template,
                logger=logger,
                sink_name="LocalSink",
            )
        self._state: FilePartState | None = None
        self._current_path: Path | None = None
        self._part_counter: int = 0

    def _next_path(self, meta: dict, *, now) -> tuple[Path, FilePartState]:
        self._part_counter += 1
        if self._part_counter > self.max_index:
            raise SinkError(
                f"Local sink exceeded max_index={self.max_index}; "
                "increase max_index or adjust rollover thresholds"
            )
        state = FilePartState(part_index=self._part_counter, opened_at=now)
        filename = render_filename(
            self.filename_template,
            opened_at=state.opened_at,
            part_index=state.part_index,
            max_index=self.max_index,
            meta=meta,
        )
        return self.path / filename, state

    def write(self, data: bytes, meta: dict) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            serializer_type = str(meta.get("serializer_type", "json"))
            serializer_config = dict(meta.get("serializer_config", {}))
            record_count = int(meta.get("output_record_count", 0))
            now = utc_now()

            if self.file_mode == "append":
                if should_roll(
                    self._state,
                    now=now,
                    incoming_records=record_count,
                    incoming_bytes=len(data),
                    max_records=self.max_records,
                    max_time=self.max_time,
                    max_bytes=self.max_bytes,
                ):
                    self._state = None
                    self._current_path = None
                is_new_file = self._state is None or self._current_path is None
                if is_new_file:
                    self._current_path, self._state = self._next_path(meta, now=now)
                dest = self._current_path
                payload = prepare_payload_for_append(
                    data,
                    serializer_type=serializer_type,
                    serializer_config=serializer_config,
                    is_new_file=is_new_file,
                )
                if not payload:
                    return
                with dest.open("ab") as fh:
                    fh.write(payload)
                assert self._state is not None
                self._state.records_written += record_count
                self._state.bytes_written += len(payload)
            else:
                dest, state = self._next_path(meta, now=now)
                if dest.exists() and not self.overwrite:
                    raise SinkError(f"File already exists and overwrite=false: {dest}")
                dest.write_bytes(data)
                self._state = state
                self._current_path = dest
            logger.info(
                "Wrote file locally",
                extra={"filepath": str(dest), "bytes": len(data)},
            )
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Error writing to {self.path}: {exc}") from exc
