"""Pipeline execution context and result dataclasses."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class PipelineRunContext:
    """Mutable context passed through a single pipeline execution."""

    pipeline_name: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    records_in: int = 0
    records_out: int = 0
    records_skipped: int = 0
    dlq_count: int = 0
    errors: list[str] = field(default_factory=list)

    def record_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.records_skipped += 1

    def record_dlq(self) -> None:
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
    error: Optional[str] = None
    dlq_count: int = 0

    @classmethod
    def from_context(
        cls,
        ctx: PipelineRunContext,
        status: RunStatus,
        error: Optional[str] = None,
    ) -> "RunResult":
        return cls(
            run_id=ctx.run_id,
            pipeline_name=ctx.pipeline_name,
            status=status,
            started_at=ctx.started_at,
            finished_at=datetime.now(timezone.utc),
            records_in=ctx.records_in,
            records_out=ctx.records_out,
            records_skipped=ctx.records_skipped,
            error=error,
            dlq_count=ctx.dlq_count,
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
            "dlq_count": self.dlq_count,
            "error": self.error,
        }
