"""Unit tests for the V18-01 §4 worker journal core (tram/agent/journal.py).

Real temp-file journals throughout; a fake clock parameter where behaviour is
time-dependent.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta

import pytest

import tram.core.config as cfg_mod
from tram.agent.journal import (
    AdmissionClosedError,
    AdmissionConflictError,
    AuthorizationDecision,
    JournalUnavailableError,
    WorkerJournal,
)

_FROZEN_TABLES = {
    "journal_meta",
    "admission_reservations",
    "revocation_tombstones",
    "start_authorizations",
    "clock_watermarks",
    "completions",
    "outbox",
    "spool_entries",
}


class FakeClock:
    """Deterministic clock for time-dependent journal behaviour."""

    def __init__(self, start: datetime | None = None) -> None:
        self._t = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)


def _auth(
    attempt_id: str = "a1",
    run_id: str = "r1",
    *,
    valid: bool = True,
    generation: int = 1,
    slot_id: str = "s1",
    worker_session: str = "ws-1",
    session_epoch: int = 1,
    reason: str | None = None,
) -> AuthorizationDecision:
    return AuthorizationDecision(
        valid=valid,
        reason=reason,
        attempt_id=attempt_id,
        run_id=run_id,
        generation=generation,
        slot_id=slot_id,
        worker_session=worker_session,
        session_epoch=session_epoch,
        token_hash=f"tok-{attempt_id}",
        issued_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-01-01T00:05:00+00:00",
    )


@pytest.fixture
def journal(tmp_path):
    j = WorkerJournal(tmp_path / "journal.db", clock=FakeClock())
    yield j
    j.close()


def _table_names(j: WorkerJournal) -> set[str]:
    conn = j._conn
    assert conn is not None
    return {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ── schema / pragmas / file mode ─────────────────────────────────────────────


def test_frozen_schema_has_all_eight_tables(journal):
    assert _FROZEN_TABLES <= _table_names(journal)


def test_schema_version_and_session_epoch_defaults(journal):
    conn = journal._conn
    assert conn is not None
    row = conn.execute("SELECT value FROM journal_meta WHERE key='schema_version'").fetchone()
    assert row["value"] == "1"
    assert journal.session_epoch() == 1


def test_wal_synchronous_full_and_busy_timeout_applied(journal):
    conn = journal._conn
    assert conn is not None
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_journal_file_mode_0600(tmp_path):
    j = WorkerJournal(tmp_path / "journal.db", clock=FakeClock())
    j.admit(_auth(), "pipe-a")  # first write creates the WAL sidecars
    try:
        for name in ("journal.db", "journal.db-wal", "journal.db-shm"):
            path = tmp_path / name
            assert path.exists(), f"missing {name}"
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        j.close()
    # The main file keeps its mode after close (WAL sidecars are checkpointed away).
    assert stat.S_IMODE((tmp_path / "journal.db").stat().st_mode) == 0o600


# ── admission ────────────────────────────────────────────────────────────────


def test_admission_idempotent_repeat_returns_existing(journal):
    first = journal.admit(_auth("a1", "r1"), "pipe-a")
    assert first.outcome == "admitted"

    repeat = journal.admit(_auth("a1", "r1"), "pipe-a")
    assert repeat.outcome == "already_admitted"
    assert repeat.existing is not None
    assert repeat.existing.attempt_id == "a1"
    assert repeat.existing.run_id == "r1"
    assert repeat.existing.generation == 1
    assert repeat.existing.slot_id == "s1"

    # A repeat is never a new row and never a second authorization.
    conn = journal._conn
    assert conn is not None
    assert conn.execute("SELECT COUNT(*) FROM admission_reservations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM start_authorizations").fetchone()[0] == 1


def test_admission_conflicting_identity_raises_typed_error(journal):
    journal.admit(_auth("a1", "r1", generation=1, slot_id="s1"), "pipe-a")
    with pytest.raises(AdmissionConflictError):
        journal.admit(_auth("a1", "r1", generation=2, slot_id="s1"), "pipe-a")
    with pytest.raises(AdmissionConflictError):
        journal.admit(_auth("a1", "r2", generation=1, slot_id="s1"), "pipe-a")
    with pytest.raises(AdmissionConflictError):
        journal.admit(_auth("a1", "r1", generation=1, slot_id="other"), "pipe-a")
    # Same identity but a different worker session is still idempotent.
    repeat = journal.admit(_auth("a1", "r1", generation=1, slot_id="s1", worker_session="ws-2"), "pipe-a")
    assert repeat.outcome == "already_admitted"


def test_tombstone_blocks_admission(journal):
    tomb = journal.record_tombstone("a1", "r1", "manager stop")
    assert tomb.reason == "manager stop"

    result = journal.admit(_auth("a1", "r1"), "pipe-a")
    assert result.outcome == "revoked"
    assert result.tombstone_reason == "manager stop"
    assert result.tombstone_revoked_at is not None

    # Nothing admitted, no authorization recorded (transaction rolled back).
    conn = journal._conn
    assert conn is not None
    assert conn.execute("SELECT COUNT(*) FROM admission_reservations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM start_authorizations").fetchone()[0] == 0


def test_refused_auth_inserts_nothing(journal):
    result = journal.admit(_auth(valid=False, reason="expired token"), "pipe-a")
    assert result.outcome == "refused_auth"
    assert result.refusal_reason == "expired token"

    assert journal.get_attempt("a1") is None
    conn = journal._conn
    assert conn is not None
    assert conn.execute("SELECT COUNT(*) FROM admission_reservations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM start_authorizations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM completions").fetchone()[0] == 0


def test_valid_auth_requires_full_fields(journal):
    incomplete = AuthorizationDecision(valid=True, attempt_id="a1", run_id="r1")
    with pytest.raises(Exception, match="missing fields"):
        journal.admit(incomplete, "pipe-a")


# ── health: quota / corruption / unavailability ──────────────────────────────


def test_health_ok_then_quota_full_and_admission_closed(tmp_path):
    clock = FakeClock()
    j = WorkerJournal(tmp_path / "journal.db", quota_mb=1.0, headroom_mb=0.5, clock=clock)
    try:
        assert j.health().state == "ok"

        blob = "x" * 100_000
        for i in range(50):
            attempt = f"a{i}"
            j.admit(_auth(attempt, f"r{i}"), "pipe-a")
            j.record_completion(attempt, json.dumps({"blob": blob}))
            if j.health().state == "quota":
                break
        else:
            pytest.fail("journal never reached the quota threshold")

        h = j.health()
        assert h.state == "quota"
        assert h.size_bytes is not None and h.size_bytes >= h.quota_bytes - h.headroom_bytes

        # Admission fails closed once quota-critical...
        with pytest.raises(AdmissionClosedError):
            j.admit(_auth("blocked", "r-blocked"), "pipe-a")
        # ...but headroom still lets completions commit (never a lost result).
        j.record_completion("late-a", '{"status":"ok"}', run_id="late-r")
        rec = j.get_attempt("late-a")
        assert rec is not None and rec.kind == "completion"
    finally:
        j.close()


def test_corruption_surfaced_and_recoverable_rows_preserved(tmp_path):
    path = tmp_path / "journal.db"
    j = WorkerJournal(path, clock=FakeClock())
    j.admit(_auth("a1", "r1"), "pipe-a")
    j.close()

    garbage = b"this is definitely not a sqlite database file" * 10
    path.write_bytes(garbage)

    h = j.health()
    assert h.state == "corrupt"

    # The corrupt file is preserved (quarantined copy), never rebuilt in place.
    copies = sorted(tmp_path.glob("journal.db.corrupt-*"))
    assert copies, "corrupt journal should be copied aside for operator salvage"
    assert copies[0].read_bytes() == garbage
    assert path.read_bytes() == garbage

    # The journal stays failed-closed for admission and other operations.
    with pytest.raises(AdmissionClosedError):
        j.admit(_auth("a2", "r2"), "pipe-a")
    with pytest.raises(JournalUnavailableError):
        j.record_completion("a2", "{}", run_id="r2")
    j.close()


def test_health_unavailable_when_path_is_not_a_database(tmp_path):
    (tmp_path / "subdir").mkdir()
    j = WorkerJournal(tmp_path / "subdir", clock=FakeClock())  # path is a directory
    try:
        h = j.health()
        assert h.state == "unavailable"
        with pytest.raises(AdmissionClosedError):
            j.admit(_auth("a1", "r1"), "pipe-a")
    finally:
        j.close()


# ── completion → outbox → ack flow ───────────────────────────────────────────


def test_completion_outbox_acked_flow(journal):
    assert journal.admit(_auth("a1", "r1"), "pipe-a").outcome == "admitted"

    journal.record_completion("a1", '{"status":"ok"}')
    # Idempotent: a second completion does not clobber the first.
    journal.record_completion("a1", '{"status":"ok"}')

    seq = journal.enqueue_outbox("a1", "run_complete", {"attempt_id": "a1", "outcome": "ok"})
    assert seq == 1

    rec = journal.get_attempt("a1")
    assert rec is not None
    assert rec.kind == "completion"
    assert rec.result_json == '{"status":"ok"}'
    assert rec.acked is False
    assert rec.acked_at is None

    unacked = journal.list_unacked_completions()
    assert [u.attempt_id for u in unacked] == ["a1"]
    assert unacked[0].result_json == '{"status":"ok"}'
    assert unacked[0].acked is False

    due = journal.fetch_due_outbox()
    assert [row.seq for row in due] == [1]
    assert due[0].attempts == 0
    assert due[0].last_error is None

    journal.mark_acked("a1")
    rec = journal.get_attempt("a1")
    assert rec is not None
    assert rec.acked is True
    assert rec.acked_at is not None
    assert journal.list_unacked_completions() == []
    assert journal.fetch_due_outbox() == []  # delivered rows are drained


def test_outbox_retry_records_attempts_and_last_error(journal):
    clock = FakeClock()
    j = WorkerJournal(journal._path, clock=clock)
    try:
        j.admit(_auth("a1", "r1"), "pipe-a")
        j.record_completion("a1", "{}")
        seq = j.enqueue_outbox("a1", "run_complete", {"attempt_id": "a1"})

        assert len(j.fetch_due_outbox()) == 1  # due immediately

        j.record_outbox_failure(seq, "connection refused")
        assert j.fetch_due_outbox() == []  # backed off, not due

        clock.advance(1.0)  # first backoff is 1s
        rows = j.fetch_due_outbox()
        assert len(rows) == 1
        assert rows[0].attempts == 1
        assert rows[0].last_error == "connection refused"

        j.record_outbox_failure(seq, "still down")
        assert j.fetch_due_outbox() == []
        clock.advance(1.0)
        assert j.fetch_due_outbox() == []  # second backoff is 2s
        clock.advance(1.0)
        rows = j.fetch_due_outbox()
        assert len(rows) == 1
        assert rows[0].attempts == 2
        assert rows[0].last_error == "still down"
    finally:
        j.close()


# ── query / replay ───────────────────────────────────────────────────────────


def test_get_attempt_distinguishes_all_four_states(journal):
    # absent → None (404)
    assert journal.get_attempt("never-seen") is None

    # active reservation
    journal.admit(_auth("a-active", "r-a"), "pipe-a")
    rec = journal.get_attempt("a-active")
    assert rec is not None and rec.kind == "active"
    assert rec.state == "reserved"
    assert rec.thread_started is False

    # running reservation is still active
    journal.admit(_auth("a-running", "r-r"), "pipe-a")
    assert journal.mark_running("a-running") is True
    rec = journal.get_attempt("a-running")
    assert rec is not None and rec.kind == "active"
    assert rec.state == "running"
    assert rec.thread_started is True

    # interrupted (post-boot)
    journal.admit(_auth("a-int", "r-i"), "pipe-a")
    journal.mark_interrupted_on_boot()
    rec = journal.get_attempt("a-int")
    assert rec is not None and rec.kind == "interrupted"
    assert rec.state == "interrupted"

    # tombstone
    journal.record_tombstone("a-tomb", "r-t", "manager stop")
    rec = journal.get_attempt("a-tomb")
    assert rec is not None and rec.kind == "tombstone"
    assert rec.reason == "manager stop"

    # completion
    journal.admit(_auth("a-done", "r-d"), "pipe-a")
    journal.record_completion("a-done", '{"ok":true}')
    rec = journal.get_attempt("a-done")
    assert rec is not None and rec.kind == "completion"
    assert rec.result_json == '{"ok":true}'

    # a tombstone shadows a completion when both exist
    journal.admit(_auth("a-both", "r-b"), "pipe-a")
    journal.record_completion("a-both", "{}")
    journal.record_tombstone("a-both", "r-b", "revoked after completion")
    assert journal.get_attempt("a-both").kind == "tombstone"


def test_mark_running_transitions_reservation(journal):
    journal.admit(_auth("a1", "r1"), "pipe-a")
    assert journal.get_attempt("a1").thread_started is False
    assert journal.mark_running("a1") is True
    rec = journal.get_attempt("a1")
    assert rec.kind == "active"
    assert rec.state == "running"
    assert rec.thread_started is True
    assert journal.mark_running("a1") is False  # idempotent


# ── boot interruption ────────────────────────────────────────────────────────


def test_mark_interrupted_on_boot_marks_reserved_and_running(journal):
    journal.admit(_auth("a1", "r1"), "pipe-a")  # reserved
    journal.admit(_auth("a2", "r2"), "pipe-a")
    journal.mark_running("a2")  # running
    journal.record_completion(_auth("a3", "r3").attempt_id, "{}", run_id="r3")  # completed

    count = journal.mark_interrupted_on_boot()
    assert count == 2  # reserved + running, not the completed one

    assert journal.get_attempt("a1").kind == "interrupted"
    assert journal.get_attempt("a2").kind == "interrupted"
    assert journal.get_attempt("a3").kind == "completion"

    # A second boot pass is a no-op.
    assert journal.mark_interrupted_on_boot() == 0


def test_session_epoch_advances_monotonically(journal):
    assert journal.session_epoch() == 1
    assert journal.advance_session_epoch() == 2
    assert journal.advance_session_epoch() == 3
    assert journal.session_epoch() == 3


# ── env readers (tram/core/config.py) ────────────────────────────────────────


def test_worker_journal_path_default(monkeypatch):
    monkeypatch.delenv("TRAM_WORKER_JOURNAL_PATH", raising=False)
    assert cfg_mod.worker_journal_path() == "/var/lib/tram/worker/journal.db"


def test_worker_journal_path_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_WORKER_JOURNAL_PATH", "/tmp/x/journal.db")
    assert cfg_mod.worker_journal_path() == "/tmp/x/journal.db"


def test_worker_journal_quota_defaults(monkeypatch):
    monkeypatch.delenv("TRAM_WORKER_JOURNAL_QUOTA_MB", raising=False)
    monkeypatch.delenv("TRAM_WORKER_JOURNAL_HEADROOM_MB", raising=False)
    assert cfg_mod.worker_journal_quota_mb() == 512
    assert cfg_mod.worker_journal_headroom_mb() == 64


def test_worker_journal_quota_from_env(monkeypatch):
    monkeypatch.setenv("TRAM_WORKER_JOURNAL_QUOTA_MB", "1024")
    monkeypatch.setenv("TRAM_WORKER_JOURNAL_HEADROOM_MB", "128")
    assert cfg_mod.worker_journal_quota_mb() == 1024
    assert cfg_mod.worker_journal_headroom_mb() == 128


def test_worker_journal_quota_invalid_raises(monkeypatch):
    monkeypatch.setenv("TRAM_WORKER_JOURNAL_QUOTA_MB", "not-an-int")
    with pytest.raises(ValueError):
        cfg_mod.worker_journal_quota_mb()