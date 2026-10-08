"""WorkerAgent — FastAPI app exposing the internal agent API on :8766.

Worker responsibilities:
  POST /agent/run     — receive pipeline config + run_id, execute, report back
  POST /agent/stop    — stop a running batch/stream job
  GET  /agent/status  — return active jobs {running: [...], streams: [...]}
  GET  /agent/health  — liveness/readiness {ok: true, worker_id: ...}

On completion the worker journals the result FIRST (V18-01 §4) and enqueues
the run-complete payload to the durable outbox; the background drain loop —
never the dispatch thread — POSTs it to the manager's run-complete callback
URL and retries until the manager's identity-checked completion returns 200
(V18-08: the outbox is the ONLY run-complete channel).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import secrets
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from tram.agent.journal import (
    AdmissionClosedError,
    AdmissionConflictError,
    JournalUnavailableError,
    WorkerJournal,
)
from tram.agent.metrics import PipelineStats
from tram.core import config as cfg
from tram.core.config import worker_legacy_admit

if TYPE_CHECKING:
    from tram.models.pipeline import PipelineConfig

logger = logging.getLogger(__name__)

# Review D2 (GH #55): the run-complete callback must not be a single
# fire-and-forget POST — a transient manager outage would otherwise lose the
# completion record and the reconciler would later synthesize a phantom
# FAILED run for a run that succeeded. Since V18-08 the durable retry lives in
# the outbox drain (retry-until-ack with exponential backoff); ``_post_run_complete``
# below is retained ONLY as the minimal legacy-bridge fallback for a legacy
# completion whose journal is fatally unavailable (nothing can be spooled then).
_RUN_COMPLETE_RETRIES = 3
_RUN_COMPLETE_BACKOFF_BASE_S = 0.5

# Consecutive stats-POST failures per worker_id, surfaced in the WARNING log
# so an operator can tell a one-off blip from a persistent manager outage.
_STATS_MISS_LOCK = threading.Lock()
_CONSECUTIVE_STATS_MISSES: dict[str, int] = {}

# V18-01 §5 (frozen): the manager↔worker protocol version and the capability
# set this worker branch ACTUALLY implements (declared truthfully at
# /agent/handshake — never claim a capability the branch does not honor):
#   fencing            — worker_session-fenced admission, revocation
#                        tombstones, clock watermark (V18-05, landed);
#   commit_receipts    — sink commit barrier + latched_error + tier
#                        declarations (V18-01 §6/§7, landed in this release);
#   durable_completion — journal-first completions + outbox redelivery
#                        (V18-05, landed);
#   query_replay       — get_attempt / list_unacked_completions replay API
#                        (V18-05, landed);
#   drain              — POST /agent/drain + admission_state on /agent/status,
#                        one monotonic deadline (plan E, V18-07, landed).
# Deliberately NOT declared: admission_limits (worker-side slot limits not
# implemented — slot capacity is only REPORTED), status_snapshot (the slot
# usage/status snapshot surface is a later lane). A v1.8 manager reading
# these capabilities enters the compatibility bridge for anything absent
# (frozen §5).
TRAM_PROTOCOL_VERSION = "1.8"
_WORKER_CAPABILITIES: tuple[str, ...] = (
    "fencing",
    "commit_receipts",
    "durable_completion",
    "query_replay",
    "drain",
)

# V18-01 §9 (frozen names): worker slot capacity REPORTED at handshake. The
# defaults follow the config freeze (2 batch / 4 stream); there is no
# enforcement lane yet — this is the manager's view of worker headroom.
_WORKER_BATCH_SLOTS_DEFAULT = 2
_WORKER_STREAM_SLOTS_DEFAULT = 4

# Outbox drain loop: how often the background daemon thread polls due outbox
# rows and how many it takes per pass. The drain is the durable backup for
# the direct run-complete post — duplicate delivery is safe (the manager's
# run-complete is identity-checked and idempotent).
_OUTBOX_DRAIN_INTERVAL_S = 1.0
_OUTBOX_DRAIN_BATCH = 10
# V18-07: when the journal is fatally unavailable a pass raises before
# touching any row — the loop then backs off exponentially (capped) and logs
# the condition once per backoff period instead of spamming an ERROR +
# traceback every second. The backoff resets on the first successful pass.
_OUTBOX_DRAIN_BACKOFF_MAX_S = 60.0

# GH #39/#54: the worker agent is a stateless executor with no per-worker DB,
# so a ProcessedFileTracker cannot be constructed here from a local DB. With a
# manager URL the executor gets an HTTP-backed tracker (GH #54) routing
# check/mark to the manager's internal API; without one — or when the manager
# is unreachable at call time (the client fails loud itself) — the fail-loud
# fallback applies: log at executor construction / first tracker failure and
# record the degradation on the run so the manager's run_history row carries
# it via the run-complete payload errors, rather than silently disabling the
# feature.
_SKIP_PROCESSED_DISABLED_NOTE = (
    "skip_processed: true requested but worker mode has no processed-file "
    "tracker (stateless worker, no per-worker DB) — already-seen files will "
    "be reprocessed on every run; duplicate records possible"
)


def _source_requests_skip_processed(config: PipelineConfig) -> bool:
    """True when the pipeline's source requests skip_processed semantics.

    Only the file/object-storage sources (sftp, local, ftp, s3, gcs,
    azure_blob) and the CORBA dedupe source define a ``skip_processed``
    attribute; the getattr default keeps every other source type (kafka,
    syslog, http, ...) out of the check.
    """
    return bool(getattr(config.source, "skip_processed", False))


# ── Request / response models ──────────────────────────────────────────────


class RunRequest(BaseModel):
    pipeline_name: str
    yaml_text: str
    run_id: str
    schedule_type: str = "batch"   # "batch" | "stream"
    callback_url: str = ""         # manager endpoint for run-complete; may be empty
    flush: bool = False            # F.1 §5: manual flush run — close(flush=True) emits
                                   # open windows as partials and clears them from state
    # V18-01 §5: start-authorization admission fields (optional). Absent
    # ``authorization`` → the legacy-admit rollback bridge
    # (TRAM_WORKER_LEGACY_ADMIT=auto) keeps v1.7 dispatches working. The
    # authoritative attempt identity comes from the token payload; the request
    # fields are echoed for observability.
    attempt_id: str = ""
    generation: int | None = None
    slot_id: str = ""
    authorization: str | None = None


class StopRequest(BaseModel):
    pipeline_name: str
    run_id: str


class HandshakeRequest(BaseModel):
    """Manager → worker handshake initiation (V18-01 §5).

    Carries the manager's view of the worker and its own protocol/capability
    set so the exchange is symmetric. Every field is optional — a manager
    that only needs the session secret may POST an empty body; the
    authoritative registration (the worker's own session_id, capabilities,
    slot capacity, journal health) and the freshly minted session secret
    always come back in the response.
    """

    worker_id: str = ""
    session_id: str = ""
    protocol_version: str = ""
    capabilities: list[str] = Field(default_factory=list)


def _worker_slot_capacity() -> dict[str, int]:
    """Slot capacity REPORTED at handshake (V18-01 §9 frozen names).

    ``TRAM_WORKER_BATCH_SLOTS`` / ``TRAM_WORKER_STREAM_SLOTS``, defaults 2 / 4
    per the config freeze. Reported only — the worker does not enforce slot
    limits (that lane is later); an invalid value logs and falls back to the
    default so a typo never fails the worker at boot.
    """

    def _slots(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return max(int(raw), 0)
        except ValueError:
            logger.warning(
                "invalid %s=%r — using default %d", name, raw, default
            )
            return default

    return {
        "batch": _slots("TRAM_WORKER_BATCH_SLOTS", _WORKER_BATCH_SLOTS_DEFAULT),
        "stream": _slots("TRAM_WORKER_STREAM_SLOTS", _WORKER_STREAM_SLOTS_DEFAULT),
    }


# ── In-memory run tracking ─────────────────────────────────────────────────


@dataclass
class ActiveRun:
    run_id: str
    pipeline_name: str
    schedule_type: str
    started_at: str
    started_at_dt: datetime | None = None
    # sha256(req.yaml_text)[:16] captured at dispatch time (D.2 §6.1). Lets the
    # manager detect stale-config adoption; empty string means "not computed".
    config_sha256: str = ""
    stats_url: str = ""
    stats: PipelineStats | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = field(default=None, compare=False)
    # GH #39: degradation notes (e.g. skip_processed unhonored) merged into the
    # run-complete payload errors so the manager's run_history row records them.
    degradation_notes: list[str] = field(default_factory=list)
    # V18-01 §5: attempt identity + legacy-admit marker. Empty ``attempt_id``
    # means a legacy dispatch (no start authorization) — no fencing is
    # claimed, and since V18-08 its completion is journaled under a synthetic
    # ``legacy:{run_id}`` identity so it rides the same durable outbox channel;
    # ``legacy`` marks it in status payloads.
    attempt_id: str = ""
    generation: int | None = None
    slot_id: str = ""
    legacy: bool = False

    def __post_init__(self) -> None:
        if self.started_at_dt is None:
            self.started_at_dt = datetime.fromisoformat(self.started_at)


class WorkerState:
    """Thread-safe store of currently-active pipeline runs.

    V18-05: active-run tracking is ATTEMPT-AWARE — an ``ActiveRun`` is keyed
    by ``attempt_id`` when it has one (authorized dispatch) and by ``run_id``
    for legacy dispatches. Two distinct attempts of the same ``run_id``
    (a superseded attempt still draining while the newer attempt runs) then
    coexist in memory: the newer attempt's ``add`` never clobbers the older
    one, and the older one's ``remove`` cannot evict the newer one. A
    ``run_id`` → key index keeps the legacy lookups (duplicate guard, stop)
    pointing at the NEWEST active attempt for that run_id.
    """

    def __init__(
        self,
        worker_id: str,
        manager_url: str,
        api_key: str = "",
        snmp_stack: str = "legacy",
    ) -> None:
        self.worker_id = worker_id
        self.manager_url = manager_url
        self.api_key = api_key
        # v1.5.0 (GH #72): the worker's TRAM_SNMP_STACK, selected once at app
        # creation from AppConfig. Reported in every periodic stats payload so
        # the manager can warn on a mixed-stack rolling upgrade.
        self.snmp_stack = snmp_stack
        self._runs: dict[str, ActiveRun] = {}
        self._by_run: dict[str, str] = {}
        self._lock = threading.Lock()
        self.stats_stop = threading.Event()
        # V18-05: outbox drain loop stop event (same pattern as stats_stop).
        self.outbox_stop = threading.Event()
        # V18-07 (plan E): drain lifecycle state. ``drain_event`` set marks
        # admission_state "draining" (POST /agent/drain or SIGTERM shutdown);
        # ``drain_deadline`` is the ONE monotonic deadline (now +
        # TRAM_DRAIN_TIMEOUT_S) threaded to the in-flight run executors —
        # there is deliberately no independent second timeout.
        self.drain_event = threading.Event()
        self.drain_deadline: float | None = None
        self.drain_started_at: str | None = None
        # V18-08: pooled manager-RPC client (stats/outbox/legacy posts) — one
        # per worker process, lazily created (see ``rpc_client``).
        self._rpc_client: httpx.Client | None = None
        self._rpc_client_lock = threading.Lock()

    @property
    def rpc_client(self) -> httpx.Client:
        """The worker's pooled manager-RPC ``httpx.Client`` (V18-08, plan F).

        One shared client per worker process, lazily created on first use with
        the frozen ``TRAM_RPC_CONNECT_TIMEOUT_S`` / ``TRAM_RPC_READ_TIMEOUT_S``
        base deadlines (V18-01 §9). All worker→manager posts (periodic stats,
        outbox drain deliveries, the legacy run-complete fallback) reuse it —
        keep-alive connections survive across runs and loops instead of one
        TCP+TLS handshake per POST. Safe to share: every post is a synchronous
        request/response with no run-scoped state, and the client is never
        closed mid-process (nothing flushes or buffers on close). The
        per-run ``is_final`` stats and the outbox rows carry their own retry
        semantics on top of this transport.
        """
        with self._rpc_client_lock:
            if self._rpc_client is None:
                self._rpc_client = httpx.Client(
                    timeout=httpx.Timeout(
                        connect=cfg.rpc_connect_timeout_s(),
                        read=cfg.rpc_read_timeout_s(),
                        write=cfg.rpc_read_timeout_s(),
                        pool=cfg.rpc_read_timeout_s(),
                    )
                )
        return self._rpc_client

    @property
    def admission_state(self) -> str:
        """Frozen §5: ``admitting`` | ``draining``."""
        return "draining" if self.drain_event.is_set() else "admitting"

    def begin_drain(self, deadline: float) -> bool:
        """Enter the draining state with the ONE monotonic deadline.

        Idempotent: a second drain call keeps the FIRST deadline (a drain
        repeat must never extend the bound) and returns False.
        """
        if self.drain_event.is_set():
            return False
        self.drain_event.set()
        self.drain_deadline = deadline
        self.drain_started_at = datetime.now(UTC).isoformat()
        return True

    @staticmethod
    def _key(run: ActiveRun) -> str:
        return run.attempt_id or run.run_id

    def add(self, run: ActiveRun) -> None:
        with self._lock:
            key = self._key(run)
            self._runs[key] = run
            # run_id index → newest attempt; a later attempt for the same
            # run_id re-points the index without touching the older entry.
            self._by_run[run.run_id] = key

    def remove(self, run_id: str, attempt_id: str = "") -> None:
        with self._lock:
            if attempt_id:
                # Authorized attempt: remove only its own entry. If the
                # run_id index points at this attempt, re-point it at the
                # next active attempt for this run_id (or clear it); if a
                # NEWER attempt owns the index, leave it alone — a
                # superseded attempt must never evict the newer one.
                self._runs.pop(attempt_id, None)
                if self._by_run.get(run_id) == attempt_id:
                    self._reindex_locked(run_id)
            else:
                # Legacy dispatch: keyed by run_id. Remove the run_id entry
                # but only re-point the index when it still pointed at
                # run_id — an authorized attempt may own it now.
                self._runs.pop(run_id, None)
                if self._by_run.get(run_id) == run_id:
                    self._reindex_locked(run_id)

    def _reindex_locked(self, run_id: str) -> None:
        """Point the run_id index at the remaining active attempt for the
        run_id, or clear it when none remains."""
        for key, run in self._runs.items():
            if run.run_id == run_id:
                self._by_run[run_id] = key
                return
        self._by_run.pop(run_id, None)

    def get(self, run_id: str) -> ActiveRun | None:
        with self._lock:
            key = self._by_run.get(run_id) or run_id
            return self._runs.get(key)

    def snapshot(self) -> list[ActiveRun]:
        with self._lock:
            return list(self._runs.values())


# ── Manager callback ───────────────────────────────────────────────────────


def _run_complete_payload(
    *,
    run_id: str,
    pipeline_name: str,
    worker_id: str,
    status: str,
    records_in: int,
    records_out: int,
    records_skipped: int,
    bytes_in: int,
    bytes_out: int,
    error: str | None,
    errors: list[str] | None,
    started_at: str | None,
    finished_at: str | None,
) -> dict:
    """The legacy-shaped run-complete payload (no attempt identity).

    Deliberately carries NO ``attempt_id``/``generation`` keys: the manager's
    ``/api/internal/run-complete`` routes a payload without attempt identity
    to the legacy run_id-keyed path (idempotent on run_id), which is exactly
    the contract a v1.7 manager expects from a legacy-shaped run. V18-08:
    this is the payload the outbox drain delivers for legacy completions and
    the fallback direct post sends when the journal is unavailable.
    """
    return {
        "run_id": run_id,
        "pipeline_name": pipeline_name,
        "worker_id": worker_id,
        "status": status,
        "records_in": records_in,
        "records_out": records_out,
        "records_skipped": records_skipped,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "error": error,
        "errors": errors or [],
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _post_run_complete(
    callback_url: str,
    run_id: str,
    pipeline_name: str,
    worker_id: str,
    status: str,
    records_in: int,
    records_out: int,
    bytes_in: int,
    bytes_out: int,
    error: str | None,
    records_skipped: int = 0,
    errors: list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    api_key: str = "",
    client: httpx.Client | None = None,
) -> None:
    """POST run-complete to the manager with bounded retry (review D2).

    V18-08: the dispatch thread no longer posts run-complete — this function
    survives ONLY as the minimal legacy-bridge fallback: a legacy completion
    (no attempt identity) whose journal is fatally unavailable cannot be
    spooled to the outbox, so the pre-V18-08 direct post remains the last
    resort for that corner. A transient manager outage must not lose the
    completion record: up to ``_RUN_COMPLETE_RETRIES`` attempts with
    exponential backoff. Exhausted retries still log and swallow — never
    raise — so the reconciler's lost/adopted-run path remains the degraded
    fallback. The manager's duplicate-callback guard (existing-run check)
    makes retries idempotent.

    V18-08: ``client`` is the worker's pooled RPC client; when omitted the
    function falls back to a per-attempt client (unchanged legacy behavior for
    direct callers).
    """
    if not callback_url:
        return
    payload = _run_complete_payload(
        run_id=run_id,
        pipeline_name=pipeline_name,
        worker_id=worker_id,
        status=status,
        records_in=records_in,
        records_out=records_out,
        records_skipped=records_skipped,
        bytes_in=bytes_in,
        bytes_out=bytes_out,
        error=error,
        errors=errors,
        started_at=started_at,
        finished_at=finished_at,
    )
    headers = {"X-API-Key": api_key} if api_key else None
    last_exc: Exception | None = None
    for attempt in range(_RUN_COMPLETE_RETRIES):
        try:
            if client is None:
                with httpx.Client(timeout=10) as _client:
                    resp = _client.post(callback_url, json=payload, headers=headers)
            else:
                resp = client.post(callback_url, json=payload, headers=headers)
            status_code = resp.status_code
            if isinstance(status_code, int) and 400 <= status_code < 500:
                # Review N4: a 4xx is a permanent rejection (malformed
                # payload, auth, unknown route) — retrying cannot succeed,
                # so stop at the first attempt instead of burning the
                # bounded retries on a pointless loop.
                logger.warning(
                    "run-complete callback rejected — not retrying (4xx)",
                    extra={
                        "callback_url": callback_url,
                        "run_id": run_id,
                        "status_code": status_code,
                    },
                )
                return
            resp.raise_for_status()
            logger.debug(
                "run-complete callback sent",
                extra={"run_id": run_id, "status": status},
            )
            return
        except Exception as exc:
            last_exc = exc
            if attempt < _RUN_COMPLETE_RETRIES - 1:
                delay = _RUN_COMPLETE_BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, 0.25)
                logger.warning(
                    "run-complete callback failed, retrying",
                    extra={
                        "callback_url": callback_url,
                        "run_id": run_id,
                        "attempt": attempt + 1,
                        "retries": _RUN_COMPLETE_RETRIES - 1,
                        "delay": delay,
                        "error": str(exc),
                    },
                )
                time.sleep(delay)
    logger.warning(
        "run-complete callback failed after all retries — reconciler adoption "
        "path will take over",
        extra={
            "callback_url": callback_url,
            "run_id": run_id,
            "attempts": _RUN_COMPLETE_RETRIES,
            "error": str(last_exc),
        },
    )


def _legacy_outbox_key(run_id: str) -> str:
    """Synthetic outbox/completion identity for a legacy-shaped run.

    Legacy dispatches carry no ``attempt_id`` (v1.7 manager → v1.8 worker
    under TRAM_WORKER_LEGACY_ADMIT), so the completion row and the outbox row
    are keyed on the run_id the manager's legacy run-complete path resolves
    on. The manager's legacy path is idempotent on run_id (first delivery
    wins), so the shared key preserving the same first-wins semantics when
    ``mark_acked`` deletes every outbox row for a run_id is safe.
    """
    return f"legacy:{run_id}"


def _journal_legacy_completion(
    journal: WorkerJournal, run_id: str, result_json: str
) -> bool:
    """Journal a legacy-shaped completion into the durable outbox machinery.

    The completion row + outbox row commit keyed on ``legacy:{run_id}`` so the
    background drain (and the shutdown final pass) deliver and retry exactly
    like an authorized completion. Returns ``False`` when the journal cannot
    record (fatally unavailable) — the caller then keeps the minimal legacy
    direct post as the fallback (a dead journal cannot spool anything).
    """
    key = _legacy_outbox_key(run_id)
    try:
        journal.record_completion(key, result_json, run_id=run_id)
        journal.enqueue_outbox(key, "run-complete", result_json)
    except JournalUnavailableError:
        logger.warning(
            "legacy completion cannot be journaled (journal unavailable) — "
            "falling back to the direct run-complete post",
            extra={"run_id": run_id},
        )
        return False
    return True


def _commit_completion(
    journal: WorkerJournal,
    *,
    attempt_id: str,
    run_id: str,
    result_json: str | None,
    callback_url: str = "",
    pipeline_name: str = "",
    worker_id: str = "",
    status: str = "",
    records_in: int = 0,
    records_out: int = 0,
    records_skipped: int = 0,
    bytes_in: int = 0,
    bytes_out: int = 0,
    error: str | None = None,
    errors: list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    api_key: str = "",
    client: httpx.Client | None = None,
) -> None:
    """V18-08: the ONE thread-side completion commit — journal-first, outbox-only.

    The run/dispatch thread NEVER posts run-complete directly; the outbox
    background drain (and the shutdown final pass) are the ONLY posters.
    Ordering guarantee preserved: ``record_completion`` commits BEFORE the
    caller's ``state.remove`` (the run leaves WorkerState only in the
    ``finally``), so a crash between the journal commit and the drain's
    delivery redelivers via the outbox on restart (at-least-once; the
    manager-side identity check makes duplicates idempotent no-ops).

    Authorized attempts (``attempt_id`` set) commit the attempt-identity
    completion payload as today. Legacy runs commit the legacy-shaped payload
    (no attempt identity) under the synthetic ``legacy:{run_id}`` key; only
    when the journal is fatally unavailable does the minimal legacy direct
    post (:func:`_post_run_complete`) fire — that corner cannot be journaled
    at all, and the pre-V18-08 path is kept so the legacy outcome is not
    silently lost.

    V18-08: ``client`` is the worker's pooled RPC client, forwarded to the
    legacy fallback post when the journal cannot spool.
    """
    if attempt_id:
        journal.record_completion(attempt_id, result_json or "")
        journal.enqueue_outbox(attempt_id, "run-complete", result_json or "")
        return
    legacy_json = json.dumps(
        _run_complete_payload(
            run_id=run_id,
            pipeline_name=pipeline_name,
            worker_id=worker_id,
            status=status,
            records_in=records_in,
            records_out=records_out,
            records_skipped=records_skipped,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
            error=error,
            errors=errors,
            started_at=started_at,
            finished_at=finished_at,
        )
    )
    if _journal_legacy_completion(journal, run_id, legacy_json):
        return
    # Journal unavailable — the minimal legacy direct post (unchanged path).
    _post_run_complete(
        callback_url, run_id, pipeline_name, worker_id, status,
        records_in, records_out, bytes_in, bytes_out, error, records_skipped,
        errors, started_at=started_at, finished_at=finished_at, api_key=api_key,
        client=client,
    )


def _stats_log_context(payload: dict) -> tuple[str, str]:
    """Best-effort (pipeline_name, run_id) for the stats-miss WARNING.

    V18-08: the periodic payload is a per-worker batch (``runs`` array) — the
    log context falls back to the first run's identity; single-run payloads
    (the completion-time ``is_final`` post) keep their top-level fields.
    """
    runs = payload.get("runs")
    if isinstance(runs, list) and runs and isinstance(runs[0], dict):
        return (
            str(runs[0].get("pipeline_name", "") or ""),
            str(runs[0].get("run_id", "") or ""),
        )
    return (
        str(payload.get("pipeline_name", "") or ""),
        str(payload.get("run_id", "") or ""),
    )


def _post_stats(
    stats_url: str,
    payload: dict,
    api_key: str = "",
    client: httpx.Client | None = None,
) -> None:
    """POST one stats payload to the manager.

    V18-08: ``client`` is the worker's pooled RPC client (passed by the stats
    loop and the run threads); when omitted the function falls back to a
    per-call client (unchanged behavior for direct callers). The payload may
    be a single-run snapshot (the completion-time ``is_final`` post) or a
    per-worker batch (``runs`` array) from the periodic loop.
    """
    if not stats_url:
        return
    headers = {"X-API-Key": api_key} if api_key else None
    pipeline_name, run_id = _stats_log_context(payload)
    try:
        if client is None:
            with httpx.Client(timeout=10) as _client:
                resp = _client.post(stats_url, json=payload, headers=headers)
        else:
            resp = client.post(stats_url, json=payload, headers=headers)
        resp.raise_for_status()
    except Exception as exc:
        from tram.metrics.registry import MGR_STATS_MISSED_TOTAL
        worker_id = str(payload.get("worker_id", "") or "")
        with _STATS_MISS_LOCK:
            consecutive = _CONSECUTIVE_STATS_MISSES.get(worker_id, 0) + 1
            _CONSECUTIVE_STATS_MISSES[worker_id] = consecutive
        MGR_STATS_MISSED_TOTAL.labels(worker_id=worker_id).inc()
        logger.warning(
            "pipeline-stats callback failed",
            extra={
                "stats_url": stats_url,
                "pipeline": pipeline_name,
                "run_id": run_id,
                "worker_id": worker_id,
                "consecutive_misses": consecutive,
                "error": str(exc),
            },
        )
        return
    worker_id = str(payload.get("worker_id", "") or "")
    with _STATS_MISS_LOCK:
        _CONSECUTIVE_STATS_MISSES.pop(worker_id, None)


def _flush_file_tracker(file_tracker) -> None:
    """Best-effort flush of buffered processed-file marks at run end (C2).

    The worker-mode ``HttpFileTracker`` buffers per-file marks and emits them
    in one batched request; this runs after ``executor.batch_run`` /
    ``executor.stream_run`` return so the manager's ``processed_files`` table
    is current when the next run's checks arrive. Never raises — a flush
    failure degrades the tracker (already-recorded note) but must not break
    the run-complete callback.
    """
    if file_tracker is None:
        return
    try:
        file_tracker.close()
    except Exception:
        logger.exception("Processed-file tracker flush failed")


def _derive_stats_url(callback_url: str, manager_url: str) -> str:
    if callback_url:
        base, _, _ = callback_url.rpartition("/")
        return f"{base}/pipeline-stats" if base else ""
    if manager_url:
        return f"{manager_url}/api/internal/pipeline-stats"
    return ""


def _emit_stats_once(state: WorkerState) -> None:
    """One periodic stats pass — ONE batched snapshot per worker (V18-08).

    All active runs' stats coalesce into a single payload (``runs`` array)
    posted once per stats URL (in practice all runs share the manager URL, so
    this is one HTTP POST per interval per worker — not per-run chatter). The
    window reset happens once per run per pass, exactly like the per-run
    posting it replaces; runs with no stats accumulator or no stats URL are
    skipped as before. ``is_final`` stays False — final per-run snapshots are
    the completion-time posts, not the periodic loop.

    The batch rides the worker's pooled RPC client (keep-alive across passes).
    """
    now = datetime.now(UTC)
    batches: dict[str, list[dict]] = {}
    for run in state.snapshot():
        if run.stats is None or not run.stats_url or run.started_at_dt is None:
            continue
        snapshot = {
            "pipeline_name": run.pipeline_name,
            "run_id": run.run_id,
            "schedule_type": run.schedule_type,
            "uptime_seconds": max((now - run.started_at_dt).total_seconds(), 0.0),
            "is_final": False,
            **run.stats.snapshot_and_reset_window(),
        }
        batches.setdefault(run.stats_url, []).append(snapshot)
    if not batches:
        return
    client = state.rpc_client
    for stats_url, run_snapshots in batches.items():
        payload = {
            "worker_id": state.worker_id,
            "timestamp": now.isoformat(),
            # v1.5.0 (GH #72): the manager's rolling-upgrade mismatch guard
            # compares this against its own TRAM_SNMP_STACK.
            "snmp_stack": state.snmp_stack,
            "runs": run_snapshots,
        }
        _post_stats(stats_url, payload, api_key=state.api_key, client=client)


def _final_stats_snapshot(run: ActiveRun) -> dict[str, int | list[str]]:
    if run.stats is None:
        return {
            "records_in": 0,
            "records_out": 0,
            "records_skipped": 0,
            "dlq_count": 0,
            "error_count": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "errors_last_window": [],
        }
    return run.stats.snapshot_and_reset_window()


def _active_run_status(run: ActiveRun, worker_id: str, now: datetime) -> dict[str, object]:
    uptime_seconds = 0.0
    if run.started_at_dt is not None:
        uptime_seconds = max((now - run.started_at_dt).total_seconds(), 0.0)
    stats = run.stats.snapshot() if run.stats is not None else {
        "records_in": 0,
        "records_out": 0,
        "records_skipped": 0,
        "dlq_count": 0,
        "error_count": 0,
        "bytes_in": 0,
        "bytes_out": 0,
        "errors_last_window": [],
    }
    return {
        "run_id": run.run_id,
        "pipeline": run.pipeline_name,
        "started_at": run.started_at,
        "schedule_type": run.schedule_type,
        "worker_id": worker_id,
        "uptime_seconds": uptime_seconds,
        "config_sha256": run.config_sha256,
        "stats": stats,
        # V18-01 §5: attempt identity on status items; ``legacy`` marks a
        # rollback-bridge dispatch (no start authorization, no fencing).
        "attempt_id": run.attempt_id,
        "generation": run.generation,
        "slot_id": run.slot_id,
        "legacy": run.legacy,
    }


def _completion_result_json(
    *,
    run_id: str,
    pipeline_name: str,
    worker_id: str,
    attempt_id: str,
    status: str,
    records_in: int = 0,
    records_out: int = 0,
    records_skipped: int = 0,
    bytes_in: int = 0,
    bytes_out: int = 0,
    error: str | None = None,
    errors: list[str] | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    legacy: bool = False,
    generation: int | None = None,
    dlq_count: int = 0,
    records_failed: int = 0,
    dlq_succeeded: int = 0,
    dlq_failed: int = 0,
    disposition: dict | None = None,
    spool: dict | None = None,
) -> str:
    """Build the journal ``completions.result_json`` payload (V18-01 §4).

    Journal-first ordering: the completion row commits BEFORE the run leaves
    ``WorkerState``, so a crash between the commit and the outbox delivery
    never loses the outcome (V18-08: the outbox drain is the only delivery
    channel — the run thread never posts). The payload carries the full
    attempt identity (``attempt_id`` + ``generation``) the manager's
    identity-checked run-complete path resolves on.

    V18-06: the run-scoped delivery counters (``dlq_count``,
    ``records_failed``, ``dlq_succeeded``, ``dlq_failed``), the per-sink
    ``disposition`` map and the DLQ ``spool`` counters ride in additively —
    the manager's run-history decode (``_decode_disposition``) reads exactly
    these keys, so a completion payload carrying them activates the
    per-sink/spool recording on the boot-adoption path.
    """
    return json.dumps(
        {
            "run_id": run_id,
            "pipeline_name": pipeline_name,
            "worker_id": worker_id,
            "attempt_id": attempt_id,
            "generation": generation,
            "status": status,
            "records_in": records_in,
            "records_out": records_out,
            "records_skipped": records_skipped,
            "bytes_in": bytes_in,
            "bytes_out": bytes_out,
            "error": error,
            "errors": errors or [],
            "started_at": started_at,
            "finished_at": finished_at,
            "legacy": legacy,
            "dlq_count": dlq_count,
            "records_failed": records_failed,
            "dlq_succeeded": dlq_succeeded,
            "dlq_failed": dlq_failed,
            "disposition": disposition,
            "spool": spool,
        }
    )


def _stats_loop(state: WorkerState, interval: int) -> None:
    while not state.stats_stop.wait(interval):
        _emit_stats_once(state)


# ── Outbox drain (V18-01 §4/§5) ────────────────────────────────────────────


def _run_complete_url(manager_url: str) -> str:
    """The manager's run-complete route for this worker's manager URL."""
    return f"{manager_url}/api/internal/run-complete" if manager_url else ""


def _drain_outbox_once(
    journal: WorkerJournal, manager_url: str, api_key: str = "",
    client: httpx.Client | None = None,
) -> int:
    """One outbox drain pass: deliver every due row, ack on manager 200.

    Each outbox row carries the completion payload (the same ``result_json``
    the journal committed first — attempt-identity for authorized runs,
    legacy-shaped for ``legacy:{run_id}`` keys). On a manager 200 the
    completion is durably acked (``mark_acked`` — the manager commits its
    ledger before 200, so 200 IS the durable ack); any other outcome records
    exponential backoff via ``record_outbox_failure`` and the row stays due
    for a later pass. ``4xx``/network failures alike back off — the manager's
    run-complete returns 200 even for an unknown/mismatched attempt
    (``ignored`` diagnostics), so a non-200 genuinely means "not delivered".

    V18-08: this loop (plus the shutdown final pass) is the ONLY run-complete
    poster — the dispatch thread never posts directly, so a crash between the
    journal commit and delivery redelivers here on restart (at-least-once;
    the manager's identity check makes duplicates idempotent no-ops).
    V18-08: ``client`` is the worker's pooled RPC client (deliveries reuse one
    keep-alive connection per pass); when omitted each row constructs its own
    client (unchanged behavior for direct callers).
    """
    url = _run_complete_url(manager_url)
    if not url:
        return 0
    headers = {"X-API-Key": api_key} if api_key else None
    processed = 0
    for row in journal.fetch_due_outbox(limit=_OUTBOX_DRAIN_BATCH):
        # mark_acked drains every outbox row for an attempt, so a row later
        # in this pass may already be acked (and deleted) by an earlier
        # duplicate — skip it rather than delivering a redundant copy.
        rec = journal.get_attempt(row.attempt_id)
        if rec is not None and rec.kind == "completion" and rec.acked:
            continue
        processed += 1
        try:
            payload = json.loads(row.payload_json)
        except ValueError as exc:
            logger.warning(
                "outbox row %d has an unparseable payload — backing off",
                row.seq,
                extra={"attempt_id": row.attempt_id, "error": str(exc)},
            )
            journal.record_outbox_failure(row.seq, f"unparseable payload: {exc}")
            continue
        try:
            if client is None:
                with httpx.Client(timeout=10) as _client:
                    resp = _client.post(url, json=payload, headers=headers)
            else:
                resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "outbox delivery failed — backing off",
                extra={
                    "attempt_id": row.attempt_id,
                    "seq": row.seq,
                    "attempts": row.attempts + 1,
                    "error": str(exc),
                },
            )
            journal.record_outbox_failure(row.seq, str(exc))
            continue
        # 200 after the manager's ledger commit — durable ack.
        journal.mark_acked(row.attempt_id)
        logger.debug(
            "outbox delivery acked",
            extra={"attempt_id": row.attempt_id, "seq": row.seq},
        )
    return processed


def _outbox_loop(
    journal: WorkerJournal, state: WorkerState, manager_url: str, api_key: str = ""
) -> None:
    """Background daemon drain loop (same pattern as the stats loop).

    V18-07: a pass that raises before touching any row (fatally unavailable
    journal) backs off exponentially — the wait IS the backoff, so the
    ERROR fires once per backoff period, never per second — capped at
    ``_OUTBOX_DRAIN_BACKOFF_MAX_S``, and the base cadence resumes after the
    first successful pass (recovery is logged once). The FIRST pass runs
    immediately at loop start (no initial sleep): a pre-existing unacked
    completion is delivered as soon as the thread starts — the
    crash-recovery path — instead of after a starvation-prone initial
    interval (this also removes the load-induced flake in the lifespan
    delivery test).
    """
    backoff = _OUTBOX_DRAIN_INTERVAL_S
    consecutive_failures = 0
    client = state.rpc_client
    while True:
        try:
            _drain_outbox_once(journal, manager_url, api_key, client=client)
        except Exception as exc:
            consecutive_failures += 1
            logger.error(
                "outbox drain pass failed — backing off",
                extra={
                    "consecutive_failures": consecutive_failures,
                    "backoff_seconds": backoff,
                    "error": str(exc),
                },
            )
            backoff = min(
                max(backoff * 2, _OUTBOX_DRAIN_INTERVAL_S),
                _OUTBOX_DRAIN_BACKOFF_MAX_S,
            )
        else:
            if consecutive_failures:
                logger.info(
                    "outbox drain recovered",
                    extra={"missed_passes": consecutive_failures},
                )
                consecutive_failures = 0
            backoff = _OUTBOX_DRAIN_INTERVAL_S
        if state.outbox_stop.wait(backoff):
            return


# ── App factory ────────────────────────────────────────────────────────────


def create_worker_app(
    worker_id: str = "",
    manager_url: str = "",
    stats_interval: int | None = None,
    *,
    journal: WorkerJournal | None = None,
) -> FastAPI:
    """Create and return the worker agent FastAPI application.

    ``journal`` injects a pre-built ``WorkerJournal`` (tests pass a temp-path
    journal); production and the default fall back to ``TRAM_WORKER_JOURNAL_PATH``.
    """
    if not worker_id:
        worker_id = os.environ.get("TRAM_WORKER_ID", socket.gethostname())
    if not manager_url:
        # Worker-mode only (daemon/server.py routes TRAM_MODE=worker here).
        # Deliberately NO localhost default: the worker's manager is remote in
        # this topology, and defaulting would make the worker POST its own
        # run-complete callbacks to itself. The v1.6.0 standalone default
        # (GH #81) lives in AppConfig.from_env and never reaches this branch.
        manager_url = os.environ.get("TRAM_MANAGER_URL", "")
    if stats_interval is None:
        stats_interval = int(os.environ.get("TRAM_STATS_INTERVAL", "30"))
    api_key = os.environ.get("TRAM_API_KEY", "")

    # v1.5.0 (GH #72): the worker selects its SNMP stack from settings
    # (TRAM_SNMP_STACK) at construction — same env the connector layer reads,
    # same env the manager reads for its mismatch guard. An invalid value
    # fails the worker app loudly here instead of silently picking a stack.
    from tram.core.config import AppConfig
    snmp_stack_value = AppConfig.from_env().snmp_stack

    # V18-01 §4/§5: per-worker durable journal. Tests/dev inject a temp-path
    # journal; production uses the frozen default path. A corrupt or
    # unavailable journal is cached and reported by health() — the app still
    # starts and /agent/status surfaces the condition distinctly.
    journal = journal if journal is not None else WorkerJournal(cfg.worker_journal_path())

    # V18-01 §4: the manager–worker session secret for start-authorization
    # tokens. The /agent/handshake lane establishes this secret at runtime —
    # each handshake mints a fresh secret, stored as the current secret with
    # the previous one retained for the rotation overlap (max TTL + skew).
    # TRAM_AUTH_SESSION_SECRET is the bootstrap/fallback: until the first
    # handshake it IS the current secret (deployments pre-share it), and after
    # a rotation it stays valid as the retained previous secret. Empty secret
    # = no manager can mint a valid token, so authorized dispatches are
    # refused until a handshake (or a pre-shared env secret) exists.
    auth_secret = os.environ.get("TRAM_AUTH_SESSION_SECRET", "")

    # V18-01 §4: session identity {worker_id}-{boot_uuid8}, minted once per
    # process start. Reported on /agent/status and stamped on journal rows so
    # a newer session for the same worker_id is proof of process termination
    # (the handshake lane builds the full exchange on this value).
    boot_uuid8 = uuid.uuid4().hex[:8]
    worker_session = f"{worker_id}-{boot_uuid8}"

    state = WorkerState(
        worker_id=worker_id,
        manager_url=manager_url,
        api_key=api_key,
        snmp_stack=snmp_stack_value,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Trigger plugin registration (connectors/transforms/serializers)
        import tram.connectors  # noqa: F401
        import tram.serializers  # noqa: F401
        import tram.transforms  # noqa: F401

        # V18-01 §4: mark reserved/running rows from a previous process boot as
        # interrupted — never silently re-executed before the manager resolves
        # ownership. Best-effort at boot: a corrupt/unavailable journal must
        # not block worker startup (status reports it distinctly) and
        # admission stays closed until recovery.
        try:
            interrupted = journal.mark_interrupted_on_boot()
        except JournalUnavailableError as exc:
            logger.error(
                "worker journal unavailable at boot — admission closed, "
                "status reports the condition",
                extra={"worker_id": worker_id, "error": str(exc)},
            )
        else:
            if interrupted:
                logger.warning(
                    "worker journal boot recovery — marked %d interrupted attempts",
                    interrupted,
                    extra={"worker_id": worker_id, "count": interrupted},
                )

        state.stats_stop.clear()
        stats_thread = threading.Thread(
            target=_stats_loop,
            args=(state, stats_interval),
            daemon=True,
            name="tram-agent-stats",
        )
        stats_thread.start()

        # V18-01 §4/§5 / V18-08: outbox drain — the ONLY run-complete channel.
        # Rows survive a crash (journal-first ordering); after a restart this
        # loop redelivers unacked completions. Only the drain and the shutdown
        # final pass below ever POST run-complete — the run threads journal
        # and enqueue, never post. The manager's run-complete is
        # identity-checked and idempotent, so redelivery is a no-op.
        state.outbox_stop.clear()
        outbox_thread = threading.Thread(
            target=_outbox_loop,
            args=(journal, state, manager_url, api_key),
            daemon=True,
            name="tram-agent-outbox",
        )
        outbox_thread.start()

        logger.info("Worker agent ready", extra={"worker_id": worker_id})
        yield

        # V18-07 (plan E, R7): uvicorn/SIGTERM shutdown runs the SAME drain
        # state machine as POST /agent/drain — admission closed, in-flight
        # runs signalled cooperatively, ONE monotonic deadline
        # (TRAM_DRAIN_TIMEOUT_S) bounding the wait. After the deadline expires
        # shutdown proceeds regardless: the run threads are daemon and are
        # never joined past the bound (an uncooperative blocked adapter is
        # resolved by process termination/recovery — plan E).
        state.stats_stop.set()
        state.outbox_stop.set()
        if not state.drain_event.is_set():
            state.begin_drain(time.monotonic() + cfg.drain_timeout_s())
        for run in state.snapshot():
            run.stop_event.set()

        def _drain_remaining() -> float:
            if state.drain_deadline is None:
                return 0.0
            return max(state.drain_deadline - time.monotonic(), 0.0)

        for run in state.snapshot():
            thread = run.thread
            if thread is None or not thread.is_alive():
                continue
            thread.join(timeout=_drain_remaining())
            if _drain_remaining() <= 0:
                break
        # Final best-effort outbox pass (bounded by the remaining deadline) so
        # completions recorded during the drain are flushed before the journal
        # closes — the background loop has stopped, this is the last chance.
        if _drain_remaining() > 0:
            try:
                _drain_outbox_once(
                    journal, manager_url, api_key, client=state.rpc_client
                )
            except Exception:
                logger.warning(
                    "final outbox drain pass failed",
                    extra={"worker_id": worker_id},
                )
        if stats_thread.is_alive():
            stats_thread.join(timeout=stats_interval + 1)
        if outbox_thread.is_alive():
            outbox_thread.join(timeout=_OUTBOX_DRAIN_INTERVAL_S + 1)
        logger.info(
            "Worker agent stopped",
            extra={"worker_id": worker_id, "admission_state": state.admission_state},
        )
        journal.close()

    app = FastAPI(
        title="TRAM Worker Agent",
        description="Internal agent API for pipeline execution workers",
        lifespan=lifespan,
    )
    app.state.worker = state
    app.state.journal = journal
    app.state.worker_session = worker_session
    # V18-01 §4: session-secret state for token validation. ``auth_secret`` is
    # the current secret (handshake-minted, or the env bootstrap before any
    # handshake); ``auth_secret_previous`` is the retained pre-rotation
    # secret accepted for the max-TTL + skew overlap. The /agent/handshake
    # route rotates these.
    app.state.auth_secret = auth_secret
    app.state.auth_secret_previous = None

    # Internal agent API: same API-key middleware as the manager ingress, with
    # the /agent/* routes as the protected internal surface. /agent/health is
    # always exempt so K8s probes never need a key. TRAM_INTERNAL_AUTH_MODE
    # defaults to warn — Phase 2 flips to enforce without code changes.
    from tram.api.middleware import APIKeyMiddleware
    app.add_middleware(APIKeyMiddleware, internal_prefixes=("/agent/",))

    # ── GET /agent/health ──────────────────────────────────────────────────

    @app.get("/agent/health")
    def health():
        active = state.snapshot()
        ingress_thread = getattr(app.state, "ingress_thread", None)
        ingress_alive = ingress_thread.is_alive() if ingress_thread is not None else True
        return {
            "ok": ingress_alive,
            "worker_id": worker_id,
            "active_runs": len(active),
            "running_pipelines": list({r.pipeline_name for r in active}),
            "ingress_up": ingress_alive,
        }

    # ── GET /agent/status ──────────────────────────────────────────────────

    @app.get("/agent/status")
    def status():
        active = state.snapshot()
        now = datetime.now(UTC)
        running = [
            _active_run_status(r, worker_id, now)
            for r in active
            if r.schedule_type != "stream"
        ]
        streams = [
            _active_run_status(r, worker_id, now)
            for r in active
            if r.schedule_type == "stream"
        ]
        # V18-01 §4: readiness-relevant journal health (never raises — a fatal
        # journal reports its state here rather than crashing status) plus the
        # current session epoch's rejection watermark.
        journal_health = journal.health()
        try:
            watermark = journal.watermark_status()
            watermark_payload = {
                "session_epoch": watermark.session_epoch,
                "watermark_ms": watermark.watermark_ms,
                "now_ms": watermark.now_ms,
                "behind_ms": watermark.behind_ms,
                "admitting": watermark.admitting,
            }
        except JournalUnavailableError as exc:
            watermark_payload = {
                "session_epoch": None,
                "watermark_ms": None,
                "now_ms": None,
                "behind_ms": None,
                "admitting": False,
                "error": str(exc),
            }
        return {
            "worker_id": worker_id,
            "worker_session": worker_session,
            # V18-07 (plan E / frozen §5): worker admission state — the
            # manager-side drain surface. ``drained`` is the release-runbook
            # gate: draining AND (all in-flight runs finished OR the single
            # monotonic deadline passed). The worker never exits the process
            # on its own — the indicator stays observable.
            "admission_state": state.admission_state,
            "drain": {
                "draining": state.drain_event.is_set(),
                "started_at": state.drain_started_at,
                "deadline_expired": (
                    state.drain_deadline is not None
                    and time.monotonic() >= state.drain_deadline
                ),
                "idle": len(active) == 0,
                "drained": state.drain_event.is_set()
                and (
                    len(active) == 0
                    or (
                        state.drain_deadline is not None
                        and time.monotonic() >= state.drain_deadline
                    )
                ),
            },
            "active_runs": len(active),
            "running_pipelines": sorted({r.pipeline_name for r in active}),
            "running": running,
            "streams": streams,
            "journal": {
                "state": journal_health.state,
                "detail": journal_health.detail,
                "size_bytes": journal_health.size_bytes,
                "quota_bytes": journal_health.quota_bytes,
                "headroom_bytes": journal_health.headroom_bytes,
            },
            "watermark": watermark_payload,
        }

    # ── POST /agent/handshake (V18-01 §5) ────────────────────────────────

    @app.post("/agent/handshake")
    def handshake(req: HandshakeRequest):
        """Manager → worker registration exchange (frozen §5 protocol table).

        The manager POSTs its protocol/capability view; the worker mints a
        fresh session secret, rotates (the previous secret is retained for
        the max-TTL + skew overlap), and replies with its registration:
        ``session_id`` ({worker_id}-{boot_uuid8}), protocol version, the
        capability set this branch implements, slot capacity, journal health,
        and the ``session_secret`` the manager must mint start-authorization
        tokens with. Machine-authenticated on the same APIKeyMiddleware
        channel as every other /agent/* route.

        Rotation on every call: a newer handshake supersedes the old secret
        (frozen §5 — a new session_id for a worker_id supersedes the old).
        The worker's own session identity rides in the response, so a manager
        holding a stale session_id self-corrects on the next exchange.
        """
        new_secret = secrets.token_hex(32)
        app.state.auth_secret_previous = app.state.auth_secret or None
        app.state.auth_secret = new_secret
        journal_health = journal.health()
        logger.info(
            "worker handshake — session secret rotated",
            extra={
                "worker_id": worker_id,
                "session_id": worker_session,
                "manager_worker_id": req.worker_id,
                "manager_session_id": req.session_id,
                "manager_protocol": req.protocol_version,
                "protocol_version": TRAM_PROTOCOL_VERSION,
            },
        )
        return {
            "worker_id": worker_id,
            "session_id": worker_session,
            "protocol_version": TRAM_PROTOCOL_VERSION,
            "capabilities": list(_WORKER_CAPABILITIES),
            "slot_capacity": _worker_slot_capacity(),
            "journal_health": {
                "state": journal_health.state,
                "detail": journal_health.detail,
                "size_bytes": journal_health.size_bytes,
                "quota_bytes": journal_health.quota_bytes,
                "headroom_bytes": journal_health.headroom_bytes,
            },
            "session_secret": new_secret,
        }

    # ── GET /agent/attempts/{attempt_id} (V18-01 §5) ─────────────────────

    @app.get("/agent/attempts/{attempt_id}")
    def attempt(attempt_id: str):
        """Replay query for one attempt (frozen §5 protocol table).

        Serves the journal's ``get_attempt``: a completion record, an active
        reservation, an ``interrupted`` reservation, or a revocation tombstone
        — each as a distinguishable response carrying a ``kind``
        discriminator. 404 means the attempt has NO journal row at all: that
        is neither revocation nor quiescence — the manager must tell the
        three apart (plan B). ``None`` rows occur for unknown attempt_ids;
        legacy dispatches surface here under their synthetic
        ``legacy:{run_id}`` identity once their completion is journaled.
        """
        rec = journal.get_attempt(attempt_id)
        if rec is None:
            raise HTTPException(
                status_code=404,
                detail={"attempt_id": attempt_id, "reason": "unknown attempt"},
            )
        if rec.kind == "tombstone":
            return {
                "kind": "tombstone",
                "attempt_id": rec.attempt_id,
                "run_id": rec.run_id,
                "reason": rec.reason,
                "revoked_at": rec.revoked_at,
                "session_epoch": rec.session_epoch,
            }
        if rec.kind == "completion":
            result: dict[str, object] = {}
            if rec.result_json:
                try:
                    result = json.loads(rec.result_json)
                except ValueError:
                    result = {"result_json": rec.result_json}
            return {
                "kind": "completion",
                "attempt_id": rec.attempt_id,
                "run_id": rec.run_id,
                "result": result,
                "completed_at": rec.completed_at,
                "acked": rec.acked,
                "acked_at": rec.acked_at,
            }
        # active | interrupted — same reservation shape, distinct kinds.
        return {
            "kind": rec.kind,
            "attempt_id": rec.attempt_id,
            "run_id": rec.run_id,
            "state": rec.state,
            "pipeline_name": rec.pipeline_name,
            "generation": rec.generation,
            "slot_id": rec.slot_id,
            "worker_session": rec.worker_session,
            "reserved_at": rec.reserved_at,
            "thread_started": rec.thread_started,
        }

    # ── POST /agent/run ────────────────────────────────────────────────────

    @app.post("/agent/run", status_code=202)
    def run(req: RunRequest):  # noqa: A001
        # ── V18-07 (plan E / frozen §5): drain admission gate ───────────────
        # While the worker is draining no NEW dispatch is admitted — 503
        # admission closed (draining), distinct from the journal-unavailable
        # 503. In-flight attempts are unaffected (they drain under the one
        # deadline); the manager's drain-only compatibility bridge expects
        # exactly this closure.
        if state.drain_event.is_set():
            raise HTTPException(
                status_code=503,
                detail={
                    "reason": "admission closed (draining)",
                    "admission_state": state.admission_state,
                },
            )
        # ── V18-01 §5: admission path selection ────────────────────────────
        # ``authorization`` present → start-authorization admission against the
        # journal (per-attempt idempotency — a repeat returns 200, never a
        # second thread). Absent → the legacy-admit rollback bridge when
        # TRAM_WORKER_LEGACY_ADMIT=auto (v1.7-shaped dispatch, explicit legacy
        # marker, no fencing claimed); ``off`` rejects such dispatches with 400.
        authorized = req.authorization is not None
        if not authorized:
            if worker_legacy_admit() == "off":
                raise HTTPException(
                    status_code=400,
                    detail={
                        "reason": "dispatch without authorization rejected "
                        "(TRAM_WORKER_LEGACY_ADMIT=off)"
                    },
                )
            if state.get(req.run_id) is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"run_id {req.run_id!r} is already active on this worker",
                )
            logger.info(
                "legacy dispatch accepted (no authorization) — rollback "
                "bridge, no fencing claimed",
                extra={
                    "run_id": req.run_id,
                    "pipeline": req.pipeline_name,
                    "worker_id": worker_id,
                    "legacy": True,
                },
            )

        from tram.pipeline.executor import PipelineExecutor
        from tram.pipeline.loader import load_pipeline_from_yaml

        # D.2 §6.1: fingerprint the dispatched YAML before loading so the
        # manager can detect stale-config adoption after a restart.
        config_sha256 = hashlib.sha256(req.yaml_text.encode()).hexdigest()[:16]

        try:
            config = load_pipeline_from_yaml(req.yaml_text)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # ── V18-01 §4: journal admission (authorized path only) ────────────
        # Runs BEFORE any thread is created — the reservation must exist before
        # the executor can start, so a crash between admission and thread start
        # leaves an interrupted reservation the manager can resolve, never an
        # untracked execution. YAML validation precedes admission so a bad
        # dispatch is a 422 and never wedges an attempt in the reserved state.
        attempt_id: str | None = None
        if authorized:
            try:
                admission = journal.validate_and_admit(
                    req.authorization,
                    req.pipeline_name,
                    worker_session=worker_session,
                    # V18-01 §4: the handshake-minted current secret, with the
                    # retained pre-rotation secret accepted for the overlap.
                    current_secret=app.state.auth_secret,
                    previous_secret=app.state.auth_secret_previous,
                )
            except AdmissionConflictError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"attempt_id": exc.attempt_id, "reason": str(exc)},
                ) from exc
            except AdmissionClosedError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"reason": str(exc), "admission": "closed"},
                ) from exc
            if admission.outcome == "revoked":
                raise HTTPException(
                    status_code=410,
                    detail={
                        "attempt_id": admission.attempt_id,
                        "reason": admission.tombstone_reason,
                        "revoked_at": admission.tombstone_revoked_at,
                        "worker_id": worker_id,
                    },
                )
            if admission.outcome == "refused_auth":
                # 403 when the token belongs to a superseded session (old
                # session epoch / retired by watermark); 401 for every other
                # authorization refusal.
                status_code = (
                    403
                    if admission.refusal_reason
                    in ("old session epoch", "token retired by watermark")
                    else 401
                )
                raise HTTPException(
                    status_code=status_code,
                    detail={
                        "attempt_id": admission.attempt_id,
                        "reason": admission.refusal_reason,
                    },
                )
            if admission.outcome == "already_admitted":
                # Repeat of an active/completed attempt: echo the existing
                # acceptance/result at 200 — never a second thread.
                rec = journal.get_attempt(admission.attempt_id)
                body: dict[str, object] = {
                    "accepted": True,
                    "run_id": req.run_id,
                    "attempt_id": admission.attempt_id,
                    "worker_id": worker_id,
                    "state": (
                        rec.state
                        if rec is not None
                        else (
                            admission.existing.state
                            if admission.existing is not None
                            else "active"
                        )
                    ),
                    "already_admitted": True,
                }
                if rec is not None and rec.kind == "completion":
                    try:
                        body["result"] = json.loads(rec.result_json or "{}")
                    except ValueError:
                        pass
                return JSONResponse(status_code=200, content=body)
            attempt_id = admission.attempt_id

        # Resolve callback URL: explicit > derived from manager_url
        callback_url = req.callback_url
        if not callback_url and state.manager_url:
            callback_url = f"{state.manager_url}/api/internal/run-complete"

        active_run = ActiveRun(
            run_id=req.run_id,
            pipeline_name=req.pipeline_name,
            schedule_type=req.schedule_type,
            started_at=datetime.now(UTC).isoformat(),
            config_sha256=config_sha256,
            stats_url=_derive_stats_url(callback_url, state.manager_url),
            stats=PipelineStats(
                run_id=req.run_id,
                pipeline_name=req.pipeline_name,
                schedule_type=req.schedule_type,
            ),
            # V18-01 §5: attempt identity from the admission; legacy dispatches
            # carry an empty attempt_id and the legacy marker.
            attempt_id=attempt_id or "",
            generation=req.generation,
            slot_id=req.slot_id,
            legacy=not authorized,
        )
        # F.1 (§3.2b): worker-mode runs reach the transform-state blob through
        # the manager's internal API (the same availability envelope as the
        # dispatch that created this run). No manager URL → no store, so
        # stateful transforms stay in-memory (correct for a single run).
        from tram.pipeline.state_store import HttpTransformStateStore

        state_store = (
            HttpTransformStateStore(state.manager_url, state.api_key)
            if state.manager_url
            else None
        )
        # GH #54: worker-mode skip_processed rides the manager's processed-file
        # tracker through the internal API (the F.1 state-store availability
        # envelope — a worker run cannot exist without a manager dispatch). No
        # manager URL → no client, and the construction guard below fires.
        from tram.agent.file_tracker_client import HttpFileTracker

        file_tracker = (
            HttpFileTracker(
                state.manager_url,
                state.api_key,
                on_degradation=active_run.degradation_notes.append,
            )
            if state.manager_url
            else None
        )
        # V18-06: an AUTHORIZED run mints a manager-authoritative
        # delivery-checkpoint client bound to the admitted attempt identity
        # (attempt_id + generation; the same manager-URL source run-complete
        # uses) so strict pipelines can gate unit acks on the manager's
        # commit. Legacy-admitted runs (no attempt identity) and runs without
        # a manager URL get None — the executor's gate then keeps the legacy
        # best-effort / strict fail-closed behavior unchanged.
        checkpoint_client = None
        if (
            authorized
            and attempt_id
            and state.manager_url
            and req.generation is not None
        ):
            from tram.pipeline.executor import CheckpointClient

            checkpoint_client = CheckpointClient(
                state.manager_url,
                state.api_key,
                generation=req.generation,
                attempt_id=attempt_id,
            )
        executor = PipelineExecutor(
            state_store=state_store,
            file_tracker=file_tracker,
            checkpoint_client=checkpoint_client,
        )

        # GH #39 (A1)/#54: the worker is stateless — no per-worker DB exists,
        # so without a manager URL the executor is built without a
        # processed-file tracker and a pipeline whose source requests
        # skip_processed would silently reprocess every file on every run. Fail
        # loud instead and record the degradation on the run (when a tracker
        # client errors at call time, HttpFileTracker fails loud itself) so the
        # manager's run_history row carries it.
        if (
            _source_requests_skip_processed(config)
            and getattr(executor, "_file_tracker", None) is None
        ):
            logger.error(
                "skip_processed cannot be honored in worker mode — no "
                "processed-file tracker (stateless worker, no per-worker DB)",
                extra={
                    "pipeline": req.pipeline_name,
                    "run_id": req.run_id,
                    "source": config.source.type,
                },
            )
            active_run.degradation_notes.append(_SKIP_PROCESSED_DISABLED_NOTE)

        data_dir = os.environ.get("TRAM_DATA_DIR", "/data")
        api_key  = os.environ.get("TRAM_API_KEY", "")

        if req.schedule_type == "stream":
            def _stream_thread():
                try:
                    from tram.agent.assets import sync_assets
                    sync_assets(config, state.manager_url, data_dir, api_key)
                    # V18-07: the single monotonic drain deadline is threaded
                    # to the in-flight stream executor; the drain also sets the
                    # run's stop_event (the mid-run interrupt). The returned
                    # RunResult carries the drain reason when the deadline
                    # interrupted the reader.
                    stream_result = executor.stream_run(
                        config, active_run.stop_event, stats=active_run.stats,
                        config_sha256=config_sha256,
                        deadline=state.drain_deadline,
                    )
                    # C2: emit buffered processed-file marks (batched) before
                    # the run-complete callback so the next run sees them.
                    _flush_file_tracker(file_tracker)
                    stats_snapshot = _final_stats_snapshot(active_run)
                    # V18-07: a stream interrupted by the drain (deadline
                    # expired, or its stop_event fired while the worker is
                    # draining) reports ABORTED with the drain reason — never
                    # clean success for an interrupted run.
                    completion_status = "success"
                    completion_error = None
                    completion_errors = (
                        list(stats_snapshot["errors_last_window"])
                        + active_run.degradation_notes
                    )
                    if (
                        stream_result is not None
                        and stream_result.status.value in ("aborted", "partial")
                    ):
                        completion_status = stream_result.status.value
                        completion_error = stream_result.error
                        if completion_error:
                            completion_errors.append(completion_error)
                    elif state.drain_event.is_set():
                        completion_status = "aborted"
                        completion_error = "drained: worker drain requested"
                        completion_errors.append(completion_error)
                    result_json = None
                    if attempt_id:
                        # V18-01 §4: journal-first completion — the row commits
                        # BEFORE the run leaves WorkerState (the outbox drain,
                        # not this thread, delivers — V18-08).
                        result_json = _completion_result_json(
                            run_id=req.run_id,
                            pipeline_name=req.pipeline_name,
                            worker_id=state.worker_id,
                            attempt_id=attempt_id,
                            status=completion_status,
                            error=completion_error,
                            records_in=int(stats_snapshot["records_in"]),
                            records_out=int(stats_snapshot["records_out"]),
                            records_skipped=int(stats_snapshot["records_skipped"]),
                            bytes_in=int(stats_snapshot["bytes_in"]),
                            bytes_out=int(stats_snapshot["bytes_out"]),
                            errors=completion_errors,
                            started_at=active_run.started_at,
                            finished_at=datetime.now(UTC).isoformat(),
                            legacy=active_run.legacy,
                            generation=active_run.generation,
                            dlq_count=int(stats_snapshot.get("dlq_count") or 0),
                        )
                    _commit_completion(
                        journal,
                        attempt_id=attempt_id,
                        run_id=req.run_id,
                        result_json=result_json,
                        callback_url=callback_url,
                        pipeline_name=req.pipeline_name,
                        worker_id=state.worker_id,
                        status=completion_status,
                        records_in=int(stats_snapshot["records_in"]),
                        records_out=int(stats_snapshot["records_out"]),
                        records_skipped=int(stats_snapshot["records_skipped"]),
                        bytes_in=int(stats_snapshot["bytes_in"]),
                        bytes_out=int(stats_snapshot["bytes_out"]),
                        error=completion_error,
                        errors=completion_errors,
                        started_at=active_run.started_at,
                        finished_at=datetime.now(UTC).isoformat(),
                        api_key=state.api_key,
                        client=state.rpc_client,
                    )
                except Exception as exc:
                    logger.error(
                        "Stream run error",
                        extra={
                            "pipeline": req.pipeline_name,
                            "run_id": req.run_id,
                            "error": str(exc),
                        },
                    )
                    # C2: files finalized before the failure still get their
                    # buffered marks flushed.
                    _flush_file_tracker(file_tracker)
                    result_json = None
                    if attempt_id:
                        result_json = _completion_result_json(
                            run_id=req.run_id,
                            pipeline_name=req.pipeline_name,
                            worker_id=state.worker_id,
                            attempt_id=attempt_id,
                            status="error",
                            error=str(exc),
                            errors=[str(exc)],
                            started_at=active_run.started_at,
                            finished_at=datetime.now(UTC).isoformat(),
                            legacy=active_run.legacy,
                            generation=active_run.generation,
                        )
                    _commit_completion(
                        journal,
                        attempt_id=attempt_id,
                        run_id=req.run_id,
                        result_json=result_json,
                        callback_url=callback_url,
                        pipeline_name=req.pipeline_name,
                        worker_id=state.worker_id,
                        status="error",
                        error=str(exc),
                        started_at=active_run.started_at,
                        finished_at=datetime.now(UTC).isoformat(),
                        api_key=state.api_key,
                        client=state.rpc_client,
                    )
                finally:
                    state.remove(req.run_id, attempt_id=attempt_id)

            t = threading.Thread(
                target=_stream_thread,
                daemon=True,
                name=f"tram-agent-stream-{req.run_id}",
            )
        else:
            def _batch_thread():
                try:
                    from tram.agent.assets import sync_assets
                    sync_assets(config, state.manager_url, data_dir, api_key)
                    # V18-07: the run's stop_event + the single monotonic drain
                    # deadline are threaded to the in-flight batch executor —
                    # on either, it finishes the CURRENT source unit cleanly
                    # (commit barrier + checkpoint + ack) and returns an
                    # ``aborted`` result whose error carries the drain reason.
                    result = executor.batch_run(
                        config, run_id=req.run_id, stats=active_run.stats,
                        config_sha256=config_sha256,
                        flush=req.flush,
                        stop_event=active_run.stop_event,
                        deadline=state.drain_deadline,
                    )
                    # V18-07: when the drain's cooperative stop interrupts a
                    # batch BEFORE the deadline, the executor reports ABORTED
                    # with a generic interrupt reason — surface the drain
                    # reason explicitly on the completion payload (mirrors the
                    # stream thread's drain override).
                    if (
                        result.status.value == "aborted"
                        and state.drain_event.is_set()
                        and "drain" not in (result.error or "")
                    ):
                        result.error = "drained: worker drain requested"
                    # C2: emit buffered processed-file marks (batched) before
                    # the run-complete callback so the next run sees them.
                    _flush_file_tracker(file_tracker)
                    result_json = None
                    if attempt_id:
                        # V18-01 §4: journal-first completion — the row commits
                        # BEFORE the run leaves WorkerState (the outbox drain,
                        # not this thread, delivers — V18-08).
                        result_json = _completion_result_json(
                            run_id=req.run_id,
                            pipeline_name=req.pipeline_name,
                            worker_id=state.worker_id,
                            attempt_id=attempt_id,
                            status=result.status.value,
                            records_in=result.records_in,
                            records_out=result.records_out,
                            records_skipped=result.records_skipped,
                            bytes_in=result.bytes_in,
                            bytes_out=result.bytes_out,
                            error=result.error,
                            errors=list(result.errors or []),
                            started_at=result.started_at.isoformat(),
                            finished_at=result.finished_at.isoformat(),
                            legacy=active_run.legacy,
                            generation=active_run.generation,
                            # V18-06: the per-sink disposition / spool maps and
                            # delivery counters the manager's run-history
                            # decode records ("where present" — empty maps
                            # stay absent from the payload's recorded set).
                            dlq_count=result.dlq_count,
                            records_failed=result.records_failed,
                            dlq_succeeded=result.dlq_succeeded,
                            dlq_failed=result.dlq_failed,
                            disposition=result.disposition or None,
                            spool=result.spool or None,
                        )
                    if active_run.stats is not None:
                        payload = {
                            "worker_id": state.worker_id,
                            "pipeline_name": req.pipeline_name,
                            "run_id": req.run_id,
                            "schedule_type": req.schedule_type,
                            "uptime_seconds": max(
                                (datetime.now(UTC) - active_run.started_at_dt).total_seconds(),
                                0.0,
                            ),
                            "timestamp": datetime.now(UTC).isoformat(),
                            "is_final": True,
                            # v1.5.0 (GH #72): stack-consistency guard field.
                            "snmp_stack": state.snmp_stack,
                            **active_run.stats.snapshot_and_reset_window(),
                        }
                        # If stats_url is empty, the completion still commits
                        # below and manager-side on_worker_run_complete removes
                        # the store entry.
                        _post_stats(
                            active_run.stats_url, payload, api_key=state.api_key,
                            client=state.rpc_client,
                        )
                    _commit_completion(
                        journal,
                        attempt_id=attempt_id,
                        run_id=req.run_id,
                        result_json=result_json,
                        callback_url=callback_url,
                        pipeline_name=req.pipeline_name,
                        worker_id=state.worker_id,
                        status=result.status.value,
                        records_in=result.records_in,
                        records_out=result.records_out,
                        records_skipped=result.records_skipped,
                        bytes_in=result.bytes_in,
                        bytes_out=result.bytes_out,
                        error=result.error,
                        errors=list(result.errors or []) + active_run.degradation_notes,
                        started_at=result.started_at.isoformat(),
                        finished_at=result.finished_at.isoformat(),
                        api_key=state.api_key,
                        client=state.rpc_client,
                    )
                except Exception as exc:
                    logger.error(
                        "Batch run error",
                        extra={
                            "pipeline": req.pipeline_name,
                            "run_id": req.run_id,
                            "error": str(exc),
                        },
                    )
                    # C2: files finalized before the failure still get their
                    # buffered marks flushed.
                    _flush_file_tracker(file_tracker)
                    result_json = None
                    if attempt_id:
                        result_json = _completion_result_json(
                            run_id=req.run_id,
                            pipeline_name=req.pipeline_name,
                            worker_id=state.worker_id,
                            attempt_id=attempt_id,
                            status="error",
                            error=str(exc),
                            errors=[str(exc)],
                            started_at=active_run.started_at,
                            finished_at=datetime.now(UTC).isoformat(),
                            legacy=active_run.legacy,
                            generation=active_run.generation,
                        )
                    _commit_completion(
                        journal,
                        attempt_id=attempt_id,
                        run_id=req.run_id,
                        result_json=result_json,
                        callback_url=callback_url,
                        pipeline_name=req.pipeline_name,
                        worker_id=state.worker_id,
                        status="error",
                        error=str(exc),
                        started_at=active_run.started_at,
                        finished_at=datetime.now(UTC).isoformat(),
                        api_key=state.api_key,
                        client=state.rpc_client,
                    )
                finally:
                    state.remove(req.run_id, attempt_id=attempt_id)

            t = threading.Thread(
                target=_batch_thread,
                daemon=True,
                name=f"tram-agent-batch-{req.run_id}",
            )

        active_run.thread = t
        state.add(active_run)
        t.start()

        if attempt_id:
            # V18-01 §4: transition the reservation to running once the thread
            # has started. A fast-completing run may already be 'completed'
            # (mark_running is a no-op then) — the read-back state is echoed.
            journal.mark_running(attempt_id)
            rec = journal.get_attempt(attempt_id)
            return {
                "accepted": True,
                "run_id": req.run_id,
                "attempt_id": attempt_id,
                "worker_id": worker_id,
                "state": rec.state if rec is not None else "running",
            }
        return {
            "accepted": True,
            "run_id": req.run_id,
            "worker_id": worker_id,
            "legacy": True,
        }

    # ── POST /agent/stop ───────────────────────────────────────────────────

    @app.post("/agent/stop")
    def stop(req: StopRequest):  # noqa: A001
        active_run = state.get(req.run_id)
        if active_run is None:
            raise HTTPException(
                status_code=404,
                detail=f"run_id {req.run_id!r} not found on this worker",
            )
        active_run.stop_event.set()
        return {"stopping": True, "run_id": req.run_id, "worker_id": worker_id}

    # ── POST /agent/drain (V18-07, plan E / frozen §5) ─────────────────────

    @app.post("/agent/drain", status_code=202)
    def drain():
        """Machine-authenticated worker drain (frozen §5 protocol table).

        Marks the worker draining — ``/agent/status`` surfaces
        ``admission_state: "draining"`` and new ``/agent/run`` dispatches are
        refused 503 admission closed (draining). SIGTERM shutdown runs the
        SAME state machine (plan E): one monotonic deadline = now +
        ``TRAM_DRAIN_TIMEOUT_S``, threaded to every in-flight run executor —
        batch runs finish the current source unit cleanly, stream readers are
        interrupted at the deadline. In-flight runs are signalled
        cooperatively via their stop_event (the mid-run delivery mechanism);
        the deadline bounds the wait and shutdown proceeds regardless after it
        expires. A second drain call is idempotent (202, first deadline kept —
        a repeat never extends the bound).
        """
        timeout_s = cfg.drain_timeout_s()
        deadline = time.monotonic() + timeout_s
        first_call = state.begin_drain(deadline)
        for run in state.snapshot():
            run.stop_event.set()
        logger.info(
            "Worker drain requested",
            extra={
                "worker_id": worker_id,
                "deadline_seconds": timeout_s,
                "active_runs": len(state.snapshot()),
                "already_draining": not first_call,
            },
        )
        return {
            "draining": True,
            "worker_id": worker_id,
            "admission_state": state.admission_state,
            "deadline_seconds": timeout_s,
            "active_runs": len(state.snapshot()),
            "already_draining": not first_call,
        }

    return app


def create_worker_ingress_app(worker_id: str = "", api_key: str = "") -> FastAPI:
    """Minimal push-traffic receiver on :8767 — /webhooks/* only, no /agent/* routes."""
    from tram.api.routers.webhooks import router as webhooks_router

    app = FastAPI(title="TRAM Worker Ingress", openapi_url=None)
    app.include_router(webhooks_router)

    if api_key:
        from tram.api.middleware import APIKeyMiddleware
        app.add_middleware(APIKeyMiddleware)

    @app.get("/agent/health")
    def ingress_health():
        return {"ok": True, "worker_id": worker_id, "port": "ingress"}

    return app
