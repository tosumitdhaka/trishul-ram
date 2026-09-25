"""SFTP source connector — reads files from a remote SFTP server."""

from __future__ import annotations

import fnmatch
import logging
import time
from collections.abc import Iterator

from tram.connectors.config_utils import cfg_bool, cfg_int
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("sftp")
class SFTPSource(BaseSource):
    """Read files from a remote SFTP server.

    Operates in batch mode: lists matching files, reads them all, then returns.
    Supports moving or deleting files after reading.

    File-done semantics (F.2 part 1):
        file_stability_seconds (int, default 0)  >0: only read files whose
            size+mtime are unchanged across two scans separated by this many
            seconds (0 = off). Protects against reading half-written files.
        file_min_age_seconds   (int, default 0)  >0: skip files whose mtime is
            younger than this (0 = off). Cheap write-in-progress gate.
        file_done_suffix       (str, optional)   When set (e.g. ".done"), only
            collect files whose name ends with the suffix; the suffix is
            stripped from the ``source_filename`` metadata so ``{source_stem}``
            / ``{source_suffix}`` sink tokens do not carry the marker.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = cfg_int(config, "port", 22)
        self.username: str = config["username"]
        self.password: str | None = config.get("password")
        self.private_key_path: str | None = config.get("private_key_path")
        self.remote_path: str = config["remote_path"].rstrip("/")
        self.file_pattern: str = config.get("file_pattern", "*")
        self.move_after_read: str | None = config.get("move_after_read")
        self.delete_after_read: bool = cfg_bool(config, "delete_after_read", False)
        self.skip_processed: bool = cfg_bool(config, "skip_processed", False)
        self.read_chunk_bytes: int = cfg_int(config, "read_chunk_bytes", 0)
        self.file_stability_seconds: int = cfg_int(config, "file_stability_seconds", 0)
        self.file_min_age_seconds: int = cfg_int(config, "file_min_age_seconds", 0)
        self.file_done_suffix: str | None = config.get("file_done_suffix")
        self._pipeline_name: str = config.get("_pipeline_name", "")
        self._file_tracker = config.get("_file_tracker")
        self._clock_skew_warned: bool = False

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
        self._transport = transport
        self._sftp = sftp
        try:
            try:
                all_files = sftp.listdir(self.remote_path)
            except Exception as exc:
                raise SourceError(f"SFTP listdir failed: {exc}") from exc

            matching = [
                f for f in all_files
                if fnmatch.fnmatch(f, self.file_pattern)
            ]
            if self.file_done_suffix:
                matching = [f for f in matching if f.endswith(self.file_done_suffix)]
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

            source_key = f"sftp:{self.host}:{self.remote_path}"
            stable = list(self._stable_candidates(matching, sftp))
            # C2 (v1.4.7): batch the run's whole candidate list into one
            # check request when the tracker supports it (worker-mode
            # HttpFileTracker); the per-file is_processed calls below then
            # hit the local cache.
            prefetch = getattr(self._file_tracker, "prefetch_many", None)
            if self.skip_processed and prefetch is not None:
                prefetch(
                    self._pipeline_name, source_key,
                    [f"{self.remote_path}/{fn}" for fn in stable],
                )
            for filename in stable:
                remote_file = f"{self.remote_path}/{filename}"
                meta_filename = self._strip_done_suffix(filename)

                if self.skip_processed and self._file_tracker:
                    if self._file_tracker.is_processed(self._pipeline_name, source_key, remote_file):
                        logger.info(
                            "Skipping already-processed SFTP file",
                            extra={"filepath": remote_file},
                        )
                        continue

                try:
                    with sftp.open(remote_file, "rb") as fh:
                        if self.read_chunk_bytes > 0:
                            chunk_meta = {
                                "source_filename": meta_filename,
                                "source_path": remote_file,
                                "source_host": self.host,
                            }
                            chunk_index = 0
                            while True:
                                chunk = fh.read(self.read_chunk_bytes)
                                if not chunk:
                                    break
                                logger.debug(
                                    "Read file chunk",
                                    extra={"filepath": remote_file, "chunk": chunk_index, "bytes": len(chunk)},
                                )
                                yield chunk, {**chunk_meta, "chunk_index": chunk_index}
                                chunk_index += 1
                        else:
                            content = fh.read()
                            logger.debug(
                                "Read file",
                                extra={"filepath": remote_file, "bytes": len(content)},
                            )
                            yield content, {
                                "source_filename": meta_filename,
                                "source_path": remote_file,
                                "source_host": self.host,
                            }
                except SourceError:
                    raise
                except Exception as exc:
                    raise SourceError(f"Error reading {remote_file}: {exc}") from exc
        except SourceError:
            raise
        # NOTE: the sftp/transport connection is intentionally NOT closed here.
        # finalize() still needs it after the executor drains the file's
        # chunks, so it stays open until the executor calls source.close().

    def finalize(self, meta: dict, *, success: bool) -> None:
        """Move/delete and mark the file once its chunks were fully processed.

        Invoked by the executor after every chunk yielded for this file has
        been drained from the worker pool, so the file is only moved/deleted/
        marked after its data was actually written — never while writes are
        still pending. On ``success=False`` the file is left untouched.
        """
        if not success:
            return
        remote_file = str(meta.get("source_path", ""))
        filename = str(meta.get("source_filename", ""))
        if not remote_file or self._sftp is None:
            return
        self._post_read(self._sftp, remote_file, filename)
        if self.skip_processed and self._file_tracker:
            source_key = f"sftp:{self.host}:{self.remote_path}"
            self._file_tracker.mark_processed(self._pipeline_name, source_key, remote_file)

    def close(self) -> None:
        """Close the connection held open across read()/finalize()."""
        for conn in (getattr(self, "_sftp", None), getattr(self, "_transport", None)):
            if conn is None:
                continue
            try:
                conn.close()
            except Exception:
                pass
        self._sftp = None
        self._transport = None

    def _stable_candidates(self, filenames: list[str], sftp) -> list[str]:
        """File-done eligibility filter (F.2 part 1).

        Batch sources list once per run, so the stability guard is implemented
        as a two-phase scan *within* the run: stat every candidate, wait
        ``file_stability_seconds``, then stat again; only files whose
        (size, mtime) are identical across the two observations are eligible.
        A file skipped here (still being written by the NE) is simply picked up
        by a later run. Interval-scheduled pipelines get the natural re-scan
        across runs on top of this in-run guard.
        """
        if self.file_stability_seconds <= 0 and self.file_min_age_seconds <= 0:
            return list(filenames)

        observed: dict[str, tuple[int, float]] = {}
        for name in filenames:
            remote = f"{self.remote_path}/{name}"
            try:
                st = sftp.stat(remote)
            except FileNotFoundError:
                continue
            if self.file_min_age_seconds > 0:
                age = time.time() - st.st_mtime
                if age < 0:
                    # Negative age = the server's clock is ahead of the
                    # manager's (mtime in the future). Two clocks, one gate:
                    # without clamping, a skewed server starves every file —
                    # each looks perpetually young. Clamp the age to 0 (a
                    # future-mtime file is old from the server's perspective,
                    # so treat it as eligible) and warn once per source
                    # instance, not per file.
                    if not self._clock_skew_warned:
                        self._clock_skew_warned = True
                        logger.warning(
                            "SFTP server clock is ahead of the manager by %.0f s — "
                            "future-mtime files are treated as eligible for the min-age gate",
                            -age,
                            extra={"host": self.host, "path": self.remote_path},
                        )
                elif age < self.file_min_age_seconds:
                    continue
            observed[remote] = (st.st_size, st.st_mtime)

        if self.file_stability_seconds <= 0:
            return [name for name in filenames if f"{self.remote_path}/{name}" in observed]

        time.sleep(self.file_stability_seconds)
        stable = []
        for name in filenames:
            remote = f"{self.remote_path}/{name}"
            if remote not in observed:
                continue
            try:
                st = sftp.stat(remote)
            except FileNotFoundError:
                continue
            if (st.st_size, st.st_mtime) == observed[remote]:
                stable.append(name)
        return stable

    def _strip_done_suffix(self, name: str) -> str:
        if self.file_done_suffix and name.endswith(self.file_done_suffix):
            return name[: -len(self.file_done_suffix)]
        return name

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
