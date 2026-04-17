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

        # v1.2.0 data migration: copy paused=1 → stopped=1 for existing rows
        try:
            conn.execute(text(
                "UPDATE registered_pipelines SET stopped = 1 WHERE paused = 1 AND stopped = 0"
            ))
        except Exception:
            pass  # paused column may not exist on fresh DBs


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
                        "node_id": self._node_id,
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
                    "created_at": datetime.now(UTC).isoformat(),
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

    def get_password_hash(self, username: str) -> str | None:
        """Return the stored password hash for *username*, or None if not overridden."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT password_hash FROM user_passwords WHERE username = :u"),
                {"u": username},
            ).fetchone()
        return row[0] if row else None

    def set_password_hash(self, username: str, password_hash: str) -> None:
        """Upsert a password hash for *username*."""
        now = datetime.now(UTC).isoformat()
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

    # ── Registered pipelines (v1.1.2) ─────────────────────────────────────

    def pause_pipeline(self, name: str) -> None:
        """Mark a pipeline as paused (legacy — maps to stop_pipeline in v1.2.0)."""
        self.stop_pipeline(name)

    def resume_pipeline(self, name: str) -> None:
        """Clear the paused flag (legacy — maps to start_pipeline in v1.2.0)."""
        self.start_pipeline_flag(name)

    def is_pipeline_paused(self, name: str) -> bool:
        """Return True if the pipeline is stopped (legacy alias for is_pipeline_stopped)."""
        return self.is_pipeline_stopped(name)

    def get_paused_pipeline_names(self) -> list[str]:
        """Return names of stopped pipelines (legacy alias for get_stopped_pipeline_names)."""
        return self.get_stopped_pipeline_names()

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
        dialect = self._engine.dialect.name
        with self._engine.begin() as conn:
            if dialect == "postgresql":
                conn.execute(text("""
                    INSERT INTO registered_pipelines (name, yaml_text, created_at, updated_at, deleted, source)
                    VALUES (:name, :yaml, :now, :now, 0, :src)
                    ON CONFLICT (name) DO UPDATE
                      SET yaml_text = EXCLUDED.yaml_text,
                          updated_at = EXCLUDED.updated_at,
                          deleted = 0,
                          source = EXCLUDED.source
                """), {"name": name, "yaml": yaml_text, "now": now, "src": source})
            elif dialect == "mysql":
                conn.execute(text("""
                    INSERT INTO registered_pipelines (name, yaml_text, created_at, updated_at, deleted, source)
                    VALUES (:name, :yaml, :now, :now, 0, :src)
                    ON DUPLICATE KEY UPDATE
                      yaml_text = VALUES(yaml_text),
                      updated_at = VALUES(updated_at),
                      deleted = 0,
                      source = VALUES(source)
                """), {"name": name, "yaml": yaml_text, "now": now, "src": source})
            else:  # sqlite
                conn.execute(text("""
                    INSERT INTO registered_pipelines (name, yaml_text, created_at, updated_at, deleted, source)
                    VALUES (:name, :yaml, :now, :now, 0, :src)
                    ON CONFLICT (name) DO UPDATE
                      SET yaml_text = excluded.yaml_text,
                          updated_at = excluded.updated_at,
                          deleted = 0,
                          source = excluded.source
                """), {"name": name, "yaml": yaml_text, "now": now, "src": source})

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
        dialect = self._engine.dialect.name
        with self._engine.begin() as conn:
            if dialect == "mysql":
                conn.execute(text("""
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (:k, :v, :now)
                    ON DUPLICATE KEY UPDATE value = :v, updated_at = :now
                """), {"k": key, "v": value, "now": now})
            else:  # sqlite + postgresql both support this syntax
                conn.execute(text("""
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (:k, :v, :now)
                    ON CONFLICT (key) DO UPDATE SET value = :v, updated_at = :now
                """), {"k": key, "v": value, "now": now})

    def delete_setting(self, key: str) -> None:
        """Remove a setting, reverting to env-var / default."""
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM settings WHERE key = :k"), {"k": key})

    # ── Broadcast placements (v1.3.0) ────────────────────────────────────

    def save_broadcast_placement(
        self,
        placement_group_id: str,
        pipeline_name: str,
        slots: list[dict],
        target_count: str,
        status: str,
        started_at: datetime | None = None,
    ) -> None:
        now = (started_at or datetime.now(UTC)).isoformat()
        payload = {
            "placement_group_id": placement_group_id,
            "pipeline_name": pipeline_name,
            "slots_json": json.dumps(slots),
            "target_count": target_count,
            "started_at": now,
            "status": status,
            "stopped_at": None,
        }
        dialect = self._engine.dialect.name
        with self._engine.begin() as conn:
            if dialect == "postgresql":
                conn.execute(text("""
                    INSERT INTO broadcast_placements
                      (placement_group_id, pipeline_name, slots_json, target_count, started_at, status, stopped_at)
                    VALUES
                      (:placement_group_id, :pipeline_name, :slots_json, :target_count, :started_at, :status, :stopped_at)
                    ON CONFLICT (placement_group_id) DO UPDATE SET
                      pipeline_name = EXCLUDED.pipeline_name,
                      slots_json = EXCLUDED.slots_json,
                      target_count = EXCLUDED.target_count,
                      started_at = EXCLUDED.started_at,
                      status = EXCLUDED.status,
                      stopped_at = EXCLUDED.stopped_at
                """), payload)
            elif dialect == "mysql":
                conn.execute(text("""
                    INSERT INTO broadcast_placements
                      (placement_group_id, pipeline_name, slots_json, target_count, started_at, status, stopped_at)
                    VALUES
                      (:placement_group_id, :pipeline_name, :slots_json, :target_count, :started_at, :status, :stopped_at)
                    ON DUPLICATE KEY UPDATE
                      pipeline_name = VALUES(pipeline_name),
                      slots_json = VALUES(slots_json),
                      target_count = VALUES(target_count),
                      started_at = VALUES(started_at),
                      status = VALUES(status),
                      stopped_at = VALUES(stopped_at)
                """), payload)
            else:
                conn.execute(text("""
                    INSERT INTO broadcast_placements
                      (placement_group_id, pipeline_name, slots_json, target_count, started_at, status, stopped_at)
                    VALUES
                      (:placement_group_id, :pipeline_name, :slots_json, :target_count, :started_at, :status, :stopped_at)
                    ON CONFLICT (placement_group_id) DO UPDATE SET
                      pipeline_name = excluded.pipeline_name,
                      slots_json = excluded.slots_json,
                      target_count = excluded.target_count,
                      started_at = excluded.started_at,
                      status = excluded.status,
                      stopped_at = excluded.stopped_at
                """), payload)

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

    def update_slot_run_id(
        self,
        placement_group_id: str,
        worker_index: int,
        current_run_id: str,
        status: str = "running",
        restart_count: int | None = None,
    ) -> None:
        placements = self.get_active_broadcast_placements()
        placement = next((p for p in placements if p["placement_group_id"] == placement_group_id), None)
        if placement is None:
            return
        for slot in placement["slots"]:
            if int(slot.get("worker_index", -1)) != worker_index:
                continue
            slot["current_run_id"] = current_run_id
            slot["status"] = status
            if restart_count is not None:
                slot["restart_count"] = restart_count
            break
        self.update_broadcast_placement_status(
            placement_group_id,
            placement["status"],
            slots=placement["slots"],
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def close(self) -> None:
        self._engine.dispose()
