"""Persistence layer — SQLAlchemy Core backend, database-agnostic.

Supports any SQLAlchemy-compatible database via TRAM_DB_URL.
Falls back to SQLite at ~/.tram/tram.db (or TRAM_DB_PATH) when TRAM_DB_URL is unset.

Extras:
  pip install tram[postgresql]   # psycopg2-binary
  pip install tram[mysql]        # PyMySQL
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from tram.core.context import RunResult, RunStatus

logger = logging.getLogger(__name__)

# Review B9: bounded retries for the save_pipeline_version (name, version)
# race — the unique constraint converts a lost race into IntegrityError, and
# the retry mints a fresh version instead of failing the caller.
_VERSION_SAVE_RETRIES = 3


# ── Engine factory ────────────────────────────────────────────────────────────


def _build_engine(url: str = "") -> Engine:
    """Build SQLAlchemy engine from an explicit URL, TRAM_DB_URL, or SQLite path fallback."""
    resolved = url or os.environ.get("TRAM_DB_URL", "")

    if resolved:
        is_sqlite = resolved.startswith("sqlite")
        kwargs: dict = {}
        if is_sqlite:
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            kwargs["pool_pre_ping"] = True
            kwargs["pool_size"] = 5
            kwargs["max_overflow"] = 10
        return create_engine(resolved, **kwargs)

    # Fallback: SQLite at TRAM_DB_PATH (or default)
    raw = os.environ.get("TRAM_DB_PATH", "~/.tram/tram.db")
    path = Path(raw).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )


# ── Schema migration ──────────────────────────────────────────────────────────


def _is_duplicate_column_error(exc: Exception) -> bool:
    """True when the exception is the dialect's 'column already exists' error.

    Only that error is ignorable in ``_add_column_if_missing`` — a locked or
    disk-full DB must surface loudly instead of being indistinguishable from
    "column exists" (code review B8). Message matching avoids hard driver
    imports (pymysql/psycopg2 may not be installed).
    """
    msg = str(exc).lower()
    return "duplicate column name" in msg or "duplicate column" in msg


def _is_duplicate_index_error(exc: Exception) -> bool:
    """True when the exception is the dialect's 'index already exists' error.

    MySQL has no ``CREATE INDEX IF NOT EXISTS``, so the unique-index migration
    for pre-existing databases is wrapped and only the duplicate-index error is
    ignored (review B9); anything else raises loudly (B8 philosophy).
    """
    msg = str(exc).lower()
    return (
        "already exists" in msg
        or "duplicate key name" in msg
        or "1061" in msg  # MySQL ER_DUP_KEYNAME
    )


def _add_column_if_missing(conn, dialect: str, table: str, column: str, typedef: str) -> None:
    """Add a column to an existing table, ignoring only the duplicate-column error."""
    if dialect == "postgresql":
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {typedef}"))
        return
    try:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}"))
    except Exception as exc:
        if _is_duplicate_column_error(exc):
            return  # column already exists (SQLite has no IF NOT EXISTS for ADD COLUMN)
        raise  # anything else (lock, disk full, ...) is loud — review B8


def _create_tables(engine: Engine) -> None:
    """Idempotent schema setup: create tables + apply column migrations."""
    dialect = engine.dialect.name

    # pipeline_versions.id uses TEXT UUID (portable across all backends)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pipeline_versions (
                id           TEXT PRIMARY KEY NOT NULL,
                name         TEXT NOT NULL,
                version      INTEGER NOT NULL,
                yaml_content TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                is_active    INTEGER NOT NULL DEFAULT 1,
                UNIQUE (name, version)
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS run_history (
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
                dlq_count       INTEGER NOT NULL DEFAULT 0
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS alert_state (
                pipeline_name   TEXT NOT NULL,
                rule_name       TEXT NOT NULL,
                last_alerted_at TEXT NOT NULL,
                PRIMARY KEY (pipeline_name, rule_name)
            )
        """))

        # Indexes
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_pv_name ON pipeline_versions(name)"
        ))
        # Review B9: unique (name, version) so two concurrent saves can never
        # mint the same version number (SELECT MAX + INSERT race). Fresh
        # databases get the UNIQUE constraint inline in CREATE TABLE; existing
        # databases get the unique index here. "Already exists" is the only
        # ignorable failure (MySQL has no CREATE INDEX IF NOT EXISTS).
        #
        # A legacy database that ALREADY holds two rows with the same
        # (name, version) — exactly the corruption the pre-B9 race could
        # produce — would make the CREATE UNIQUE INDEX fail with an
        # IntegrityError and crash TramDB init (manager cannot boot after
        # upgrade). Dedup first, idempotently: for each (name, version) group
        # keep the newest row (created_at, then id as the deterministic
        # tiebreak) and delete the losers — the table is insert-only version
        # history, so stale duplicates are safe to drop. If any row of a
        # group was active, the survivor stays active so the "one active
        # version" invariant holds. Clean databases have no groups with more
        # than one row, so this is a no-op (a cheap COUNT probe gates it).
        _dup_groups = conn.execute(text("""
            SELECT COUNT(*) FROM (
                SELECT name, version FROM pipeline_versions
                GROUP BY name, version HAVING COUNT(*) > 1
            )
        """)).scalar()
        if _dup_groups:
            conn.execute(text("""
                UPDATE pipeline_versions SET is_active = 1
                WHERE id IN (
                    SELECT keep_id FROM (
                        SELECT
                            p.id AS pid,
                            (SELECT p2.id FROM pipeline_versions p2
                             WHERE p2.name = p.name AND p2.version = p.version
                             ORDER BY p2.created_at DESC, p2.id DESC LIMIT 1)
                                AS keep_id,
                            MAX(p.is_active) AS any_active
                        FROM pipeline_versions p
                        GROUP BY p.name, p.version
                    ) grouped
                    WHERE grouped.pid = grouped.keep_id
                      AND grouped.any_active = 1
                )
            """))
            conn.execute(text("""
                DELETE FROM pipeline_versions
                WHERE id IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY name, version
                                   ORDER BY created_at DESC, id DESC
                               ) AS rn
                        FROM pipeline_versions
                    ) ranked
                    WHERE rn > 1
                )
            """))
        try:
            if dialect in ("sqlite", "postgresql"):
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pv_name_version "
                    "ON pipeline_versions(name, version)"
                ))
            else:
                conn.execute(text(
                    "CREATE UNIQUE INDEX uq_pv_name_version "
                    "ON pipeline_versions(name, version)"
                ))
        except Exception as exc:
            if not _is_duplicate_index_error(exc):
                raise
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rh_pipeline ON run_history(pipeline_name)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rh_status ON run_history(status)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rh_started ON run_history(started_at)"
        ))

        # v0.9.0: processed-file tracking for batch file sources
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS processed_files (
                pipeline_name  TEXT NOT NULL,
                source_key     TEXT NOT NULL,
                filepath       TEXT NOT NULL,
                processed_at   TEXT NOT NULL,
                PRIMARY KEY (pipeline_name, source_key, filepath)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_pf_lookup "
            "ON processed_files(pipeline_name, source_key)"
        ))

        # v1.1.0: user password overrides (DB takes precedence over TRAM_AUTH_USERS env var)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_passwords (
                username    TEXT PRIMARY KEY NOT NULL,
                password_hash TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """))

        # v1.1.2: API-registered pipeline persistence (shared across cluster nodes)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS registered_pipelines (
                name        TEXT PRIMARY KEY NOT NULL,
                yaml_text   TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                deleted     INTEGER NOT NULL DEFAULT 0
            )
        """))

        # v1.1.4: generic key-value settings store (AI config, etc.)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY NOT NULL,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS broadcast_placements (
                placement_group_id TEXT PRIMARY KEY NOT NULL,
                pipeline_name      TEXT NOT NULL,
                slots_json         TEXT NOT NULL,
                target_count       TEXT NOT NULL,
                started_at         TEXT NOT NULL,
                status             TEXT NOT NULL,
                stopped_at         TEXT
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_bp_pipeline ON broadcast_placements(pipeline_name)"
        ))

        # v1.4.0 (E.2 / GH #21): queued manual runs — DB-backed queue for
        # no-capacity manual triggers. Broadcast-placement conventions: TEXT
        # columns, ISO-8601 UTC timestamps, idempotent DDL. Terminal rows
        # (dispatched/expired) are kept for audit; read paths filter on status.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS queued_runs (
                run_id        TEXT PRIMARY KEY NOT NULL,
                pipeline_name TEXT NOT NULL,
                yaml_snapshot TEXT NOT NULL,
                status        TEXT NOT NULL,
                requested_at  TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                dispatched_at TEXT
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_qr_pipeline ON queued_runs(pipeline_name)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_qr_status ON queued_runs(status)"
        ))

        # F.1 (GH #W-5.1): durable transform-state blobs — one row per pipeline
        # holding a JSON blob of every stateful transform's state, keyed by a
        # stable state_key (transform type + position in the transforms list).
        # Write rate is one row per pipeline per tick (see design §3.2a).
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS transform_state (
                pipeline_name TEXT PRIMARY KEY NOT NULL,
                state_json    TEXT NOT NULL,           -- {state_key: transform-specific blob}
                config_sha256 TEXT NOT NULL,           -- D.2 §6.1 convention
                updated_at    TEXT NOT NULL,
                updated_by    TEXT NOT NULL            -- run_id (audit)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_ts_updated ON transform_state(updated_at)"
        ))

        # v1.4.1 (A10): AI-audit append-only log — one row per AI call when
        # TRAM_AI_AUDIT is on. Not upserted: every call inserts a fresh UUID,
        # rows are never updated or deleted (append-only audit trail).
        # schema_version (v1.4.3, Issue #24): the content hash of SCHEMA_FIELDS
        # the prompt was built against — makes each row attributable to the
        # exact schema knowledge that produced the call.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_usage (
                id              TEXT PRIMARY KEY NOT NULL,
                ts              TEXT NOT NULL,
                mode            TEXT NOT NULL,
                client          TEXT NOT NULL,
                provider        TEXT NOT NULL,
                model           TEXT NOT NULL,
                tokens_in       INTEGER,
                tokens_out      INTEGER,
                ok              INTEGER NOT NULL DEFAULT 1,
                schema_version  TEXT
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_au_ts ON ai_usage(ts)"
        ))

        # v0.7.0 column migrations: add new columns to existing databases
        _add_column_if_missing(conn, dialect, "run_history", "node_id", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, dialect, "run_history", "dlq_count", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, dialect, "run_history", "bytes_in", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, dialect, "run_history", "bytes_out", "INTEGER NOT NULL DEFAULT 0")
        # v1.1.1: per-record error strings
        _add_column_if_missing(conn, dialect, "run_history", "errors_json", "TEXT")
        # v1.1.4: pause/resume support (kept for migration; superseded by 'stopped' in v1.2.0)
        _add_column_if_missing(conn, dialect, "registered_pipelines", "paused", "INTEGER NOT NULL DEFAULT 0")
        # v1.1.5: track whether pipeline was last saved by disk seed or API/UI
        _add_column_if_missing(conn, dialect, "registered_pipelines", "source", "TEXT NOT NULL DEFAULT 'api'")
        # v1.2.0: PipelineController — unified state machine
        # 'stopped': user explicitly stopped this pipeline; sync must NOT restart it
        #            replaces 'paused' (same semantics, clearer name)
        _add_column_if_missing(conn, dialect, "registered_pipelines", "stopped", "INTEGER NOT NULL DEFAULT 0")
        # v1.4.3 (Issue #24): schema identity on AI-audit rows — pre-existing
        # databases (v1.4.1/v1.4.2) get the column added; old rows stay NULL
        # (they predate the schema hash). The deployed /data/tram.db must not
        # break on upgrade.
        _add_column_if_missing(conn, dialect, "ai_usage", "schema_version", "TEXT")

        # v1.2.0 data migration: copy paused=1 → stopped=1 for existing rows
        try:
            conn.execute(text(
                "UPDATE registered_pipelines SET stopped = 1 WHERE paused = 1 AND stopped = 0"
            ))
        except Exception:
            pass  # paused column may not exist on fresh DBs


def _slot_matches_clause(dialect: str, check_run_id: bool) -> str:
    """SQL fragment verifying a placement slot's identity inside slots_json.

    Used as the WHERE guard of ``update_slot_run_id``'s conditional UPDATE so
    the write is keyed on the full slot identity (placement_group_id +
    worker_index, plus the expected current_run_id when provided) and fails
    (0 rows) when a concurrent writer already replaced the slot's run id
    (review A13 / plan D.1). slots_json is a JSON array of slot objects on all
    backends (column declared as TEXT), so each dialect needs its own
    extraction path.

    Note: only the SQLite branch is exercised by the test suite; the
    PostgreSQL and MySQL fragments follow each dialect's JSON semantics but
    are unverified against live servers (MySQL's number-vs-string binding
    in JSON_CONTAINS is the fragile spot).
    """
    if dialect == "postgresql":
        run_id_clause = "AND slot->>'current_run_id' = :expected_run_id" if check_run_id else ""
        return (
            "EXISTS ("
            "  SELECT 1 FROM jsonb_array_elements(broadcast_placements.slots_json::jsonb) AS slot"
            "  WHERE slot->>'worker_index' = CAST(:worker_index AS text)"
            f" {run_id_clause}"
            ")"
        )
    if dialect == "mysql":
        run_id_clause = ", 'current_run_id', :expected_run_id" if check_run_id else ""
        return (
            "JSON_CONTAINS("
            "  CAST(broadcast_placements.slots_json AS JSON),"
            f"  JSON_OBJECT('worker_index', :worker_index{run_id_clause})"
            ")"
        )
    # sqlite (the json1 extension ships with CPython's bundled sqlite3)
    run_id_clause = "AND json_extract(value, '$.current_run_id') = :expected_run_id" if check_run_id else ""
    return (
        "EXISTS ("
        "  SELECT 1 FROM json_each(broadcast_placements.slots_json)"
        "  WHERE json_extract(value, '$.worker_index') = :worker_index"
        f" {run_id_clause}"
        ")"
    )


# ── TramDB ────────────────────────────────────────────────────────────────────


class TramDB:
    """Database persistence for TRAM — pipeline versions, run history, alert state."""

    def __init__(self, url: str = "", node_id: str = "") -> None:
        """
        Args:
            url: SQLAlchemy database URL. Falls back to TRAM_DB_URL env var,
                 then to SQLite at TRAM_DB_PATH (default ~/.tram/tram.db).
            node_id: Identifier for this daemon instance, stored in run_history.
        """
        self._engine = _build_engine(url)
        self._node_id = node_id
        _create_tables(self._engine)
        logger.debug(
            "TramDB initialised",
            extra={"dialect": self._engine.dialect.name, "node_id": node_id},
        )

    # ── Reusable upsert ────────────────────────────────────────────────────

    def _upsert(
        self,
        table: str,
        values: dict[str, object],
        key_columns: tuple[str, ...],
        update_columns: tuple[str, ...] | None = None,
    ) -> None:
        """Insert-or-update a single row, keyed on *key_columns*, across dialects.

        sqlite / postgresql : INSERT ... ON CONFLICT (keys) DO UPDATE SET col = excluded.col
        mysql               : INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col)
        other               : DELETE by key + INSERT, in one transaction (generic fallback)

        ``values`` maps column -> value for ALL columns (keys included).
        *update_columns* selects which non-key columns are written on conflict:
        ``None`` (default) updates all non-key columns; ``()`` degrades to
        insert-if-absent (DO NOTHING / INSERT IGNORE / plain insert that raises
        on duplicate for exotic dialects). Raises on failure (does not swallow —
        contrast parked B8).
        """
        dialect = self._engine.dialect.name
        columns = list(values)
        if update_columns is None:
            update_columns = tuple(c for c in columns if c not in key_columns)

        col_list = ", ".join(columns)
        placeholders = ", ".join(f":{c}" for c in columns)
        insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
        key_where = " AND ".join(f"{c} = :{c}" for c in key_columns)

        with self._engine.begin() as conn:
            if dialect == "mysql":
                if update_columns:
                    updates = ", ".join(f"{c} = VALUES({c})" for c in update_columns)
                    sql = f"{insert_sql} ON DUPLICATE KEY UPDATE {updates}"
                else:
                    sql = f"INSERT IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
                conn.execute(text(sql), values)
            elif dialect in ("sqlite", "postgresql"):
                conflict = ", ".join(key_columns)
                if update_columns:
                    updates = ", ".join(f"{c} = excluded.{c}" for c in update_columns)
                    sql = f"{insert_sql} ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
                else:
                    sql = f"{insert_sql} ON CONFLICT ({conflict}) DO NOTHING"
                conn.execute(text(sql), values)
            elif update_columns:
                # Generic fallback: delete by key + insert, in one transaction.
                conn.execute(text(f"DELETE FROM {table} WHERE {key_where}"), values)
                conn.execute(text(insert_sql), values)
            else:
                # Generic fallback, insert-if-absent mode: no portable DO NOTHING;
                # a duplicate key surfaces as IntegrityError for the caller's
                # existing handling (mark_processed's try/except).
                conn.execute(text(insert_sql), values)

    # ── Health ─────────────────────────────────────────────────────────────

    def health_check(self) -> bool:
        """Execute SELECT 1 to verify DB connectivity. Returns True on success."""
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.error("DB health check failed", extra={"error": str(exc)})
            return False

    # ── Run history ────────────────────────────────────────────────────────

    def save_run(self, result: RunResult) -> None:
        """Persist a RunResult. Silently ignores duplicate run_ids."""
        import json as _json
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO run_history
                          (run_id, pipeline_name, status, started_at, finished_at,
                           records_in, records_out, records_skipped, bytes_in, bytes_out,
                           error, node_id, dlq_count, errors_json)
                        VALUES
                          (:run_id, :pipeline_name, :status, :started_at, :finished_at,
                           :records_in, :records_out, :records_skipped, :bytes_in, :bytes_out,
                           :error, :node_id, :dlq_count, :errors_json)
                    """),
                    {
                        "run_id": result.run_id,
                        "pipeline_name": result.pipeline_name,
                        "status": result.status.value,
                        "started_at": result.started_at.isoformat(),
                        "finished_at": result.finished_at.isoformat(),
                        "records_in": result.records_in,
                        "records_out": result.records_out,
                        "records_skipped": result.records_skipped,
                        "bytes_in": result.bytes_in,
                        "bytes_out": result.bytes_out,
                        "error": result.error,
                        "node_id": result.node_id or self._node_id,
                        "dlq_count": result.dlq_count,
                        "errors_json": _json.dumps(result.errors) if result.errors else None,
                    },
                )
        except IntegrityError:
            logger.debug("Run %s already persisted — skipping duplicate", result.run_id)

    def get_runs(
        self,
        pipeline_name: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        from_dt: datetime | None = None,
    ) -> list[RunResult]:
        """Return run history with optional filtering and pagination."""
        sql = "SELECT * FROM run_history WHERE 1=1"
        params: dict = {}

        if pipeline_name:
            sql += " AND pipeline_name = :pipeline_name"
            params["pipeline_name"] = pipeline_name
        if status:
            sql += " AND status = :status"
            params["status"] = status
        if from_dt:
            sql += " AND started_at >= :from_dt"
            params["from_dt"] = from_dt.isoformat()

        sql += " ORDER BY finished_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = limit
        params["offset"] = offset

        with self._engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().fetchall()
        return [self._row_to_run_result(r) for r in rows]

    def get_run(self, run_id: str) -> RunResult | None:
        """Fetch a single run by run_id."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM run_history WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).mappings().fetchone()
        if row is None:
            return None
        return self._row_to_run_result(row)

    def _row_to_run_result(self, row) -> RunResult:
        import json as _json
        raw_errors = row.get("errors_json")
        try:
            errors = _json.loads(raw_errors) if raw_errors else []
        except Exception:
            errors = []
        return RunResult(
            run_id=row["run_id"],
            pipeline_name=row["pipeline_name"],
            status=RunStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]),
            records_in=row["records_in"],
            records_out=row["records_out"],
            records_skipped=row["records_skipped"],
            bytes_in=row.get("bytes_in", 0) or 0,
            bytes_out=row.get("bytes_out", 0) or 0,
            error=row["error"],
            dlq_count=row.get("dlq_count", 0) or 0,
            node_id=row.get("node_id", "") or "",
            errors=errors,
        )

    # ── Pipeline versions ──────────────────────────────────────────────────

    def save_pipeline_version(self, name: str, yaml_content: str) -> int:
        """Save a new pipeline version unless it matches the active version.

        Returns the active/new version number.

        Two processes sharing a database can race the ``MAX(version)+1``
        computation; the unique ``(name, version)`` constraint (review B9)
        turns that race into an ``IntegrityError``, which is retried with a
        fresh computation instead of surfacing as a 500.
        """
        for attempt in range(_VERSION_SAVE_RETRIES):
            try:
                return self._save_pipeline_version_once(name, yaml_content)
            except IntegrityError:
                if attempt >= _VERSION_SAVE_RETRIES - 1:
                    raise
                logger.warning(
                    "Pipeline version save raced another writer — retrying",
                    extra={"pipeline": name, "attempt": attempt + 1},
                )

    def _save_pipeline_version_once(self, name: str, yaml_content: str) -> int:
        with self._engine.begin() as conn:
            active = conn.execute(
                text(
                    "SELECT version, yaml_content FROM pipeline_versions "
                    "WHERE name = :name AND is_active = 1 ORDER BY version DESC LIMIT 1"
                ),
                {"name": name},
            ).mappings().fetchone()
            if active is not None and active["yaml_content"] == yaml_content:
                return int(active["version"])

            row = conn.execute(
                text("SELECT COALESCE(MAX(version), 0) FROM pipeline_versions WHERE name = :name"),
                {"name": name},
            ).scalar()
            next_version = (row or 0) + 1

            conn.execute(
                text("UPDATE pipeline_versions SET is_active = 0 WHERE name = :name"),
                {"name": name},
            )
            conn.execute(
                text("""
                    INSERT INTO pipeline_versions
                      (id, name, version, yaml_content, created_at, is_active)
                    VALUES (:id, :name, :version, :yaml_content, :created_at, 1)
                """),
                {
                    "id": str(uuid.uuid4()),
                    "name": name,
                    "version": next_version,
                    "yaml_content": yaml_content,
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
        return next_version

    def activate_pipeline_version(self, name: str, version: int) -> str:
        """Mark an existing version active and return its YAML content."""
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT yaml_content FROM pipeline_versions "
                    "WHERE name = :name AND version = :version"
                ),
                {"name": name, "version": version},
            ).mappings().fetchone()
            if row is None:
                raise KeyError(f"Pipeline '{name}' version {version} not found")

            conn.execute(
                text("UPDATE pipeline_versions SET is_active = 0 WHERE name = :name"),
                {"name": name},
            )
            conn.execute(
                text(
                    "UPDATE pipeline_versions SET is_active = 1 "
                    "WHERE name = :name AND version = :version"
                ),
                {"name": name, "version": version},
            )
        return row["yaml_content"]

    def get_pipeline_versions(self, name: str) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, name, version, created_at, is_active "
                    "FROM pipeline_versions WHERE name = :name ORDER BY version DESC"
                ),
                {"name": name},
            ).mappings().fetchall()
        return [dict(r) for r in rows]

    def get_pipeline_version(self, name: str, version: int) -> str:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT yaml_content FROM pipeline_versions "
                    "WHERE name = :name AND version = :version"
                ),
                {"name": name, "version": version},
            ).mappings().fetchone()
        if row is None:
            raise KeyError(f"Pipeline '{name}' version {version} not found")
        return row["yaml_content"]

    def get_latest_version(self, name: str) -> str:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT yaml_content FROM pipeline_versions "
                    "WHERE name = :name AND is_active = 1"
                ),
                {"name": name},
            ).mappings().fetchone()
        if row is None:
            raise KeyError(f"No active version found for pipeline '{name}'")
        return row["yaml_content"]

    # ── Alert cooldown state ───────────────────────────────────────────────

    def get_alert_cooldown(self, pipeline_name: str, rule_name: str) -> datetime | None:
        """Return the last-alerted datetime for a rule, or None if never alerted."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT last_alerted_at FROM alert_state "
                    "WHERE pipeline_name = :pn AND rule_name = :rn"
                ),
                {"pn": pipeline_name, "rn": rule_name},
            ).mappings().fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["last_alerted_at"])

    def set_alert_cooldown(self, pipeline_name: str, rule_name: str, dt: datetime) -> None:
        """Upsert the last-alerted timestamp for a rule."""
        self._upsert(
            "alert_state",
            {
                "pipeline_name": pipeline_name,
                "rule_name": rule_name,
                "last_alerted_at": dt.isoformat(),
            },
            key_columns=("pipeline_name", "rule_name"),
        )

    # ── Processed-file tracking (v0.9.0) ──────────────────────────────────

    def is_processed(self, pipeline_name: str, source_key: str, filepath: str) -> bool:
        """Return True if this file has already been processed by this pipeline."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT 1 FROM processed_files
                    WHERE pipeline_name = :pn AND source_key = :sk AND filepath = :fp
                """),
                {"pn": pipeline_name, "sk": source_key, "fp": filepath},
            ).fetchone()
        return row is not None

    def mark_processed(self, pipeline_name: str, source_key: str, filepath: str) -> None:
        """Record a file as successfully processed. Silently ignores duplicates."""
        now = datetime.now(UTC).isoformat()
        try:
            # update_columns=() → insert-if-absent (DO NOTHING / INSERT IGNORE).
            self._upsert(
                "processed_files",
                {
                    "pipeline_name": pipeline_name,
                    "source_key": source_key,
                    "filepath": filepath,
                    "processed_at": now,
                },
                key_columns=("pipeline_name", "source_key", "filepath"),
                update_columns=(),
            )
        except Exception as exc:
            logger.warning(
                "Failed to mark file as processed",
                extra={"pipeline": pipeline_name, "filepath": filepath, "error": str(exc)},
            )

    # ── User passwords ─────────────────────────────────────────────────────

    def get_password_hash(self, username: str) -> str | None:
        """Return the stored password hash for *username*, or None if not overridden."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT password_hash FROM user_passwords WHERE username = :u"),
                {"u": username},
            ).fetchone()
        return row[0] if row else None

    def has_password_users(self) -> bool:
        """Return True when at least one DB-backed browser-auth user exists."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM user_passwords LIMIT 1"),
            ).fetchone()
        return row is not None

    def set_password_hash(self, username: str, password_hash: str) -> None:
        """Upsert a password hash for *username*."""
        now = datetime.now(UTC).isoformat()
        self._upsert(
            "user_passwords",
            {"username": username, "password_hash": password_hash, "updated_at": now},
            key_columns=("username",),
        )

    # ── v1.2.0: stopped flag (replaces paused) ────────────────────────────

    def stop_pipeline(self, name: str) -> None:
        """Set stopped=1: pipeline will not be auto-restarted by sync or rebalance."""
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE registered_pipelines SET stopped = 1, updated_at = :now WHERE name = :name"),
                {"now": now, "name": name},
            )

    def start_pipeline_flag(self, name: str) -> None:
        """Clear stopped=0: pipeline is free to be scheduled again."""
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE registered_pipelines SET stopped = 0, updated_at = :now WHERE name = :name"),
                {"now": now, "name": name},
            )

    def is_pipeline_stopped(self, name: str) -> bool:
        """Return True if the pipeline has been explicitly stopped by the user."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT stopped FROM registered_pipelines WHERE name = :name AND deleted = 0"),
                {"name": name},
            ).fetchone()
        return bool(row[0]) if row else False

    def get_stopped_pipeline_names(self) -> list[str]:
        """Return names of non-deleted pipelines that are explicitly stopped."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name FROM registered_pipelines WHERE deleted = 0 AND stopped = 1")
            ).fetchall()
        return [r[0] for r in rows]

    def save_pipeline(self, name: str, yaml_text: str, source: str = "api") -> None:
        """Upsert a pipeline YAML into the shared registry (marks deleted=False).

        source: 'api'  — saved by the UI or REST API (user owns it; disk seed will not overwrite)
                'disk' — seeded from ConfigMap/filesystem (may be overwritten by later disk seed
                         as long as the user has never saved it via API/UI)
        """
        now = datetime.now(UTC).isoformat()
        # created_at is deliberately excluded from update_columns — it is set on
        # first insert and preserved across later saves (legacy behavior).
        self._upsert(
            "registered_pipelines",
            {
                "name": name,
                "yaml_text": yaml_text,
                "created_at": now,
                "updated_at": now,
                "deleted": 0,
                "source": source,
            },
            key_columns=("name",),
            update_columns=("yaml_text", "updated_at", "deleted", "source"),
        )

    def delete_pipeline(self, name: str) -> None:
        """Soft-delete a pipeline from the shared registry."""
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE registered_pipelines SET deleted = 1, updated_at = :now WHERE name = :name"),
                {"now": now, "name": name},
            )

    def get_pipeline_source(self, name: str) -> str | None:
        """Return the source ('disk' or 'api') of a pipeline, or None if not found."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT source FROM registered_pipelines WHERE name = :name AND deleted = 0"),
                {"name": name},
            ).fetchone()
        return row[0] if row else None

    def get_all_pipelines(self) -> list[tuple[str, str]]:
        """Return (name, yaml_text) for all non-deleted registered pipelines."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name, yaml_text FROM registered_pipelines WHERE deleted = 0 ORDER BY name")
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def get_deleted_pipeline_names(self) -> list[str]:
        """Return names of soft-deleted pipelines (used by sync to deregister)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name FROM registered_pipelines WHERE deleted = 1")
            ).fetchall()
        return [r[0] for r in rows]

    # ── Settings (v1.1.4) ─────────────────────────────────────────────────

    def get_setting(self, key: str) -> str | None:
        """Return the stored value for *key*, or None if not set."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM settings WHERE key = :k"),
                {"k": key},
            ).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Upsert a key-value setting."""
        now = datetime.now(UTC).isoformat()
        self._upsert(
            "settings",
            {"key": key, "value": value, "updated_at": now},
            key_columns=("key",),
        )

    def delete_setting(self, key: str) -> None:
        """Remove a setting, reverting to env-var / default."""
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM settings WHERE key = :k"), {"k": key})

    # ── AI usage audit (v1.4.1, A10) ──────────────────────────────────────

    def append_ai_usage(
        self,
        ts: str,
        mode: str,
        client: str,
        provider: str,
        model: str,
        tokens_in: int | None,
        tokens_out: int | None,
        ok: bool,
        schema_version: str | None = None,
    ) -> None:
        """Append one AI-call audit row. Append-only: every call gets a fresh
        UUID, so nothing is ever updated or deleted. ``schema_version`` is the
        content hash of the connector schema the prompt was built against
        (Issue #24); None for callers/rows that predate the field."""
        with self._engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO ai_usage
                      (id, ts, mode, client, provider, model, tokens_in, tokens_out, ok, schema_version)
                    VALUES
                      (:id, :ts, :mode, :client, :provider, :model, :tokens_in, :tokens_out, :ok, :schema_version)
                """),
                {
                    "id": str(uuid.uuid4()),
                    "ts": ts,
                    "mode": mode,
                    "client": client,
                    "provider": provider,
                    "model": model,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "ok": 1 if ok else 0,
                    "schema_version": schema_version,
                },
            )

    def get_ai_usage(self, limit: int = 100) -> list[dict]:
        """Return the most recent AI-audit rows, newest first."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, ts, mode, client, provider, model, tokens_in, tokens_out, ok, schema_version
                    FROM ai_usage
                    ORDER BY ts DESC, id DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            ).mappings().fetchall()
        return [dict(r) for r in rows]

    # ── Broadcast placements (v1.3.0) ────────────────────────────────────

    def save_broadcast_placement(
        self,
        placement_group_id: str,
        pipeline_name: str,
        slots: list[dict],
        target_count: str | int,
        status: str,
        started_at: datetime | None = None,
    ) -> None:
        now = (started_at or datetime.now(UTC)).isoformat()
        self._upsert(
            "broadcast_placements",
            {
                "placement_group_id": placement_group_id,
                "pipeline_name": pipeline_name,
                "slots_json": json.dumps(slots),
                "target_count": target_count,
                "started_at": now,
                "status": status,
                "stopped_at": None,
            },
            key_columns=("placement_group_id",),
        )

    def get_active_broadcast_placements(self) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT placement_group_id, pipeline_name, slots_json, target_count, started_at, status, stopped_at
                FROM broadcast_placements
                WHERE stopped_at IS NULL AND status != 'stopped'
                ORDER BY started_at
            """)).mappings().fetchall()
        placements = []
        for row in rows:
            placements.append({
                "placement_group_id": row["placement_group_id"],
                "pipeline_name": row["pipeline_name"],
                "slots": json.loads(row["slots_json"]),
                "target_count": row["target_count"],
                "started_at": datetime.fromisoformat(row["started_at"]),
                "status": row["status"],
                "stopped_at": datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else None,
            })
        return placements

    def update_broadcast_placement_status(
        self,
        placement_group_id: str,
        status: str,
        slots: list[dict] | None = None,
    ) -> None:
        stopped_at = datetime.now(UTC).isoformat() if status == "stopped" else None
        sql = """
            UPDATE broadcast_placements
            SET status = :status,
                stopped_at = :stopped_at
        """
        params: dict[str, object] = {
            "placement_group_id": placement_group_id,
            "status": status,
            "stopped_at": stopped_at,
        }
        if slots is not None:
            sql += ", slots_json = :slots_json"
            params["slots_json"] = json.dumps(slots)
        sql += " WHERE placement_group_id = :placement_group_id"
        with self._engine.begin() as conn:
            conn.execute(text(sql), params)

    def deactivate_other_placements(self, pipeline_name: str, keep_placement_group_id: str) -> int:
        """Mark every other active placement row for a pipeline as stopped.

        Enforces the one-active-row-per-pipeline invariant (§7.5): a crash
        between a redispatch and a stop could otherwise leave two active rows.
        A single dialect-free UPDATE — no per-dialect JSON extraction needed
        because the WHERE clause only touches scalar columns.
        """
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE broadcast_placements
                SET status = 'stopped', stopped_at = :now
                WHERE pipeline_name = :pipeline_name
                  AND stopped_at IS NULL
                  AND status != 'stopped'
                  AND placement_group_id != :keep
            """), {
                "now": now,
                "pipeline_name": pipeline_name,
                "keep": keep_placement_group_id,
            })
        return result.rowcount

    def update_slot_run_id(
        self,
        placement_group_id: str,
        worker_index: int,
        current_run_id: str,
        status: str = "running",
        restart_count: int | None = None,
        expected_run_id: str | None = None,
    ) -> int:
        """Set a placement slot's current run id via a per-slot, CAS-scoped update.

        Replaces the previous read-modify-write (which scanned every active
        placement, mutated a slot copy, and wrote the whole placement back —
        clobbering concurrent slot updates, review A13). The write now targets
        the single placement row and is keyed on the full slot identity
        (placement_group_id + worker_index) plus, when *expected_run_id* is
        given, an optimistic-concurrency check that the slot still holds that
        run id — so a stale writer (a late stats payload or an old dispatch
        path racing a redispatch) detects the lost race instead of overwriting
        the newer run id (plan D.1).

        Returns the number of rows updated: 1 on success, 0 when the placement
        or slot is absent, the placement is stopped, or the slot no longer
        matches *expected_run_id* (lost race).
        """
        dialect = self._engine.dialect.name
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                SELECT placement_group_id, pipeline_name, slots_json, target_count,
                       started_at, status, stopped_at
                FROM broadcast_placements
                WHERE placement_group_id = :placement_group_id
                  AND stopped_at IS NULL
                  AND status != 'stopped'
            """), {"placement_group_id": placement_group_id}).mappings().fetchone()
            if row is None:
                return 0
            slots = json.loads(row["slots_json"])
            slot = next(
                (s for s in slots if int(s.get("worker_index", -1)) == worker_index),
                None,
            )
            if slot is None:
                return 0
            if expected_run_id is not None and str(slot.get("current_run_id", "")) != expected_run_id:
                return 0
            slot["current_run_id"] = current_run_id
            slot["status"] = status
            if restart_count is not None:
                slot["restart_count"] = restart_count

            params: dict[str, object] = {
                "placement_group_id": placement_group_id,
                "worker_index": worker_index,
                "slots_json": json.dumps(slots),
            }
            if expected_run_id is not None:
                params["expected_run_id"] = expected_run_id
            result = conn.execute(text(f"""
                UPDATE broadcast_placements
                SET slots_json = :slots_json
                WHERE placement_group_id = :placement_group_id
                  AND stopped_at IS NULL
                  AND status != 'stopped'
                  AND {_slot_matches_clause(dialect, expected_run_id is not None)}
            """), params)
            return result.rowcount

    # ── Queued manual runs (v1.4.0 / E.2, GH #21) ──────────────────────────

    @staticmethod
    def _parse_utc_ts(raw: str | None) -> datetime | None:
        """Parse an ISO-8601 timestamp, coercing naive values to UTC.

        Mirrors the reconciler's ``_slot_dispatch_time`` handling
        (reconciler.py): ``datetime.fromisoformat``, tzinfo-coerced to UTC.
        """
        if raw is None:
            return None
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    def _queued_run_row(self, row) -> dict:
        """Shape a queued_runs row as a dict with UTC-aware datetimes."""
        return {
            "run_id": row["run_id"],
            "pipeline_name": row["pipeline_name"],
            "yaml_snapshot": row["yaml_snapshot"],
            "status": row["status"],
            "requested_at": self._parse_utc_ts(row["requested_at"]),
            "expires_at": self._parse_utc_ts(row["expires_at"]),
            "dispatched_at": self._parse_utc_ts(row["dispatched_at"]),
        }

    def save_queued_run(
        self,
        run_id: str,
        pipeline_name: str,
        yaml_snapshot: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Persist a queued manual run (enqueue path; uses ``_upsert``).

        ``run_id`` is the primary key — a re-save of the same run_id refreshes
        the whole row (pipeline, snapshot, timestamps), matching the broadcast
        placement upsert semantics.
        """
        self._upsert(
            "queued_runs",
            {
                "run_id": run_id,
                "pipeline_name": pipeline_name,
                "yaml_snapshot": yaml_snapshot,
                "status": "queued",
                "requested_at": requested_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "dispatched_at": None,
            },
            key_columns=("run_id",),
        )

    def get_active_queued_runs(self) -> list[dict]:
        """Return all rows with status='queued', ordered by requested_at (drain order)."""
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT run_id, pipeline_name, yaml_snapshot, status,
                       requested_at, expires_at, dispatched_at
                FROM queued_runs
                WHERE status = 'queued'
                ORDER BY requested_at
            """)).mappings().fetchall()
        return [self._queued_run_row(r) for r in rows]

    def get_queued_run_view(self) -> list[dict]:
        """Return non-terminal queued/dispatching rows, ordered by requested_at (API merge)."""
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT run_id, pipeline_name, yaml_snapshot, status,
                       requested_at, expires_at, dispatched_at
                FROM queued_runs
                WHERE status IN ('queued', 'dispatching')
                ORDER BY requested_at
            """)).mappings().fetchall()
        return [self._queued_run_row(r) for r in rows]

    def get_active_queued_run_for_pipeline(self, pipeline_name: str) -> dict | None:
        """Return the active (non-terminal) queued run for a pipeline, or None."""
        with self._engine.connect() as conn:
            row = conn.execute(text("""
                SELECT run_id, pipeline_name, yaml_snapshot, status,
                       requested_at, expires_at, dispatched_at
                FROM queued_runs
                WHERE pipeline_name = :pipeline_name
                  AND status IN ('queued', 'dispatching')
                ORDER BY requested_at
                LIMIT 1
            """), {"pipeline_name": pipeline_name}).mappings().fetchone()
        return self._queued_run_row(row) if row is not None else None

    def claim_queued_run_row(self, run_id: str) -> int:
        """queued → dispatching. Conditional UPDATE; rowcount 1 is the single-claim fence."""
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE queued_runs SET status = 'dispatching'
                WHERE run_id = :run_id AND status = 'queued'
            """), {"run_id": run_id})
        return result.rowcount

    def mark_queued_run_dispatched(self, run_id: str, dispatched_at: datetime) -> int:
        """dispatching → dispatched (worker accepted the run). Records dispatched_at."""
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE queued_runs
                SET status = 'dispatched', dispatched_at = :dispatched_at
                WHERE run_id = :run_id AND status = 'dispatching'
            """), {"run_id": run_id, "dispatched_at": dispatched_at.isoformat()})
        return result.rowcount

    def revert_queued_run_row(self, run_id: str) -> int:
        """dispatching → queued (dispatch failed / capacity vanished mid-pass)."""
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE queued_runs SET status = 'queued'
                WHERE run_id = :run_id AND status = 'dispatching'
            """), {"run_id": run_id})
        return result.rowcount

    def expire_queued_run_row(self, run_id: str) -> int:
        """queued → expired (TTL elapsed without capacity)."""
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE queued_runs SET status = 'expired'
                WHERE run_id = :run_id AND status = 'queued'
            """), {"run_id": run_id})
        return result.rowcount

    def refresh_queued_run_yaml(self, pipeline_name: str, yaml_text: str) -> int:
        """Refresh yaml_snapshot for a pipeline's queued rows (config currency).

        Only status='queued' rows are refreshed — a 'dispatching' row has
        already handed its snapshot to the worker.
        """
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE queued_runs SET yaml_snapshot = :yaml_text
                WHERE pipeline_name = :pipeline_name AND status = 'queued'
            """), {"pipeline_name": pipeline_name, "yaml_text": yaml_text})
        return result.rowcount

    def delete_queued_runs(self, pipeline_name: str) -> int:
        """Purge a pipeline's non-terminal queued rows (delete/stop); keep audit rows."""
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                DELETE FROM queued_runs
                WHERE pipeline_name = :pipeline_name
                  AND status IN ('queued', 'dispatching')
            """), {"pipeline_name": pipeline_name})
        return result.rowcount

    def reset_dispatching_queued_runs(self) -> int:
        """dispatching → queued for every row (boot recovery; nothing in flight)."""
        with self._engine.begin() as conn:
            result = conn.execute(text("""
                UPDATE queued_runs SET status = 'queued'
                WHERE status = 'dispatching'
            """))
        return result.rowcount

    # ── Transform state (F.1 / GH #W-5.1) ─────────────────────────────────

    def load_transform_state(self, pipeline_name: str) -> dict | None:
        """Return a pipeline's transform_state row, or None when absent.

        The ``state`` value is the decoded JSON blob (``{state_key: blob}``);
        ``config_sha256`` lets the executor discard state on config change
        (D.2 §6.1 convention, design §3.2d).
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT state_json, config_sha256, updated_at, updated_by "
                    "FROM transform_state WHERE pipeline_name = :pn"
                ),
                {"pn": pipeline_name},
            ).mappings().fetchone()
        if row is None:
            return None
        return {
            "state": json.loads(row["state_json"]),
            "config_sha256": row["config_sha256"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    def save_transform_state(
        self,
        pipeline_name: str,
        state: dict,
        config_sha256: str,
        updated_by: str = "",
    ) -> None:
        """Upsert a pipeline's transform-state blob (one row per pipeline).

        Uses the E.2 ``_upsert`` helper; the row is replaced wholesale (single
        writer per pipeline, design §3.3), ``updated_at`` refreshed and
        ``updated_by`` recording the last writer's run_id for audit.
        """
        now = datetime.now(UTC).isoformat()
        self._upsert(
            "transform_state",
            {
                "pipeline_name": pipeline_name,
                "state_json": json.dumps(state),
                "config_sha256": config_sha256,
                "updated_at": now,
                "updated_by": updated_by,
            },
            key_columns=("pipeline_name",),
            update_columns=("state_json", "config_sha256", "updated_at", "updated_by"),
        )

    def delete_transform_state(self, pipeline_name: str) -> None:
        """Remove a pipeline's transform-state row.

        Belt-and-braces config currency (design §3.2d): a changed transform
        list may change key semantics, so ``controller.update()``/``delete()``
        delete the row outright instead of letting hydration discard it.
        """
        with self._engine.begin() as conn:
            conn.execute(
                text("DELETE FROM transform_state WHERE pipeline_name = :pn"),
                {"pn": pipeline_name},
            )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def close(self) -> None:
        self._engine.dispose()
