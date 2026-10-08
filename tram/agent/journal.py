"""V18-01 §4 worker journal — stdlib-sqlite durable admission/completion store.

One journal file per worker (``TRAM_WORKER_JOURNAL_PATH``, frozen at
``/var/lib/tram/worker/journal.db``).  This module implements the journal
CORE: the full frozen schema (all tables, so no schema change is ever added
later), the atomic admission transaction, the revocation tombstone, the
completion/outbox protocol, the replay query API, boot interruption, the
health/quota surface the server lane uses to fail readiness closed, and the
V18-01 §4 rejection watermark / GC / retention lane.

Deliberately stdlib-``sqlite3`` only — the worker image must not depend on
SQLAlchemy.  Every journal is opened with WAL, ``synchronous=FULL``,
``busy_timeout=5000`` and file mode 0600.

Admission accepts either a pre-validated ``AuthorizationDecision`` (the
server lane's legacy-admit path, raw ``admit()``) or a real
start-authorization token via ``validate_and_admit`` (stdlib HMAC
mint/validate helpers in ``tram.agent.auth_tokens``, reused by the manager).
Every admission transaction first advances the current epoch's
``clock_watermarks`` rejection watermark (frozen ordering); a local clock
below ``watermark - skew`` fails admission closed.  ``gc_expired`` and
``cleanup_retention`` delete expired/resolved idempotency rows only after the
authorizations that could have admitted them are dead, with the watermark
advance in the same transaction — after GC, replaying a retired token is
refused by the watermark alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from tram.agent.auth_tokens import validate_start_authorization
from tram.core import config as cfg

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"
_WAL_MODE = "wal"
_BUSY_TIMEOUT_MS = 5000
_JOURNAL_FILE_MODE = 0o600
_OUTBOX_BACKOFF_BASE_S = 1.0
_OUTBOX_BACKOFF_CAP_S = 300.0

# The frozen V18-01 §4 schema, verbatim (all eight tables including
# ``spool_entries`` so the follow-up lanes never add a schema change).
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS journal_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS admission_reservations (
        attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, pipeline_name TEXT NOT NULL,
        generation INTEGER NOT NULL, slot_id TEXT NOT NULL DEFAULT '',
        worker_session TEXT NOT NULL, reserved_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('reserved','running','completed','interrupted')),
        thread_started INTEGER NOT NULL DEFAULT 0)
    """,
    """
    CREATE TABLE IF NOT EXISTS revocation_tombstones (
        attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, reason TEXT NOT NULL,
        revoked_at TEXT NOT NULL, session_epoch INTEGER NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS start_authorizations (
        attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, session_epoch INTEGER NOT NULL,
        token_hash TEXT NOT NULL UNIQUE, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS clock_watermarks (
        session_epoch INTEGER PRIMARY KEY, watermark_ms INTEGER NOT NULL, updated_at TEXT NOT NULL)
    """,
    """
    CREATE TABLE IF NOT EXISTS completions (
        attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, result_json TEXT NOT NULL,
        completed_at TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0, acked_at TEXT)
    """,
    """
    CREATE TABLE IF NOT EXISTS outbox (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL, kind TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)
    """,
    """
    CREATE TABLE IF NOT EXISTS spool_entries (
        spool_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, source_unit TEXT NOT NULL,
        path TEXT NOT NULL, bytes INTEGER NOT NULL, fsynced_at TEXT NOT NULL,
        published INTEGER NOT NULL DEFAULT 0, published_at TEXT)
    """,
)

# Fields a *valid* AuthorizationDecision must carry to record the
# start_authorizations row and the admission reservation identity.
_AUTH_REQUIRED_FIELDS = (
    "attempt_id",
    "run_id",
    "generation",
    "slot_id",
    "worker_session",
    "session_epoch",
    "token_hash",
    "issued_at",
    "expires_at",
)


class JournalError(Exception):
    """Base error for all worker-journal failures."""


class JournalUnavailableError(JournalError):
    """The journal cannot serve requests (corrupt or otherwise unavailable)."""


class AdmissionClosedError(JournalUnavailableError):
    """Admission is closed: journal unavailable, corrupt, or at quota.

    The server lane maps this to HTTP 503 (retryable admission closed).
    """


class AdmissionConflictError(JournalError):
    """Repeat of an attempt_id with a different run/generation/slot identity.

    The server lane maps this to HTTP 409.
    """

    def __init__(self, message: str, attempt_id: str) -> None:
        super().__init__(message)
        self.attempt_id = attempt_id


@dataclass(frozen=True)
class AuthorizationDecision:
    """Pre-validated start-authorization decision accepted by ``admit``.

    Produced by the follow-up authorization lane (real HMAC validator +
    ``mint_start_authorization`` helper).  ``valid=False`` decisions carry a
    ``reason`` and are refused without any insert; valid decisions must carry
    every field in ``_AUTH_REQUIRED_FIELDS``.
    """

    valid: bool
    reason: str | None = None
    attempt_id: str | None = None
    run_id: str | None = None
    generation: int | None = None
    slot_id: str | None = None
    worker_session: str | None = None
    session_epoch: int | None = None
    token_hash: str | None = None
    issued_at: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True)
class AttemptRecord:
    """Discriminated record for one attempt, from ``get_attempt``.

    ``kind`` is one of ``tombstone`` | ``completion`` | ``active`` |
    ``interrupted``; a missing attempt returns ``None`` so the caller can
    distinguish 404 from a revoked/interrupted/completed state.
    """

    kind: Literal["tombstone", "completion", "active", "interrupted"]
    attempt_id: str
    run_id: str | None = None
    reason: str | None = None
    revoked_at: str | None = None
    session_epoch: int | None = None
    result_json: str | None = None
    completed_at: str | None = None
    acked: bool | None = None
    acked_at: str | None = None
    pipeline_name: str | None = None
    generation: int | None = None
    slot_id: str | None = None
    worker_session: str | None = None
    reserved_at: str | None = None
    state: str | None = None
    thread_started: bool | None = None


@dataclass(frozen=True)
class AdmissionResult:
    """Outcome of one admission transaction.

    ``outcome`` is ``admitted`` | ``already_admitted`` | ``revoked`` |
    ``refused_auth``.  ``existing`` carries the prior reservation row for
    ``already_admitted``; ``tombstone_*`` echoes the tombstone for ``revoked``;
    ``refusal_reason`` explains ``refused_auth``.
    """

    outcome: Literal["admitted", "already_admitted", "revoked", "refused_auth"]
    attempt_id: str | None
    existing: AttemptRecord | None = None
    tombstone_reason: str | None = None
    tombstone_revoked_at: str | None = None
    refusal_reason: str | None = None


@dataclass(frozen=True)
class TombstoneRecord:
    attempt_id: str
    run_id: str
    reason: str
    revoked_at: str
    session_epoch: int


@dataclass(frozen=True)
class OutboxRow:
    seq: int
    attempt_id: str
    kind: str
    payload_json: str
    created_at: str
    next_attempt_at: str
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class JournalHealth:
    """Readiness-relevant health of the journal.

    ``state`` is ``ok`` | ``unavailable`` | ``corrupt`` | ``quota``.  The
    server lane fails readiness closed unless the state is ``ok`` and reports
    ``quota``/``corrupt``/``unavailable`` distinctly on ``/agent/status``.
    """

    state: Literal["ok", "unavailable", "corrupt", "quota"]
    detail: str = ""
    size_bytes: int | None = None
    quota_bytes: int | None = None
    headroom_bytes: int | None = None


@dataclass(frozen=True)
class WatermarkStatus:
    """Rejection watermark of the current session epoch (V18-01 §4).

    ``admitting`` is ``False`` when the local clock is below
    ``watermark_ms - skew_ms`` — new admission then fails closed and the
    server lane reports the condition distinctly on ``/agent/status``.  The
    watermark is never reset by a restart; ``behind_ms`` is how far the local
    clock is behind the watermark (0 or negative when it is not behind).
    """

    session_epoch: int
    watermark_ms: int | None
    now_ms: int
    behind_ms: int
    admitting: bool


@dataclass(frozen=True)
class GcReport:
    """Outcome of one ``gc_expired`` run (authorization/tombstone GC)."""

    authorizations_deleted: int
    tombstones_deleted: int
    watermark_ms: int


@dataclass(frozen=True)
class RetentionReport:
    """Outcome of one ``cleanup_retention`` run."""

    completions_deleted: int
    outbox_deleted: int
    tombstones_deleted: int


class WorkerJournal:
    """Durable per-worker journal (V18-01 §4) backed by stdlib sqlite3.

    All mutations run inside ``BEGIN IMMEDIATE`` transactions serialized by a
    process-local lock (one process owns the journal; ``busy_timeout`` covers
    cross-process contention during rolling restarts).  A corrupt or
    unavailable journal is cached and reported by ``health()`` forever after —
    the file is never destructively recreated, so recoverable rows survive for
    operator salvage.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        quota_mb: int | float | None = None,
        headroom_mb: int | float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        quota_mb = cfg.worker_journal_quota_mb() if quota_mb is None else quota_mb
        headroom_mb = cfg.worker_journal_headroom_mb() if headroom_mb is None else headroom_mb
        self._quota_bytes = int(max(float(quota_mb), 0.0) * 1024 * 1024)
        self._headroom_bytes = int(max(float(headroom_mb), 0.0) * 1024 * 1024)
        self._quota_enabled = self._quota_bytes > 0
        # Admission fails closed once size >= quota - headroom; the headroom
        # is the budget in-flight completions/outbox writes may still commit.
        self._quota_threshold_bytes = (
            max(0, self._quota_bytes - self._headroom_bytes) if self._quota_enabled else None
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._fatal_kind: Literal["unavailable", "corrupt"] | None = None
        self._fatal_detail: str | None = None
        self._quarantined = False
        try:
            self._open()
        except sqlite3.OperationalError as exc:
            self._set_fatal("unavailable", exc)
        except sqlite3.DatabaseError as exc:
            self._set_fatal("corrupt", exc)
            self._quarantine_if_corrupt()
        except (sqlite3.Error, OSError) as exc:
            self._set_fatal("unavailable", exc)

    # ── lifecycle ───────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying connection (WAL persists for recovery)."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def __enter__(self) -> WorkerJournal:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _set_fatal(self, kind: Literal["unavailable", "corrupt"], exc: BaseException) -> None:
        self._fatal_kind = kind
        self._fatal_detail = str(exc)
        logger.error("worker journal %s: %s", kind, exc)

    def _open(self) -> None:
        conn = sqlite3.connect(
            str(self._path),
            timeout=_BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            conn.execute(f"PRAGMA journal_mode={_WAL_MODE}")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA_STATEMENTS:
                    conn.execute(statement)
                conn.execute(
                    "INSERT OR IGNORE INTO journal_meta (key, value) VALUES ('schema_version', ?)",
                    (_SCHEMA_VERSION,),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO journal_meta (key, value) VALUES ('session_epoch', '1')"
                )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        self._chmod_files()

    def _ensure_open(self) -> None:
        if self._fatal_kind is not None:
            raise JournalUnavailableError(f"journal {self._fatal_kind}: {self._fatal_detail}")
        if self._conn is None:
            try:
                self._open()
            except sqlite3.OperationalError as exc:
                self._set_fatal("unavailable", exc)
                raise JournalUnavailableError(f"journal unavailable: {exc}") from exc
            except sqlite3.DatabaseError as exc:
                self._set_fatal("corrupt", exc)
                self._quarantine_if_corrupt()
                raise JournalUnavailableError(
                    f"journal corrupt: {exc}"
                ) from exc
            except (sqlite3.Error, OSError) as exc:
                self._set_fatal("unavailable", exc)
                raise JournalUnavailableError(
                    f"journal unavailable: {exc}"
                ) from exc

    def _chmod_files(self) -> None:
        """Best-effort 0600 on the journal and its WAL sidecars."""
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self._path) + suffix)
            try:
                if candidate.exists():
                    os.chmod(candidate, _JOURNAL_FILE_MODE)
            except OSError:
                logger.debug("could not chmod journal file %s", candidate, exc_info=True)

    def _quarantine_if_corrupt(self) -> None:
        """Copy the corrupt journal (main + WAL sidecars) aside for salvage.

        The journal file itself is never deleted or recreated in place, so the
        recoverable rows stay available for operator recovery (V18-01 §4:
        journal/PVC loss requires a new worker session and operator recovery).
        """
        if self._quarantined:
            return
        self._quarantined = True
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(self._path) + suffix)
                if src.is_file():
                    shutil.copy2(src, Path(str(self._path) + f".corrupt-{stamp}" + suffix))
        except OSError:
            logger.warning("could not quarantine corrupt journal %s", self._path, exc_info=True)

    # ── transactions ────────────────────────────────────────────────────────

    def _txn(self, fn: Callable[[sqlite3.Connection], object]) -> object:
        """Run ``fn(conn)`` inside one ``BEGIN IMMEDIATE`` transaction."""
        with self._lock:
            self._ensure_open()
            conn = self._conn
            assert conn is not None
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise
            self._chmod_files()
            return result

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    @staticmethod
    def _normalize_dt(now: datetime) -> datetime:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return now.astimezone(UTC)

    def _now(self) -> datetime:
        return self._normalize_dt(self._clock())

    def _now_iso(self) -> str:
        return self._now().isoformat()

    @staticmethod
    def _session_epoch_locked(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM journal_meta WHERE key = 'session_epoch'").fetchone()
        return int(row["value"]) if row is not None else 0

    def _advance_watermark_locked(
        self, conn: sqlite3.Connection, session_epoch: int, now_ms: int
    ) -> None:
        """Advance the epoch's watermark to MAX(existing, now_ms) (frozen ordering).

        Called as the FIRST statement of every admission / GC / retention
        transaction, before any identity-row work or delete.
        """
        conn.execute(
            "INSERT INTO clock_watermarks (session_epoch, watermark_ms, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(session_epoch) DO UPDATE SET "
            "watermark_ms = MAX(clock_watermarks.watermark_ms, excluded.watermark_ms), "
            "updated_at = excluded.updated_at",
            (session_epoch, now_ms, self._now_iso()),
        )

    # ── health ──────────────────────────────────────────────────────────────

    def health(self) -> JournalHealth:
        """Report ``ok`` | ``unavailable`` | ``corrupt`` | ``quota``.

        A corrupt journal is quarantined (copy preserved) on first detection
        and the state is cached: the file is never silently rebuilt, so
        recoverable rows are preserved for operator salvage.
        """
        with self._lock:
            return self._health_locked()

    def _health_locked(self) -> JournalHealth:
        if self._fatal_kind is not None:
            return JournalHealth(
                state=self._fatal_kind,
                detail=self._fatal_detail or "",
                size_bytes=self._db_size_locked(),
                quota_bytes=self._quota_bytes if self._quota_enabled else None,
                headroom_bytes=self._headroom_bytes if self._quota_enabled else None,
            )
        try:
            self._ensure_open()
            conn = self._conn
            assert conn is not None
            conn.execute("SELECT 1 FROM journal_meta LIMIT 1").fetchone()
        except JournalUnavailableError:
            return JournalHealth(
                state=self._fatal_kind or "unavailable",
                detail=self._fatal_detail or "",
                size_bytes=self._db_size_locked(),
                quota_bytes=self._quota_bytes if self._quota_enabled else None,
                headroom_bytes=self._headroom_bytes if self._quota_enabled else None,
            )
        except sqlite3.OperationalError as exc:
            self._set_fatal("unavailable", exc)
            return JournalHealth(
                state="unavailable",
                detail=str(exc),
                size_bytes=self._db_size_locked(),
                quota_bytes=self._quota_bytes if self._quota_enabled else None,
                headroom_bytes=self._headroom_bytes if self._quota_enabled else None,
            )
        except sqlite3.DatabaseError as exc:
            self._set_fatal("corrupt", exc)
            self._quarantine_if_corrupt()
            return JournalHealth(
                state="corrupt",
                detail=str(exc),
                size_bytes=self._db_size_locked(),
                quota_bytes=self._quota_bytes if self._quota_enabled else None,
                headroom_bytes=self._headroom_bytes if self._quota_enabled else None,
            )
        size = self._db_size_locked()
        if self._quota_enabled and size >= self._quota_threshold_bytes:
            return JournalHealth(
                state="quota",
                detail=f"journal size {size} bytes >= quota threshold "
                f"{self._quota_threshold_bytes} bytes",
                size_bytes=size,
                quota_bytes=self._quota_bytes,
                headroom_bytes=self._headroom_bytes,
            )
        return JournalHealth(
            state="ok",
            size_bytes=size,
            quota_bytes=self._quota_bytes if self._quota_enabled else None,
            headroom_bytes=self._headroom_bytes if self._quota_enabled else None,
        )

    def _db_size_locked(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self._path) + suffix)
            try:
                total += candidate.stat().st_size
            except OSError:
                continue
        return total

    def _ensure_admitting(self) -> None:
        """Fail admission closed unless the journal is healthy and under quota."""
        state = self._health_locked().state
        if state != "ok":
            raise AdmissionClosedError(f"journal {state}: admission closed")

    # ── admission ───────────────────────────────────────────────────────────

    def admit(self, auth: AuthorizationDecision, pipeline_name: str) -> AdmissionResult:
        """One atomic admission transaction (V18-01 §4, frozen ordering).

        1. Refuse invalid authorizations without any insert;
        2. reject attempts with a revocation tombstone (echo the reason);
        3. insert the reservation — a repeat with the same identity returns
           ``already_admitted`` with the existing row, a repeat with a
           different run/generation/slot raises ``AdmissionConflictError``.
        """
        with self._lock:
            if not auth.valid:
                return AdmissionResult(
                    outcome="refused_auth",
                    attempt_id=auth.attempt_id,
                    refusal_reason=auth.reason,
                )
            missing = [
                field for field in _AUTH_REQUIRED_FIELDS if getattr(auth, field) is None
            ]
            if missing:
                raise JournalError(
                    f"valid AuthorizationDecision missing fields: {', '.join(missing)}"
                )
            self._ensure_admitting()
            conn = self._conn
            assert conn is not None
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Step 0 — rejection watermark (frozen ordering, V18-01 §4):
                # every admission decision FIRST advances the current epoch's
                # clock_watermarks to MAX(existing, now_ms) in this same
                # transaction.  A local clock below watermark - skew fails
                # admission closed with a distinct reportable reason; the
                # watermark itself is never lowered by a restart.
                epoch = self._session_epoch_locked(conn)
                now_ms = int(self._now().timestamp() * 1000)
                self._advance_watermark_locked(conn, epoch, now_ms)
                watermark_row = conn.execute(
                    "SELECT watermark_ms FROM clock_watermarks WHERE session_epoch = ?",
                    (epoch,),
                ).fetchone()
                assert watermark_row is not None
                watermark_ms = int(watermark_row["watermark_ms"])
                skew_ms = cfg.auth_clock_skew_s() * 1000
                if now_ms < watermark_ms - skew_ms:
                    raise AdmissionClosedError(
                        "admission closed: local clock below watermark "
                        f"(clock {now_ms} ms < watermark {watermark_ms} ms "
                        f"- skew {skew_ms} ms)"
                    )
                # Step 1 — record the pre-validated authorization.  A repeat
                # of the same attempt keeps its first token (ON CONFLICT DO
                # NOTHING); reusing a token_hash for a *different* attempt is a
                # conflict the caller reports as 409.
                try:
                    conn.execute(
                        "INSERT INTO start_authorizations "
                        "(attempt_id, run_id, session_epoch, token_hash, issued_at, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(attempt_id) DO NOTHING",
                        (
                            auth.attempt_id,
                            auth.run_id,
                            auth.session_epoch,
                            auth.token_hash,
                            auth.issued_at,
                            auth.expires_at,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AdmissionConflictError(
                        f"attempt {auth.attempt_id!r}: authorization token already in use",
                        auth.attempt_id,
                    ) from exc
                # Step 2 — revocation tombstone gate.
                tomb = conn.execute(
                    "SELECT run_id, reason, revoked_at FROM revocation_tombstones "
                    "WHERE attempt_id = ?",
                    (auth.attempt_id,),
                ).fetchone()
                if tomb is not None:
                    self._rollback(conn)
                    return AdmissionResult(
                        outcome="revoked",
                        attempt_id=auth.attempt_id,
                        tombstone_reason=tomb["reason"],
                        tombstone_revoked_at=tomb["revoked_at"],
                    )
                # Step 3 — reservation insert (idempotent repeat / 409 conflict).
                try:
                    conn.execute(
                        "INSERT INTO admission_reservations "
                        "(attempt_id, run_id, pipeline_name, generation, slot_id, "
                        " worker_session, reserved_at, state, thread_started) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', 0)",
                        (
                            auth.attempt_id,
                            auth.run_id,
                            pipeline_name,
                            auth.generation,
                            auth.slot_id,
                            auth.worker_session,
                            self._now_iso(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        "SELECT attempt_id, run_id, pipeline_name, generation, slot_id, "
                        "worker_session, reserved_at, state, thread_started "
                        "FROM admission_reservations WHERE attempt_id = ?",
                        (auth.attempt_id,),
                    ).fetchone()
                    if existing is None:
                        raise AdmissionConflictError(
                            f"attempt {auth.attempt_id!r}: reservation vanished mid-transaction",
                            auth.attempt_id,
                        ) from None
                    identity_matches = (
                        existing["run_id"],
                        existing["generation"],
                        existing["slot_id"],
                    ) == (
                        auth.run_id,
                        auth.generation,
                        auth.slot_id,
                    )
                    if not identity_matches:
                        raise AdmissionConflictError(
                            f"attempt {auth.attempt_id!r} already admitted with a different "
                            f"identity (existing run={existing['run_id']!r} "
                            f"generation={existing['generation']} slot={existing['slot_id']!r}; "
                            f"requested run={auth.run_id!r} generation={auth.generation} "
                            f"slot={auth.slot_id!r})",
                            auth.attempt_id,
                        )
                    self._rollback(conn)
                    return AdmissionResult(
                        outcome="already_admitted",
                        attempt_id=auth.attempt_id,
                        existing=self._attempt_from_reservation(existing),
                    )
                conn.execute("COMMIT")
                return AdmissionResult(outcome="admitted", attempt_id=auth.attempt_id)
            except BaseException:
                self._rollback(conn)
                raise
            finally:
                self._chmod_files()

    def validate_and_admit(
        self,
        token: str,
        pipeline_name: str,
        *,
        worker_session: str,
        current_secret: str,
        previous_secret: str | None = None,
        now: datetime | None = None,
    ) -> AdmissionResult:
        """Validate a start-authorization token and admit (V18-01 §4 wiring).

        Uses ``tram.agent.auth_tokens.validate_start_authorization`` (the
        stdlib HMAC validator, reused by the manager's minter), builds the
        ``AuthorizationDecision``, and runs the existing ``admit()`` so the
        admission transaction advances the watermark atomically.

        Refusals: token-level reasons (bad signature, worker-session
        mismatch, future-issued beyond skew, expired, over-max TTL, malformed)
        come back as ``refused_auth`` with the validator's reason; a token
        whose authorization row carries a retired session epoch is refused
        ``old session epoch``; a token whose row is gone (GC'd) is refused by
        the current epoch's watermark alone once the watermark has advanced
        past the token's validity window — replaying a retired token is
        rejected even if the local clock was rolled back inside the token's
        validity.

        Boundary (D4, accepted): that rejection is same-epoch. A token GC'd
        in an OLD epoch and replayed after a trusted-time recovery, at a
        clock still inside its validity window, is not covered by the new
        epoch's watermark (old-epoch watermarks are not consulted — they
        would over-reject fresh tokens after a legitimate recovery); the
        reservation idempotency makes such a replay a no-op while
        reservation rows survive.
        """
        with self._lock:
            now_dt = self._normalize_dt(now if now is not None else self._now())
            self._ensure_open()
            conn = self._conn
            assert conn is not None
            result = validate_start_authorization(
                token,
                worker_session=worker_session,
                current_secret=current_secret,
                previous_secret=previous_secret,
                max_ttl_s=cfg.auth_max_ttl_s(),
                clock_skew_s=cfg.auth_clock_skew_s(),
                now_unix=int(now_dt.timestamp()),
            )
            if not result.valid:
                return self.admit(
                    AuthorizationDecision(
                        valid=False, reason=result.reason, attempt_id=result.attempt_id
                    ),
                    pipeline_name,
                )
            epoch = self._session_epoch_locked(conn)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            row = conn.execute(
                "SELECT session_epoch FROM start_authorizations WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is not None:
                if int(row["session_epoch"]) != epoch:
                    return AdmissionResult(
                        outcome="refused_auth",
                        attempt_id=result.attempt_id,
                        refusal_reason="old session epoch",
                    )
            else:
                watermark_row = conn.execute(
                    "SELECT watermark_ms FROM clock_watermarks WHERE session_epoch = ?",
                    (epoch,),
                ).fetchone()
                if watermark_row is not None:
                    assert result.issued_at_unix is not None and result.ttl_s is not None
                    window_end_ms = (
                        result.issued_at_unix + result.ttl_s + cfg.auth_clock_skew_s()
                    ) * 1000
                    if int(watermark_row["watermark_ms"]) >= window_end_ms:
                        return AdmissionResult(
                            outcome="refused_auth",
                            attempt_id=result.attempt_id,
                            refusal_reason="token retired by watermark",
                        )
            issued_dt = datetime.fromtimestamp(result.issued_at_unix, tz=UTC)
            decision = AuthorizationDecision(
                valid=True,
                attempt_id=result.attempt_id,
                run_id=result.run_id,
                generation=result.generation,
                slot_id=result.slot_id,
                worker_session=worker_session,
                session_epoch=epoch,
                token_hash=token_hash,
                issued_at=issued_dt.isoformat(),
                expires_at=datetime.fromtimestamp(
                    result.issued_at_unix + result.ttl_s, tz=UTC
                ).isoformat(),
            )
            return self.admit(decision, pipeline_name)

    def mark_running(self, attempt_id: str) -> bool:
        """Transition a reserved admission to running (thread started)."""

        def fn(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE admission_reservations SET state = 'running', thread_started = 1 "
                "WHERE attempt_id = ? AND state = 'reserved'",
                (attempt_id,),
            )
            return cur.rowcount > 0

        return bool(self._txn(fn))

    # ── revocation ──────────────────────────────────────────────────────────

    def record_tombstone(self, attempt_id: str, run_id: str, reason: str) -> TombstoneRecord:
        """Record a durable revocation; blocks any later admission."""

        def fn(conn: sqlite3.Connection) -> TombstoneRecord:
            now = self._now_iso()
            epoch = self._session_epoch_locked(conn)
            conn.execute(
                "INSERT OR REPLACE INTO revocation_tombstones "
                "(attempt_id, run_id, reason, revoked_at, session_epoch) "
                "VALUES (?, ?, ?, ?, ?)",
                (attempt_id, run_id, reason, now, epoch),
            )
            return TombstoneRecord(
                attempt_id=attempt_id,
                run_id=run_id,
                reason=reason,
                revoked_at=now,
                session_epoch=epoch,
            )

        return self._txn(fn)  # type: ignore[return-value]

    # ── completion protocol ─────────────────────────────────────────────────

    def record_completion(
        self, attempt_id: str, result_json: str, run_id: str | None = None
    ) -> None:
        """Commit the completion row BEFORE the run leaves active tracking.

        Idempotent: the first completion wins (``ON CONFLICT DO NOTHING``);
        the reservation state is flipped to ``completed`` in the same
        transaction.  ``run_id`` falls back to the reservation row and is
        required when no reservation exists.
        """

        def fn(conn: sqlite3.Connection) -> None:
            if run_id is None:
                row = conn.execute(
                    "SELECT run_id FROM admission_reservations WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if row is None:
                    raise JournalError(
                        f"record_completion: no reservation for attempt {attempt_id!r} "
                        "and no run_id given"
                    )
                resolved_run_id = row["run_id"]
            else:
                resolved_run_id = run_id
            conn.execute(
                "UPDATE admission_reservations SET state = 'completed' WHERE attempt_id = ?",
                (attempt_id,),
            )
            conn.execute(
                "INSERT INTO completions "
                "(attempt_id, run_id, result_json, completed_at, acked, acked_at) "
                "VALUES (?, ?, ?, ?, 0, NULL) ON CONFLICT(attempt_id) DO NOTHING",
                (attempt_id, resolved_run_id, result_json, self._now_iso()),
            )

        self._txn(fn)

    def enqueue_outbox(self, attempt_id: str, kind: str, payload: dict | str) -> int:
        """Enqueue an undelivered event for the attempt; returns its ``seq``."""

        def fn(conn: sqlite3.Connection) -> int:
            payload_json = json.dumps(payload) if not isinstance(payload, str) else payload
            now = self._now_iso()
            cur = conn.execute(
                "INSERT INTO outbox "
                "(attempt_id, kind, payload_json, created_at, next_attempt_at, "
                " attempts, last_error) "
                "VALUES (?, ?, ?, ?, ?, 0, NULL)",
                (attempt_id, kind, payload_json, now, now),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

        return int(self._txn(fn))

    def mark_acked(self, attempt_id: str) -> None:
        """Durable manager ack: mark the completion acked and drain its outbox."""

        def fn(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE completions SET acked = 1, acked_at = ? "
                "WHERE attempt_id = ? AND acked = 0",
                (self._now_iso(), attempt_id),
            )
            conn.execute("DELETE FROM outbox WHERE attempt_id = ?", (attempt_id,))

        self._txn(fn)

    def fetch_due_outbox(self, limit: int = 10) -> list[OutboxRow]:
        """Return due outbox rows (``next_attempt_at <= now``), oldest first."""
        with self._lock:
            self._ensure_open()
            conn = self._conn
            assert conn is not None
            rows = conn.execute(
                "SELECT seq, attempt_id, kind, payload_json, created_at, "
                "next_attempt_at, attempts, last_error FROM outbox "
                "WHERE next_attempt_at <= ? ORDER BY seq LIMIT ?",
                (self._now_iso(), max(1, limit)),
            ).fetchall()
            return [OutboxRow(**dict(row)) for row in rows]

    def record_outbox_failure(self, seq: int, last_error: str) -> None:
        """Record one delivery failure: bump attempts, set error, back off.

        Backoff is exponential (1s, 2s, 4s, ...) capped at 300s.
        """

        def fn(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT attempts FROM outbox WHERE seq = ?", (seq,)
            ).fetchone()
            if row is None:
                raise JournalError(f"record_outbox_failure: no outbox row seq={seq}")
            attempts = int(row["attempts"]) + 1
            delay_s = min(
                _OUTBOX_BACKOFF_BASE_S * (2 ** (attempts - 1)), _OUTBOX_BACKOFF_CAP_S
            )
            next_at = (self._now() + timedelta(seconds=delay_s)).isoformat()
            conn.execute(
                "UPDATE outbox SET attempts = ?, last_error = ?, next_attempt_at = ? "
                "WHERE seq = ?",
                (attempts, last_error, next_at, seq),
            )

        self._txn(fn)

    # ── query / replay ──────────────────────────────────────────────────────

    def get_attempt(self, attempt_id: str) -> AttemptRecord | None:
        """Resolve an attempt: tombstone > completion > reservation.

        ``None`` means the attempt is unknown to this worker (404); the other
        kinds give the caller the 404-vs-state distinction the protocol needs.
        """
        with self._lock:
            self._ensure_open()
            conn = self._conn
            assert conn is not None
            row = conn.execute(
                "SELECT attempt_id, run_id, reason, revoked_at, session_epoch "
                "FROM revocation_tombstones WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is not None:
                return AttemptRecord(
                    kind="tombstone",
                    attempt_id=row["attempt_id"],
                    run_id=row["run_id"],
                    reason=row["reason"],
                    revoked_at=row["revoked_at"],
                    session_epoch=row["session_epoch"],
                )
            row = conn.execute(
                "SELECT attempt_id, run_id, result_json, completed_at, acked, acked_at "
                "FROM completions WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is not None:
                return AttemptRecord(
                    kind="completion",
                    attempt_id=row["attempt_id"],
                    run_id=row["run_id"],
                    result_json=row["result_json"],
                    completed_at=row["completed_at"],
                    acked=bool(row["acked"]),
                    acked_at=row["acked_at"],
                )
            row = conn.execute(
                "SELECT attempt_id, run_id, pipeline_name, generation, slot_id, "
                "worker_session, reserved_at, state, thread_started "
                "FROM admission_reservations WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is not None:
                return self._attempt_from_reservation(row)
            return None

    def list_unacked_completions(self) -> list[AttemptRecord]:
        """Completions not yet durably acknowledged by the manager."""
        with self._lock:
            self._ensure_open()
            conn = self._conn
            assert conn is not None
            rows = conn.execute(
                "SELECT attempt_id, run_id, result_json, completed_at, acked, acked_at "
                "FROM completions WHERE acked = 0 ORDER BY completed_at, attempt_id"
            ).fetchall()
            return [
                AttemptRecord(
                    kind="completion",
                    attempt_id=row["attempt_id"],
                    run_id=row["run_id"],
                    result_json=row["result_json"],
                    completed_at=row["completed_at"],
                    acked=bool(row["acked"]),
                    acked_at=row["acked_at"],
                )
                for row in rows
            ]

    def _attempt_from_reservation(self, row: sqlite3.Row) -> AttemptRecord:
        state = row["state"]
        if state in ("reserved", "running"):
            kind: Literal["tombstone", "completion", "active", "interrupted"] = "active"
        elif state == "interrupted":
            kind = "interrupted"
        else:
            # state == 'completed' without a completions row is unreachable via
            # this API (record_completion is transactional); report it as a
            # completion with no result rather than as active.
            kind = "completion"
        return AttemptRecord(
            kind=kind,
            attempt_id=row["attempt_id"],
            run_id=row["run_id"],
            pipeline_name=row["pipeline_name"],
            generation=row["generation"],
            slot_id=row["slot_id"],
            worker_session=row["worker_session"],
            reserved_at=row["reserved_at"],
            state=state,
            thread_started=bool(row["thread_started"]),
        )

    # ── boot / restart ──────────────────────────────────────────────────────

    def mark_interrupted_on_boot(self) -> int:
        """Mark every reserved/running row interrupted at worker boot.

        Returns the number of rows transitioned.  Interrupted attempts are
        reported (``get_attempt`` kind ``interrupted``) and are never silently
        re-executed — the manager resolves ownership first.
        """

        def fn(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "UPDATE admission_reservations SET state = 'interrupted' "
                "WHERE state IN ('reserved', 'running')"
            )
            return cur.rowcount

        return int(self._txn(fn))

    # ── session epoch ───────────────────────────────────────────────────────

    def session_epoch(self) -> int:
        """Current persisted session epoch (starts at 1 on a fresh journal)."""
        with self._lock:
            self._ensure_open()
            conn = self._conn
            assert conn is not None
            return self._session_epoch_locked(conn)

    def advance_session_epoch(self) -> int:
        """Bump the persisted session epoch by one; returns the new value.

        The watermark/GC lane uses this to invalidate an old session epoch
        (trusted-time recovery); the server lane may also call it at boot.
        Reservations, completions and tombstones are preserved.
        """

        def fn(conn: sqlite3.Connection) -> int:
            new_epoch = self._session_epoch_locked(conn) + 1
            conn.execute(
                "INSERT OR REPLACE INTO journal_meta (key, value) "
                "VALUES ('session_epoch', ?)",
                (str(new_epoch),),
            )
            return new_epoch

        return int(self._txn(fn))

    def recover_clock_lowering(self) -> int:
        """Trusted-time recovery for a clock that fell below the watermark.

        Bumps ``session_epoch`` — invalidating every old-epoch
        ``start_authorizations`` row (``validate_and_admit`` refuses them as
        ``old session epoch``) — and baselines the new epoch's watermark at
        the recovery-time local clock.  Reservations, active ownership and
        completions are preserved for query/recovery (nothing is deleted).
        """

        def fn(conn: sqlite3.Connection) -> int:
            new_epoch = self._session_epoch_locked(conn) + 1
            conn.execute(
                "INSERT OR REPLACE INTO journal_meta (key, value) "
                "VALUES ('session_epoch', ?)",
                (str(new_epoch),),
            )
            now_ms = int(self._now().timestamp() * 1000)
            self._advance_watermark_locked(conn, new_epoch, now_ms)
            return new_epoch

        return int(self._txn(fn))

    # ── watermark ────────────────────────────────────────────────────────────

    def watermark_status(self) -> WatermarkStatus:
        """Current epoch's rejection watermark vs the local clock (V18-01 §4).

        ``admitting=False`` means the local clock is below
        ``watermark - skew``: new admission fails closed until a trusted-time
        recovery (``recover_clock_lowering``) establishes a new epoch.  The
        watermark is persisted — a restart can never reset it.
        """
        with self._lock:
            self._ensure_open()
            conn = self._conn
            assert conn is not None
            epoch = self._session_epoch_locked(conn)
            row = conn.execute(
                "SELECT watermark_ms FROM clock_watermarks WHERE session_epoch = ?",
                (epoch,),
            ).fetchone()
            watermark_ms = int(row["watermark_ms"]) if row is not None else None
            now_ms = int(self._now().timestamp() * 1000)
            skew_ms = cfg.auth_clock_skew_s() * 1000
            behind_ms = (watermark_ms - now_ms) if watermark_ms is not None else 0
            admitting = watermark_ms is None or now_ms >= watermark_ms - skew_ms
            return WatermarkStatus(
                session_epoch=epoch,
                watermark_ms=watermark_ms,
                now_ms=now_ms,
                behind_ms=behind_ms,
                admitting=admitting,
            )

    # ── GC / retention ──────────────────────────────────────────────────────

    def gc_expired(self, now: datetime | None = None) -> GcReport:
        """GC expired authorizations and resolved tombstones (V18-01 §4).

        One transaction: the current epoch's watermark is advanced to ``now``
        FIRST, then ``start_authorizations`` whose ``expires_at + skew`` is in
        the past are deleted, then the tombstones for those attempts (resolved
        = the authorization that could have admitted them is provably dead).
        After the run, replaying a retired token is refused by the watermark
        alone — ``validate_and_admit`` consults the watermark when the token
        row is gone.  Tombstones with no expired authorization (unresolved)
        are never deleted here, and unexpired rows always survive.
        """

        def fn(conn: sqlite3.Connection) -> GcReport:
            now_dt = self._normalize_dt(now if now is not None else self._now())
            now_ms = int(now_dt.timestamp() * 1000)
            cutoff = (now_dt - timedelta(seconds=cfg.auth_clock_skew_s())).isoformat()
            epoch = self._session_epoch_locked(conn)
            self._advance_watermark_locked(conn, epoch, now_ms)
            expired = [
                row["attempt_id"]
                for row in conn.execute(
                    "SELECT attempt_id FROM start_authorizations WHERE expires_at <= ?",
                    (cutoff,),
                )
            ]
            auths = conn.execute(
                "DELETE FROM start_authorizations WHERE expires_at <= ?", (cutoff,)
            ).rowcount
            tombs = 0
            if expired:
                placeholders = ",".join("?" for _ in expired)
                tombs = conn.execute(
                    f"DELETE FROM revocation_tombstones WHERE attempt_id IN ({placeholders})",
                    expired,
                ).rowcount
            return GcReport(
                authorizations_deleted=auths, tombstones_deleted=tombs, watermark_ms=now_ms
            )

        return self._txn(fn)  # type: ignore[return-value]

    def cleanup_retention(self, now: datetime | None = None) -> RetentionReport:
        """Retention sweep for audit/replay data (V18-01 §9, frozen).

        Deletes acked completions — and any leftover outbox rows for them —
        whose ack is older than ``TRAM_WORKER_JOURNAL_AUDIT_RETENTION_S``, and
        resolved tombstones (attempt has an expired authorization) whose
        revocation is older than ``TRAM_WORKER_JOURNAL_REPLAY_RETENTION_S``.
        Unacked completions and unresolved tombstones are NEVER deleted — an
        undelivered completion or a live revocation is exactly what the
        journal exists to preserve.  The current epoch's watermark is advanced
        first (same transaction), so a resolved tombstone's retirement is also
        enforced by the watermark.
        """

        def fn(conn: sqlite3.Connection) -> RetentionReport:
            now_dt = self._normalize_dt(now if now is not None else self._now())
            now_ms = int(now_dt.timestamp() * 1000)
            audit_cutoff = (
                now_dt - timedelta(seconds=cfg.worker_journal_audit_retention_s())
            ).isoformat()
            replay_cutoff = (
                now_dt - timedelta(seconds=cfg.worker_journal_replay_retention_s())
            ).isoformat()
            resolved_cutoff = (
                now_dt - timedelta(seconds=cfg.auth_clock_skew_s())
            ).isoformat()
            epoch = self._session_epoch_locked(conn)
            self._advance_watermark_locked(conn, epoch, now_ms)
            acked_old = [
                row["attempt_id"]
                for row in conn.execute(
                    "SELECT attempt_id FROM completions WHERE acked = 1 AND acked_at <= ?",
                    (audit_cutoff,),
                )
            ]
            completions = conn.execute(
                "DELETE FROM completions WHERE acked = 1 AND acked_at <= ?",
                (audit_cutoff,),
            ).rowcount
            outbox = 0
            if acked_old:
                placeholders = ",".join("?" for _ in acked_old)
                outbox = conn.execute(
                    f"DELETE FROM outbox WHERE attempt_id IN ({placeholders})",
                    acked_old,
                ).rowcount
            tombstones = conn.execute(
                "DELETE FROM revocation_tombstones WHERE revoked_at <= ? AND attempt_id IN "
                "(SELECT attempt_id FROM start_authorizations WHERE expires_at <= ?)",
                (replay_cutoff, resolved_cutoff),
            ).rowcount
            return RetentionReport(
                completions_deleted=completions,
                outbox_deleted=outbox,
                tombstones_deleted=tombstones,
            )

        return self._txn(fn)  # type: ignore[return-value]