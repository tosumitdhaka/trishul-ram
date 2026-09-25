"""SFTP sink connector — writes files to a remote SFTP server."""

from __future__ import annotations

import logging
import threading

from tram.connectors.config_utils import cfg_int
from tram.connectors.file_sink_common import (
    RollingWriter,
    SftpRollingBackend,
    source_unit_key,
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
    - ``{epoch_m}`` / ``{epoch_ms}`` — UTC file-open epoch milliseconds
    - ``{part}`` / ``{index}`` — rolling file part number
    - ``{source_filename}`` — original source filename (from meta)

    The transport is opened once per run and reused across writes; it is
    closed by :meth:`close` (called by the executor after a run finishes). A
    write failure on a stale connection triggers exactly one reconnect before
    surfacing the error (review D7). Rolling/staging/partition bookkeeping is
    shared with LocalSink via :class:`RollingWriter` (review E3).
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = cfg_int(config, "port", 22)
        self.username: str = config["username"]
        self.password: str | None = config.get("password")
        self.private_key_path: str | None = config.get("private_key_path")
        self.remote_path: str = config["remote_path"].rstrip("/")
        self._writer = RollingWriter(
            filename_template=config.get("filename_template", "{pipeline}_{timestamp}.bin"),
            file_mode=str(config.get("file_mode", "append")),
            max_records=config.get("max_records"),
            max_time=config.get("max_time"),
            max_bytes=config.get("max_bytes"),
            max_index=cfg_int(config, "max_index", 99999),
            sink_name="SFTP",
            logger=logger,
        )
        self._backend = SftpRollingBackend(self.remote_path)
        # Legacy attribute the executor's partition logic reads; reflects the
        # writer's effective template (review E3).
        self.filename_template = self._writer.filename_template
        # Run-scoped pooled connection (review D7): created on first use,
        # reused across writes/finalize, closed by close().
        self._transport = None
        self._sftp = None
        self._conn_lock = threading.Lock()

    def _connect(self):
        """Return the pooled (transport, sftp) pair, creating it on first use.

        The connection is cached for the sink's lifetime so chunk-heavy runs
        stop paying a full TCP + SSH handshake per write (review D7). Tests
        that patch ``_connect`` bypass the cache and keep working unchanged.
        """
        with self._conn_lock:
            if self._transport is not None and self._sftp is not None:
                return self._transport, self._sftp
            try:
                import paramiko
                transport = paramiko.Transport((self.host, self.port))
                if self.private_key_path:
                    key = paramiko.RSAKey.from_private_key_file(self.private_key_path)
                    transport.connect(username=self.username, pkey=key)
                else:
                    transport.connect(username=self.username, password=self.password)
                sftp = paramiko.SFTPClient.from_transport(transport)
            except Exception as exc:
                raise SinkError(f"SFTP connect failed to {self.host}:{self.port} — {exc}") from exc
            self._transport = transport
            self._sftp = sftp
            return transport, sftp

    def _disconnect(self) -> None:
        """Drop the pooled connection (if any). Idempotent — safe to call
        repeatedly and from close() (which the executor may call more than once)."""
        with self._conn_lock:
            for conn in (self._sftp, self._transport):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._sftp = None
            self._transport = None

    def write(self, data: bytes, meta: dict) -> None:
        try:
            _transport, sftp = self._connect()
            dest = self._writer.write(data, meta, backend=self._backend, handle=sftp)
        except SinkError:
            raise
        except Exception as exc:
            # Stale-connection guard (review D7): a pooled transport whose
            # session the server closed while idle fails mid-write. Drop it
            # and retry the write exactly once on a fresh connection.
            logger.warning(
                "SFTP write failed; reconnecting once",
                extra={"host": self.host, "error": str(exc)},
            )
            self._disconnect()
            try:
                _transport, sftp = self._connect()
                dest = self._writer.write(data, meta, backend=self._backend, handle=sftp)
            except SinkError:
                raise
            except Exception as exc2:
                raise SinkError(f"Error writing to SFTP {self.host}: {exc2}") from exc2
        if dest is not None:
            logger.info(
                "Wrote file to SFTP",
                extra={
                    "host": self.host,
                    "file": dest,
                    "bytes": len(data),
                },
            )

    def finalize_source(self, meta: dict, success: bool) -> None:
        source_key = source_unit_key(meta)
        if not self._writer.has_staged_targets(source_key):
            return
        try:
            _transport, sftp = self._connect()
            self._writer.finalize_source(source_key, backend=self._backend, handle=sftp, success=success)
        except SinkError:
            raise
        except Exception as exc:
            # Same stale-connection guard as write(): one reconnect attempt.
            logger.warning(
                "SFTP finalize failed; reconnecting once",
                extra={"host": self.host, "error": str(exc)},
            )
            self._disconnect()
            try:
                _transport, sftp = self._connect()
                self._writer.finalize_source(source_key, backend=self._backend, handle=sftp, success=success)
            except SinkError:
                raise
            except Exception as exc2:
                raise SinkError(f"Error finalizing SFTP sink output: {exc2}") from exc2

    def close(self) -> None:
        """Release the pooled SFTP connection (review D7). Idempotent."""
        self._disconnect()