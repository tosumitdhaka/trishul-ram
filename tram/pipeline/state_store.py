"""Transform state stores — durable per-pipeline state blobs for stateful transforms.

State placement decision (design F.1 §3.2): a per-pipeline JSON blob in the
manager's ``transform_state`` table, reached either directly through ``TramDB``
(standalone mode, ``DbTransformStateStore``) or via two internal-API calls per
run (worker mode, ``HttpTransformStateStore``). A worker run cannot exist
without a manager dispatch, so the HTTP GET/PUT rides the same availability
envelope as dispatch itself.

Both implementations are best-effort: a load failure degrades to first-sight
(no state) and a save failure is logged and swallowed — cumulative counters
make a lost state update self-healing (the next delta spans a longer interval
correctly), so no transactional protocol is needed (design §3.2c).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

_STATE_TIMEOUT = 10.0


@dataclass
class TransformState:
    """A pipeline's persisted transform-state blob plus its config fingerprint."""

    state: dict
    config_sha256: str = ""


class TransformStateStore(Protocol):
    """Interface for loading/saving a pipeline's transform-state blob."""

    def get(self, pipeline_name: str) -> TransformState | None:
        """Return the pipeline's saved state, or None when absent/unavailable."""
        ...

    def put(
        self,
        pipeline_name: str,
        state: dict,
        config_sha256: str = "",
        run_id: str = "",
    ) -> None:
        """Persist the pipeline's state blob (best-effort; failures logged)."""
        ...


class DbTransformStateStore:
    """Standalone-mode store backed by the manager's ``TramDB`` handle."""

    def __init__(self, db) -> None:
        self._db = db

    def get(self, pipeline_name: str) -> TransformState | None:
        from tram.metrics.registry import TRANSFORM_STATE_IO_TOTAL
        try:
            row = self._db.load_transform_state(pipeline_name)
        except Exception as exc:
            TRANSFORM_STATE_IO_TOTAL.labels(op="get", result="error").inc()
            logger.warning(
                "Transform state load failed — continuing unhydrated",
                extra={"pipeline": pipeline_name, "error": str(exc)},
            )
            return None
        TRANSFORM_STATE_IO_TOTAL.labels(op="get", result="ok").inc()
        if row is None:
            return None
        return TransformState(state=row["state"], config_sha256=row["config_sha256"])

    def put(
        self,
        pipeline_name: str,
        state: dict,
        config_sha256: str = "",
        run_id: str = "",
    ) -> None:
        from tram.metrics.registry import TRANSFORM_STATE_IO_TOTAL
        try:
            self._db.save_transform_state(
                pipeline_name, state, config_sha256, updated_by=run_id
            )
        except Exception as exc:
            TRANSFORM_STATE_IO_TOTAL.labels(op="put", result="error").inc()
            logger.warning(
                "Transform state save failed — counters self-heal over the "
                "longer interval",
                extra={"pipeline": pipeline_name, "error": str(exc)},
            )
            return
        TRANSFORM_STATE_IO_TOTAL.labels(op="put", result="ok").inc()


class HttpTransformStateStore:
    """Worker-mode store backed by the manager's internal API.

    GET/PUT ``/api/internal/transform-state/{pipeline}`` authenticated with the
    shared internal API key (the same auth mode ``run-complete`` and
    ``pipeline-stats`` use). ``transport`` is a test seam (``httpx.MockTransport``).
    """

    def __init__(
        self,
        manager_url: str,
        api_key: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.manager_url = manager_url.rstrip("/")
        self.api_key = api_key
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=_STATE_TIMEOUT, transport=self._transport)

    def _headers(self) -> dict[str, str] | None:
        return {"X-API-Key": self.api_key} if self.api_key else None

    def get(self, pipeline_name: str) -> TransformState | None:
        from tram.metrics.registry import TRANSFORM_STATE_IO_TOTAL
        url = f"{self.manager_url}/api/internal/transform-state/{pipeline_name}"
        try:
            with self._client() as client:
                resp = client.get(url, headers=self._headers())
            if resp.status_code == 404:
                # No row (or flag-off) — a normal "no state" outcome.
                TRANSFORM_STATE_IO_TOTAL.labels(op="get", result="ok").inc()
                return None
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            TRANSFORM_STATE_IO_TOTAL.labels(op="get", result="error").inc()
            logger.warning(
                "Transform state GET failed — continuing unhydrated",
                extra={"pipeline": pipeline_name, "url": url, "error": str(exc)},
            )
            return None
        TRANSFORM_STATE_IO_TOTAL.labels(op="get", result="ok").inc()
        return TransformState(
            state=data.get("state", {}),
            config_sha256=data.get("config_sha256", ""),
        )

    def put(
        self,
        pipeline_name: str,
        state: dict,
        config_sha256: str = "",
        run_id: str = "",
    ) -> None:
        from tram.metrics.registry import TRANSFORM_STATE_IO_TOTAL
        url = f"{self.manager_url}/api/internal/transform-state/{pipeline_name}"
        payload = {"state": state, "config_sha256": config_sha256, "run_id": run_id}
        try:
            with self._client() as client:
                resp = client.put(url, json=payload, headers=self._headers())
                resp.raise_for_status()
        except Exception as exc:
            TRANSFORM_STATE_IO_TOTAL.labels(op="put", result="error").inc()
            logger.warning(
                "Transform state PUT failed — counters self-heal over the "
                "longer interval",
                extra={"pipeline": pipeline_name, "url": url, "error": str(exc)},
            )
            return
        TRANSFORM_STATE_IO_TOTAL.labels(op="put", result="ok").inc()