"""SQLite persistence layer for pipeline versions and run history."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tram.core.context import RunResult, RunStatus


def _db_path() -> Path:
    raw = os.environ.get("TRAM_DB_PATH", "~/.tram/tram.db")
    return Path(raw).expanduser()


_DDL = """\
CREATE TABLE IF NOT EXISTS pipeline_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    yaml_content TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS run_history (
    run_id          TEXT PRIMARY KEY,
    pipeline_name   TEXT NOT NULL,
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    records_in      INTEGER NOT NULL DEFAULT 0,
    records_out     INTEGER NOT NULL DEFAULT 0,
    records_skipped INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS alert_state (
    pipeline_name   TEXT NOT NULL,
    rule_name       TEXT NOT NULL,
    last_alerted_at TEXT NOT NULL,
    PRIMARY KEY (pipeline_name, rule_name)
);

CREATE INDEX IF NOT EXISTS idx_pv_name ON pipeline_versions(name);
CREATE INDEX IF NOT EXISTS idx_rh_pipeline ON run_history(pipeline_name);
CREATE INDEX IF NOT EXISTS idx_rh_status ON run_history(status);
"""


class TramDB:
    """Thin wrapper around a SQLite connection for TRAM persistence."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

    # ── Run history ────────────────────────────────────────────────────────

    def save_run(self, result: RunResult) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO run_history
              (run_id, pipeline_name, status, started_at, finished_at,
               records_in, records_out, records_skipped, error)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                result.run_id,
                result.pipeline_name,
                result.status.value,
                result.started_at.isoformat(),
                result.finished_at.isoformat(),
                result.records_in,
                result.records_out,
                result.records_skipped,
                result.error,
            ),
        )
        self._conn.commit()

    def get_runs(
        self,
        pipeline_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[RunResult]:
        sql = "SELECT * FROM run_history WHERE 1=1"
        params: list = []
        if pipeline_name:
            sql += " AND pipeline_name = ?"
            params.append(pipeline_name)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY finished_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_run_result(r) for r in rows]

    def _row_to_run_result(self, row: sqlite3.Row) -> RunResult:
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
        )

    # ── Pipeline versions ──────────────────────────────────────────────────

    def save_pipeline_version(self, name: str, yaml_content: str) -> int:
        """Save a new version; deactivate previous versions. Returns new version number."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(version), 0) FROM pipeline_versions WHERE name = ?",
            (name,),
        ).fetchone()
        next_version = row[0] + 1

        # Deactivate old
        self._conn.execute(
            "UPDATE pipeline_versions SET is_active = 0 WHERE name = ?", (name,)
        )
        self._conn.execute(
            """
            INSERT INTO pipeline_versions (name, version, yaml_content, created_at, is_active)
            VALUES (?,?,?,?,1)
            """,
            (name, next_version, yaml_content, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return next_version

    def get_pipeline_versions(self, name: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, name, version, created_at, is_active FROM pipeline_versions "
            "WHERE name = ? ORDER BY version DESC",
            (name,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pipeline_version(self, name: str, version: int) -> str:
        row = self._conn.execute(
            "SELECT yaml_content FROM pipeline_versions WHERE name = ? AND version = ?",
            (name, version),
        ).fetchone()
        if row is None:
            raise KeyError(f"Pipeline '{name}' version {version} not found")
        return row["yaml_content"]

    def get_latest_version(self, name: str) -> str:
        row = self._conn.execute(
            "SELECT yaml_content FROM pipeline_versions WHERE name = ? AND is_active = 1",
            (name,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No active version found for pipeline '{name}'")
        return row["yaml_content"]

    # ── Alert cooldown state ───────────────────────────────────────────────

    def get_alert_cooldown(self, pipeline_name: str, rule_name: str) -> Optional[datetime]:
        """Return the last-alerted datetime for a rule, or None if never alerted."""
        row = self._conn.execute(
            "SELECT last_alerted_at FROM alert_state WHERE pipeline_name = ? AND rule_name = ?",
            (pipeline_name, rule_name),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["last_alerted_at"])

    def set_alert_cooldown(self, pipeline_name: str, rule_name: str, dt: datetime) -> None:
        """Upsert the last-alerted timestamp for a rule."""
        self._conn.execute(
            """
            INSERT INTO alert_state (pipeline_name, rule_name, last_alerted_at)
            VALUES (?, ?, ?)
            ON CONFLICT(pipeline_name, rule_name)
            DO UPDATE SET last_alerted_at = excluded.last_alerted_at
            """,
            (pipeline_name, rule_name, dt.isoformat()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
