"""Live statistics endpoint — aggregates from run_history for dashboard metrics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Query, Request

router = APIRouter()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


_PERIOD_MINUTES = {
    "1h": 60,
    "6h": 360,
    "24h": 1440,
}

_GRANULARITY_MINUTES = {
    "5m": 5,
    "15m": 15,
    "1h": 60,
}


@router.get("/api/stats")
async def get_stats(
    request: Request,
    period: Literal["1h", "6h", "24h"] = Query("1h", description="Chart lookback window"),
    granularity: Literal["5m", "15m", "1h"] = Query("5m", description="Chart bucket size"),
) -> dict:
    """Return aggregated pipeline and run statistics for the dashboard."""
    manager = request.app.state.manager
    db = getattr(request.app.state, "db", None)

    now = _now()
    since_15m  = now - timedelta(minutes=15)
    since_1h   = now - timedelta(hours=1)
    since_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    chart_window_minutes = _PERIOD_MINUTES[period]
    bucket_minutes = _GRANULARITY_MINUTES[granularity]
    bucket_count = max(1, chart_window_minutes // bucket_minutes)
    chart_since = now - timedelta(minutes=chart_window_minutes)

    # ── Pipeline status counts from in-memory manager ──────────────────────
    all_states = manager.list_all()
    pipelines_total     = len(all_states)
    pipelines_running   = sum(1 for s in all_states if s.status == "running")
    pipelines_scheduled = sum(1 for s in all_states if s.status == "scheduled")
    pipelines_error     = sum(1 for s in all_states if s.status == "error")

    # ── Run aggregations ────────────────────────────────────────────────────
    if db is not None:
        runs_today  = _db_count(db, since_today)
        runs_1h     = _db_count(db, since_1h)
        stats_15m   = _db_agg(db, since_15m)
        stats_1h    = _db_agg(db, since_1h)
        per_pipeline = _db_per_pipeline(db, since_1h, all_states)
        sparkline   = _db_sparkline(db, chart_since, bucket_count=bucket_count, minutes=bucket_minutes)
    else:
        # In-memory fallback: scan deque[RunResult] in each PipelineState
        all_runs = []
        for state in all_states:
            all_runs.extend(state.run_history)
        runs_today  = sum(1 for r in all_runs if _after(r.finished_at, since_today))
        runs_1h     = sum(1 for r in all_runs if _after(r.finished_at, since_1h))
        stats_15m   = _mem_agg(all_runs, since_15m)
        stats_1h    = _mem_agg(all_runs, since_1h)
        per_pipeline = _mem_per_pipeline(all_states, since_1h)
        sparkline   = _mem_sparkline(all_runs, chart_since, bucket_count=bucket_count, minutes=bucket_minutes)

    chart_total = sum(point["bytes_processed"] for point in sparkline)
    sparkline_legacy = [
        {"bucket": point["bucket"], "records_out": point["records_out"]}
        for point in sparkline
    ]

    return {
        "pipelines_total":      pipelines_total,
        "pipelines_running":    pipelines_running,
        "pipelines_scheduled":  pipelines_scheduled,
        "pipelines_error":      pipelines_error,
        "runs_today":           runs_today,
        "runs_last_hour":       runs_1h,
        "records_in_last_15m":  stats_15m["records_in"],
        "records_out_last_15m": stats_15m["records_out"],
        "bytes_in_last_15m":    stats_15m["bytes_in"],
        "bytes_out_last_15m":   stats_15m["bytes_out"],
        "errors_last_15m":      stats_15m["errors"],
        "avg_duration_last_hour_s": stats_1h["avg_duration_s"],
        "per_pipeline":         per_pipeline,
        "sparkline":            sparkline_legacy,
        "window": {
            "period": period,
            "granularity": granularity,
            "bucket_count": bucket_count,
        },
        "chart": {
            "metric": "bytes_processed",
            "period": period,
            "granularity": granularity,
            "bucket_count": bucket_count,
            "total": chart_total,
            "points": sparkline,
        },
    }


# ── DB helpers ──────────────────────────────────────────────────────────────

def _db_count(db, since: datetime) -> int:
    from sqlalchemy import text
    with db._engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM run_history WHERE finished_at >= :since"),
            {"since": since.isoformat()},
        ).fetchone()
    return row[0] if row else 0


def _db_agg(db, since: datetime) -> dict:
    from sqlalchemy import text
    dialect = db._engine.dialect.name
    if dialect == "postgresql":
        dur_expr = "EXTRACT(EPOCH FROM (finished_at::timestamptz - started_at::timestamptz))"
    elif dialect in ("mysql", "mariadb"):
        dur_expr = "TIMESTAMPDIFF(SECOND, started_at, finished_at)"
    else:  # sqlite
        dur_expr = "(julianday(finished_at) - julianday(started_at)) * 86400.0"
    with db._engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT
                COALESCE(SUM(records_in), 0),
                COALESCE(SUM(records_out), 0),
                COALESCE(SUM(bytes_in), 0),
                COALESCE(SUM(bytes_out), 0),
                COALESCE(SUM(CASE WHEN status IN ('error','failed') THEN 1 ELSE 0 END), 0),
                AVG(CASE
                    WHEN finished_at IS NOT NULL AND started_at IS NOT NULL
                    THEN {dur_expr} ELSE NULL END)
            FROM run_history WHERE finished_at >= :since
        """), {"since": since.isoformat()}).fetchone()
    return {
        "records_in":    int(row[0] or 0) if row else 0,
        "records_out":   int(row[1] or 0) if row else 0,
        "bytes_in":      int(row[2] or 0) if row else 0,
        "bytes_out":     int(row[3] or 0) if row else 0,
        "errors":        int(row[4] or 0) if row else 0,
        "avg_duration_s": round(row[5], 2) if row and row[5] is not None else None,
    }


def _db_per_pipeline(db, since: datetime, all_states) -> list[dict]:
    from sqlalchemy import text
    with db._engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT pipeline_name,
                   COUNT(*) AS runs,
                   COALESCE(SUM(records_in), 0),
                   COALESCE(SUM(records_out), 0),
                   SUM(CASE WHEN status IN ('error','failed') THEN 1 ELSE 0 END)
            FROM run_history
            WHERE finished_at >= :since
            GROUP BY pipeline_name
            ORDER BY runs DESC
        """), {"since": since.isoformat()}).fetchall()

    by_name = {r[0]: r for r in rows}
    result = []
    for state in all_states:
        name = state.config.name
        r = by_name.get(name)
        result.append({
            "name":           name,
            "status":         state.status,
            "runs_last_hour": int(r[1]) if r else 0,
            "records_in":     int(r[2]) if r else 0,
            "records_out":    int(r[3]) if r else 0,
            "errors":         int(r[4]) if r else 0,
        })
    return result


def _db_sparkline(db, since: datetime, *, bucket_count: int, minutes: int) -> list[dict]:
    """Fixed-width buckets for the requested chart window with records and bytes load."""
    from sqlalchemy import text
    with db._engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT finished_at, records_out, COALESCE(bytes_in, 0), COALESCE(bytes_out, 0)
            FROM run_history
            WHERE finished_at >= :since
            ORDER BY finished_at
        """), {"since": since.isoformat()}).fetchall()
    return _bucket_sparkline(rows, since, bucket_count=bucket_count, minutes=minutes)


# ── In-memory fallbacks ──────────────────────────────────────────────────────

def _after(dt_val, threshold: datetime) -> bool:
    if dt_val is None:
        return False
    if isinstance(dt_val, str):
        try:
            dt_val = datetime.fromisoformat(dt_val)
        except ValueError:
            return False
    if dt_val.tzinfo is None:
        dt_val = dt_val.replace(tzinfo=UTC)
    return dt_val >= threshold


def _mem_agg(runs, since: datetime) -> dict:
    filtered = [r for r in runs if _after(r.finished_at, since)]
    if not filtered:
        return {
            "records_in": 0,
            "records_out": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "errors": 0,
            "avg_duration_s": None,
        }
    durations = []
    for r in filtered:
        if r.started_at and r.finished_at:
            try:
                s = (datetime.fromisoformat(str(r.finished_at)) -
                     datetime.fromisoformat(str(r.started_at))).total_seconds()
                durations.append(s)
            except Exception:
                pass
    return {
        "records_in":    sum(int(getattr(r, "records_in", 0) or 0) for r in filtered),
        "records_out":   sum(int(getattr(r, "records_out", 0) or 0) for r in filtered),
        "bytes_in":      sum(int(getattr(r, "bytes_in", 0) or 0) for r in filtered),
        "bytes_out":     sum(int(getattr(r, "bytes_out", 0) or 0) for r in filtered),
        "errors":        sum(1 for r in filtered if r.status.value in ("error", "failed")),
        "avg_duration_s": round(sum(durations) / len(durations), 2) if durations else None,
    }


def _mem_per_pipeline(all_states, since: datetime) -> list[dict]:
    result = []
    for state in all_states:
        runs = [r for r in state.run_history if _after(r.finished_at, since)]
        result.append({
            "name":           state.config.name,
            "status":         state.status,
            "runs_last_hour": len(runs),
            "records_in":     sum(r.records_in  for r in runs),
            "records_out":    sum(r.records_out for r in runs),
            "errors":         sum(1 for r in runs if r.status.value in ("error", "failed")),
        })
    return result


def _mem_sparkline(runs, since: datetime, *, bucket_count: int, minutes: int) -> list[dict]:
    rows = [
        (
            str(r.finished_at),
            getattr(r, "records_out", 0),
            getattr(r, "bytes_in", 0),
            getattr(r, "bytes_out", 0),
        )
        for r in runs if _after(r.finished_at, since)
    ]
    return _bucket_sparkline(rows, since, bucket_count=bucket_count, minutes=minutes)


def _bucket_sparkline(rows, since: datetime, *, bucket_count: int, minutes: int) -> list[dict]:
    """Bin sparkline rows into fixed-width time buckets.

    Supports legacy ``(finished_at, records_out)`` rows and extended rows that
    also include bytes-processed data.
    """
    bucket_secs = minutes * 60
    counts = [0] * bucket_count
    bytes_counts = [0] * bucket_count
    bucket_starts = [since + timedelta(seconds=bucket_secs * i) for i in range(bucket_count)]
    for row in rows:
        try:
            values = tuple(row)
            ts_str = values[0]
            rec_out = values[1] if len(values) > 1 else 0
            bytes_processed = sum(int(value or 0) for value in values[2:])
            ts = datetime.fromisoformat(str(ts_str))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            offset = (ts - since).total_seconds()
            idx = int(offset // bucket_secs)
            if 0 <= idx < bucket_count:
                counts[idx] += (rec_out or 0)
                bytes_counts[idx] += bytes_processed
        except Exception:
            pass
    points = []
    for i in range(bucket_count):
        start = bucket_starts[i]
        end = start + timedelta(seconds=bucket_secs)
        points.append({
            "bucket": i,
            "bucket_start": _iso(start),
            "bucket_end": _iso(end),
            "records_out": counts[i],
            "bytes_processed": bytes_counts[i],
        })
    return points
