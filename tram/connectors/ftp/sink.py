"""FTP sink connector — writes files to a remote FTP server."""

from __future__ import annotations

import io
import logging
import threading

from tram.connectors.config_utils import cfg_bool, cfg_int
from tram.connectors.file_sink_common import render_filename, utc_now
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("ftp")
class FTPSink(BaseSink):
    """Write data to a remote FTP server.

    Filename is generated from a template supporting tokens:
    - ``{pipeline}``        — pipeline name (from meta or config)
    - ``{timestamp}``       — UTC file-open timestamp
    - ``{epoch}`` / ``{epoch_m}`` / ``{epoch_ms}`` — UTC file-open epoch seconds / millis
    - ``{part}`` / ``{index}`` — file part number
    - ``{source_filename}`` — original source filename
    - ``{source_stem}`` / ``{source_suffix}`` — derived from source filename
    - ``{source_path}``     — original source path when available

    Uses ftplib (stdlib) — no extra dependencies required.

    The control connection is opened once per run and reused across writes; it
    is closed by :meth:`close` (called by the executor after a run finishes).
    A write failure on a stale connection triggers exactly one reconnect
    before surfacing the error (review D7).
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = cfg_int(config, "port", 21)
        self.username: str = config["username"]
        self.password: str = config["password"]
        self.remote_path: str = config.get("remote_path", "/").rstrip("/") or "/"
        self.filename_template: str = config.get(
            "filename_template", "{pipeline}_{timestamp}.bin"
        )
        self.passive: bool = cfg_bool(config, "passive", True)
        # Run-scoped pooled connection (review D7).
        self._ftp = None
        self._conn_lock = threading.Lock()

    def _connect(self):
        """Return the pooled FTP connection, creating it on first use.

        Cached for the sink's lifetime so chunk-heavy runs stop paying a full
        TCP + login handshake per write (review D7). Tests that patch
        ``ftplib.FTP`` construct the connection through the mock as before.
        """
        with self._conn_lock:
            if self._ftp is not None:
                return self._ftp
            import ftplib
            try:
                ftp = ftplib.FTP()
                ftp.connect(self.host, self.port)
                ftp.login(self.username, self.password)
                if self.passive:
                    ftp.set_pasv(True)
            except Exception as exc:
                raise SinkError(f"FTP connect failed to {self.host}:{self.port} — {exc}") from exc
            self._ftp = ftp
            return ftp

    def _disconnect(self) -> None:
        """Drop the pooled connection (if any). Idempotent — safe for close()."""
        with self._conn_lock:
            ftp = self._ftp
            self._ftp = None
            if ftp is not None:
                try:
                    ftp.quit()
                except Exception:
                    pass

    def _render_filename(self, meta: dict) -> str:
        return render_filename(
            self.filename_template,
            opened_at=utc_now(),
            part_index=1,
            max_index=1,
            meta=meta,
        )

    def write(self, data: bytes, meta: dict) -> None:
        try:
            ftp = self._connect()
            self._write_once(data, meta, ftp)
        except SinkError:
            raise
        except Exception as exc:
            # Stale-connection guard (review D7): a pooled control connection
            # the server closed while idle fails mid-transfer. Drop it and
            # retry the write exactly once on a fresh connection.
            logger.warning(
                "FTP write failed; reconnecting once",
                extra={"host": self.host, "error": str(exc)},
            )
            self._disconnect()
            try:
                ftp = self._connect()
                self._write_once(data, meta, ftp)
            except SinkError:
                raise
            except Exception as exc2:
                raise SinkError(f"Error writing to FTP {self.host}: {exc2}") from exc2

    def _write_once(self, data: bytes, meta: dict, ftp) -> None:
        import ftplib

        # Ensure remote directory exists
        try:
            ftp.cwd(self.remote_path)
        except ftplib.error_perm:
            try:
                ftp.mkd(self.remote_path)
            except ftplib.error_perm:
                pass

        filename = self._render_filename(meta)
        remote_file = f"{self.remote_path}/{filename}"

        buf = io.BytesIO(data)
        ftp.storbinary(f"STOR {remote_file}", buf)

        logger.info(
            "Wrote file to FTP",
            extra={
                "host": self.host,
                "filepath": remote_file,
                "bytes": len(data),
            },
        )

    def close(self) -> None:
        """Release the pooled FTP connection (review D7). Idempotent."""
        self._disconnect()