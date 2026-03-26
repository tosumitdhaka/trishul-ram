# Feature 2 — Live Metrics on Dashboard

## Goal

Replace static "last run" counters on the dashboard with auto-refreshing throughput,
error rate, and record flow metrics. No Prometheus required — all data comes from
the existing `run_history` DB table.

## Metrics

| Metric | Aggregation window | Refresh |
|--------|--------------------|---------|
| Pipelines total | — | 10 s |
| Pipelines running / scheduled | live `manager.list_all()` | 10 s |
| Runs today | `run_history` count, `finished_at >= today` | 10 s |
| Runs last hour | `run_history` count | 10 s |
| Records in / out (last 15 min) | `run_history` SUM | 10 s |
| Errors last 15 min | `run_history` WHERE status='error' | 10 s |
| Avg run duration last hour | `run_history` AVG(duration_s) | 30 s |
| Per-pipeline: runs, records in, errors (last hour) | same table | 30 s |

## New API Endpoint

```
GET /api/stats
```

Response:
```json
{
  "pipelines_total": 5,
  "pipelines_running": 1,
  "pipelines_scheduled": 3,
  "runs_today": 147,
  "runs_last_hour": 12,
  "records_in_last_15m": 48200,
  "records_out_last_15m": 48198,
  "errors_last_15m": 2,
  "avg_duration_last_hour_s": 1.4,
  "per_pipeline": [
    {
      "name": "sample-health",
      "runs_last_hour": 12,
      "records_in_last_hour": 48200,
      "records_out_last_hour": 48198,
      "errors_last_hour": 0
    }
  ]
}
```

## Backend

New file: `tram/api/routers/stats.py`

All queries against existing `run_history` table — no schema changes.

```python
@router.get("/api/stats")
async def get_stats(request: Request) -> dict:
    manager = request.app.state.manager
    db = getattr(request.app.state, "db", None)
    now = datetime.now(timezone.utc)
    ...
```

In-memory fallback when no DB: scan `manager.list_all()` run_history deques
(already stored as `deque[RunResult]` per pipeline, max 500 entries).

Register in `tram/api/app.py` alongside existing routers.

## UI

`tram-ui/src/pages/dashboard.js` changes:
- Poll `GET /api/stats` every 10 s via `setInterval`
- Update summary cards: Total / Running / Scheduled / Errors (last 15m)
- Add per-pipeline mini-table: name | runs/hr | records/hr | errors
- Add sparkline charts (records/min, last 12 data points) using Canvas API directly:
  no chart library dependency, ~40 lines per sparkline

Live indicator:
- Small pulsing green dot in dashboard header while polling is active
- Turns grey if last fetch failed (with "Last updated Xs ago" label)

## Files Changed

| File | Change |
|------|--------|
| `tram/api/routers/stats.py` | New |
| `tram/api/app.py` | Register stats router |
| `tram-ui/src/pages/dashboard.js` | Replace static counters with polling |
| `tram-ui/src/pages/dashboard.html` | Add sparkline canvas elements |
