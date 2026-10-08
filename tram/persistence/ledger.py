"""Manager execution ledger — V18-01 §3 claim helpers.

Thin functions over the shared SQLAlchemy engine built by
``tram.persistence.db``, implementing the frozen conditional-UPDATE statements
(docs/plans/v1.8.0-v18-01-contracts.md §3). Rowcount is the authority — no
read-then-write. The whole claim (guard acquire + intent claim + attempt
insert) is one transaction: ``BEGIN IMMEDIATE`` on SQLite so the writer lock
covers the rowcount reads, plain ``BEGIN`` on PostgreSQL where row-level
locking serializes concurrent claimers and the loser sees 0 rows.

The worker journal deliberately does not use this engine (frozen §3 — worker
image isolation, plan B).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine

# ── Claim outcome statuses ────────────────────────────────────────────────────

CLAIMED = "claimed"            # guard acquired, intent claimed, attempt inserted
ALREADY_HELD = "already_held"  # guard already held by this exact attempt (idempotent re-claim)
LOST = "lost"                  # guard held by a different attempt_id/run_id
NO_INTENT = "no_intent"        # intent row absent or already resolved; claim aborted

# Checkpoint commit outcome statuses (frozen §7)
COMMITTED = "committed"        # row inserted or frontier advanced
STALE = "stale"                # older/equal frontier_seq rejected by the monotonic guard


@dataclass
class ClaimOutcome:
    """Result of a ledger claim. ``status`` is one of the module CLAIM_* constants.

    ``fence_token`` is populated on CLAIMED (freshly minted). On ALREADY_HELD
    it is None — the guard already holds the attempt's original fence token,
    which the caller must fetch via :func:`get_attempt` (the ledger never
    reissues an authorization for a retired attempt, frozen §1).
    """

    status: str
    attempt_id: str | None = None
    run_id: str | None = None
    pipeline_name: str | None = None
    ordinal: int | None = None
    generation: int | None = None
    slot_id: str = ""
    fence_token: str | None = None


@dataclass
class CheckpointOutcome:
    """Result of a checkpoint commit (frozen §7).

    On STALE, ``existing`` carries the committed row so the caller can restore
    committed state and answer ``already_committed: true`` without reapplying
    transforms or re-emitting recorded outputs.
    """

    status: str
    checkpoint_id: str | None = None
    existing: dict | None = None


# ── Identity minting (frozen §1: the ledger is the only minting authority) ────


def mint_attempt_id(run_id: str, ordinal: int) -> str:
    """attempt_id = ``run_id + '-a' + N``, N = 1-based decimal ordinal, unpadded."""
    return f"{run_id}-a{ordinal}"


# ── Transaction wrapper ───────────────────────────────────────────────────────


def _in_claim_txn(engine: Engine, fn) -> ClaimOutcome:
    """Run a claim inside one transaction (frozen V18-01 §3).

    SQLite: ``BEGIN IMMEDIATE`` — the writer lock covers every rowcount read,
    so concurrent claimers serialize and exactly one sees rowcount 1.
    PostgreSQL: plain ``BEGIN``; row-level locking serializes the claimers and
    the loser sees 0 rows (the frozen disposition — ``RETURNING guard_key`` is
    appended to the acquire so the affected row is asserted).

    Commits only when the outcome is CLAIMED; every other outcome rolls the
    transaction back (no orphan guard rows). An exception rolls back and, if
    the rollback itself fails, invalidates the pooled connection so an open
    manual transaction can never leak into the pool.
    """
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        if engine.dialect.name == "sqlite":
            conn.execute(text("BEGIN IMMEDIATE"))
        else:
            conn.execute(text("BEGIN"))
        try:
            outcome = fn(conn)
            if outcome.status == CLAIMED:
                conn.execute(text("COMMIT"))
            else:
                conn.execute(text("ROLLBACK"))
            return outcome
        except BaseException:
            try:
                conn.execute(text("ROLLBACK"))
            except Exception:
                conn.invalidate()
            raise
    finally:
        conn.close()


# ── Claim (frozen §3) ─────────────────────────────────────────────────────────


def claim_run(
    engine: Engine,
    *,
    guard_key: str,
    guard_kind: str,
    pipeline_name: str,
    run_id: str,
    generation: int,
    ordinal: int = 1,
    slot_id: str = "",
    yaml_snapshot: str | None = None,
    now: str | None = None,
) -> ClaimOutcome:
    """Claim a run: acquire the guard, claim the intent, insert the attempt.

    One transaction (frozen §3). ``attempt_id`` is minted here as
    ``{run_id}-a{ordinal}`` and ``fence_token`` as a fresh UUID4. Rowcount is
    the authority: the guard acquire returns 1 to exactly one concurrent
    claimer; a guard already held by this exact attempt_id/run_id (a retry
    inside the run, or a duplicate claim) is an idempotent ALREADY_HELD. A
    missing or already-resolved intent row aborts the claim (NO_INTENT) and
    rolls the transaction back — the queue reservation does not convert to a
    guard for a run that can no longer proceed.

    ``yaml_snapshot`` is the claimed dispatch snapshot (frozen column
    comment); None keeps whatever the intent row already carries.
    """
    now = now or datetime.now(UTC).isoformat()
    attempt_id = mint_attempt_id(run_id, ordinal)
    fence_token = str(uuid.uuid4())
    dialect = engine.dialect.name
    returning = " RETURNING guard_key" if dialect == "postgresql" else ""

    def _run(conn) -> ClaimOutcome:
        # Guard-row lifecycle: ensure the row exists (free = attempt_id NULL),
        # then acquire with the frozen conditional UPDATE. Both dialects
        # support INSERT ... ON CONFLICT DO NOTHING.
        conn.execute(
            text("""
                INSERT INTO execution_guards (guard_key, guard_kind)
                VALUES (:guard_key, :guard_kind)
                ON CONFLICT (guard_key) DO NOTHING
            """),
            {"guard_key": guard_key, "guard_kind": guard_kind},
        )
        acquired = conn.execute(
            text(f"""
                UPDATE execution_guards
                   SET run_id = :run_id, attempt_id = :attempt_id,
                       generation = :generation, acquired_at = :now
                 WHERE guard_key = :guard_key AND attempt_id IS NULL
                 {returning}
            """),
            {
                "guard_key": guard_key,
                "run_id": run_id,
                "attempt_id": attempt_id,
                "generation": generation,
                "now": now,
            },
        )
        if acquired.rowcount != 1:
            # Disambiguation read (not the authority — the conditional UPDATE
            # already decided): lost to a different claimer, or already held
            # by this same attempt (idempotent re-claim)?
            held = conn.execute(
                text(
                    "SELECT run_id, attempt_id FROM execution_guards "
                    "WHERE guard_key = :guard_key"
                ),
                {"guard_key": guard_key},
            ).mappings().fetchone()
            if held is not None and held["attempt_id"] == attempt_id and held["run_id"] == run_id:
                return ClaimOutcome(
                    ALREADY_HELD, attempt_id=attempt_id, run_id=run_id,
                    pipeline_name=pipeline_name, ordinal=ordinal,
                    generation=generation, slot_id=slot_id,
                )
            return ClaimOutcome(
                LOST, attempt_id=attempt_id, run_id=run_id,
                pipeline_name=pipeline_name, ordinal=ordinal,
                generation=generation, slot_id=slot_id,
            )

        # Intent claim: the queue reservation converts to the guard only when
        # the intent is still unresolved (final_outcome IS NULL). COALESCE
        # keeps an existing snapshot when the caller has none to supply.
        claimed = conn.execute(
            text("""
                UPDATE run_intents
                   SET yaml_snapshot = COALESCE(:yaml_snapshot, yaml_snapshot)
                 WHERE run_id = :run_id AND final_outcome IS NULL
            """),
            {"run_id": run_id, "yaml_snapshot": yaml_snapshot},
        )
        if claimed.rowcount != 1:
            return ClaimOutcome(
                NO_INTENT, attempt_id=attempt_id, run_id=run_id,
                pipeline_name=pipeline_name, ordinal=ordinal,
                generation=generation, slot_id=slot_id,
            )

        # Attempt row: state = 'claimed' (frozen §2: [*] --> claimed on guard
        # acquisition). A conflicting attempt_id surfaces as IntegrityError and
        # rolls the whole transaction back — nothing half-claimed survives.
        conn.execute(
            text("""
                INSERT INTO execution_attempts
                    (attempt_id, run_id, pipeline_name, ordinal, generation,
                     slot_id, fence_token, state)
                VALUES
                    (:attempt_id, :run_id, :pipeline_name, :ordinal, :generation,
                     :slot_id, :fence_token, 'claimed')
            """),
            {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "pipeline_name": pipeline_name,
                "ordinal": ordinal,
                "generation": generation,
                "slot_id": slot_id,
                "fence_token": fence_token,
            },
        )
        return ClaimOutcome(
            CLAIMED, attempt_id=attempt_id, run_id=run_id,
            pipeline_name=pipeline_name, ordinal=ordinal,
            generation=generation, slot_id=slot_id, fence_token=fence_token,
        )

    return _in_claim_txn(engine, _run)


# ── Guard release / completion (frozen §3) ───────────────────────────────────


def release_guard(engine: Engine, *, guard_key: str, attempt_id: str, run_id: str) -> int:
    """Release the guard — identity-compared, never name-keyed (frozen R5).

    Returns the rowcount: 1 when *this* attempt still holds the guard (released),
    0 when a foreign attempt_id/run_id holds it (releases nothing) or the guard
    is already free.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                UPDATE execution_guards
                   SET run_id = NULL, attempt_id = NULL,
                       generation = NULL, acquired_at = NULL
                 WHERE guard_key = :guard_key
                   AND attempt_id = :attempt_id
                   AND run_id = :run_id
            """),
            {"guard_key": guard_key, "attempt_id": attempt_id, "run_id": run_id},
        )
    return result.rowcount


# ── Attempt state transition (frozen §3 / §2 table) ──────────────────────────


def transition_attempt(
    engine: Engine,
    *,
    attempt_id: str,
    run_id: str,
    from_state: str,
    to_state: str,
    generation: int,
) -> int:
    """Fenced attempt-state transition.

    Identity- and current-state-fenced conditional UPDATE (frozen §3); rowcount
    is the authority: 1 = transition applied, 0 = wrong attempt/run/generation
    or the attempt is no longer in ``from_state``. Transitions not listed in
    the frozen §2 table never match the fence and are therefore rejected. This
    helper moves ``state`` only; detail fields (finished_at, result_json,
    cancel_reason, ...) are written by the controller lane in the same
    transaction using the same fence.
    """
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                UPDATE execution_attempts
                   SET state = :to_state
                 WHERE attempt_id = :attempt_id
                   AND run_id = :run_id
                   AND state = :from_state
                   AND generation = :generation
            """),
            {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "from_state": from_state,
                "to_state": to_state,
                "generation": generation,
            },
        )
    return result.rowcount


# ── Intent resolution (frozen §3) ────────────────────────────────────────────


def resolve_intent(
    engine: Engine,
    *,
    run_id: str,
    outcome: str,
    attempt_id: str,
    now: str | None = None,
) -> int:
    """Terminal resolution of a run intent — idempotent for the winner,
    harmless for the duplicate (frozen §3).

    Returns the rowcount: 1 when this call resolved the intent, 0 when it was
    already resolved (a late/duplicate callback for a terminal run leaves the
    winner's row untouched and cannot touch a newer guard or generation).
    """
    now = now or datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                UPDATE run_intents
                   SET final_outcome = :outcome, final_attempt_id = :attempt_id,
                       resolved_at = :now
                 WHERE run_id = :run_id AND final_outcome IS NULL
            """),
            {"run_id": run_id, "outcome": outcome, "attempt_id": attempt_id, "now": now},
        )
    return result.rowcount


# ── Checkpoint upsert (frozen §7) ────────────────────────────────────────────


def commit_checkpoint(
    engine: Engine,
    *,
    checkpoint_id: str,
    pipeline_name: str,
    generation: int,
    attempt_id: str,
    run_id: str,
    source_unit: str,
    frontier_json: str,
    frontier_seq: int,
    sink_receipts: str,
    state_revision: int,
    now: str | None = None,
) -> CheckpointOutcome:
    """Atomic checkpoint upsert (frozen §7).

    ``(pipeline_name, source_unit)`` is the row key; advancing frontiers upsert
    in place under the monotonic guard ``WHERE :frontier_seq >
    delivery_checkpoints.frontier_seq``. Rowcount is the authority: 1 =
    inserted or advanced (COMMITTED), 0 = an older/equal frontier_seq was
    rejected (STALE) and the committed row is returned in ``existing``.

    This helper commits the checkpoint row only; the frozen §7 atomic
    checkpoint additionally writes the generation- and revision-fenced
    transform_state CAS in the same transaction (a V18-06 lane concern, wired
    by the controller lane).

    ``checkpoint_id`` is minted by the manager at the unit's first commit and
    the DO UPDATE clause never rewrites it — advancing frontiers keep the
    original identity, so both outcomes report the *stored* checkpoint_id
    (frozen §7: "a committed unit returns the existing checkpoint_id").
    """
    now = now or datetime.now(UTC).isoformat()
    params = {
        "checkpoint_id": checkpoint_id,
        "pipeline_name": pipeline_name,
        "generation": generation,
        "attempt_id": attempt_id,
        "run_id": run_id,
        "source_unit": source_unit,
        "frontier_json": frontier_json,
        "frontier_seq": frontier_seq,
        "sink_receipts": sink_receipts,
        "state_revision": state_revision,
        "committed_at": now,
    }
    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO delivery_checkpoints
                    (checkpoint_id, pipeline_name, generation, attempt_id, run_id,
                     source_unit, frontier_json, frontier_seq, sink_receipts,
                     state_revision, committed_at)
                VALUES
                    (:checkpoint_id, :pipeline_name, :generation, :attempt_id, :run_id,
                     :source_unit, :frontier_json, :frontier_seq, :sink_receipts,
                     :state_revision, :committed_at)
                ON CONFLICT (pipeline_name, source_unit) DO UPDATE
                   SET frontier_json = :frontier_json, frontier_seq = :frontier_seq,
                       sink_receipts = :sink_receipts, state_revision = :state_revision,
                       generation = :generation, attempt_id = :attempt_id,
                       committed_at = :committed_at
                 WHERE :frontier_seq > delivery_checkpoints.frontier_seq
            """),
            params,
        )
        # Post-write read of the stored identity — the write already happened
        # (rowcount is the authority for committed/stale); this only reports
        # the row's checkpoint_id, which the DO UPDATE never changes.
        stored = conn.execute(
            text("""
                SELECT checkpoint_id, pipeline_name, generation, attempt_id, run_id,
                       source_unit, frontier_json, frontier_seq, sink_receipts,
                       state_revision, committed_at
                FROM delivery_checkpoints
                WHERE pipeline_name = :pipeline_name AND source_unit = :source_unit
            """),
            {"pipeline_name": pipeline_name, "source_unit": source_unit},
        ).mappings().fetchone()
        stored_row = dict(stored) if stored is not None else None
        stored_id = stored_row["checkpoint_id"] if stored_row else checkpoint_id
        if result.rowcount == 1:
            return CheckpointOutcome(COMMITTED, checkpoint_id=stored_id)
        return CheckpointOutcome(STALE, checkpoint_id=stored_id, existing=stored_row)


# ── Read helper ───────────────────────────────────────────────────────────────


def get_attempt(engine: Engine, attempt_id: str) -> dict | None:
    """Read one attempt row as a dict, or None.

    Convenience for boot adoption (resolve dispatching rows against the worker
    journal by run ID before any scheduler fires, frozen §3) and for the
    ALREADY_HELD re-claim path — the attempt row carries the original
    fence_token the ledger never reissues.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT * FROM execution_attempts WHERE attempt_id = :attempt_id"
            ),
            {"attempt_id": attempt_id},
        ).mappings().fetchone()
    return dict(row) if row is not None else None