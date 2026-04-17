"""Pipeline execution context and result dataclasses."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class PipelineRunContext:
    """Mutable context passed through a single pipeline execution.

    All counter mutations are protected by an internal lock so the context is
    safe to share across multiple worker threads (thread_workers > 1).
    """

    pipeline_name: str
    # Full UUID to avoid birthday-paradox collisions on high-frequency pipelines.
    # A truncated 8-char hex ID has only ~4 billion values; at 1000 runs/day the
    # collision probability becomes material after ~65 000 runs.
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    records_in: int = 0
    records_out: int = 0
    records_skipped: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    dlq_count: int = 0
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Not a dataclass field — invisible to from_context / to_dict
        object.__setattr__(self, "_lock", threading.Lock())

    # ── Thread-safe counter helpers ──────────────────────────────────────────

    def inc_records_in(self, n: int) -> None:
        with self._lock:  # type: ignore[attr-defined]
            self.records_in += n

    def inc_records_out(self, n: int) -> None:
        with self._lock:  # type: ignore[attr-defined]
            self.records_out += n

    def inc_records_skipped(self, n: int) -> None:
        with self._lock:  # type: ignore[attr-defined]
            self.records_skipped += n

    def inc_bytes_in(self, n: int) -> None:
        with self._lock:  # type: ignore[attr-defined]
            self.bytes_in += n

    def inc_bytes_out(self, n: int) -> None:
        with self._lock:  # type: ignore[attr-defined]
            self.bytes_out += n

    def record_error(self, msg: str) -> None:
        """Append an error message and increment records_skipped by 1.

        Use this for per-record transform or sink failures where a single
        record is being abandoned. Do **not** call this and then also call
        ``inc_records_skipped`` for the same record — that would double-count.
        For batch skips (e.g. all sinks filtered out an entire chunk), call
        ``inc_records_skipped(len(records))`` and then ``note_skip(msg)``.
        """
        with self._lock:  # type: ignore[attr-defined]
            self.errors.append(msg)
            self.records_skipped += 1

    def note_skip(self, msg: str) -> None:
        """Append a skip-reason note to errors without incrementing records_skipped.

        Used when the skip counter has already been bumped (e.g. by
        ``inc_records_skipped``) and you only need to record the reason.
        Keeping the two concerns separate prevents double-counting.
        """
        with self._lock:  # type: ignore[attr-defined]
            self.errors.append(msg)

    def record_dlq(self) -> None:
        with self._lock:  # type: ignore[attr-defined]
            self.dlq_count += 1


@dataclass
class RunResult:
    """Immutable result produced at the end of a batch run."""

    run_id: str
    pipeline_name: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    records_in: int
    records_out: int
    records_skipped: int
    bytes_in: int = 0
    bytes_out: int = 0
    error: str | None = None
    dlq_count: int = 0
    node_id: str = ""
    errors: list = field(default_factory=list)  # per-record error strings

    @classmethod
    def from_context(
        cls,
        ctx: PipelineRunContext,
        status: RunStatus,
        error: str | None = None,
    ) -> RunResult:
        return cls(
            run_id=ctx.run_id,
            pipeline_name=ctx.pipeline_name,
            status=status,
            started_at=ctx.started_at,
            finished_at=datetime.now(UTC),
            records_in=ctx.records_in,
            records_out=ctx.records_out,
            records_skipped=ctx.records_skipped,
            bytes_in=ctx.bytes_in,
            bytes_out=ctx.bytes_out,
            error=error,
            dlq_count=ctx.dlq_count,
            errors=list(ctx.errors),
        )

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "pipeline": self.pipeline_name,
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "records_in": self.records_in,
            "records_out": self.records_out,
            "records_skipped": self.records_skipped,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "dlq_count": self.dlq_count,
            "error": self.error,
            "errors": self.errors,
            "node": self.node_id or None,
        }
