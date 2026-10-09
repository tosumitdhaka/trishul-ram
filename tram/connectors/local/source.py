"""Local filesystem source connector."""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

from tram.connectors.config_utils import cfg_bool, cfg_int
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import AckDisposition, BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("local")
class LocalSource(BaseSource):
    """Read files from a local directory.

    Batch mode: lists matching files once, reads them all, returns.

    Config keys:
        path                   (str, required)   Directory to read from.
        file_pattern           (str, default "*") Glob pattern for file matching.
        move_after_read        (str, optional)   Move files here after reading.
        delete_after_read      (bool, default False) Delete files after reading.
        recursive              (bool, default False) Recurse into subdirectories.
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
        self.path = Path(config["path"])
        self.file_pattern: str = config.get("file_pattern", "*")
        self.move_after_read: Path | None = (
            Path(config["move_after_read"]) if config.get("move_after_read") else None
        )
        self.delete_after_read: bool = cfg_bool(config, "delete_after_read", False)
        self.recursive: bool = cfg_bool(config, "recursive", False)
        self.skip_processed: bool = cfg_bool(config, "skip_processed", False)
        self.file_stability_seconds: int = cfg_int(config, "file_stability_seconds", 0)
        self.file_min_age_seconds: int = cfg_int(config, "file_min_age_seconds", 0)
        self.file_done_suffix: str | None = config.get("file_done_suffix")
        self._pipeline_name: str = config.get("_pipeline_name", "")
        self._file_tracker = config.get("_file_tracker")

    def test_connection(self) -> dict:
        import os
        path = self.config.get("path", "")
        if not path:
            raise RuntimeError("No 'path' in config")
        if os.path.exists(path):
            return {"ok": True, "latency_ms": 0, "detail": f"Path exists: {path}"}
        raise RuntimeError(f"Path not found: {path}")

    def read(self) -> Iterator[tuple[bytes, dict]]:
        if not self.path.exists():
            raise SourceError(f"Local source path does not exist: {self.path}")

        glob_fn = self.path.rglob if self.recursive else self.path.glob
        files = sorted(
            f for f in glob_fn(self.file_pattern)
            if f.is_file()
        )
        if self.file_done_suffix:
            files = [f for f in files if f.name.endswith(self.file_done_suffix)]

        logger.info(
            "Local source found files",
            extra={"path": str(self.path), "pattern": self.file_pattern, "matched": len(files)},
        )

        source_key = f"local:{self.path}"
        candidates = list(self._stable_candidates(files))
        # C2 (v1.4.7): batch the run's whole candidate list into one check
        # request when the tracker supports it (worker-mode HttpFileTracker);
        # the per-file is_processed calls below then hit the local cache.
        prefetch = getattr(self._file_tracker, "prefetch_many", None)
        if self.skip_processed and prefetch is not None:
            prefetch(self._pipeline_name, source_key, [str(f) for f in candidates])
        for filepath in candidates:
            fp_str = str(filepath)
            if self.skip_processed and self._file_tracker:
                if self._file_tracker.is_processed(self._pipeline_name, source_key, fp_str):
                    logger.info("Skipping already-processed local file", extra={"filepath": fp_str})
                    continue
            try:
                content = filepath.read_bytes()
                yield content, {
                    "source_filename": self._strip_done_suffix(filepath.name),
                    "source_path": fp_str,
                    # Content fingerprint captured at read time — the identity
                    # of the unit actually processed. Same-path replacement
                    # between read and ack is detected by a fingerprint change
                    # (V18-01 §7 replay identity matrix).
                    "source_fingerprint": hashlib.sha256(content).hexdigest(),
                }
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError(f"Error reading {filepath}: {exc}") from exc

    def _stable_candidates(self, files: list[Path]) -> list[Path]:
        """File-done eligibility filter (F.2 part 1).

        Batch sources list once per run, so the stability guard is implemented
        as a two-phase scan *within* the run: stat every candidate, wait
        ``file_stability_seconds``, then stat again; only files whose
        (size, mtime) are identical across the two observations are eligible.
        A file skipped here (still being written) is simply picked up by a
        later run. Interval-scheduled pipelines get the natural re-scan across
        runs on top of this in-run guard.
        """
        if self.file_stability_seconds <= 0 and self.file_min_age_seconds <= 0:
            return list(files)

        observed: dict[str, tuple[int, float]] = {}
        for filepath in files:
            try:
                st = filepath.stat()
            except FileNotFoundError:
                continue
            if self.file_min_age_seconds > 0:
                if time.time() - st.st_mtime < self.file_min_age_seconds:
                    continue
            observed[str(filepath)] = (st.st_size, st.st_mtime)

        if self.file_stability_seconds <= 0:
            return [f for f in files if str(f) in observed]

        time.sleep(self.file_stability_seconds)
        stable = []
        for filepath in files:
            key = str(filepath)
            if key not in observed:
                continue
            try:
                st = filepath.stat()
            except FileNotFoundError:
                continue
            if (st.st_size, st.st_mtime) == observed[key]:
                stable.append(filepath)
        return stable

    def _strip_done_suffix(self, name: str) -> str:
        if self.file_done_suffix and name.endswith(self.file_done_suffix):
            return name[: -len(self.file_done_suffix)]
        return name

    def finalize(self, meta: dict, *, success: bool) -> None:
        """Non-destructive compatibility hook (V18-01 §6).

        The destructive mark/move/delete moved to :meth:`ack`; ``finalize()``
        is retained as a no-op so unmigrated executor paths can never destroy
        input before a decided delivery outcome (plan C, R10). An incomplete
        file at a ``batch_size`` boundary is therefore never marked done or
        deleted.
        """
        return None

    def source_unit_id(self, meta: dict) -> str | None:
        """Stable replay identity: ``{source_namespace}:{fingerprint}:{unit_position}``.

        Namespace is ``local:{path}`` (mirrors the file-tracker source key);
        the fingerprint is the sha256 of the unit's file content captured at
        read time (``meta["source_fingerprint"]``); the unit position is 0
        because the batch local source reads each file as one unit. Returns
        None when the unit has no readable file identity.
        """
        fp_str = str(meta.get("source_path", "") or "").strip()
        if not fp_str:
            return None
        fingerprint = str(meta.get("source_fingerprint", "") or "").strip()
        if not fingerprint:
            return None
        return f"local:{self.path}:{fingerprint}:0"

    def ack(self, meta: dict, disposition: AckDisposition) -> None:
        """Destructive mark/move/delete for a decided unit (V18-01 §6).

        Called by the executor only for decided units (delivered/filtered/
        dlq/dropped): the file is moved/deleted/marked here and nowhere else.
        An undecided unit (abort, exhausted retry, failed DLQ) is never acked
        and its input survives for replay.
        """
        if disposition not in {
            AckDisposition.DELIVERED,
            AckDisposition.FILTERED,
            AckDisposition.DLQ,
            AckDisposition.DROPPED,
        }:
            return
        fp_str = str(meta.get("source_path", "") or "")
        if not fp_str:
            return
        filepath = Path(fp_str)
        try:
            self._post_read(filepath)
        except Exception as exc:
            raise SourceError(f"Error acknowledging {fp_str}: {exc}") from exc
        if self.skip_processed and self._file_tracker:
            source_key = f"local:{self.path}"
            self._file_tracker.mark_processed(self._pipeline_name, source_key, fp_str)

    def _post_read(self, filepath: Path) -> None:
        dest_name = self._strip_done_suffix(filepath.name)
        if self.move_after_read:
            self.move_after_read.mkdir(parents=True, exist_ok=True)
            dest = self.move_after_read / dest_name
            shutil.move(str(filepath), str(dest))
            logger.debug("Moved file", extra={"from": str(filepath), "to": str(dest)})
        elif self.delete_after_read:
            filepath.unlink()
            logger.debug("Deleted file", extra={"filepath": str(filepath)})
