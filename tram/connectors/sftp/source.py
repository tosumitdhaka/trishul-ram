"""SFTP source connector — reads files from a remote SFTP server."""

from __future__ import annotations

import fnmatch
import logging
from typing import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("sftp")
class SFTPSource(BaseSource):
    """Read files from a remote SFTP server.

    Operates in batch mode: lists matching files, reads them all, then returns.
    Supports moving or deleting files after reading.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 22))
        self.username: str = config["username"]
        self.password: str | None = config.get("password")
        self.private_key_path: str | None = config.get("private_key_path")
        self.remote_path: str = config["remote_path"].rstrip("/")
        self.file_pattern: str = config.get("file_pattern", "*")
        self.move_after_read: str | None = config.get("move_after_read")
        self.delete_after_read: bool = bool(config.get("delete_after_read", False))

    def _connect(self):
        """Return an open (transport, sftp) pair."""
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
            raise SourceError(f"SFTP connect failed to {self.host}:{self.port} — {exc}") from exc

    def read(self) -> Iterator[tuple[bytes, dict]]:
        transport, sftp = self._connect()
        try:
            try:
                all_files = sftp.listdir(self.remote_path)
            except Exception as exc:
                raise SourceError(f"SFTP listdir failed: {exc}") from exc

            matching = [
                f for f in all_files
                if fnmatch.fnmatch(f, self.file_pattern)
            ]
            logger.info(
                "SFTP source found files",
                extra={
                    "host": self.host,
                    "path": self.remote_path,
                    "pattern": self.file_pattern,
                    "matched": len(matching),
                    "total": len(all_files),
                },
            )

            for filename in matching:
                remote_file = f"{self.remote_path}/{filename}"
                try:
                    with sftp.open(remote_file, "rb") as fh:
                        content = fh.read()
                    logger.debug(
                        "Read file",
                        extra={"filepath": remote_file, "bytes": len(content)},
                    )
                    yield content, {
                        "source_filename": filename,
                        "source_path": remote_file,
                        "source_host": self.host,
                    }
                    self._post_read(sftp, remote_file, filename)
                except SourceError:
                    raise
                except Exception as exc:
                    raise SourceError(f"Error reading {remote_file}: {exc}") from exc
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                transport.close()
            except Exception:
                pass

    def _post_read(self, sftp, remote_file: str, filename: str) -> None:
        """Move or delete file after successful read."""
        if self.move_after_read:
            dest_dir = self.move_after_read.rstrip("/")
            dest = f"{dest_dir}/{filename}"
            try:
                # Ensure destination directory exists
                try:
                    sftp.stat(dest_dir)
                except FileNotFoundError:
                    sftp.mkdir(dest_dir)
                sftp.rename(remote_file, dest)
                logger.debug("Moved file", extra={"from": remote_file, "to": dest})
            except Exception as exc:
                logger.warning("Failed to move file %s: %s", remote_file, exc)
        elif self.delete_after_read:
            try:
                sftp.remove(remote_file)
                logger.debug("Deleted file", extra={"filepath": remote_file})
            except Exception as exc:
                logger.warning("Failed to delete file %s: %s", remote_file, exc)
