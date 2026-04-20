"""FTP sink connector — writes files to a remote FTP server."""

from __future__ import annotations

import io
import logging

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
    - ``{epoch}`` / ``{epoch_m}`` — UTC file-open epoch seconds / millis
    - ``{part}`` / ``{index}`` — file part number
    - ``{source_filename}`` — original source filename
    - ``{source_stem}`` / ``{source_suffix}`` — derived from source filename
    - ``{source_path}``     — original source path when available

    Uses ftplib (stdlib) — no extra dependencies required.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 21))
        self.username: str = config["username"]
        self.password: str = config["password"]
        self.remote_path: str = config.get("remote_path", "/").rstrip("/") or "/"
        self.filename_template: str = config.get(
            "filename_template", "{pipeline}_{timestamp}.bin"
        )
        self.passive: bool = bool(config.get("passive", True))

    def _connect(self):
        """Return an open FTP connection."""
        import ftplib
        try:
            ftp = ftplib.FTP()
            ftp.connect(self.host, self.port)
            ftp.login(self.username, self.password)
            if self.passive:
                ftp.set_pasv(True)
            return ftp
        except Exception as exc:
            raise SinkError(f"FTP connect failed to {self.host}:{self.port} — {exc}") from exc

    def _render_filename(self, meta: dict) -> str:
        return render_filename(
            self.filename_template,
            opened_at=utc_now(),
            part_index=1,
            max_index=1,
            meta=meta,
        )

    def write(self, data: bytes, meta: dict) -> None:
        import ftplib
        ftp = self._connect()
        try:
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
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Error writing to FTP {self.host}: {exc}") from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
