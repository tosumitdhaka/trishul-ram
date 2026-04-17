"""Manager-side in-memory store of latest worker pipeline stats."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tram.api.routers.internal import PipelineStatsPayload


class StatsStore:
    """Thread-safe store of active run stats keyed by run_id."""

    def __init__(self, interval: int = 30) -> None:
        self._interval = interval
        self._entries: dict[str, PipelineStatsPayload] = {}
        self._lock = threading.Lock()

    def update(self, payload: PipelineStatsPayload) -> None:
        with self._lock:
            self._entries[payload.run_id] = payload

    def remove(self, run_id: str) -> None:
        with self._lock:
            self._entries.pop(run_id, None)

    def get_by_run_id(self, run_id: str) -> PipelineStatsPayload | None:
        with self._lock:
            return self._entries.get(run_id)

    def for_pipeline(self, pipeline_name: str) -> list[PipelineStatsPayload]:
        with self._lock:
            entries = list(self._entries.values())
        return [
            entry for entry in entries
            if entry.pipeline_name == pipeline_name and not self.is_stale(entry)
        ]

    def for_worker(self, worker_id: str) -> list[PipelineStatsPayload]:
        with self._lock:
            entries = list(self._entries.values())
        return [
            entry for entry in entries
            if entry.worker_id == worker_id and not self.is_stale(entry)
        ]

    def all_active(self) -> list[PipelineStatsPayload]:
        with self._lock:
            entries = list(self._entries.values())
        return [entry for entry in entries if not self.is_stale(entry)]

    def is_stale(self, entry: PipelineStatsPayload, interval: int | None = None) -> bool:
        stale_after = timedelta(seconds=(interval or self._interval) * 3)
        timestamp = entry.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return datetime.now(UTC) - timestamp > stale_after
