"""SFTP sink connector — writes files to a remote SFTP server."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("sftp")
class SFTPSink(BaseSink):
    """Write data to a remote SFTP server.

    Filename is generated from a template supporting tokens:
    - ``{pipeline}``        — pipeline name (from meta or config)
    - ``{timestamp}``       — UTC timestamp (ISO format, : replaced with -)
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

    def _render_filename(self, meta: dict) -> str:
        ts = datetime.now(UTC).isoformat().replace(":", "-")
        return self.filename_template.format(
            pipeline=meta.get("pipeline_name", "tram"),
            timestamp=ts,
            source_filename=meta.get("source_filename", "data"),
        )

    def write(self, data: bytes, meta: dict) -> None:
        transport, sftp = self._connect()
        try:
            # Ensure remote directory exists
            try:
                sftp.stat(self.remote_path)
            except FileNotFoundError:
                sftp.mkdir(self.remote_path)

            filename = self._render_filename(meta)
            remote_file = f"{self.remote_path}/{filename}"

            with sftp.open(remote_file, "wb") as fh:
                fh.write(data)

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
