"""Local filesystem sink connector."""

from __future__ import annotations

import logging
from pathlib import Path

from tram.connectors.file_sink_common import (
    FilePartState,
    StagedFileTarget,
    ensure_rolling_token,
    file_state_key,
    prepare_payload_for_append,
    render_filename,
    should_roll,
    should_stage_file_output,
    source_unit_key,
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
                                             {timestamp}, {epoch_m}/{epoch_ms},
                                             {source_filename}.
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
        self._states: dict[tuple[tuple[str, str], ...], FilePartState] = {}
        self._current_paths: dict[tuple[tuple[str, str], ...], Path] = {}
        self._part_counters: dict[tuple[tuple[str, str], ...], int] = {}
        self._staged_targets: dict[tuple[str, str, str], dict[tuple[tuple[str, str], ...], StagedFileTarget]] = {}

    @staticmethod
    def _temp_path_for(final_path: Path, run_id: str) -> Path:
        return final_path.with_name(f".{final_path.name}.tram-{run_id}.tmp")

    @staticmethod
    def _cleanup_stale_temp_files(final_path: Path, *, keep_path: Path | None = None) -> None:
        pattern = f".{final_path.name}.tram-*.tmp"
        for candidate in final_path.parent.glob(pattern):
            if keep_path is not None and candidate == keep_path:
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue

    def _next_path(
        self,
        meta: dict,
        *,
        now,
        state_key: tuple[tuple[str, str], ...],
    ) -> tuple[Path, FilePartState]:
        part_index = self._part_counters.get(state_key, 0) + 1
        if part_index > self.max_index:
            raise SinkError(
                f"Local sink exceeded max_index={self.max_index}; "
                "increase max_index or adjust rollover thresholds"
            )
        self._part_counters[state_key] = part_index
        state = FilePartState(part_index=part_index, opened_at=now)
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
            state_key = file_state_key(self.filename_template, meta=meta)
            stage_output = should_stage_file_output(meta, serializer_type)
            staged_source_key = source_unit_key(meta) if stage_output else None

            if self.file_mode == "append":
                state = self._states.get(state_key)
                current_path = self._current_paths.get(state_key)
                if should_roll(
                    state,
                    now=now,
                    incoming_records=record_count,
                    incoming_bytes=len(data),
                    max_records=self.max_records,
                    max_time=self.max_time,
                    max_bytes=self.max_bytes,
                ):
                    self._states.pop(state_key, None)
                    self._current_paths.pop(state_key, None)
                    state = None
                    current_path = None
                is_new_file = state is None or current_path is None
                if is_new_file:
                    current_path, state = self._next_path(meta, now=now, state_key=state_key)
                    if stage_output:
                        final_path = current_path
                        current_path = self._temp_path_for(
                            final_path,
                            str(meta.get("run_id", "") or "run"),
                        )
                        self._cleanup_stale_temp_files(final_path, keep_path=current_path)
                        staged_targets = self._staged_targets.setdefault(staged_source_key, {})
                        staged_targets[state_key] = StagedFileTarget(
                            state_key=state_key,
                            temp_path=str(current_path),
                            final_path=str(final_path),
                        )
                    self._current_paths[state_key] = current_path
                    self._states[state_key] = state
                dest = current_path
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
                assert state is not None
                state.records_written += record_count
                state.bytes_written += len(payload)
            else:
                dest, state = self._next_path(meta, now=now, state_key=state_key)
                if stage_output:
                    final_path = dest
                    dest = self._temp_path_for(final_path, str(meta.get("run_id", "") or "run"))
                    self._cleanup_stale_temp_files(final_path, keep_path=dest)
                    staged_targets = self._staged_targets.setdefault(staged_source_key, {})
                    staged_targets[state_key] = StagedFileTarget(
                        state_key=state_key,
                        temp_path=str(dest),
                        final_path=str(final_path),
                    )
                    existing = dest.exists()
                    payload = prepare_payload_for_append(
                        data,
                        serializer_type=serializer_type,
                        serializer_config=serializer_config,
                        is_new_file=not existing,
                    )
                    if payload:
                        with dest.open("ab") as fh:
                            fh.write(payload)
                        state.records_written += record_count
                        state.bytes_written += len(payload)
                else:
                    if dest.exists() and not self.overwrite:
                        raise SinkError(f"File already exists and overwrite=false: {dest}")
                    dest.write_bytes(data)
                self._states[state_key] = state
                self._current_paths[state_key] = dest
            logger.info(
                "Wrote file locally",
                extra={"filepath": str(dest), "bytes": len(data)},
            )
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Error writing to {self.path}: {exc}") from exc

    def finalize_source(self, meta: dict, success: bool) -> None:
        source_key = source_unit_key(meta)
        staged_targets = self._staged_targets.pop(source_key, {})
        for state_key, target in staged_targets.items():
            temp_path = Path(target.temp_path)
            final_path = Path(target.final_path)

            try:
                if success and temp_path.exists():
                    if final_path.exists():
                        if not self.overwrite:
                            raise SinkError(
                                f"File already exists and overwrite=false: {final_path}"
                            )
                        final_path.unlink()
                    temp_path.replace(final_path)
                elif not success and temp_path.exists():
                    temp_path.unlink()
                self._states.pop(state_key, None)
                self._current_paths.pop(state_key, None)
                self._part_counters.pop(state_key, None)
            except SinkError:
                raise
            except Exception as exc:
                raise SinkError(f"Error finalizing local sink output for {final_path}: {exc}") from exc
