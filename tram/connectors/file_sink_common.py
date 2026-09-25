"""Common helpers for append/rolling file sinks."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from string import Formatter

from tram.core.exceptions import SinkError

_FORMATTER = Formatter()
_ROLLING_TOKENS = {"timestamp", "epoch", "epoch_m", "epoch_ms", "part", "index"}


@dataclass
class FilePartState:
    part_index: int
    opened_at: datetime
    records_written: int = 0
    bytes_written: int = 0


@dataclass(frozen=True)
class StagedFileTarget:
    state_key: tuple[tuple[str, str], ...]
    temp_path: str
    final_path: str


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


def source_unit_key(meta: dict) -> tuple[str, str, str]:
    source_path = str(meta.get("source_path", "") or "").strip()
    source_filename = _resolve_source_filename(meta)
    run_id = str(meta.get("run_id", "") or "").strip()
    return source_path, source_filename, run_id


def is_record_safe_serializer(serializer_type: str) -> bool:
    return serializer_type in {"ndjson", "csv"}


def should_stage_file_output(meta: dict, serializer_type: str) -> bool:
    if not meta.get("enable_safe_finalize"):
        return False
    if not is_record_safe_serializer(serializer_type):
        return False
    source_path, source_filename, run_id = source_unit_key(meta)
    return bool(run_id and (source_path or source_filename))


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
    epoch_ms = epoch_m
    part = format_part_index(part_index, max_index)
    return {
        "pipeline": meta.get("pipeline_name", "tram"),
        "timestamp": opened_at.strftime("%Y%m%dT%H%M%S"),
        "epoch": epoch,
        "epoch_m": epoch_m,
        "epoch_ms": epoch_ms,
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


def validate_template_tokens(template: str) -> list[str]:
    allowed_tokens = set(
        build_filename_vars(
            opened_at=datetime.now(UTC),
            part_index=1,
            max_index=1,
            meta={},
        ).keys()
    )
    issues: list[str] = []
    for _, field_name, _, _ in _FORMATTER.parse(template):
        if not field_name or field_name.startswith("field."):
            continue
        if field_name in allowed_tokens:
            continue
        issues.append(f"unknown template token '{field_name}'")
    return issues


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
    if "{part}" in template or "{index}" in template or "{epoch_m}" in template or "{epoch_ms}" in template:
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


# ── Shared roll/stage/partition state machine (review E3) ───────────────────
#
# LocalSink and SFTPSink previously duplicated this entire append/single-mode
# state machine verbatim. The transport-specific operations (mkdir, append,
# rename, remove) are delegated to a small backend adapter so both sinks run
# byte-identical logic with one copy of the bookkeeping (A10's thread-unsafe
# dicts included).


class RollingFileBackend(ABC):
    """Filesystem operations a file sink needs, abstracted over the transport.

    ``handle`` is the open transport object (e.g. the paramiko SFTPClient);
    local filesystems pass ``None``. All methods must be safe to call on a
    stale connection — the sink's reconnect guard wraps the whole write.
    """

    @property
    @abstractmethod
    def overwrite(self) -> bool:
        """Whether single-mode writes may replace an existing file."""

    @abstractmethod
    def join_root(self, rendered: str) -> str:
        """Join the sink root directory to a rendered filename."""

    @abstractmethod
    def ensure_dir(self, handle) -> None:
        """Create the sink root directory if absent."""

    @abstractmethod
    def temp_path(self, final: str, run_id: str) -> str:
        """Staging temp path for *final* under the current run."""

    @abstractmethod
    def cleanup_stale_temp(self, handle, final: str, *, keep: str | None = None) -> None:
        """Remove stale ``*.tram-*.tmp`` siblings of *final*, keeping *keep*."""

    @abstractmethod
    def exists(self, handle, path: str) -> bool:
        """True when *path* exists on the backend."""

    @abstractmethod
    def append(self, handle, path: str, payload: bytes) -> None:
        """Open *path* in append mode and write *payload*."""

    @abstractmethod
    def write_bytes(self, handle, path: str, data: bytes) -> None:
        """Write *data* to *path*, truncating any existing file."""

    @abstractmethod
    def replace(self, handle, temp: str, final: str) -> None:
        """Atomically move *temp* over *final* (finalize success path)."""

    @abstractmethod
    def remove(self, handle, path: str) -> None:
        """Best-effort removal of *path* (missing file is a no-op)."""


class LocalRollingBackend(RollingFileBackend):
    """Local-filesystem adapter for :class:`RollingWriter`."""

    def __init__(self, root: Path, *, overwrite: bool) -> None:
        self._root = root
        self._overwrite = overwrite

    @property
    def overwrite(self) -> bool:
        return self._overwrite

    def join_root(self, rendered: str) -> str:
        return str(self._root / rendered)

    def ensure_dir(self, handle) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def temp_path(self, final: str, run_id: str) -> str:
        final_path = Path(final)
        return str(final_path.with_name(f".{final_path.name}.tram-{run_id}.tmp"))

    def cleanup_stale_temp(self, handle, final: str, *, keep: str | None = None) -> None:
        final_path = Path(final)
        pattern = f".{final_path.name}.tram-*.tmp"
        for candidate in final_path.parent.glob(pattern):
            if keep is not None and str(candidate) == keep:
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue

    def exists(self, handle, path: str) -> bool:
        return Path(path).exists()

    def append(self, handle, path: str, payload: bytes) -> None:
        with Path(path).open("ab") as fh:
            fh.write(payload)

    def write_bytes(self, handle, path: str, data: bytes) -> None:
        Path(path).write_bytes(data)

    def replace(self, handle, temp: str, final: str) -> None:
        temp_path = Path(temp)
        final_path = Path(final)
        if not temp_path.exists():
            return
        if final_path.exists():
            if not self._overwrite:
                raise SinkError(f"File already exists and overwrite=false: {final_path}")
            final_path.unlink()
        temp_path.replace(final_path)

    def remove(self, handle, path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except FileNotFoundError:
            pass


class SftpRollingBackend(RollingFileBackend):
    """SFTP adapter for :class:`RollingWriter` (``handle`` = SFTPClient)."""

    # Single-mode uses open(..., "wb") — SFTP always overwrites, so the
    # overwrite-guard never fires for this backend.
    overwrite = True

    def __init__(self, remote_path: str) -> None:
        self._remote_path = remote_path

    def join_root(self, rendered: str) -> str:
        return f"{self._remote_path}/{rendered}"

    def ensure_dir(self, handle) -> None:
        try:
            handle.stat(self._remote_path)
        except FileNotFoundError:
            handle.mkdir(self._remote_path)

    def temp_path(self, final: str, run_id: str) -> str:
        if "/" in final:
            directory, filename = final.rsplit("/", 1)
            return f"{directory}/.{filename}.tram-{run_id}.tmp"
        return f".{final}.tram-{run_id}.tmp"

    def cleanup_stale_temp(self, handle, final: str, *, keep: str | None = None) -> None:
        if "/" in final:
            directory, filename = final.rsplit("/", 1)
        else:
            directory, filename = "", final
        prefix = f".{filename}.tram-"
        suffix = ".tmp"
        listing_path = directory or "."
        try:
            names = handle.listdir(listing_path)
        except Exception:
            return
        for name in names:
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            candidate = f"{directory}/{name}" if directory else name
            if keep is not None and candidate == keep:
                continue
            try:
                handle.remove(candidate)
            except Exception:
                continue

    def exists(self, handle, path: str) -> bool:
        try:
            handle.stat(path)
            return True
        except Exception:
            return False

    def append(self, handle, path: str, payload: bytes) -> None:
        with handle.open(path, "ab") as fh:
            fh.write(payload)

    def write_bytes(self, handle, path: str, data: bytes) -> None:
        with handle.open(path, "wb") as fh:
            fh.write(data)

    def replace(self, handle, temp: str, final: str) -> None:
        if hasattr(handle, "posix_rename"):
            handle.posix_rename(temp, final)
        else:
            try:
                handle.remove(final)
            except Exception:
                pass
            handle.rename(temp, final)

    def remove(self, handle, path: str) -> None:
        try:
            handle.remove(path)
        except Exception:
            pass


class RollingWriter:
    """Owns the roll/stage/partition state machine shared by LocalSink and
    SFTPSink (review E3): file-part bookkeeping, max_records/max_time/max_bytes
    rollover, staging temp files for record-safe serializers, and partition
    state per field-value key. Transport I/O goes through the *backend*."""

    def __init__(
        self,
        *,
        filename_template: str,
        file_mode: str,
        max_records: int | None,
        max_time: int | None,
        max_bytes: int | None,
        max_index: int,
        sink_name: str,
        logger: logging.Logger,
    ) -> None:
        self._filename_template = filename_template
        self._file_mode = file_mode
        self._max_records = max_records
        self._max_time = max_time
        self._max_bytes = max_bytes
        self._max_index = max_index
        self._sink_name = sink_name
        self._logger = logger
        self._states: dict[tuple[tuple[str, str], ...], FilePartState] = {}
        self._current_paths: dict[tuple[tuple[str, str], ...], str] = {}
        self._part_counters: dict[tuple[tuple[str, str], ...], int] = {}
        self._staged_targets: dict[tuple[str, str, str], dict[tuple[tuple[str, str], ...], StagedFileTarget]] = {}
        if self._file_mode == "append" and any(
            value is not None for value in (self._max_records, self._max_time, self._max_bytes)
        ):
            self._filename_template = ensure_rolling_token(
                self._filename_template,
                logger=self._logger,
                sink_name=self._sink_name,
            )

    @property
    def filename_template(self) -> str:
        """Effective template (after ensure_rolling_token may have appended a
        ``{part}`` token). Exposed so sinks can mirror the legacy
        ``self.filename_template`` attribute the executor's partition logic
        reads (review E3)."""
        return self._filename_template

    def _next_path(
        self,
        backend: RollingFileBackend,
        meta: dict,
        *,
        now,
        state_key: tuple[tuple[str, str], ...],
    ) -> tuple[str, FilePartState]:
        part_index = self._part_counters.get(state_key, 0) + 1
        if part_index > self._max_index:
            raise SinkError(
                f"{self._sink_name} sink exceeded max_index={self._max_index}; "
                "increase max_index or adjust rollover thresholds"
            )
        self._part_counters[state_key] = part_index
        state = FilePartState(part_index=part_index, opened_at=now)
        filename = render_filename(
            self._filename_template,
            opened_at=state.opened_at,
            part_index=state.part_index,
            max_index=self._max_index,
            meta=meta,
        )
        return backend.join_root(filename), state

    def write(
        self,
        data: bytes,
        meta: dict,
        *,
        backend: RollingFileBackend,
        handle,
    ) -> str | None:
        """Write one chunk. Returns the destination path, or None when the
        payload was empty (nothing written, nothing to log)."""
        backend.ensure_dir(handle)
        serializer_type = str(meta.get("serializer_type", "json"))
        serializer_config = dict(meta.get("serializer_config", {}))
        record_count = int(meta.get("output_record_count", 0))
        now = utc_now()
        state_key = file_state_key(self._filename_template, meta=meta)
        stage_output = should_stage_file_output(meta, serializer_type)
        staged_source_key = source_unit_key(meta) if stage_output else None

        if self._file_mode == "append":
            state = self._states.get(state_key)
            current_path = self._current_paths.get(state_key)
            if should_roll(
                state,
                now=now,
                incoming_records=record_count,
                incoming_bytes=len(data),
                max_records=self._max_records,
                max_time=self._max_time,
                max_bytes=self._max_bytes,
            ):
                self._states.pop(state_key, None)
                self._current_paths.pop(state_key, None)
                state = None
                current_path = None
            is_new_file = state is None or current_path is None
            if is_new_file:
                current_path, state = self._next_path(backend, meta, now=now, state_key=state_key)
                if stage_output:
                    final_path = current_path
                    current_path = backend.temp_path(
                        final_path,
                        str(meta.get("run_id", "") or "run"),
                    )
                    backend.cleanup_stale_temp(handle, final_path, keep=current_path)
                    staged_targets = self._staged_targets.setdefault(staged_source_key, {})
                    staged_targets[state_key] = StagedFileTarget(
                        state_key=state_key,
                        temp_path=current_path,
                        final_path=final_path,
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
                return None
            backend.append(handle, dest, payload)
            assert state is not None
            state.records_written += record_count
            state.bytes_written += len(payload)
            return dest

        dest, state = self._next_path(backend, meta, now=now, state_key=state_key)
        if stage_output:
            final_path = dest
            dest = backend.temp_path(final_path, str(meta.get("run_id", "") or "run"))
            backend.cleanup_stale_temp(handle, final_path, keep=dest)
            staged_targets = self._staged_targets.setdefault(staged_source_key, {})
            staged_targets[state_key] = StagedFileTarget(
                state_key=state_key,
                temp_path=dest,
                final_path=final_path,
            )
            existing = backend.exists(handle, dest)
            payload = prepare_payload_for_append(
                data,
                serializer_type=serializer_type,
                serializer_config=serializer_config,
                is_new_file=not existing,
            )
            if payload:
                backend.append(handle, dest, payload)
                state.records_written += record_count
                state.bytes_written += len(payload)
        else:
            if backend.exists(handle, dest) and not backend.overwrite:
                raise SinkError(f"File already exists and overwrite=false: {dest}")
            backend.write_bytes(handle, dest, data)
        self._states[state_key] = state
        self._current_paths[state_key] = dest
        return dest

    def has_staged_targets(self, source_key: tuple[str, str, str]) -> bool:
        """True when *source_key* has pending staged temp files. Lets sinks
        with connection costs skip finalize entirely (review E3)."""
        return bool(self._staged_targets.get(source_key))

    def finalize_source(
        self,
        source_key: tuple[str, str, str],
        *,
        backend: RollingFileBackend,
        handle,
        success: bool,
    ) -> None:
        """Publish (or discard) all staged temp files for one source unit."""
        staged_targets = self._staged_targets.pop(source_key, {})
        for state_key, target in staged_targets.items():
            temp_path = target.temp_path
            final_path = target.final_path
            try:
                if success:
                    backend.replace(handle, temp_path, final_path)
                else:
                    backend.remove(handle, temp_path)
                self._states.pop(state_key, None)
                self._current_paths.pop(state_key, None)
                self._part_counters.pop(state_key, None)
            except SinkError:
                raise
            except Exception as exc:
                raise SinkError(
                    f"Error finalizing {self._sink_name.lower()} sink output "
                    f"for {final_path}: {exc}"
                ) from exc
