"""HTTP-backed processed-file tracker for worker mode (GH #54).

Workers are stateless by design (no per-worker DB), so ``skip_processed``
dedup state lives in the manager's ``processed_files`` table. This facade
implements the same ``is_processed`` / ``mark_processed`` interface the
executor injects into file sources, routing each per-file call to the
manager's internal API — the F.1 ``HttpTransformStateStore`` pattern (a
worker run cannot exist without a manager dispatch, so the tracker rides the
same availability envelope). When the manager is unreachable the call fails
loud (ERROR + degradation note on the run) and the file is reprocessed —
never blocking a run, never silently dropping the warning.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

# Match the stats-callback / run-complete posture: a short bounded timeout so
# an unreachable manager can never stall a worker run.
_TRACKER_TIMEOUT = 10.0

# GH #54: recorded on the run (like the v1.4.6 disabled note) when the
# manager's tracker is unreachable, so the manager's run_history row carries
# it via the run-complete payload errors.
_SKIP_PROCESSED_UNAVAILABLE_NOTE = (
    "skip_processed: true requested but the manager's processed-file tracker "
    "was unreachable — already-seen files will be reprocessed on every run; "
    "duplicate records possible"
)


class HttpFileTracker:
    """Worker-side ProcessedFileTracker facade backed by the manager's internal API.

    Interface matches ``ProcessedFileTracker``
    (``tram/persistence/file_tracker.py``): ``is_processed`` and
    ``mark_processed``, called by the file connectors at per-file granularity
    (one HTTP round-trip per file — never per record). ``transport`` is a test
    seam (``httpx.MockTransport`` / ``httpx.ASGITransport``), the same pattern
    as ``HttpTransformStateStore``.
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
        # First failure per run escalates to ERROR + note; later failures stay
        # WARNING so a dead manager over a large directory cannot flood logs.
        self._degradation_recorded = False

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=_TRACKER_TIMEOUT, transport=self._transport)

    def _headers(self) -> dict[str, str] | None:
        return {"X-API-Key": self.api_key} if self.api_key else None

    def _fail_loud(self, op: str, pipeline_name: str, filepath: str, exc: Exception) -> None:
        """GH #54 fallback: manager tracker unreachable — never silent, never blocking."""
        extra = {
            "pipeline": pipeline_name,
            "filepath": filepath,
            "op": op,
            "error": str(exc),
        }
        if self._degradation_recorded:
            logger.warning(
                "processed-file tracker still unreachable — skip_processed "
                "remains unhonored; already-seen files will be reprocessed "
                "(duplicate records possible)",
                extra=extra,
            )
            return
        self._degradation_recorded = True
        logger.error(
            "skip_processed cannot be honored in worker mode — the manager's "
            "processed-file tracker is unreachable; already-seen files will "
            "be reprocessed (duplicate records possible)",
            extra=extra,
        )
        if self._on_degradation is not None:
            self._on_degradation(_SKIP_PROCESSED_UNAVAILABLE_NOTE)

    def is_processed(self, pipeline_name: str, source_key: str, filepath: str) -> bool:
        url = f"{self.manager_url}/api/internal/processed-files/check"
        payload = {
            "pipeline_name": pipeline_name,
            "files": [{"source_key": source_key, "filepath": filepath}],
        }
        try:
            with self._client() as client:
                resp = client.post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            self._fail_loud("check", pipeline_name, filepath, exc)
            return False
        processed = data.get("processed") or []
        return bool(processed[0]) if processed else False

    def mark_processed(self, pipeline_name: str, source_key: str, filepath: str) -> None:
        url = f"{self.manager_url}/api/internal/processed-files/mark"
        payload = {
            "pipeline_name": pipeline_name,
            "files": [{"source_key": source_key, "filepath": filepath}],
        }
        try:
            with self._client() as client:
                resp = client.post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
        except Exception as exc:
            self._fail_loud("mark", pipeline_name, filepath, exc)