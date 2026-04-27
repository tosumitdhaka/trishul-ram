"""Worker-side runtime metrics for active pipeline runs."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class PipelineStats:
    """Thread-safe cumulative stats for a single active run."""

    run_id: str
    pipeline_name: str
    schedule_type: str
    records_in: int = 0
    records_out: int = 0
    records_skipped: int = 0
    dlq_count: int = 0
    error_count: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    errors_last_window: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    def increment(
        self,
        *,
        records_in: int = 0,
        records_out: int = 0,
        skipped: int = 0,
        dlq: int = 0,
        bytes_in: int = 0,
        bytes_out: int = 0,
        errors: list[str] | None = None,
    ) -> None:
        with self._lock:
            self.records_in += records_in
            self.records_out += records_out
            self.records_skipped += skipped
            self.dlq_count += dlq
            self.bytes_in += bytes_in
            self.bytes_out += bytes_out
            if errors:
                self.error_count += len(errors)
                self.errors_last_window.extend(errors[-10:])

    def snapshot_and_reset_window(self) -> dict[str, int | list[str]]:
        with self._lock:
            snapshot = {
                "records_in": self.records_in,
                "records_out": self.records_out,
                "records_skipped": self.records_skipped,
                "dlq_count": self.dlq_count,
                "error_count": self.error_count,
                "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out,
                "errors_last_window": list(self.errors_last_window),
            }
            self.errors_last_window.clear()
        return snapshot

    def snapshot(self) -> dict[str, int | list[str]]:
        with self._lock:
            return {
                "records_in": self.records_in,
                "records_out": self.records_out,
                "records_skipped": self.records_skipped,
                "dlq_count": self.dlq_count,
                "error_count": self.error_count,
                "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out,
                "errors_last_window": list(self.errors_last_window),
            }
