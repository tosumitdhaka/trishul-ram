"""FTP source connector — reads files from a remote FTP server."""

from __future__ import annotations

import fnmatch
import io
import logging
from typing import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("ftp")
class FTPSource(BaseSource):
    """Read files from a remote FTP server.

    Operates in batch mode: lists matching files, reads them all, then returns.
    Supports moving or deleting files after reading.
    Uses ftplib (stdlib) — no extra dependencies required.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 21))
        self.username: str = config["username"]
        self.password: str = config["password"]
        self.remote_path: str = config.get("remote_path", "/").rstrip("/") or "/"
        self.file_pattern: str = config.get("file_pattern", "*")
        self.move_after_read: str | None = config.get("move_after_read")
        self.delete_after_read: bool = bool(config.get("delete_after_read", False))
        self.passive: bool = bool(config.get("passive", True))
        self.skip_processed: bool = bool(config.get("skip_processed", False))
        self._pipeline_name: str = config.get("_pipeline_name", "")
        self._file_tracker = config.get("_file_tracker")

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
            raise SourceError(f"FTP connect failed to {self.host}:{self.port} — {exc}") from exc

    def read(self) -> Iterator[tuple[bytes, dict]]:
        ftp = self._connect()
        try:
            try:
                all_files = ftp.nlst(self.remote_path)
            except Exception as exc:
                raise SourceError(f"FTP nlst failed: {exc}") from exc

            # nlst returns full paths; get basenames for matching
            basenames = [f.rsplit("/", 1)[-1] for f in all_files]
            matching = [
                (full, base)
                for full, base in zip(all_files, basenames)
                if fnmatch.fnmatch(base, self.file_pattern)
            ]
            logger.info(
                "FTP source found files",
                extra={
                    "host": self.host,
                    "path": self.remote_path,
                    "pattern": self.file_pattern,
                    "matched": len(matching),
                    "total": len(all_files),
                },
            )

            source_key = f"ftp:{self.host}:{self.remote_path}"
            for remote_file, filename in matching:
                if self.skip_processed and self._file_tracker:
                    if self._file_tracker.is_processed(self._pipeline_name, source_key, remote_file):
                        logger.info("Skipping already-processed FTP file", extra={"filepath": remote_file})
                        continue
                buf = io.BytesIO()
                try:
                    ftp.retrbinary(f"RETR {remote_file}", buf.write)
                    content = buf.getvalue()
                    logger.debug(
                        "Read file",
                        extra={"filepath": remote_file, "bytes": len(content)},
                    )
                    yield content, {
                        "source_filename": filename,
                        "source_path": remote_file,
                        "source_host": self.host,
                    }
                    self._post_read(ftp, remote_file, filename)
                    if self.skip_processed and self._file_tracker:
                        self._file_tracker.mark_processed(self._pipeline_name, source_key, remote_file)
                except SourceError:
                    raise
                except Exception as exc:
                    raise SourceError(f"Error reading {remote_file}: {exc}") from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                pass

    def _post_read(self, ftp, remote_file: str, filename: str) -> None:
        """Move or delete file after successful read."""
        if self.move_after_read:
            import ftplib
            dest_dir = self.move_after_read.rstrip("/")
            dest = f"{dest_dir}/{filename}"
            try:
                try:
                    ftp.mkd(dest_dir)
                except ftplib.error_perm:
                    pass  # directory already exists
                ftp.rename(remote_file, dest)
                logger.debug("Moved file", extra={"from": remote_file, "to": dest})
            except Exception as exc:
                logger.warning("Failed to move file %s: %s", remote_file, exc)
        elif self.delete_after_read:
            try:
                ftp.delete(remote_file)
                logger.debug("Deleted file", extra={"filepath": remote_file})
            except Exception as exc:
                logger.warning("Failed to delete file %s: %s", remote_file, exc)
