"""Persistence layer — SQLAlchemy Core backend, database-agnostic.

Supports any SQLAlchemy-compatible database via TRAM_DB_URL.
Falls back to SQLite at ~/.tram/tram.db (or TRAM_DB_PATH) when TRAM_DB_URL is unset.

Extras:
  pip install tram[postgresql]   # psycopg2-binary
  pip install tram[mysql]        # PyMySQL
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from tram.core.context import RunResult, RunStatus

logger = logging.getLogger(__name__)


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


def _add_column_if_missing(conn, dialect: str, table: str, column: str, typedef: str) -> None:
    """Add a column to an existing table, ignoring errors if it already exists."""
    if dialect == "postgresql":
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {typedef}"))
    else:
        try:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}"))
        except Exception:
            pass  # column already exists (SQLite has no IF NOT EXISTS for ADD COLUMN)


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
                is_active    INTEGER NOT NULL DEFAULT 1
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
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rh_pipeline ON run_history(pipeline_name)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rh_status ON run_history(status)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_rh_started ON run_history(started_at)"
        ))

        # v0.8.0: node registry for cluster coordination
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS node_registry (
                node_id        TEXT PRIMARY KEY,
                ordinal        INTEGER NOT NULL DEFAULT 0,
                registered_at  TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL,
                status         TEXT NOT NULL DEFAULT 'active'
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_nr_heartbeat ON node_registry(last_heartbeat)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_nr_status ON node_registry(status)"
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

        # v0.7.0 column migrations: add new columns to existing databases
        _add_column_if_missing(conn, dialect, "run_history", "node_id", "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, dialect, "run_history", "dlq_count", "INTEGER NOT NULL DEFAULT 0")


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
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text("""
                        INSERT INTO run_history
                          (run_id, pipeline_name, status, started_at, finished_at,
                           records_in, records_out, records_skipped, error,
                           node_id, dlq_count)
                        VALUES
                          (:run_id, :pipeline_name, :status, :started_at, :finished_at,
                           :records_in, :records_out, :records_skipped, :error,
                           :node_id, :dlq_count)
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
                        "error": result.error,
                        "node_id": self._node_id,
                        "dlq_count": result.dlq_count,
                    },
                )
        except IntegrityError:
            logger.debug("Run %s already persisted — skipping duplicate", result.run_id)

    def get_runs(
        self,
        pipeline_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        from_dt: Optional[datetime] = None,
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

    def get_run(self, run_id: str) -> Optional[RunResult]:
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
        return RunResult(
            run_id=row["run_id"],
            pipeline_name=row["pipeline_name"],
            status=RunStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=datetime.fromisoformat(row["finished_at"]),
            records_in=row["records_in"],
            records_out=row["records_out"],
            records_skipped=row["records_skipped"],
            error=row["error"],
            dlq_count=row.get("dlq_count", 0) or 0,
            node_id=row.get("node_id", "") or "",
        )

    # ── Pipeline versions ──────────────────────────────────────────────────

    def save_pipeline_version(self, name: str, yaml_content: str) -> int:
        """Save a new pipeline version; deactivate previous. Returns new version number."""
        with self._engine.begin() as conn:
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
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        return next_version

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

    def get_alert_cooldown(self, pipeline_name: str, rule_name: str) -> Optional[datetime]:
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
        dialect = self._engine.dialect.name
        ts = dt.isoformat()
        with self._engine.begin() as conn:
            if dialect == "sqlite":
                conn.execute(
                    text("""
                        INSERT OR REPLACE INTO alert_state
                          (pipeline_name, rule_name, last_alerted_at)
                        VALUES (:pn, :rn, :ts)
                    """),
                    {"pn": pipeline_name, "rn": rule_name, "ts": ts},
                )
            elif dialect in ("postgresql", "postgres"):
                conn.execute(
                    text("""
                        INSERT INTO alert_state (pipeline_name, rule_name, last_alerted_at)
                        VALUES (:pn, :rn, :ts)
                        ON CONFLICT (pipeline_name, rule_name)
                        DO UPDATE SET last_alerted_at = EXCLUDED.last_alerted_at
                    """),
                    {"pn": pipeline_name, "rn": rule_name, "ts": ts},
                )
            elif dialect == "mysql":
                conn.execute(
                    text("""
                        INSERT INTO alert_state (pipeline_name, rule_name, last_alerted_at)
                        VALUES (:pn, :rn, :ts)
                        ON DUPLICATE KEY UPDATE last_alerted_at = VALUES(last_alerted_at)
                    """),
                    {"pn": pipeline_name, "rn": rule_name, "ts": ts},
                )
            else:
                # Generic fallback: delete + insert
                conn.execute(
                    text(
                        "DELETE FROM alert_state WHERE pipeline_name = :pn AND rule_name = :rn"
                    ),
                    {"pn": pipeline_name, "rn": rule_name},
                )
                conn.execute(
                    text("""
                        INSERT INTO alert_state (pipeline_name, rule_name, last_alerted_at)
                        VALUES (:pn, :rn, :ts)
                    """),
                    {"pn": pipeline_name, "rn": rule_name, "ts": ts},
                )

    # ── Node registry (cluster coordination) ──────────────────────────────

    def register_node(self, node_id: str, ordinal: int) -> None:
        """Upsert node registration with current timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        dialect = self._engine.dialect.name
        with self._engine.begin() as conn:
            if dialect == "sqlite":
                conn.execute(
                    text("""
                        INSERT OR REPLACE INTO node_registry
                          (node_id, ordinal, registered_at, last_heartbeat, status)
                        VALUES (:node_id, :ordinal, :now, :now, 'active')
                    """),
                    {"node_id": node_id, "ordinal": ordinal, "now": now},
                )
            elif dialect in ("postgresql", "postgres"):
                conn.execute(
                    text("""
                        INSERT INTO node_registry
                          (node_id, ordinal, registered_at, last_heartbeat, status)
                        VALUES (:node_id, :ordinal, :now, :now, 'active')
                        ON CONFLICT (node_id) DO UPDATE
                          SET ordinal = EXCLUDED.ordinal,
                              last_heartbeat = EXCLUDED.last_heartbeat,
                              status = 'active'
                    """),
                    {"node_id": node_id, "ordinal": ordinal, "now": now},
                )
            elif dialect == "mysql":
                conn.execute(
                    text("""
                        INSERT INTO node_registry
                          (node_id, ordinal, registered_at, last_heartbeat, status)
                        VALUES (:node_id, :ordinal, :now, :now, 'active')
                        ON DUPLICATE KEY UPDATE
                          ordinal = VALUES(ordinal),
                          last_heartbeat = VALUES(last_heartbeat),
                          status = 'active'
                    """),
                    {"node_id": node_id, "ordinal": ordinal, "now": now},
                )
            else:
                conn.execute(
                    text("DELETE FROM node_registry WHERE node_id = :node_id"),
                    {"node_id": node_id},
                )
                conn.execute(
                    text("""
                        INSERT INTO node_registry
                          (node_id, ordinal, registered_at, last_heartbeat, status)
                        VALUES (:node_id, :ordinal, :now, :now, 'active')
                    """),
                    {"node_id": node_id, "ordinal": ordinal, "now": now},
                )

    def heartbeat(self, node_id: str) -> None:
        """Update last_heartbeat timestamp for a node."""
        now = datetime.now(timezone.utc).isoformat()
        with self._engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE node_registry
                    SET last_heartbeat = :now, status = 'active'
                    WHERE node_id = :node_id
                """),
                {"now": now, "node_id": node_id},
            )

    def expire_nodes(self, ttl_seconds: int) -> None:
        """Mark nodes with a stale heartbeat as 'dead'."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat()
        with self._engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE node_registry SET status = 'dead'
                    WHERE status = 'active' AND last_heartbeat < :cutoff
                """),
                {"cutoff": cutoff},
            )

    def get_live_nodes(self, ttl_seconds: int) -> list[dict]:
        """Return nodes whose heartbeat is within the TTL window."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat()
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT node_id, ordinal, registered_at, last_heartbeat, status
                    FROM node_registry
                    WHERE status = 'active' AND last_heartbeat >= :cutoff
                    ORDER BY node_id
                """),
                {"cutoff": cutoff},
            ).mappings().fetchall()
        return [dict(r) for r in rows]

    def deregister_node(self, node_id: str) -> None:
        """Mark a node as stopped (graceful shutdown)."""
        with self._engine.begin() as conn:
            conn.execute(
                text("UPDATE node_registry SET status = 'stopped' WHERE node_id = :node_id"),
                {"node_id": node_id},
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
        now = datetime.now(timezone.utc).isoformat()
        dialect = self._engine.dialect.name
        try:
            with self._engine.begin() as conn:
                if dialect == "sqlite":
                    conn.execute(
                        text("""
                            INSERT OR IGNORE INTO processed_files
                              (pipeline_name, source_key, filepath, processed_at)
                            VALUES (:pn, :sk, :fp, :now)
                        """),
                        {"pn": pipeline_name, "sk": source_key, "fp": filepath, "now": now},
                    )
                elif dialect in ("postgresql", "postgres"):
                    conn.execute(
                        text("""
                            INSERT INTO processed_files
                              (pipeline_name, source_key, filepath, processed_at)
                            VALUES (:pn, :sk, :fp, :now)
                            ON CONFLICT DO NOTHING
                        """),
                        {"pn": pipeline_name, "sk": source_key, "fp": filepath, "now": now},
                    )
                elif dialect == "mysql":
                    conn.execute(
                        text("""
                            INSERT IGNORE INTO processed_files
                              (pipeline_name, source_key, filepath, processed_at)
                            VALUES (:pn, :sk, :fp, :now)
                        """),
                        {"pn": pipeline_name, "sk": source_key, "fp": filepath, "now": now},
                    )
                else:
                    conn.execute(
                        text("""
                            INSERT INTO processed_files
                              (pipeline_name, source_key, filepath, processed_at)
                            VALUES (:pn, :sk, :fp, :now)
                        """),
                        {"pn": pipeline_name, "sk": source_key, "fp": filepath, "now": now},
                    )
        except Exception as exc:
            logger.warning(
                "Failed to mark file as processed",
                extra={"pipeline": pipeline_name, "filepath": filepath, "error": str(exc)},
            )

    # ── User passwords ─────────────────────────────────────────────────────

    def get_password_hash(self, username: str) -> Optional[str]:
        """Return the stored password hash for *username*, or None if not overridden."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT password_hash FROM user_passwords WHERE username = :u"),
                {"u": username},
            ).fetchone()
        return row[0] if row else None

    def set_password_hash(self, username: str, password_hash: str) -> None:
        """Upsert a password hash for *username*."""
        now = datetime.now(timezone.utc).isoformat()
        dialect = self._engine.dialect.name
        with self._engine.begin() as conn:
            if dialect == "postgresql":
                conn.execute(text("""
                    INSERT INTO user_passwords (username, password_hash, updated_at)
                    VALUES (:u, :h, :now)
                    ON CONFLICT (username) DO UPDATE SET password_hash = :h, updated_at = :now
                """), {"u": username, "h": password_hash, "now": now})
            elif dialect == "mysql":
                conn.execute(text("""
                    INSERT INTO user_passwords (username, password_hash, updated_at)
                    VALUES (:u, :h, :now)
                    ON DUPLICATE KEY UPDATE password_hash = :h, updated_at = :now
                """), {"u": username, "h": password_hash, "now": now})
            else:  # sqlite
                conn.execute(text("""
                    INSERT INTO user_passwords (username, password_hash, updated_at)
                    VALUES (:u, :h, :now)
                    ON CONFLICT (username) DO UPDATE SET password_hash = :h, updated_at = :now
                """), {"u": username, "h": password_hash, "now": now})

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def close(self) -> None:
        self._engine.dispose()
