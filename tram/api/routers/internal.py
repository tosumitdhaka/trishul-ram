"""Internal API — worker-to-manager callbacks.

These endpoints are called by tram-worker agents, not by external clients.
They are excluded from the public OpenAPI schema and exempt from API key auth.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)


class RunCompletePayload(BaseModel):
    run_id: str
    pipeline_name: str
    worker_id: str | None = None
    status: str         # success | error | failed
    records_in: int = 0
    records_out: int = 0
    records_skipped: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    error: str | None = None
    errors: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # V18-04: ledger attempt identity. Present ⇒ the identity-checked
    # run-complete path (frozen §5 protocol table); absent ⇒ legacy path.
    attempt_id: str | None = None
    generation: int | None = None


class PipelineStatsPayload(BaseModel):
    worker_id: str
    pipeline_name: str
    run_id: str
    schedule_type: str
    uptime_seconds: float
    timestamp: datetime
    records_in: int = 0
    records_out: int = 0
    records_skipped: int = 0
    dlq_count: int = 0
    error_count: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    errors_last_window: list[str] = Field(default_factory=list)
    is_final: bool = False
    # v1.5.0 (GH #72): the worker's TRAM_SNMP_STACK. Default "legacy" so a
    # pre-flag worker (which omits the field) reports the stack default — a
    # legacy worker under a trishul manager still trips the mismatch warning.
    snmp_stack: str = "legacy"


# v1.5.0 (GH #72): rolling-upgrade stack-consistency guard. The manager warns
# loudly once per worker when the worker's reported SNMP stack differs from
# its own — a mixed fleet serves one stack's MIB corpus to the other (a
# trishul worker resolves JSON bundles against a .py consumer and vice versa).
# Keyed by worker_id so a restart (new pod name) re-warns if the fleet is
# still mixed; the set is bounded to avoid unbounded growth in a churning K8s
# fleet (clearing the whole set only costs a re-warn for still-mixed workers).
_MISMATCH_WARNED_WORKERS: set[str] = set()
_MISMATCH_WARNED_MAX = 4096
_MISMATCH_LOCK = threading.Lock()


def _manager_snmp_stack(request: Request) -> str:
    """The manager's TRAM_SNMP_STACK — app.state.config when present (the real
    app), else the env parse so bare-router test apps behave consistently."""
    config = getattr(request.app.state, "config", None)
    if config is not None and hasattr(config, "snmp_stack"):
        return str(config.snmp_stack)
    from tram.core.config import AppConfig
    return AppConfig.from_env().snmp_stack


def _warn_snmp_stack_mismatch(payload: PipelineStatsPayload, request: Request) -> None:
    """Log a WARNING once per worker when its SNMP stack differs from the manager's.

    Called on every stats report (the check is free); the log fires once per
    worker_id so a mismatched rolling upgrade surfaces loudly without per-
    report spam.
    """
    worker_stack = payload.snmp_stack
    manager_stack = _manager_snmp_stack(request)
    if worker_stack == manager_stack:
        return
    worker_id = payload.worker_id or "?"
    with _MISMATCH_LOCK:
        if worker_id in _MISMATCH_WARNED_WORKERS:
            return
        _MISMATCH_WARNED_WORKERS.add(worker_id)
        if len(_MISMATCH_WARNED_WORKERS) > _MISMATCH_WARNED_MAX:
            _MISMATCH_WARNED_WORKERS.clear()
    logger.warning(
        "SNMP stack mismatch: worker %s reports snmp_stack=%s but the manager "
        "runs snmp_stack=%s — a mixed-stack fleet serves one stack's MIB "
        "corpus to the other; align TRAM_SNMP_STACK across every manager and "
        "worker before enabling the trishul stack",
        worker_id,
        worker_stack,
        manager_stack,
        extra={
            "worker_id": worker_id,
            "worker_snmp_stack": worker_stack,
            "manager_snmp_stack": manager_stack,
        },
    )


@router.post("/api/internal/run-complete")
async def run_complete(payload: RunCompletePayload, request: Request) -> dict:
    """Worker callback: a dispatched pipeline run has finished.

    V18-04: a request carrying the ledger ``attempt_id``/``generation`` goes
    through the identity-checked path — the controller commits the ledger
    (attempt → terminal fenced, intent resolved, guard released by identity)
    and 200 is returned only after that commit (the outbox's durable ack).
    Legacy requests (no attempt_id) keep today's path unchanged.
    """
    controller = request.app.state.controller

    logger.debug(
        "run-complete received",
        extra={
            "run_id": payload.run_id,
            "pipeline": payload.pipeline_name,
            "status": payload.status,
            "attempt_id": payload.attempt_id,
        },
    )

    from tram.metrics.registry import MGR_RUN_COMPLETE_RECEIVED_TOTAL
    MGR_RUN_COMPLETE_RECEIVED_TOTAL.labels(
        pipeline=payload.pipeline_name, status=payload.status
    ).inc()

    if payload.attempt_id is not None:
        return controller.on_attempt_run_complete(
            attempt_id=payload.attempt_id,
            generation=payload.generation,
            run_id=payload.run_id,
            pipeline_name=payload.pipeline_name,
            worker_id=payload.worker_id,
            status=payload.status,
            records_in=payload.records_in,
            records_out=payload.records_out,
            records_skipped=payload.records_skipped,
            bytes_in=payload.bytes_in,
            bytes_out=payload.bytes_out,
            error=payload.error,
            errors=payload.errors,
            started_at=payload.started_at,
            finished_at=payload.finished_at,
        )

    controller.on_worker_run_complete(
        run_id=payload.run_id,
        pipeline_name=payload.pipeline_name,
        worker_id=payload.worker_id,
        status=payload.status,
        records_in=payload.records_in,
        records_out=payload.records_out,
        records_skipped=payload.records_skipped,
        bytes_in=payload.bytes_in,
        bytes_out=payload.bytes_out,
        error=payload.error,
        errors=payload.errors,
        started_at=payload.started_at,
        finished_at=payload.finished_at,
    )
    return {"ok": True}


@router.post("/api/internal/pipeline-stats")
async def pipeline_stats(payload: PipelineStatsPayload, request: Request) -> dict:
    store = request.app.state.stats_store
    controller = request.app.state.controller
    from tram.metrics.registry import MGR_PIPELINE_STATS_RECEIVED_TOTAL
    MGR_PIPELINE_STATS_RECEIVED_TOTAL.inc()

    # v1.5.0 (GH #72): stack-consistency guard — warn once per worker when the
    # reported TRAM_SNMP_STACK differs from the manager's.
    _warn_snmp_stack_mismatch(payload, request)

    if payload.is_final:
        store.remove(payload.run_id)
    else:
        store.update(payload)
        controller.on_pipeline_stats(payload)
    return {"ok": True}


class TransformStatePayload(BaseModel):
    """PUT body for /api/internal/transform-state/{pipeline} (design F.1 §3.2b)."""

    state: dict = Field(default_factory=dict)   # {state_key: transform-specific blob}
    config_sha256: str = ""                     # D.2 §6.1 convention
    run_id: str = ""                            # audit — stored as updated_by


class ProcessedFileEntry(BaseModel):
    """One file identity inside a processed-files check/mark request (GH #54)."""

    source_key: str
    filepath: str


class ProcessedFilesPayload(BaseModel):
    """POST body for the processed-files check/mark endpoints (GH #54).

    Namespaced by ``pipeline_name`` — the same (pipeline_name, source_key,
    filepath) key the manager-side ``ProcessedFileTracker`` uses.
    """

    pipeline_name: str
    files: list[ProcessedFileEntry] = Field(default_factory=list)


# C5 (v1.4.7): bound the per-request file list so the internal endpoints stay
# batch-friendly (the worker client sub-batches its marks) without letting a
# runaway run send an unbounded payload to the manager.
_MAX_PROCESSED_FILES_PER_REQUEST = 1000


def _check_processed_files_cap(payload: ProcessedFilesPayload) -> None:
    """Reject payloads above the per-request file bound with a 400.

    The worker client bounds its own batches below this; an oversized request
    is a client bug (or a runaway run) and is rejected loudly rather than
    processed.
    """
    if len(payload.files) > _MAX_PROCESSED_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=(
                f"too many files in one processed-files request: "
                f"{len(payload.files)} > {_MAX_PROCESSED_FILES_PER_REQUEST}"
            ),
        )


def _processed_files_db(request: Request):
    """The DB handle backing the processed-files endpoints, or 503 when absent.

    The manager app holds the ``TramDB`` (not the tracker wrapper) on
    ``app.state.db``; the endpoints call the same ``is_processed`` /
    ``mark_processed`` methods the manager-side tracker wraps.
    """
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return db


@router.post("/api/internal/processed-files/check")
async def check_processed_files(
    payload: ProcessedFilesPayload, request: Request
) -> dict:
    """Worker → manager: which of these files has this pipeline already processed?

    GH #54: worker-mode ``skip_processed`` dedup state lives in the manager's
    ``processed_files`` table (workers are stateless by design). List-in /
    list-out — ``processed`` aligns with the request's ``files`` order. Auth
    rides the existing internal middleware (same as run-complete /
    pipeline-stats). A DB error surfaces as a 5xx so the worker-side client
    fails loud instead of silently reprocessing.
    """
    db = _processed_files_db(request)
    _check_processed_files_cap(payload)
    processed = [
        db.is_processed(payload.pipeline_name, f.source_key, f.filepath)
        for f in payload.files
    ]
    return {"processed": processed}


@router.post("/api/internal/processed-files/mark")
async def mark_processed_files(
    payload: ProcessedFilesPayload, request: Request
) -> dict:
    """Worker → manager: record these files as processed by this pipeline.

    Insert-if-absent (duplicates ignored) — the same semantics as
    ``ProcessedFileTracker.mark_processed``. Mark failures are logged
    manager-side and swallowed by ``TramDB.mark_processed``, matching the
    manager-side tracker posture.
    """
    db = _processed_files_db(request)
    _check_processed_files_cap(payload)
    for f in payload.files:
        db.mark_processed(payload.pipeline_name, f.source_key, f.filepath)
    return {"ok": True}


def _stateful_transforms_enabled(request: Request) -> bool:
    """Feature flag (F.1 §9): the internal endpoints 404 while the flag is off.

    Reads ``app.state.config`` when present (the real app), falling back to the
    env parse so bare-router test apps behave consistently.
    """
    config = getattr(request.app.state, "config", None)
    if config is not None and hasattr(config, "stateful_transforms"):
        return bool(config.stateful_transforms)
    from tram.core.config import stateful_transforms_enabled as _flag
    return _flag()


def _state_max_bytes(request: Request) -> int:
    """Body-size cap for the transform-state PUT (``TRAM_STATE_MAX_BYTES``).

    Reads ``app.state.config`` when present (the real app), falling back to
    the env parse so bare-router test apps behave consistently (the same
    convention as ``_stateful_transforms_enabled``).
    """
    config = getattr(request.app.state, "config", None)
    if config is not None and hasattr(config, "state_max_bytes"):
        return int(config.state_max_bytes)
    from tram.core.config import state_max_bytes as _cap
    return _cap()


@router.get("/api/internal/transform-state/{pipeline}")
async def get_transform_state(pipeline: str, request: Request) -> dict:
    """Worker → manager: fetch a pipeline's transform-state blob.

    Auth rides the existing internal middleware (same as run-complete /
    pipeline-stats). Flag-gated 404 when ``TRAM_STATEFUL_TRANSFORMS`` is off.
    """
    if not _stateful_transforms_enabled(request):
        raise HTTPException(
            status_code=404,
            detail="stateful transforms disabled (TRAM_STATEFUL_TRANSFORMS=0)",
        )
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=404, detail="no transform state")
    row = db.load_transform_state(pipeline)
    if row is None:
        raise HTTPException(status_code=404, detail="no transform state")
    return {
        "pipeline": pipeline,
        "state": row["state"],
        "config_sha256": row["config_sha256"],
    }


@router.put("/api/internal/transform-state/{pipeline}")
async def put_transform_state(
    pipeline: str, payload: TransformStatePayload, request: Request
) -> dict:
    """Worker → manager: overwrite a pipeline's transform-state blob.

    Single-writer by construction (one active run per pipeline, design §3.3):
    the PUT overwrites the row; last writer wins. Returns 413 when the body
    exceeds ``TRAM_STATE_MAX_BYTES`` (the webhook body-cap pattern: a
    Content-Length fast-path, then a post-parse byte check) so an unbounded
    ``state`` dict can never inflate the ``transform_state`` DB row.
    """
    if not _stateful_transforms_enabled(request):
        raise HTTPException(
            status_code=404,
            detail="stateful transforms disabled (TRAM_STATEFUL_TRANSFORMS=0)",
        )
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")

    max_bytes = _state_max_bytes(request)
    # Fast-path rejection from the Content-Length header (the body is already
    # buffered by FastAPI's JSON parse; this is the cheap rejection).
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise HTTPException(status_code=413, detail="Transform state too large")
    if len(await request.body()) > max_bytes:
        raise HTTPException(status_code=413, detail="Transform state too large")

    db.save_transform_state(
        pipeline, payload.state, payload.config_sha256, updated_by=payload.run_id
    )
    return {"ok": True}
