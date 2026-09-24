"""HTTP-backed processed-file tracker for worker mode (GH #54).

Workers are stateless by design (no per-worker DB), so ``skip_processed``
dedup state lives in the manager's ``processed_files`` table. This facade
implements the same ``is_processed`` / ``mark_processed`` interface the
executor injects into file sources, routing each call to the manager's
internal API — the F.1 ``HttpTransformStateStore`` pattern (a worker run
cannot exist without a manager dispatch, so the tracker rides the same
availability envelope). When the manager is unreachable the call fails loud
(ERROR + degradation note on the run) and the file is reprocessed — never
blocking a run, never silently dropping the warning.

Review C2 (v1.4.7): the tracker is now batch-friendly instead of one
round-trip per file:

- One ``httpx.Client`` is reused for the whole tracker lifetime (one per
  run — safe lifecycle), instead of a fresh client per call.
- Checks are batched via ``prefetch_many``: the list-based file sources
  (local, sftp, ftp, s3, gcs, azure_blob) send the run's whole candidate
  list in ONE check request and the per-file ``is_processed`` calls then
  hit the local cache with zero network. Sources without a materialized
  list (e.g. CORBA's single invocation) fall back to a synchronous
  per-file check, which is also cached.
- Marks are buffered and flushed in ONE batched request at the end of the
  run (``flush()``/``close()``), sub-batched at ``_BATCH_SIZE`` entries for
  runs larger than the bound.
- After the FIRST connection failure the tracker records the degradation
  once and short-circuits: every remaining file is treated as unprocessed
  (reprocess fallback) with no further network calls, bounding a
  blackholed manager to ~one timeout per run, not per file.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

# Match the stats-callback / run-complete posture: a short bounded timeout so
# an unreachable manager can never stall a worker run.
_TRACKER_TIMEOUT = 10.0

# C2: bounded sub-batch for check/mark requests — a run larger than this
# splits its files into multiple requests instead of one unbounded payload
# (the manager side caps each request at the same order of magnitude).
_BATCH_SIZE = 500

# GH #54: recorded on the run (like the v1.4.6 disabled note) when the
# manager's tracker is unreachable, so the manager's run_history row carries
# it via the run-complete payload errors.
_SKIP_PROCESSED_UNAVAILABLE_NOTE = (
    "skip_processed: true requested but the manager's processed-file tracker "
    "was unreachable — already-seen files will be reprocessed on every run; "
    "duplicate records possible"
)


def _chunks(items: list, size: int):
    """Yield *items* in bounded sub-batches of *size* entries."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


class HttpFileTracker:
    """Worker-side ProcessedFileTracker facade backed by the manager's internal API.

    Interface matches ``ProcessedFileTracker``
    (``tram/persistence/file_tracker.py``): ``is_processed`` and
    ``mark_processed``, called by the file connectors at per-file granularity,
    plus ``prefetch_many`` for batched checks, ``flush``/``close`` to emit
    buffered marks. ``transport`` is a test seam (``httpx.MockTransport`` /
    ``httpx.ASGITransport``), the same pattern as ``HttpTransformStateStore``.
    """

    def __init__(
        self,
        manager_url: str,
        api_key: str = "",
        transport: httpx.BaseTransport | None = None,
        on_degradation: Callable[[str], None] | None = None,
    ) -> None:
        self.manager_url = manager_url.rstrip("/")
        self.api_key = api_key
        self._transport = transport
        self._on_degradation = on_degradation
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()
        # First failure per run escalates to ERROR + note; later failures stay
        # WARNING so a dead manager over a large directory cannot flood logs.
        self._degraded = False
        self._degradation_recorded = False
        # C2: batched-check cache + buffered marks.
        self._check_cache: dict[tuple[str, str], bool] = {}
        self._pending_marks: list[dict] = []

    # ── Client lifecycle ────────────────────────────────────────────────────

    def _client_or_create(self) -> httpx.Client:
        """The tracker's shared client (created lazily on first use)."""
        if self._client is None:
            self._client = httpx.Client(timeout=_TRACKER_TIMEOUT, transport=self._transport)
        return self._client

    def _headers(self) -> dict[str, str] | None:
        return {"X-API-Key": self.api_key} if self.api_key else None

    def close(self) -> None:
        """Flush buffered marks and release the shared client (run end)."""
        try:
            self.flush()
        finally:
            client, self._client = self._client, None
            if client is not None:
                client.close()

    # ── Degradation (short-circuit) ─────────────────────────────────────────

    def _fail_loud(self, op: str, pipeline_name: str, filepath: str, exc: Exception) -> None:
        """GH #54 fallback: manager tracker unreachable — never silent, never blocking.

        The FIRST failure records the degradation once and short-circuits the
        rest of the run (C2): every remaining check returns ``False`` and
        buffered marks are dropped, so a blackholed manager costs ~one
        timeout per run instead of one timeout per file.
        """
        extra = {
            "pipeline": pipeline_name,
            "filepath": filepath,
            "op": op,
            "error": str(exc),
        }
        with self._lock:
            if self._degradation_recorded:
                logger.warning(
                    "processed-file tracker still unreachable — skip_processed "
                    "remains unhonored; already-seen files will be reprocessed "
                    "(duplicate records possible)",
                    extra=extra,
                )
                return
            self._degradation_recorded = True
            self._degraded = True
        logger.error(
            "skip_processed cannot be honored in worker mode — the manager's "
            "processed-file tracker is unreachable; already-seen files will "
            "be reprocessed (duplicate records possible)",
            extra=extra,
        )
        if self._on_degradation is not None:
            self._on_degradation(_SKIP_PROCESSED_UNAVAILABLE_NOTE)

    # ── Checks ─────────────────────────────────────────────────────────────

    def prefetch_many(
        self, pipeline_name: str, source_key: str, filepaths: list[str]
    ) -> None:
        """Batch-check a list of files in ONE request (C2).

        The list-based file sources call this once per run with their whole
        candidate list; the per-file ``is_processed`` calls that follow hit
        the populated cache with zero network. Oversized lists are sent in
        bounded sub-batches of ``_BATCH_SIZE``. A failure here degrades the
        run (short-circuit) — every file is then treated as unprocessed and
        reprocessed, which is the documented fail-loud fallback.
        """
        if not filepaths:
            return
        if self._degraded:
            return
        url = f"{self.manager_url}/api/internal/processed-files/check"
        for batch in _chunks(list(filepaths), _BATCH_SIZE):
            payload = {
                "pipeline_name": pipeline_name,
                "files": [{"source_key": source_key, "filepath": fp} for fp in batch],
            }
            try:
                resp = self._client_or_create().post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                self._fail_loud("check", pipeline_name, batch[0], exc)
                return
            processed = data.get("processed") or []
            for fp, is_done in zip(batch, processed):
                self._check_cache[(source_key, fp)] = bool(is_done)

    def is_processed(self, pipeline_name: str, source_key: str, filepath: str) -> bool:
        if self._degraded:
            # Short-circuited: treat as not processed so the file is
            # reprocessed (the documented fail-loud fallback).
            return False
        cached = self._check_cache.get((source_key, filepath))
        if cached is not None:
            return cached
        # No prefetched answer (source without a materialized file list, or a
        # file that appeared after the prefetch): synchronous per-file check,
        # also cached.
        url = f"{self.manager_url}/api/internal/processed-files/check"
        payload = {
            "pipeline_name": pipeline_name,
            "files": [{"source_key": source_key, "filepath": filepath}],
        }
        try:
            resp = self._client_or_create().post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self._fail_loud("check", pipeline_name, filepath, exc)
            return False
        processed = data.get("processed") or []
        result = bool(processed[0]) if processed else False
        self._check_cache[(source_key, filepath)] = result
        return result

    # ── Marks (buffered, flushed per run) ──────────────────────────────────

    def mark_processed(self, pipeline_name: str, source_key: str, filepath: str) -> None:
        """Buffer a mark; the run's marks are flushed in ONE batched request.

        Buffering trades crash-durability for batching (C2): a worker crash
        before ``flush()`` loses the run's unflushed marks, so those files are
        reprocessed by the next run — the documented at-least-once fallback.
        Marks are flushed automatically when the buffer reaches ``_BATCH_SIZE``
        and explicitly at run end via ``flush()``/``close()``.
        """
        if self._degraded:
            return
        self._pending_marks.append(
            {
                "pipeline_name": pipeline_name,
                "source_key": source_key,
                "filepath": filepath,
            }
        )
        if len(self._pending_marks) >= _BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        """Send all buffered marks to the manager in batched requests (C2).

        Best-effort and non-blocking: a failure records the degradation once
        and short-circuits the rest of the run — never raises.
        """
        if not self._pending_marks or self._degraded:
            return
        pending, self._pending_marks = self._pending_marks, []
        url = f"{self.manager_url}/api/internal/processed-files/mark"
        for batch in _chunks(pending, _BATCH_SIZE):
            pipeline_name = batch[0]["pipeline_name"]
            payload = {
                "pipeline_name": pipeline_name,
                "files": [
                    {"source_key": entry["source_key"], "filepath": entry["filepath"]}
                    for entry in batch
                ],
            }
            try:
                resp = self._client_or_create().post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
            except Exception as exc:
                self._fail_loud("mark", pipeline_name, batch[0]["filepath"], exc)
                return
