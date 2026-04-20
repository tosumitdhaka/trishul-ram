"""PipelineManager — in-memory registry of loaded pipelines and their state."""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tram.core.context import RunResult
from tram.core.exceptions import PipelineAlreadyExistsError, PipelineNotFoundError
from tram.models.pipeline import PipelineConfig

if TYPE_CHECKING:
    from tram.alerts.evaluator import AlertEvaluator
    from tram.persistence.db import TramDB

logger = logging.getLogger(__name__)

_MAX_RUN_HISTORY = 500  # per-pipeline


class PipelineState:
    """Tracks the live state of a registered pipeline."""

    def __init__(self, config: PipelineConfig, yaml_text: str | None = None) -> None:
        self.config = config
        self.yaml_text: str | None = yaml_text
        self.status: str = "stopped"  # stopped | running | error
        self.registered_at: datetime = datetime.now(UTC)
        self.last_run: datetime | None = None
        self.last_run_status: str | None = None
        self.run_history: deque[RunResult] = deque(maxlen=_MAX_RUN_HISTORY)

    def record_run(self, result: RunResult) -> None:
        self.last_run = result.finished_at
        self.last_run_status = result.status.value
        self.run_history.appendleft(result)

    def to_dict(self) -> dict:
        c = self.config
        return {
            "name": c.name,
            "description": c.description,
            "enabled": c.enabled,
            "status": self.status,
            "schedule_type": c.schedule.type,
            "interval_seconds": c.schedule.interval_seconds,
            "cron_expr": getattr(c.schedule, "cron_expr", None),
            "registered_at": self.registered_at.isoformat(),
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "last_run_status": self.last_run_status,
            "source": {"type": c.source.type} if c.source else None,
            "sinks": [{"type": s.type} for s in c.sinks],
        }

    def to_detail_dict(self) -> dict:
        """Extended detail view — includes full config fields for the UI detail page."""
        c = self.config
        return {
            **self.to_dict(),
            "source": {"type": c.source.type} if c.source else None,
            "sinks": [{"type": s.type} for s in c.sinks],
            "serializer_in": c.serializer_in.type if c.serializer_in else None,
            "transforms": [{"type": t.type} for t in c.transforms],
            "interval_seconds": c.schedule.interval_seconds,
            "cron_expr": getattr(c.schedule, "cron_expr", None),
            "on_error": c.on_error,
            "dlq": {"type": c.dlq.type} if c.dlq else None,
            "parallel_sinks": c.parallel_sinks,
            "yaml": self.yaml_text,
        }


class PipelineManager:
    """Thread-safe registry of pipeline configurations and their live state."""

    def __init__(
        self,
        db: TramDB | None = None,
        alert_evaluator: AlertEvaluator | None = None,
    ) -> None:
        self._pipelines: dict[str, PipelineState] = {}
        self._db = db
        self._alert_evaluator = alert_evaluator

    # ── CRUD ───────────────────────────────────────────────────────────────

    def register(
        self,
        config: PipelineConfig,
        replace: bool = False,
        yaml_text: str | None = None,
    ) -> PipelineState:
        """Register a pipeline configuration."""
        if config.name in self._pipelines and not replace:
            raise PipelineAlreadyExistsError(
                f"Pipeline '{config.name}' is already registered. "
                "Use replace=True or call reload()."
            )
        state = PipelineState(config, yaml_text=yaml_text)
        self._pipelines[config.name] = state
        logger.info("Registered pipeline", extra={"pipeline": config.name})

        # Hydrate last_run / last_run_status from DB so all cluster nodes
        # show correct last-run info regardless of which pod executed the run.
        if self._db:
            recent = self._db.get_runs(pipeline_name=config.name, limit=1)
            if recent:
                state.last_run = recent[0].finished_at
                state.last_run_status = recent[0].status.value

        # Persist version if yaml_text provided
        if yaml_text and self._db:
            self._db.save_pipeline_version(config.name, yaml_text)

        return state

    def deregister(self, name: str) -> None:
        """Deregister a pipeline (caller must stop it first)."""
        if name not in self._pipelines:
            raise PipelineNotFoundError(f"Pipeline '{name}' not found")
        del self._pipelines[name]
        logger.info("Deregistered pipeline", extra={"pipeline": name})

    def get(self, name: str) -> PipelineState:
        if name not in self._pipelines:
            raise PipelineNotFoundError(f"Pipeline '{name}' not found")
        return self._pipelines[name]

    def list_all(self) -> list[PipelineState]:
        return list(self._pipelines.values())

    def exists(self, name: str) -> bool:
        return name in self._pipelines

    # ── State transitions ──────────────────────────────────────────────────

    def set_status(self, name: str, status: str) -> None:
        self.get(name).status = status

    def record_run(self, name: str, result: RunResult) -> None:
        state = self.get(name)
        state.record_run(result)
        if self._db:
            self._db.save_run(result)
        if self._alert_evaluator is not None:
            try:
                self._alert_evaluator.check(result, state.config)
            except Exception as exc:
                logger.error(
                    "Alert evaluator error",
                    extra={"pipeline": name, "error": str(exc)},
                )

    # ── Run history ────────────────────────────────────────────────────────

    def get_runs(
        self,
        pipeline_name: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        from_dt: datetime | None = None,
    ) -> list[RunResult]:
        """Return run history. Uses DB if available (survives restarts)."""
        if self._db:
            return self._db.get_runs(
                pipeline_name=pipeline_name,
                status=status,
                limit=limit,
                offset=offset,
                from_dt=from_dt,
            )

        # In-memory fallback (no persistence configured)
        all_runs: list[RunResult] = []
        for state in self._pipelines.values():
            if pipeline_name and state.config.name != pipeline_name:
                continue
            all_runs.extend(state.run_history)

        all_runs.sort(key=lambda r: r.finished_at, reverse=True)

        if status:
            all_runs = [r for r in all_runs if r.status.value == status]
        if from_dt:
            all_runs = [r for r in all_runs if r.started_at >= from_dt]

        return all_runs[offset: offset + limit]

    def get_run(self, run_id: str) -> RunResult | None:
        if self._db:
            return self._db.get_run(run_id)
        for state in self._pipelines.values():
            for result in state.run_history:
                if result.run_id == run_id:
                    return result
        return None

    # ── Version management ─────────────────────────────────────────────────

    def save_version(self, name: str, yaml_text: str) -> int:
        """Save a pipeline version. Returns version number."""
        if self._db is None:
            raise RuntimeError("Persistence not configured (no TramDB)")
        return self._db.save_pipeline_version(name, yaml_text)

    def get_versions(self, name: str) -> list[dict]:
        """List all saved versions for a pipeline."""
        if self._db is None:
            return []
        return self._db.get_pipeline_versions(name)

    def get_version_yaml(self, name: str, version: int) -> str:
        """Return YAML content for a specific version."""
        if self._db is None:
            raise RuntimeError("Persistence not configured (no TramDB)")
        return self._db.get_pipeline_version(name, version)

    def rollback(self, name: str, version: int) -> PipelineConfig:
        """Restore a previous version, re-register and return new config."""
        from tram.pipeline.loader import load_pipeline_from_yaml

        yaml_text = self.get_version_yaml(name, version)
        config = load_pipeline_from_yaml(yaml_text)

        # Re-register (replace)
        self.register(config, replace=True, yaml_text=yaml_text)
        return config
