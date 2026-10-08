"""Tests for the v1.8.0 manager execution ledger (frozen V18-01 §3).

Covers the M1–M5 migrations (ledger schema, additive columns, backfills)
recorded in ``schema_migrations``, the claim helpers in
``tram/persistence/ledger.py`` (conditional-UPDATE rowcount semantics, the
BEGIN IMMEDIATE serialization), the checkpoint monotonic guard, and the MySQL
fail-closed disposition.

SQLite is tested for real (file-backed, matching the existing db-test
conventions in test_persistence.py / test_queued_runs.py). PostgreSQL has no
live fixture in this repo's test suite (no server, no docker fixture), so the
PG dialect is covered by SQL-shape tests against a recording engine (the
test_queued_runs.py convention) plus live tests gated on
``TRAM_TEST_POSTGRES_URL`` that skip when unset.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import types
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from tram.core.context import RunResult, RunStatus
from tram.persistence.db import TramDB
from tram.persistence.ledger import (
    ALREADY_HELD,
    CLAIMED,
    COMMITTED,
    LOST,
    NO_INTENT,
    STALE,
    claim_run,
    commit_checkpoint,
    get_attempt,
    mint_attempt_id,
    release_guard,
    resolve_intent,
    transition_attempt,
)

NOW = "2026-10-08T12:00:00+00:00"


@pytest.fixture
def db(tmp_path):
    """TramDB backed by a temp SQLite file (schema init runs M1–M5)."""
    d = TramDB(url=f"sqlite:///{tmp_path / 'ledger.db'}")
    yield d
    d.close()


def _table_names(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )).fetchall()
    return {r[0] for r in rows}


def _index_names(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )).fetchall()
    return {r[0] for r in rows}


def _columns(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def _build_v17_db(path, *, pipelines=(), queued=(), transform=(), history=()):
    """Create a v1.7-shaped SQLite DB (pre-ledger schema) and seed it.

    Only the tables the v1.8 migrations read/write are created — the schema
    init path recreates the rest with CREATE TABLE IF NOT EXISTS.
    """
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE registered_pipelines (
            name        TEXT PRIMARY KEY NOT NULL,
            yaml_text   TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            deleted     INTEGER NOT NULL DEFAULT 0,
            paused      INTEGER NOT NULL DEFAULT 0,
            source      TEXT NOT NULL DEFAULT 'api',
            stopped     INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE queued_runs (
            run_id        TEXT PRIMARY KEY NOT NULL,
            pipeline_name TEXT NOT NULL,
            yaml_snapshot TEXT NOT NULL,
            status        TEXT NOT NULL,
            requested_at  TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            dispatched_at TEXT
        );
        CREATE TABLE transform_state (
            pipeline_name TEXT PRIMARY KEY NOT NULL,
            state_json    TEXT NOT NULL,
            config_sha256 TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            updated_by    TEXT NOT NULL
        );
        CREATE TABLE run_history (
            run_id          TEXT PRIMARY KEY,
            pipeline_name   TEXT NOT NULL,
            status          TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            finished_at     TEXT NOT NULL,
            records_in      INTEGER NOT NULL DEFAULT 0,
            records_out     INTEGER NOT NULL DEFAULT 0,
            records_skipped INTEGER NOT NULL DEFAULT 0,
            bytes_in        INTEGER NOT NULL DEFAULT 0,
            bytes_out       INTEGER NOT NULL DEFAULT 0,
            error           TEXT,
            node_id         TEXT NOT NULL DEFAULT '',
            dlq_count       INTEGER NOT NULL DEFAULT 0,
            errors_json     TEXT
        );
    """)
    conn.executemany(
        "INSERT INTO registered_pipelines "
        "(name, yaml_text, created_at, updated_at, deleted, stopped) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        pipelines,
    )
    conn.executemany(
        "INSERT INTO queued_runs "
        "(run_id, pipeline_name, yaml_snapshot, status, requested_at, expires_at, dispatched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        queued,
    )
    conn.executemany(
        "INSERT INTO transform_state "
        "(pipeline_name, state_json, config_sha256, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?)",
        transform,
    )
    conn.executemany(
        "INSERT INTO run_history "
        "(run_id, pipeline_name, status, started_at, finished_at, records_in, records_out, "
        " records_skipped, bytes_in, bytes_out, error, node_id, dlq_count, errors_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        history,
    )
    conn.commit()
    conn.close()


def _fetch_all(engine, sql: str, params: dict | None = None) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params or {}).mappings().fetchall()
    return [dict(r) for r in rows]


def _make_intent(engine, run_id="run-1", pipeline_name="p", origin="manual", generation=1):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO run_intents
                    (run_id, pipeline_name, origin, flush, requested_at, expires_at,
                     requested_generation)
                VALUES (:run_id, :pipeline_name, :origin, 0, :requested_at, :expires_at,
                        :requested_generation)
            """),
            {
                "run_id": run_id,
                "pipeline_name": pipeline_name,
                "origin": origin,
                "requested_at": NOW,
                "expires_at": "2026-10-09T12:00:00+00:00",
                "requested_generation": generation,
            },
        )


# ── M1: ledger schema ────────────────────────────────────────────────────────


def test_m1_ledger_tables_and_indexes_exist(db):
    tables = _table_names(db._engine)
    for t in (
        "pipeline_desired_state",
        "run_intents",
        "execution_attempts",
        "execution_guards",
        "delivery_checkpoints",
        "lifecycle_operations",
        "schema_migrations",
    ):
        assert t in tables, f"missing table {t}"
    # legacy tables stay present and readable
    for t in ("run_history", "broadcast_placements", "queued_runs", "transform_state",
              "registered_pipelines", "pipeline_versions"):
        assert t in tables, f"legacy table {t} missing"

    indices = _index_names(db._engine)
    for ix in ("idx_ri_pipeline", "idx_ri_expires", "idx_ea_state", "idx_ea_run"):
        assert ix in indices, f"missing index {ix}"


def test_m1_frozen_column_sets(db):
    """The six tables carry exactly the frozen column sets (V18-01 §3)."""
    assert _columns(db._engine, "run_intents") >= {
        "run_id", "pipeline_name", "origin", "flush", "requested_at", "expires_at",
        "requested_generation", "requested_config_version", "yaml_snapshot",
        "schedule_type", "final_outcome", "final_attempt_id", "resolved_at",
    }
    assert _columns(db._engine, "execution_attempts") >= {
        "attempt_id", "run_id", "pipeline_name", "ordinal", "generation", "slot_id",
        "worker_id", "worker_session", "fence_token", "state", "dispatch_sent_at",
        "accepted_at", "started_at", "finished_at", "uncertainty_reason",
        "cancel_reason", "result_json",
    }
    assert _columns(db._engine, "execution_guards") >= {
        "guard_key", "guard_kind", "run_id", "attempt_id", "generation", "acquired_at",
    }
    assert _columns(db._engine, "delivery_checkpoints") >= {
        "checkpoint_id", "pipeline_name", "generation", "attempt_id", "run_id",
        "source_unit", "frontier_json", "frontier_seq", "sink_receipts",
        "state_revision", "committed_at",
    }
    assert _columns(db._engine, "lifecycle_operations") >= {
        "operation_id", "pipeline_name", "op_kind", "state", "attempt_id", "detail",
        "created_at", "updated_at",
    }
    assert _columns(db._engine, "pipeline_desired_state") >= {
        "pipeline_name", "desired_status", "generation", "config_version",
        "schedule_type", "misfire_policy", "deleted", "stopped_reason", "updated_at",
    }


def test_m2_additive_columns_exist(db):
    assert _columns(db._engine, "run_history") >= {
        "attempt_id", "generation", "outcome", "disposition_json",
    }
    assert _columns(db._engine, "queued_runs") >= {
        "flush", "requested_generation", "schedule_type", "origin", "terminal_reason",
    }
    assert _columns(db._engine, "transform_state") >= {"generation", "revision"}


def test_schema_migrations_recorded(db):
    rows = _fetch_all(db._engine, "SELECT migration_id FROM schema_migrations ORDER BY migration_id")
    assert [r["migration_id"] for r in rows] == ["M1", "M2", "M3", "M4", "M5"]


# ── M3–M5 backfills from a real v1.7 DB ─────────────────────────────────────


def test_m3_desired_state_backfill(tmp_path):
    p = tmp_path / "v17-m3.db"
    _build_v17_db(
        p,
        pipelines=[
            ("p-running", "yaml: 1", NOW, NOW, 0, 0),
            ("p-stopped", "yaml: 2", NOW, NOW, 0, 1),
            ("p-deleted", "yaml: 3", NOW, NOW, 1, 0),
        ],
    )
    d = TramDB(url=f"sqlite:///{p}")
    try:
        rows = _fetch_all(
            d._engine,
            "SELECT pipeline_name, desired_status, generation, deleted FROM pipeline_desired_state "
            "ORDER BY pipeline_name",
        )
        assert rows == [
            {"pipeline_name": "p-deleted", "desired_status": "stopped", "generation": 1, "deleted": 1},
            {"pipeline_name": "p-running", "desired_status": "running", "generation": 1, "deleted": 0},
            {"pipeline_name": "p-stopped", "desired_status": "stopped", "generation": 1, "deleted": 0},
        ]
    finally:
        d.close()


def test_m4_run_intents_backfill(tmp_path):
    p = tmp_path / "v17-m4.db"
    _build_v17_db(
        p,
        queued=[
            ("qr-queued", "p", "snap-q", "queued", NOW, "2026-10-09T12:00:00+00:00", None),
            ("qr-dispatching", "p", "snap-d", "dispatching", NOW, "2026-10-09T12:00:00+00:00", None),
            ("qr-dispatched", "p", "snap-x", "dispatched", NOW, "2026-10-09T12:00:00+00:00", NOW),
            ("qr-expired", "p", "snap-e", "expired", NOW, "2026-10-08T06:00:00+00:00", None),
        ],
    )
    d = TramDB(url=f"sqlite:///{p}")
    try:
        rows = _fetch_all(
            d._engine,
            "SELECT run_id, origin, flush, requested_generation, yaml_snapshot, final_outcome "
            "FROM run_intents ORDER BY run_id",
        )
        # only the two in-flight rows are reconstructed; flush 0 (lost flags are
        # not invented); origin 'queued'; yaml_snapshot copied from the queue row
        assert rows == [
            {"run_id": "qr-dispatching", "origin": "queued", "flush": 0,
             "requested_generation": 1, "yaml_snapshot": "snap-d", "final_outcome": None},
            {"run_id": "qr-queued", "origin": "queued", "flush": 0,
             "requested_generation": 1, "yaml_snapshot": "snap-q", "final_outcome": None},
        ]
    finally:
        d.close()


def test_m5_transform_state_null_marking(tmp_path):
    p = tmp_path / "v17-m5.db"
    _build_v17_db(p, transform=[("p", '{"k": "v"}', "sha", NOW, "run-legacy")])
    d = TramDB(url=f"sqlite:///{p}")
    try:
        rows = _fetch_all(d._engine, "SELECT pipeline_name, generation, revision FROM transform_state")
        # legacy blob: generation NULL, revision 0 — hydrates under weaker semantics
        assert rows == [{"pipeline_name": "p", "generation": None, "revision": 0}]
    finally:
        d.close()


def test_migrations_idempotent_run_twice(tmp_path):
    """M1–M5 re-run from a v1.7 DB: no error, no duplicate records, no re-backfill."""
    p = tmp_path / "v17-twice.db"
    _build_v17_db(
        p,
        pipelines=[("p", "yaml: 1", NOW, NOW, 0, 0)],
        queued=[("qr-1", "p", "snap", "queued", NOW, "2026-10-09T12:00:00+00:00", None)],
        transform=[("p", '{"k": "v"}', "sha", NOW, "run-legacy")],
        history=[("rh-1", "p", "success", NOW, NOW, 5, 5, 0, 10, 10, None, "", 0, None)],
    )
    for _ in range(2):  # two TramDB inits on the same file
        d = TramDB(url=f"sqlite:///{p}")
        try:
            migs = _fetch_all(d._engine, "SELECT migration_id FROM schema_migrations")
            assert [m["migration_id"] for m in migs] == ["M1", "M2", "M3", "M4", "M5"]
            # no double backfill
            assert len(_fetch_all(d._engine, "SELECT 1 FROM pipeline_desired_state")) == 1
            assert len(_fetch_all(d._engine, "SELECT 1 FROM run_intents")) == 1
            assert len(_fetch_all(d._engine, "SELECT 1 FROM transform_state")) == 1
        finally:
            d.close()


def test_legacy_tables_stay_readable_after_migration(db):
    """The existing TramDB APIs still work over the migrated schema."""
    now = datetime.now(UTC)
    db.save_run(RunResult(
        run_id="legacy-run", pipeline_name="p", status=RunStatus.SUCCESS,
        started_at=now, finished_at=now, records_in=1, records_out=1,
        records_skipped=0, error=None, dlq_count=0,
    ))
    db.save_pipeline("p", "yaml: v1")
    db.save_queued_run("qr-1", "p", "snap", now, now)
    db.save_transform_state("p", {"k": "v"}, "sha")
    assert db.get_run("legacy-run") is not None
    assert db.get_all_pipelines() == [("p", "yaml: v1")]
    assert db.get_active_queued_runs()[0]["run_id"] == "qr-1"
    assert db.load_transform_state("p")["state"] == {"k": "v"}


# ── Claim helpers (frozen conditional-UPDATE semantics) ─────────────────────


def test_claim_run_acquires_guard_and_inserts_attempt(db):
    _make_intent(db._engine, run_id="run-1", pipeline_name="p")
    outcome = claim_run(
        db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
        run_id="run-1", generation=3, yaml_snapshot="snap-claimed",
    )
    assert outcome.status == CLAIMED
    assert outcome.attempt_id == "run-1-a1"
    assert outcome.ordinal == 1
    assert outcome.generation == 3
    assert outcome.fence_token is not None

    guards = _fetch_all(db._engine, "SELECT * FROM execution_guards")
    assert guards[0]["attempt_id"] == "run-1-a1"
    assert guards[0]["run_id"] == "run-1"
    assert guards[0]["generation"] == 3

    attempts = _fetch_all(db._engine, "SELECT * FROM execution_attempts")
    assert attempts[0]["state"] == "claimed"
    assert attempts[0]["fence_token"] == outcome.fence_token
    assert attempts[0]["slot_id"] == ""
    assert attempts[0]["worker_id"] is None

    intents = _fetch_all(db._engine, "SELECT yaml_snapshot FROM run_intents WHERE run_id = 'run-1'")
    assert intents[0]["yaml_snapshot"] == "snap-claimed"


def test_claim_run_second_claimer_loses(db):
    _make_intent(db._engine, run_id="run-1", pipeline_name="p")
    _make_intent(db._engine, run_id="run-2", pipeline_name="p")
    assert claim_run(db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
                     run_id="run-1", generation=1).status == CLAIMED
    # a different run on the same guard key loses the acquire
    outcome = claim_run(db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
                        run_id="run-2", generation=1)
    assert outcome.status == LOST
    assert outcome.attempt_id == "run-2-a1"
    # the loser's intent stays unresolved, its attempt row is never inserted
    assert _fetch_all(db._engine, "SELECT final_outcome FROM run_intents WHERE run_id = 'run-2'")[0]["final_outcome"] is None
    assert _fetch_all(db._engine, "SELECT attempt_id FROM execution_attempts WHERE run_id = 'run-2'") == []


def test_concurrent_guard_acquire_exactly_one_wins(tmp_path):
    """Two threads claim the same guard concurrently — exactly one wins."""
    d = TramDB(url=f"sqlite:///{tmp_path / 'conc.db'}")
    try:
        with d._engine.begin() as conn:
            for rid in ("run-a", "run-b"):
                conn.execute(
                    text("""
                        INSERT INTO run_intents
                            (run_id, pipeline_name, origin, flush, requested_at, expires_at,
                             requested_generation)
                        VALUES (:run_id, 'p', 'manual', 0, :now, :exp, 1)
                    """),
                    {"run_id": rid, "now": NOW, "exp": "2026-10-09T12:00:00+00:00"},
                )
        barrier = threading.Barrier(2)
        results: list[str] = []

        def _claimer(run_id: str):
            barrier.wait()
            outcome = claim_run(
                d._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
                run_id=run_id, generation=1,
            )
            results.append(outcome.status)

        threads = [threading.Thread(target=_claimer, args=(rid,)) for rid in ("run-a", "run-b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == [CLAIMED, LOST]
        # the winner's attempt row exists, the loser's does not
        attempts = _fetch_all(d._engine, "SELECT run_id FROM execution_attempts")
        assert len(attempts) == 1
    finally:
        d.close()


def test_claim_run_idempotent_reclaim_same_attempt(db):
    _make_intent(db._engine, run_id="run-1", pipeline_name="p")
    first = claim_run(db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
                      run_id="run-1", generation=1)
    assert first.status == CLAIMED
    # a retry inside the run keeps its attempt — idempotent re-claim
    again = claim_run(db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
                      run_id="run-1", generation=1)
    assert again.status == ALREADY_HELD
    assert again.attempt_id == first.attempt_id
    assert again.fence_token is None  # the ledger never reissues a token
    assert len(_fetch_all(db._engine, "SELECT 1 FROM execution_attempts")) == 1


def test_claim_run_without_intent_rolls_back(db):
    # no intent row for run-orphan at all
    outcome = claim_run(db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
                        run_id="run-orphan", generation=1)
    assert outcome.status == NO_INTENT
    # the whole transaction rolled back: no guard row, no attempt row
    assert _fetch_all(db._engine, "SELECT 1 FROM execution_guards") == []
    assert _fetch_all(db._engine, "SELECT 1 FROM execution_attempts") == []


def test_claim_run_resolved_intent_rolls_back(db):
    _make_intent(db._engine, run_id="run-1", pipeline_name="p")
    resolve_intent(db._engine, run_id="run-1", outcome="superseded", attempt_id="other-a1")
    outcome = claim_run(db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
                        run_id="run-1", generation=1)
    assert outcome.status == NO_INTENT
    assert _fetch_all(db._engine, "SELECT 1 FROM execution_guards") == []


def test_release_guard_only_by_holding_attempt(db):
    _make_intent(db._engine, run_id="run-1", pipeline_name="p")
    claim_run(db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
              run_id="run-1", generation=1)
    # a foreign attempt_id/run_id releases nothing (identity-compared, R5)
    assert release_guard(db._engine, guard_key="p", attempt_id="run-999-a1", run_id="run-999") == 0
    assert release_guard(db._engine, guard_key="p", attempt_id="run-1-a1", run_id="run-999") == 0
    guards = _fetch_all(db._engine, "SELECT attempt_id FROM execution_guards WHERE guard_key = 'p'")
    assert guards[0]["attempt_id"] == "run-1-a1"
    # the holding attempt releases
    assert release_guard(db._engine, guard_key="p", attempt_id="run-1-a1", run_id="run-1") == 1
    guards = _fetch_all(db._engine, "SELECT * FROM execution_guards WHERE guard_key = 'p'")
    assert guards[0]["attempt_id"] is None
    assert guards[0]["run_id"] is None
    # releasing a free guard is a no-op
    assert release_guard(db._engine, guard_key="p", attempt_id="run-1-a1", run_id="run-1") == 0


def test_transition_attempt_fenced_on_state_and_generation(db):
    _make_intent(db._engine, run_id="run-1", pipeline_name="p")
    claim_run(db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
              run_id="run-1", generation=2)
    # claimed → dispatching is a listed transition
    assert transition_attempt(
        db._engine, attempt_id="run-1-a1", run_id="run-1",
        from_state="claimed", to_state="dispatching", generation=2,
    ) == 1
    # wrong current state → 0
    assert transition_attempt(
        db._engine, attempt_id="run-1-a1", run_id="run-1",
        from_state="claimed", to_state="running", generation=2,
    ) == 0
    # wrong generation → 0
    assert transition_attempt(
        db._engine, attempt_id="run-1-a1", run_id="run-1",
        from_state="dispatching", to_state="running", generation=1,
    ) == 0
    # wrong run_id → 0
    assert transition_attempt(
        db._engine, attempt_id="run-1-a1", run_id="run-999",
        from_state="dispatching", to_state="running", generation=2,
    ) == 0
    # the fenced transition applied
    attempts = _fetch_all(db._engine, "SELECT state FROM execution_attempts WHERE attempt_id = 'run-1-a1'")
    assert attempts[0]["state"] == "dispatching"


def test_resolve_intent_idempotent_for_winner(db):
    _make_intent(db._engine, run_id="run-1", pipeline_name="p")
    assert resolve_intent(db._engine, run_id="run-1", outcome="success",
                          attempt_id="run-1-a1") == 1
    # duplicate resolution is harmless: 0 rows, winner's row untouched
    assert resolve_intent(db._engine, run_id="run-1", outcome="failed",
                          attempt_id="run-9-a9") == 0
    rows = _fetch_all(db._engine, "SELECT final_outcome, final_attempt_id FROM run_intents WHERE run_id = 'run-1'")
    assert rows == [{"final_outcome": "success", "final_attempt_id": "run-1-a1"}]


def test_checkpoint_upsert_monotonic(db):
    def _commit(seq, cid):
        return commit_checkpoint(
            db._engine, checkpoint_id=cid, pipeline_name="p", generation=1,
            attempt_id="run-1-a1", run_id="run-1", source_unit="kafka/t/0",
            frontier_json=f'{{"offset": {seq}}}', frontier_seq=seq,
            sink_receipts='[{"sink_key": "k", "tier": "remote_durable", "confirmed": true}]',
            state_revision=1,
        )

    first = _commit(5, "cp-5")
    assert first.status == COMMITTED
    assert first.checkpoint_id == "cp-5"

    # advancing the frontier keeps the first-minted checkpoint_id (frozen §7:
    # "a committed unit returns the existing checkpoint_id")
    advanced = _commit(9, "cp-9")
    assert advanced.status == COMMITTED
    assert advanced.checkpoint_id == "cp-5"

    # an older frontier_seq is rejected by the monotonic guard
    stale = _commit(4, "cp-4")
    assert stale.status == STALE
    assert stale.checkpoint_id == "cp-5"  # the stored identity is unchanged
    assert stale.existing is not None
    assert stale.existing["frontier_seq"] == 9
    assert stale.existing["frontier_json"] == '{"offset": 9}'

    # an equal frontier_seq is also rejected (strictly greater)
    assert _commit(9, "cp-9b").status == STALE

    rows = _fetch_all(db._engine, "SELECT checkpoint_id, frontier_seq FROM delivery_checkpoints")
    assert rows == [{"checkpoint_id": "cp-5", "frontier_seq": 9}]


def test_get_attempt_returns_row_or_none(db):
    _make_intent(db._engine, run_id="run-1", pipeline_name="p")
    outcome = claim_run(db._engine, guard_key="p", guard_kind="batch", pipeline_name="p",
                        run_id="run-1", generation=1)
    row = get_attempt(db._engine, "run-1-a1")
    assert row is not None
    assert row["fence_token"] == outcome.fence_token
    assert row["state"] == "claimed"
    assert get_attempt(db._engine, "nope-a1") is None


def test_mint_attempt_id():
    assert mint_attempt_id("run-1", 1) == "run-1-a1"
    assert mint_attempt_id("run-1", 12) == "run-1-a12"  # unpadded ordinal


# ── MySQL fail-closed disposition (frozen V18-01 §3) ─────────────────────────


@pytest.mark.parametrize("url", [
    "mysql+pymysql://user:pass@localhost/tram",
    "mysql://user:pass@localhost/tram",
    "mariadb+pymysql://user:pass@localhost/tram",
])
def test_mysql_url_fails_closed(tmp_path, url):
    with pytest.raises(RuntimeError) as exc:
        TramDB(url=url)
    assert "Unsupported database dialect" in str(exc.value)
    assert "MySQL" in str(exc.value)
    assert "PostgreSQL" in str(exc.value)


def test_mysql_url_fails_closed_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAM_DB_URL", "mysql+pymysql://user:pass@localhost/tram")
    with pytest.raises(RuntimeError) as exc:
        TramDB()
    assert "Unsupported database dialect" in str(exc.value)


# ── PostgreSQL dialect shapes (recording engine; test_queued_runs convention) ─


class _RecordingResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount

    def mappings(self):
        return self

    def fetchone(self):
        return None


class _RecordingConn:
    def __init__(self, engine):
        self._engine = engine
        self.calls: list[str] = []

    def execution_options(self, **kwargs):
        return self

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append(sql)
        return _RecordingResult(self._engine._rowcount_for(sql))

    def close(self):
        pass

    def invalidate(self):
        pass


class _RecordingEngine:
    def __init__(self, dialect, rowcount_for=None):
        self.dialect = types.SimpleNamespace(name=dialect)
        self._rowcount_for = rowcount_for or (lambda sql: 1)
        self.conn = _RecordingConn(self)

    def connect(self):
        return self.conn


def test_pg_claim_emits_returning_guard_key():
    """The frozen PG disposition appends RETURNING guard_key to the acquire."""
    engine = _RecordingEngine("postgresql")
    outcome = claim_run(
        engine, guard_key="p", guard_kind="batch", pipeline_name="p",
        run_id="run-1", generation=1, now=NOW,
    )
    assert outcome.status == CLAIMED
    updates = [s for s in engine.connect().calls if "UPDATE execution_guards" in s]
    assert updates, "guard acquire not emitted"
    assert "RETURNING guard_key" in updates[0]
    # the frozen acquire predicate is preserved
    assert "attempt_id IS NULL" in updates[0]


def test_sqlite_claim_emits_no_returning():
    engine = _RecordingEngine("sqlite")
    outcome = claim_run(
        engine, guard_key="p", guard_kind="batch", pipeline_name="p",
        run_id="run-1", generation=1, now=NOW,
    )
    assert outcome.status == CLAIMED
    assert "BEGIN IMMEDIATE" in engine.connect().calls
    updates = [s for s in engine.connect().calls if "UPDATE execution_guards" in s]
    assert "RETURNING" not in updates[0]


def test_pg_claim_loser_sees_zero_rows():
    """A 0-rowcount acquire on PG resolves to LOST (row-level locking fence)."""
    engine = _RecordingEngine(
        "postgresql",
        rowcount_for=lambda sql: 0 if "UPDATE execution_guards" in sql else 1,
    )
    outcome = claim_run(
        engine, guard_key="p", guard_kind="batch", pipeline_name="p",
        run_id="run-1", generation=1, now=NOW,
    )
    assert outcome.status == LOST


# ── Live PostgreSQL (env-gated; skipped without TRAM_TEST_POSTGRES_URL) ──────

PG_URL = os.environ.get("TRAM_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not PG_URL, reason="TRAM_TEST_POSTGRES_URL not set — no live PostgreSQL fixture")
class TestLivePostgres:
    """Mirror of the SQLite claim tests against a real PostgreSQL server."""

    @pytest.fixture
    def pgdb(self):
        d = TramDB(url=PG_URL)
        yield d
        d.close()

    def test_live_pg_claim_and_release(self, pgdb):
        # Unique namespace per run: the live server is shared and outlives the
        # test session — fixed ids would collide on a second run.
        sfx = uuid.uuid4().hex[:8]
        _make_intent(pgdb._engine, run_id=f"pg-run-1-{sfx}", pipeline_name=f"pg-p-{sfx}")
        out = claim_run(pgdb._engine, guard_key=f"pg-guard-{sfx}", guard_kind="batch",
                        pipeline_name=f"pg-p-{sfx}", run_id=f"pg-run-1-{sfx}", generation=1)
        assert out.status == CLAIMED
        assert release_guard(pgdb._engine, guard_key=f"pg-guard-{sfx}",
                             attempt_id=out.attempt_id, run_id=f"pg-run-1-{sfx}") == 1

    def test_live_pg_transition_fence(self, pgdb):
        sfx = uuid.uuid4().hex[:8]
        _make_intent(pgdb._engine, run_id=f"pg-run-2-{sfx}", pipeline_name=f"pg-p-{sfx}")
        claim_run(pgdb._engine, guard_key=f"pg-guard-{sfx}", guard_kind="batch",
                  pipeline_name=f"pg-p-{sfx}", run_id=f"pg-run-2-{sfx}", generation=1)
        assert transition_attempt(
            pgdb._engine, attempt_id=f"pg-run-2-{sfx}-a1", run_id=f"pg-run-2-{sfx}",
            from_state="claimed", to_state="dispatching", generation=1,
        ) == 1
        assert transition_attempt(
            pgdb._engine, attempt_id=f"pg-run-2-{sfx}-a1", run_id=f"pg-run-2-{sfx}",
            from_state="claimed", to_state="running", generation=1,
        ) == 0

    def test_live_pg_checkpoint_monotonic(self, pgdb):
        sfx = uuid.uuid4().hex[:8]

        def _commit(seq, cid):
            return commit_checkpoint(
                pgdb._engine, checkpoint_id=f"{cid}-{sfx}", pipeline_name=f"pg-p-{sfx}",
                generation=1,
                attempt_id=f"pg-run-3-{sfx}-a1", run_id=f"pg-run-3-{sfx}",
                source_unit="kafka/t/0",
                frontier_json="{}", frontier_seq=seq, sink_receipts="[]", state_revision=1,
            )

        assert _commit(5, "pg-cp-5").status == COMMITTED
        assert _commit(9, "pg-cp-9").status == COMMITTED
        assert _commit(4, "pg-cp-4").status == STALE