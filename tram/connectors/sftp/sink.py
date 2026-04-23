"""SFTP sink connector — writes files to a remote SFTP server."""

from __future__ import annotations

import logging

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


@register_sink("sftp")
class SFTPSink(BaseSink):
    """Write data to a remote SFTP server.

    Filename is generated from a template supporting tokens:
    - ``{pipeline}``        — pipeline name (from meta or config)
    - ``{timestamp}``       — UTC file-open timestamp
    - ``{epoch}``          — UTC file-open epoch seconds
    - ``{epoch_m}``        — UTC file-open epoch milliseconds
    - ``{part}`` / ``{index}`` — rolling file part number
    - ``{source_filename}`` — original source filename (from meta)
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 22))
        self.username: str = config["username"]
        self.password: str | None = config.get("password")
        self.private_key_path: str | None = config.get("private_key_path")
        self.remote_path: str = config["remote_path"].rstrip("/")
        self.filename_template: str = config.get(
            "filename_template", "{pipeline}_{timestamp}.bin"
        )
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
                sink_name="SFTPSink",
            )
        self._states: dict[tuple[tuple[str, str], ...], FilePartState] = {}
        self._current_remote_files: dict[tuple[tuple[str, str], ...], str] = {}
        self._part_counters: dict[tuple[tuple[str, str], ...], int] = {}
        self._staged_targets: dict[tuple[str, str, str], dict[tuple[tuple[str, str], ...], StagedFileTarget]] = {}

    @staticmethod
    def _temp_remote_file(final_remote_file: str, run_id: str) -> str:
        if "/" in final_remote_file:
            directory, filename = final_remote_file.rsplit("/", 1)
            return f"{directory}/.{filename}.tram-{run_id}.tmp"
        return f".{final_remote_file}.tram-{run_id}.tmp"

    @staticmethod
    def _cleanup_stale_temp_files(sftp, final_remote_file: str, *, keep_path: str | None = None) -> None:
        if "/" in final_remote_file:
            directory, filename = final_remote_file.rsplit("/", 1)
        else:
            directory, filename = "", final_remote_file
        prefix = f".{filename}.tram-"
        suffix = ".tmp"
        listing_path = directory or "."
        try:
            names = sftp.listdir(listing_path)
        except Exception:
            return
        for name in names:
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            candidate = f"{directory}/{name}" if directory else name
            if keep_path is not None and candidate == keep_path:
                continue
            try:
                sftp.remove(candidate)
            except Exception:
                continue

    def _connect(self):
        try:
            import paramiko
            transport = paramiko.Transport((self.host, self.port))
            if self.private_key_path:
                key = paramiko.RSAKey.from_private_key_file(self.private_key_path)
                transport.connect(username=self.username, pkey=key)
            else:
                transport.connect(username=self.username, password=self.password)
            sftp = paramiko.SFTPClient.from_transport(transport)
            return transport, sftp
        except Exception as exc:
            raise SinkError(f"SFTP connect failed to {self.host}:{self.port} — {exc}") from exc

    def _next_remote_file(
        self,
        meta: dict,
        *,
        now,
        state_key: tuple[tuple[str, str], ...],
    ) -> tuple[str, FilePartState]:
        part_index = self._part_counters.get(state_key, 0) + 1
        if part_index > self.max_index:
            raise SinkError(
                f"SFTP sink exceeded max_index={self.max_index}; "
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
        return f"{self.remote_path}/{filename}", state

    def write(self, data: bytes, meta: dict) -> None:
        transport, sftp = self._connect()
        try:
            # Ensure remote directory exists
            try:
                sftp.stat(self.remote_path)
            except FileNotFoundError:
                sftp.mkdir(self.remote_path)

            serializer_type = str(meta.get("serializer_type", "json"))
            serializer_config = dict(meta.get("serializer_config", {}))
            record_count = int(meta.get("output_record_count", 0))
            now = utc_now()
            state_key = file_state_key(self.filename_template, meta=meta)
            stage_output = should_stage_file_output(meta, serializer_type)
            staged_source_key = source_unit_key(meta) if stage_output else None

            if self.file_mode == "append":
                state = self._states.get(state_key)
                current_remote_file = self._current_remote_files.get(state_key)
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
                    self._current_remote_files.pop(state_key, None)
                    state = None
                    current_remote_file = None
                is_new_file = state is None or current_remote_file is None
                if is_new_file:
                    current_remote_file, state = self._next_remote_file(
                        meta,
                        now=now,
                        state_key=state_key,
                    )
                    if stage_output:
                        final_remote_file = current_remote_file
                        current_remote_file = self._temp_remote_file(
                            final_remote_file,
                            str(meta.get("run_id", "") or "run"),
                        )
                        self._cleanup_stale_temp_files(
                            sftp,
                            final_remote_file,
                            keep_path=current_remote_file,
                        )
                        staged_targets = self._staged_targets.setdefault(staged_source_key, {})
                        staged_targets[state_key] = StagedFileTarget(
                            state_key=state_key,
                            temp_path=current_remote_file,
                            final_path=final_remote_file,
                        )
                    self._current_remote_files[state_key] = current_remote_file
                    self._states[state_key] = state
                remote_file = current_remote_file
                payload = prepare_payload_for_append(
                    data,
                    serializer_type=serializer_type,
                    serializer_config=serializer_config,
                    is_new_file=is_new_file,
                )
                if not payload:
                    return
                with sftp.open(remote_file, "ab") as fh:
                    fh.write(payload)
                assert state is not None
                state.records_written += record_count
                state.bytes_written += len(payload)
            else:
                remote_file, state = self._next_remote_file(meta, now=now, state_key=state_key)
                if stage_output:
                    final_remote_file = remote_file
                    remote_file = self._temp_remote_file(
                        final_remote_file,
                        str(meta.get("run_id", "") or "run"),
                    )
                    self._cleanup_stale_temp_files(
                        sftp,
                        final_remote_file,
                        keep_path=remote_file,
                    )
                    staged_targets = self._staged_targets.setdefault(staged_source_key, {})
                    staged_targets[state_key] = StagedFileTarget(
                        state_key=state_key,
                        temp_path=remote_file,
                        final_path=final_remote_file,
                    )
                    try:
                        sftp.stat(remote_file)
                        existing = True
                    except Exception:
                        existing = False
                    payload = prepare_payload_for_append(
                        data,
                        serializer_type=serializer_type,
                        serializer_config=serializer_config,
                        is_new_file=not existing,
                    )
                    if payload:
                        with sftp.open(remote_file, "ab") as fh:
                            fh.write(payload)
                        state.records_written += record_count
                        state.bytes_written += len(payload)
                else:
                    with sftp.open(remote_file, "wb") as fh:
                        fh.write(data)
                self._current_remote_files[state_key] = remote_file
                self._states[state_key] = state

            logger.info(
                "Wrote file to SFTP",
                extra={
                    "host": self.host,
                    "file": remote_file,
                    "bytes": len(data),
                },
            )
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Error writing to SFTP {self.host}: {exc}") from exc
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                transport.close()
            except Exception:
                pass

    def finalize_source(self, meta: dict, success: bool) -> None:
        source_key = source_unit_key(meta)
        staged_targets = self._staged_targets.pop(source_key, {})
        if not staged_targets:
            return

        transport, sftp = self._connect()
        try:
            for state_key, target in staged_targets.items():
                temp_remote_file = target.temp_path
                final_remote_file = target.final_path

                try:
                    if success:
                        try:
                            if hasattr(sftp, "posix_rename"):
                                sftp.posix_rename(temp_remote_file, final_remote_file)
                            else:
                                try:
                                    sftp.remove(final_remote_file)
                                except Exception:
                                    pass
                                sftp.rename(temp_remote_file, final_remote_file)
                        except Exception as exc:
                            raise SinkError(
                                f"Error finalizing SFTP sink output for {final_remote_file}: {exc}"
                            ) from exc
                    else:
                        try:
                            sftp.remove(temp_remote_file)
                        except Exception:
                            pass
                    self._states.pop(state_key, None)
                    self._current_remote_files.pop(state_key, None)
                    self._part_counters.pop(state_key, None)
                except SinkError:
                    raise
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                transport.close()
            except Exception:
                pass
