"""FastAPI application factory with lifespan."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tram.api.routers import health, pipelines, runs
from tram.api.routers import metrics_router, webhooks
from tram.core.config import AppConfig
from tram.pipeline.loader import scan_pipeline_dir
from tram.pipeline.manager import PipelineManager
from tram.scheduler.scheduler import TramScheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown."""
    config: AppConfig = app.state.config
    manager: PipelineManager = app.state.manager
    scheduler: TramScheduler = app.state.scheduler

    # ── Startup ────────────────────────────────────────────────────────────
    logger.info("TRAM daemon starting")

    # Import plugins to trigger registration
    import tram.connectors  # noqa: F401
    import tram.serializers  # noqa: F401
    import tram.transforms  # noqa: F401

    # Load pipelines from directory
    if config.reload_on_start:
        configs = scan_pipeline_dir(config.pipeline_dir)
        for pipeline_config in configs:
            try:
                manager.register(pipeline_config)
                logger.info("Loaded pipeline", extra={"pipeline": pipeline_config.name})
            except Exception as exc:
                logger.error(
                    "Failed to load pipeline",
                    extra={"pipeline": pipeline_config.name, "error": str(exc)},
                )

    # Start scheduler
    scheduler.start()
    logger.info("TRAM daemon ready", extra={"port": config.port})

    yield  # application runs here

    # ── Shutdown ───────────────────────────────────────────────────────────
    logger.info("TRAM daemon shutting down")
    scheduler.stop(timeout=config.shutdown_timeout)

    # Close DB if present
    db = getattr(app.state, "db", None)
    if db is not None:
        db.close()

    logger.info("TRAM daemon stopped")


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = AppConfig.from_env()

    # Initialise persistence
    try:
        from tram.persistence.db import TramDB
        db = TramDB(url=config.db_url, node_id=config.node_id)
    except Exception as exc:
        logger.warning("Could not initialise TramDB: %s — run history will be in-memory only", exc)
        db = None

    # Initialise alert evaluator
    try:
        from tram.alerts.evaluator import AlertEvaluator
        alert_evaluator = AlertEvaluator(db=db)
    except Exception as exc:
        logger.warning("Could not initialise AlertEvaluator: %s", exc)
        alert_evaluator = None

    manager = PipelineManager(db=db, alert_evaluator=alert_evaluator)
    scheduler = TramScheduler(manager)

    app = FastAPI(
        title="TRAM",
        description="Trishul Real-time Adapter & Mapper",
        version="0.7.0",
        lifespan=lifespan,
    )

    # Store shared state
    app.state.config = config
    app.state.manager = manager
    app.state.scheduler = scheduler
    app.state.db = db
    app.state.alert_evaluator = alert_evaluator

    # Register routers
    app.include_router(health.router)
    app.include_router(pipelines.router)
    app.include_router(runs.router)
    app.include_router(webhooks.router)
    app.include_router(metrics_router.router)

    return app
