# Queued Manual Runs — Design (E.2 / GH #21)

**Status:** proposed (requires approval before implementation)
**Branch:** `wave-a-stopgaps` · **Plan ref:** `docs/plans/issue-implementation-plan.md` Wave E, item E.2
**Sources:** RCA `docs/reviews/issue-rca.md` #21 · plan v2 (B.5/A.6 prerequisite notes, parked-in B8/B9) · code as of this branch
**Prerequisites:** **B.5** (controller lifecycle RLock) and **A.6** (`DispatchOutcome` label split + health hysteresis) — both landed on this branch; this design builds directly on them.

---

## 0. Problem statement

A manual run (`POST /api/pipelines/{name}/run` → `controller.trigger_run`, `controller.py:436-450` → `_run_batch` manager+worker branch, `controller.py:634-703`) dispatched when no healthy worker exists fails immediately and permanently: the no-capacity branch (`controller.py:648-675`) synthesizes a `FAILED` `RunResult` ("No healthy workers available for dispatch"), persists it to run history, flips the pipeline to `error`, and deactivates its K8s Service. The user's intent — "run this once, when you can" — is discarded; in a fleet where workers restart (node drains, OOM recovery, upgrades), every manual trigger during the outage window is a dead run the user must re-issue by hand.

RCA #21 confirms the current failure path is state-consistent (no phantom leases, no placements) and that the queue is a well-supported extension — provided it does not inherit the four traced bugs (§2.6). The RCA also fixes the two design hazards: in-memory queuing (manager restarts drop user requests → must be DB-backed) and queueing scheduled runs (they retry naturally on interval; queuing them floods on outages).

Line-number note: the plan's "enqueue at `controller.py:478-497`" refers to pre-B.5/A.6 line numbers; on this branch the branch is `controller.py:648-675`.

## 1. Goals and non-goals

**Goals**

- A manual run triggered with no healthy workers is durably queued (survives manager restarts), dispatched automatically when capacity returns, and expires to a clearly-labeled `FAILED` run if capacity never returns within a configurable TTL.
- Enqueue happens **only** on genuine no-capacity (the debounced health signal), never on a dispatch failure; a flapping worker cannot cause queue/run thrash.
- Exactly one queued manual run per pipeline; a second trigger while queued is idempotent.
- The `queued → dispatching` claim is single-claim under the B.5 controller RLock; drain is authoritative inside `BatchReconciler`; worker-restored health only nudges.
- `db.py` gains one reusable `_upsert` helper; the six existing dialect-branching upserts are refactored onto it (no seventh copy).
- API: manual-run responses gain `status: "queued"` / HTTP 202; run read paths render queued runs.
- Feature-flagged; flag off is today's behavior verbatim.

**Non-goals**

- Scheduled/interval/cron runs — never queued; today's fail-fast per tick is retained (they self-retry on interval).
- Stream pipelines — cannot be triggered manually at all (`controller.py:444-445`); no interaction with the D.2 placement machinery.
- Standalone mode (`_worker_pool is None`) — local execution always has capacity; queue is a manager+worker concept.
- Queue depth > 1 per pipeline (backlog semantics) — explicitly deferred; dedupe makes depth ≤ pipeline count, which bounds everything (§13).
- Queueing across manager HA / a second manager process — single-manager architecture, same assumption as broadcast placements.

## 2. Core decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | DB-backed `queued_runs` table, broadcast-placement precedent | RCA: in-memory drops user requests on manager restart. `broadcast_placements` (`db.py:174-186`) is the established pattern: TEXT columns, ISO timestamps, `CREATE TABLE IF NOT EXISTS`, per-pipeline index. |
| 2 | Two enqueue sites, one funnel: **synchronous check in `trigger_run`** (authoritative for the 202 response) + **fallback in `_run_batch`'s no-capacity branch** (the RCA's enqueue point — capacity vanished between trigger and dispatch) | The API contract requires the POST to *know* it queued. `healthy_workers()` (`worker_pool.py:330-333`) is a pure dict read over the debounced state — cheap under the lock, no probe I/O. The async fallback covers the trigger→dispatch race window. Both call `_enqueue_manual_run`, which dedupes. |
| 3 | **Return-existing** dedupe, not replace | (a) Replacing would reset the TTL clock — repeated triggers during a long outage would keep a request alive indefinitely, defeating the TTL's starvation bound. (b) The queued run_id is already visible to the user (202 response); churning it breaks polling. (c) Return-existing makes "trigger while queued" idempotent, matching the `already_running` fast path of `trigger_run`. |
| 4 | Queued runs **survive manager restart**; the TTL clock keeps running (`expires_at` is absolute) | The queue exists precisely because manager outages and worker outages overlap — a queue that drains on manager restart would drop exactly the requests it exists to protect. An absolute `expires_at` means a request from before a long manager downtime expires (truthfully) at the first drain pass rather than silently running stale YAML forever. |
| 5 | Dispatch uses the **`yaml_snapshot`** column; `update()` on a pipeline with a queued run **refreshes the snapshot** | Config currency is the stronger invariant (D.2 §6 parity): a drifted dispatch produces wrong billing/KPI records with no signal. Refresh-on-update keeps the auditable snapshot column as the dispatch source and makes update+drain converge on current config. |
| 6 | Drain `dispatch_failed` → **revert to `queued`**, not FAILED | A worker that is healthy-per-debounce but fails `/agent/run` is a transient or a half-broken worker; failing the user's request forces a manual re-trigger (the exact UX #21 exists to remove). Revert + retry-next-pass is bounded: ≤ 1 dispatch attempt per 10s reconciler pass, hard-bounded by TTL. No run-history churn (nothing recorded on revert). |
| 7 | No `run_history` row at enqueue; queued runs surface via an **API read-merge**; a `FAILED` row is written only at TTL expiry | Keeps run_history append-only and completion-shaped (no started-but-unfinished rows, no resurrection race with `on_worker_run_complete`'s duplicate guard). Run-id continuity: the queued `run_id` is used for dispatch, so the worker's eventual run-complete callback records the *same* run_id the user saw in the 202. |
| 8 | New pipeline status value **`"queued"`** | Today the no-capacity path lands on `error` — misleading (nothing failed). `queued` keeps the pipelines list truthful and gives the UI one badge to draw. Claim phase of `_run_batch` treats `queued` like `running` (skip) — preserves the one-active-run-per-pipeline invariant (§6.3). |
| 9 | Flag `TRAM_QUEUE_MANUAL_RUNS`, default ON, fail-open with a loud warning on unrecognized values | Mirrors the `TRAM_STREAM_SINGLE_PLACEMENT` pattern (`controller.py:87-100`). Default ON because the behavior change is strictly user-visible improvement (202 + eventual run instead of an immediate dead run); rollback = set `0`. |

### 2.6 The four RCA bugs — how this design avoids them

1. **Error-label conflation** — solved by A.6's `DispatchOutcome` (`worker_pool.py:50-60`, `dispatch_with_result` `:762-797`). The synchronous enqueue site keys off `healthy_workers() == []` (the same debounced state `dispatch_with_result` uses); the fallback site keys off `outcome == DISPATCH_NO_CAPACITY` only. `DISPATCH_FAILED` never enqueues — it keeps today's fail-fast with its truthful error label.
2. **Single-probe health flap** — solved by A.6's hysteresis (`health_failures_to_down=2`, `worker_pool.py:78-87`, `:263-280`). Both enqueue sites and the drain pre-check read the debounced state; one failed probe cannot produce a queued run.
3. **Trigger TOCTOU / duplicate dispatch** — solved by B.5's RLock (`controller.py:114`). Every queue transition is under it (§6.4); the DB conditional UPDATE rowcount is the second fence.
4. **Optimistic health init** — bounded by `WorkerPool.start()`'s synchronous first poll; B.6's boot hysteresis note (first-probe failures mark down) covers the boot-time drain. No additional work here.

## 3. Database schema and the `_upsert` helper

### 3.1 `queued_runs` table

Created in `_create_tables` (`db.py:71`), broadcast-placement conventions (TEXT columns, ISO-8601 UTC strings, idempotent DDL):

```sql
CREATE TABLE IF NOT EXISTS queued_runs (
    run_id        TEXT PRIMARY KEY NOT NULL,
    pipeline_name TEXT NOT NULL,
    yaml_snapshot TEXT NOT NULL,
    status        TEXT NOT NULL,          -- queued | dispatching | dispatched | expired
    requested_at  TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    dispatched_at TEXT
)
CREATE INDEX IF NOT EXISTS idx_qr_pipeline ON queued_runs(pipeline_name)
CREATE INDEX IF NOT EXISTS idx_qr_status   ON queued_runs(status)
```

No migration of existing tables; no column additions (nothing like `_add_column_if_missing` needed). Like `broadcast_placements`, terminal rows are kept for audit (`expired`, `dispatched`); the read paths filter on status.

### 3.2 The reusable `_upsert` (plan: "don't write a 7th dialect-branching upsert")

`db.py` currently has six hand-rolled dialect branches: `set_alert_cooldown` (`:516-563`), `mark_processed` (`:579-626`), `set_password_hash` (`:647-669`), `save_pipeline` (`:708-747`), `set_setting` (`:794-810`), `save_broadcast_placement` (`:819-881`). Add **one** private helper and refactor all six onto it:

```python
def _upsert(self, table: str, values: dict[str, object], key_columns: tuple[str, ...]) -> None:
    """Insert-or-update a single row, keyed on *key_columns*, across dialects.

    sqlite / postgresql : INSERT ... ON CONFLICT (keys) DO UPDATE SET col = excluded.col
    mysql               : INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col)
    other               : DELETE by key + INSERT, in one transaction (generic fallback)

    ``values`` maps column -> value for ALL columns (keys included). Non-key
    columns are updated on conflict. Raises on failure (does not swallow —
    contrast parked B8).
    """
```

Behavior-preservation notes for the refactor:

- `set_alert_cooldown`'s generic fallback is delete+insert — the helper's fallback reproduces it.
- `save_broadcast_placement` explicitly writes `stopped_at=None` on conflict — the helper's DO UPDATE includes all non-key columns, matching.
- `mark_processed` is insert-only-else-nothing (`INSERT OR IGNORE` / `ON CONFLICT DO NOTHING`) — **not** an upsert; give it an `update_columns: tuple[str, ...] | None = None` mode where an empty update list degrades to DO NOTHING, or leave `mark_processed` as-is with a comment. **Recommendation:** add the `update_columns` param (defaults to all non-keys; pass `()` for insert-if-absent). One helper, two modes, still one implementation.

**Only the initial insert** of a queued run uses `_upsert` (`save_queued_run`). Every subsequent transition is a single-row conditional `UPDATE ... WHERE` returning rowcount — the `deactivate_other_placements` pattern (`db.py:928-950`), which is dialect-free and provides the single-claim fence (§6.4).

### 3.3 TramDB additions (signatures)

```python
def save_queued_run(self, run_id: str, pipeline_name: str, yaml_snapshot: str,
                    requested_at: datetime, expires_at: datetime) -> None          # _upsert
def get_active_queued_runs(self) -> list[dict]                                      # status='queued', ORDER BY requested_at
def get_queued_run_view(self) -> list[dict]                                         # status IN ('queued','dispatching') — API merge
def get_active_queued_run_for_pipeline(self, pipeline_name: str) -> dict | None
def claim_queued_run_row(self, run_id: str) -> int                                   # UPDATE → status='dispatching' WHERE run_id=:r AND status='queued'
def mark_queued_run_dispatched(self, run_id: str, dispatched_at: datetime) -> int   # WHERE status='dispatching'
def revert_queued_run_row(self, run_id: str) -> int                                  # 'dispatching' → 'queued'  WHERE status='dispatching'
def expire_queued_run_row(self, run_id: str) -> int                                  # 'queued'     → 'expired'  WHERE status='queued'
def refresh_queued_run_yaml(self, pipeline_name: str, yaml_text: str) -> int         # update yaml_snapshot WHERE pipeline_name=:p AND status='queued'
def delete_queued_runs(self, pipeline_name: str) -> int                              # any non-terminal status for pipeline
def reset_dispatching_queued_runs(self) -> int                                       # 'dispatching' → 'queued' (boot; WHERE status='dispatching')
```

All timestamps ISO-8601 UTC (`.isoformat()`), parsed with `datetime.fromisoformat`, tzinfo-coerced to UTC exactly like `reconciler.py:29-36`.

## 4. Enqueue path

### 4.1 `trigger_run` — synchronous decision site

```python
@dataclass
class TriggerResult:
    run_id: str
    disposition: Literal["dispatched", "queued"]   # "dispatched" = today's async submit

def trigger_run(self, name: str) -> TriggerResult:
    """Immediate one-shot run. Works even when pipeline is stopped.

    Manager+worker mode with the queue flag on and zero healthy workers
    (debounced health state): durably enqueue instead of submitting a run
    that would immediately fail. The run_id is stable across the queue's
    lifetime — the 202 response, the queued_runs row, and (on dispatch)
    the run_history row all share it.
    """
```

Inside the existing `with self._lock:` block (`controller.py:442`), after the stream/running checks:

```python
run_id = str(uuid.uuid4())
if (self._worker_pool is not None and self._queue_manual_runs and self._db is not None
        and not self._worker_pool.healthy_workers()):
    if self._enqueue_manual_run(name, run_id, state.yaml_text):   # dedupe inside
        return TriggerResult(run_id, "queued")
    # dedupe hit: an active queued run exists — return it (Decision 3)
    existing = self._db.get_active_queued_run_for_pipeline(name)
    return TriggerResult(existing["run_id"], "queued")
self._thread_pool.submit(partial(self._run_batch, name, run_id, origin="manual"))
return TriggerResult(run_id, "dispatched")
```

`healthy_workers()` is a dict comprehension over the debounced `_health` state — no I/O, safe under the lock (same trade-off class as the placement bookkeeping calls already held there).

```python
def _enqueue_manual_run(self, pipeline_name: str, run_id: str, yaml_text: str) -> bool:
    """Persist a queued manual run. RLock held by both callers (reentrant for
    the _run_batch fallback site). Returns False when an active queued run
    already exists for the pipeline (dedupe). Sets pipeline status 'queued',
    bumps MGR_DISPATCH_TOTAL{no_workers} (metric continuity with the legacy
    fail-fast) and MGR_QUEUE_ENQUEUED_TOTAL."""
```

Expiry computed at enqueue: `expires_at = now + timedelta(seconds=self._queue_ttl_seconds)`.

### 4.2 `_run_batch` — fallback site (the RCA's enqueue point)

`_run_batch` gains a keyword-only origin so manual triggers are distinguishable from APScheduler fires (config's `schedule.type` cannot tell them apart — a manual trigger of an interval pipeline has `schedule_type == "interval"`):

```python
def _run_batch(self, pipeline_name: str, run_id: str | None = None, *, origin: str = "scheduled") -> None:
```

APScheduler `args=[name]` (`controller.py:568`, `:588`) keeps the default; `trigger_run` passes `origin="manual"`. The claim phase (`controller.py:618-621`) widens one line:

```python
if state.status in ("running", "queued"):
    logger.warning("Batch job: previous run still active or queued, skipping", ...)
    return
```

Rationale: preserves the one-active-run-per-pipeline invariant — a scheduled fire during a queued window is skipped (bounded loss: at most one tick; the queued manual run runs as soon as capacity returns, and the scheduler re-arms on completion via `_on_run_complete`). This also closes the double-enqueue race between two near-simultaneous triggers: whichever `_run_batch` claims first flips status, the other skips.

In the no-capacity branch (`controller.py:648-654`), before synthesizing the FAILED result:

```python
if outcome.outcome == DISPATCH_NO_CAPACITY:
    if origin == "manual" and self._queue_manual_runs and self._db is not None:
        if self._enqueue_manual_run(pipeline_name, run_id, yaml_text):
            return                       # queued — no FAILED row, no finalize
        self._db.delete_queued_runs_if_stale(run_id)  # lost dedupe race — drop this duplicate row
        return
    ... today's FAILED RunResult + _finalize_batch_result verbatim ...
```

`DISPATCH_FAILED` (`controller.py:655-660`) is **untouched** — fail-fast with its truthful error. (The duplicate-row cleanup handles the trigger→enqueue race where the fallback enqueued run_id A and the synchronous site already enqueued run_id B: `_enqueue_manual_run` returns False and the fallback's row must not linger.)

### 4.3 Enqueue gate (exact, for the test plan)

Queue only when **all** hold: manager+worker mode · `TRAM_QUEUE_MANUAL_RUNS` on · `self._db is not None` · `origin == "manual"` · no-capacity per the debounced health state (sync site: `healthy_workers() == []`; fallback site: `outcome == DISPATCH_NO_CAPACITY`). Everything else keeps today's behavior.

## 5. State machine

```
                     enqueue (trigger_run sync / _run_batch fallback)
                              │
                              ▼
  (pipeline status: queued)  queued ──────────── expires_at ≤ now ──────► expired
                              │  ▲                                            (terminal;
              drain claims    │  │ drain dispatch_failed /                   FAILED run_history row
              (conditional    │  │ no_capacity race: revert                  with distinct error;
               UPDATE, RLock) │  │                                          pipeline status → error)
                              ▼  │
  (pipeline status: running) dispatching ── worker accepted ──► dispatched
                                   │                          (terminal; lease recorded;
                                   │ destroyed by             run continues under the normal
                                   │ delete/stop purge         on_worker_run_complete path)
                                   │
                                   stuck at manager crash → boot resets to queued
```

| Status | Entered from | Exit |
|---|---|---|
| `queued` | enqueue (both sites); `dispatching` revert; boot reset | claim → `dispatching`; TTL → `expired` |
| `dispatching` | drain claim (rowcount=1 under RLock) | accept → `dispatched`; failure → `queued`; manager crash → boot reset |
| `dispatched` | worker accepted the queued run_id | terminal (row kept with `dispatched_at`; the run itself lives in `_active_batch_runs` + run history) |
| `expired` | drain expiry pass | terminal (row kept for audit; `FAILED` run history row is the user-facing record) |

Pipeline status transitions: enqueue → `queued`; claim+accept → `running` (with the `_active_batch_runs` lease, mirroring `_run_batch`'s post-dispatch CAS); expiry → `error` (via `_finalize_batch_result`, which also records the run and deactivates the K8s service). Dispatch of a queued run *is* a normal batch run from the worker's perspective — run-complete callbacks, stats, and the `BatchReconciler`'s lease reconciliation (`reconciler.py:378-398`) all apply unchanged once the lease exists.

## 6. Drain — BatchReconciler integration and lock discipline

### 6.1 Placement in the loop

The drain is a new pass in `BatchReconciler.run_once` (`reconciler.py:442-445`), **after** `_reconcile_tracked_runs` and `_reconcile_untracked_running_pipelines`:

```python
def run_once(self) -> None:
    cleared = self._reconcile_tracked_runs()
    self._reconcile_untracked_running_pipelines(skip=cleared)
    self._drain_queued_runs()
```

Ordering rationale: lease/lost reconciliation runs first so the drain's per-run gates (`_active_batch_runs`, pipeline status) observe settled state. `BatchReconciler.run_once` executes serially on one thread (`reconciler.py:371-376`) — there is no concurrent drain within a process; the races that remain are drain-vs-API (trigger/delete/stop/update), all closed by the RLock (§6.4).

### 6.2 The drain pass

```python
def _drain_queued_runs(self) -> None:
    """Drain queued manual runs. The loop is authoritative: it re-evaluates
    capacity, pipeline state, and TTL every pass. Health-restored nudges
    only wake the loop early (§6.5) — they never dispatch."""
    if not self._worker_pool.healthy_workers():
        return                                     # debounced state — flap-safe pre-check
    for run in self._controller.drainable_queued_runs():   # copies, ordered by requested_at
        now = datetime.now(UTC)
        if run["expires_at"] <= now:
            self._controller.expire_queued_run(run["run_id"])
            continue
        claimed = self._controller.claim_queued_run(run["run_id"])
        if claimed is None:
            continue                                # lost the claim (delete/stop raced us)
        # ── network I/O with the lock released (redispatch_broadcast_slot pattern) ──
        outcome = self._worker_pool.dispatch_with_result(
            run_id=claimed["run_id"],
            pipeline_name=claimed["pipeline_name"],
            yaml_text=claimed["yaml_snapshot"],   # the auditable snapshot (Decision 5)
            schedule_type=claimed["schedule_type"],  # derived from current config at claim time
            callback_url=claimed["callback_url"],
        )
        if outcome.outcome == DISPATCH_ACCEPTED:
            self._controller.commit_queued_dispatch(claimed["run_id"], outcome.worker_url)
        else:   # DISPATCH_FAILED, or DISPATCH_NO_CAPACITY (capacity vanished mid-pass)
            self._controller.revert_queued_claim(claimed["run_id"])
```

### 6.3 Controller-side queue operations (all under the B.5 RLock)

```python
def drainable_queued_runs(self) -> list[dict]:
    """[{"run_id", "pipeline_name", "yaml_snapshot", "schedule_type",
    "callback_url", "requested_at", "expires_at"}] — status='queued', ordered by
    requested_at, EXCLUDING pipelines that are deleted, have an active batch
    lease, or have status 'running'. Lock-held read; returns copies."""

def claim_queued_run(self, run_id: str) -> dict | None:
    """queued → dispatching. RLock + conditional UPDATE (rowcount fence):
    re-reads the row, verifies pipeline exists / not running / no lease,
    runs db.claim_queued_run_row(run_id), returns the claim payload or None
    when the row was claimed, purged, or expired elsewhere."""

def commit_queued_dispatch(self, run_id: str, worker_url: str) -> bool:
    """dispatching → dispatched + CAS: re-check pipeline exists under the lock,
    record the _active_batch_runs lease (schedule_type from current config),
    set pipeline status 'running', db.mark_queued_run_dispatched,
    MGR_DISPATCH_TOTAL{accepted} + MGR_QUEUE_DISPATCHED_TOTAL + wait histogram."""

def revert_queued_claim(self, run_id: str) -> bool:
    """dispatching → queued. Log WARNING + MGR_QUEUE_DRAIN_RESULT{failed|no_capacity}."""

def expire_queued_run(self, run_id: str) -> bool:
    """queued → expired: db.expire_queued_run_row, then a FAILED RunResult
    (started_at=requested_at, finished_at=now, error="no worker capacity within
    {N} minutes — queued run expired") through _finalize_batch_result, so the
    run-history row, pipeline 'error' status, and K8s service deactivation all
    reuse the proven finalize path. Pipeline deleted meanwhile → drop the row only."""
```

### 6.4 Lock discipline (the D.2 commit-through-controller pattern)

| Operation | Lock | Notes |
|---|---|---|
| `trigger_run` (sync enqueue decision) | RLock held (existing block) | `healthy_workers()` is a dict read; `_enqueue_manual_run` is reentrant |
| `_enqueue_manual_run` (fallback site, batch thread) | takes RLock | dedupe check + insert + `set_status("queued")` atomic |
| `claim_queued_run` | RLock | in-memory gate + conditional UPDATE; **rowcount=1 is the single-claim fence** — the DB WHERE clause (`status='queued'`) makes the claim idempotent even against a hypothetical second writer |
| dispatch HTTP | **no lock** | mirrors `redispatch_broadcast_slot` (`controller.py:1929-1938`) and `_run_batch`'s post-claim release |
| `commit_queued_dispatch` / `revert_queued_claim` / `expire_queued_run` | RLock | CAS re-check under the lock before mutating (a `delete()` that landed mid-dispatch wins; the stale dispatch result is discarded, the row already purged) |
| `delete` / `stop_pipeline` / `update` queue hooks | RLock (callers already hold it) | §7 |

Anti-thrash argument (requirement 2): the drain pre-check and both enqueue sites read the *same* debounced health state (`health_failures_to_down=2`), so a single probe flap can neither enqueue nor trigger a drain attempt. A worker that flaps *while* marked healthy yields `DISPATCH_FAILED` on drain → revert (Decision 6) — bounded at one dispatch attempt per 10s pass per queued run, hard-bounded by TTL, with zero run-history churn. A `dispatching` row never re-enqueues a duplicate: revert is a conditional UPDATE on `status='dispatching'`.

### 6.5 Worker-restored nudge (loop stays authoritative)

`WorkerPool` gains an optional constructor hook:

```python
on_health_restored: Callable[[], None] | None = None   # called when a worker transitions down→up in the poll loop
```

The app wires it to a `threading.Event` owned by `BatchReconciler` (`_drain_nudge`). `_loop` waits on **either** the stop event or the nudge (nudge → clear + run `run_once` immediately). The nudge never dispatches itself; worst case it is redundant with the next tick. If wiring is absent (older construction path), behavior degrades to pure 10s polling — correct, just slower.

## 7. Lifecycle wiring

| Path | Behavior |
|---|---|
| `controller.delete` (`:358-370`) | purge the pipeline's non-terminal queued rows (`db.delete_queued_runs`) inside the existing RLock block, next to the `_active_batch_runs.pop`. A deleted pipeline's queued request must never dispatch. |
| `controller.stop_pipeline` (`:395-402`) | purge likewise — a user stopping a pipeline while its manual run waits clearly rescinds it. (No FAILED run-history row: the run never started; the row simply disappears from the queue view.) |
| `controller.update` (`:321-356`) | `db.refresh_queued_run_yaml(name, yaml_text)` inside the RLock block (Decision 5). If the updated pipeline is active it gets rescheduled as today; the queued row survives, now carrying current config. |
| `controller.restart_pipeline` | no special handling — it stops execution and reschedules; a queued manual run is not "execution". If the user wants the queued request gone, stop purges it. |
| `_boot_load` (`:204-234`) | `db.reset_dispatching_queued_runs()` first (a crash mid-claim leaves `dispatching`; nothing is in flight at boot), then nothing else — the BatchReconciler (started by the app alongside the controller) drains within one interval and expires past-TTL rows on its first pass. **Decision 4:** queued runs survive the restart and dispatch after, with the absolute TTL still running. |

`_boot_load` deliberately does **not** dispatch directly: boot has enough to do, the reconciler is the single drain authority, and 10s of extra latency is noise against a minutes-long outage.

## 8. API contract and UI surfaces

### 8.1 API

**`POST /api/pipelines/{name}/run`** (`pipelines.py:330-346`) — `trigger_run` now returns `TriggerResult`:

- capacity (or flag off / standalone): **200** `{"name", "status": "triggered", "run_id"}` — unchanged.
- enqueued: **202** `{"name", "status": "queued", "run_id", "expires_at"}`.
- dedupe hit: **202** with the *existing* run_id (idempotent re-trigger).

**`GET /api/runs`** (`runs.py:16-54`) — before serialization, merge `db.get_queued_run_view()` rows for pipelines the caller can see, shaped exactly like `RunResult.to_dict()` (`context.py:138-154`): `{"run_id", "pipeline", "status": "queued", "started_at": requested_at, "finished_at": None, records 0, "error": None, ...}`. Merged rows sort with the run-history rows by `started_at` (the merge happens in the router before limit/offset pagination — queued rows are ≤ pipeline count, so the pagination skew is bounded and documented). CSV export (`format=csv`) inherits the merge; `finished_at: None` renders empty.

**`GET /api/runs/{run_id}`** (`runs.py:57-64`) — fall back to the queued view when run history misses, before 404.

**Pipelines list / detail** — each pipeline state gains `"queued_run": {"run_id", "requested_at", "expires_at"} | None` (single read on `list_all`'s existing lock pass; join against `get_queued_run_view` in the router, not a per-pipeline DB hit).

### 8.2 UI (described, not implemented)

- **Badge** — `utils.js:59-71` status map gains `queued: 'badge-queued has-dot queued'`; visual: the amber/paused family (`badge-paused`-adjacent — amber background, pulsing dot, same as `reconciling`'s treatment), a new `.badge-queued` class rather than reusing paused, so the two remain visually distinct.
- **Pipelines list** — a pipeline in `queued` status shows the amber `queued` badge in the Status column (it replaces today's misleading `error`); the queued indicator is the status itself — no extra column.
- **Detail page** — while queued: Run Now button (`detail.js:178-238`) renders disabled with `<i class="bi bi-hourglass-split"></i><span>Queued…</span>`, and a one-line info row "Queued at {requested_at} — expires {expires_at}" if capacity doesn't return. On dispatch, the normal Running… state takes over.
- **Runs table** — `queued` badge; `started_at` populated, duration "—" (finished_at null already renders as such); actions column shows no stop/log links for queued rows (nothing to stop — the entry is a request, not a run).
- Dark+light verification per the E.3 checklist habit; no template-page changes.

## 9. Metrics (registry conventions: `tram_mgr_*`, MGR_ constants, `_NoOp` fallbacks — `metrics/registry.py:40-88, 130-142`)

```python
MGR_QUEUE_DEPTH = Gauge("tram_mgr_queue_depth",
    "Manual runs currently queued (non-terminal queued_runs rows)", ["pipeline"])
MGR_QUEUE_ENQUEUED_TOTAL = Counter("tram_mgr_queue_enqueued_total",
    "Manual runs enqueued on no-capacity", ["pipeline"])
MGR_QUEUE_DISPATCHED_TOTAL = Counter("tram_mgr_queue_dispatched_total",
    "Queued manual runs dispatched after capacity returned", ["pipeline"])
MGR_QUEUE_EXPIRED_TOTAL = Counter("tram_mgr_queue_expired_total",
    "Queued manual runs expired at TTL without capacity", ["pipeline"])
MGR_QUEUE_WAIT_SECONDS = Histogram("tram_mgr_queue_wait_seconds",
    "Wait from request to dispatch for queued manual runs", ["pipeline"],
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 900, 1800))
MGR_QUEUE_DRAIN_RESULT_TOTAL = Counter("tram_mgr_queue_drain_result_total",
    "Drain attempt outcomes", ["pipeline", "result"])   # dispatched | no_capacity | failed
```

`MGR_QUEUE_DEPTH` is updated by enqueue/commit/expire/purge (set to remaining non-terminal count for that pipeline). The enqueue path also increments the existing `MGR_DISPATCH_TOTAL{no_workers}` for metric continuity with the legacy fail-fast. Logs: enqueue, dispatch, revert (WARNING), and expiry (WARNING) at INFO/WARNING with `pipeline`/`run_id` extras, matching existing structured style.

## 10. Rollout and rollback

### 10.1 Feature flag and config

- `TRAM_QUEUE_MANUAL_RUNS` — `1` (default; fail-open with a loud warning on unrecognized values, mirroring `controller.py:87-100`). Plumbing: `PipelineController(..., queue_manual_runs: bool | None = None)`, `tram/core/config.py` `AppConfig`, `tram/api/app.py` construction, `.env.example`, `docs/deployment.md`, `helm/values.yaml` + manager StatefulSet env (AGENTS.md: all four).
- `TRAM_QUEUE_TTL_SECONDS` — default **900** (15 min). Same plumbing. The expiry error renders "no worker capacity within 15 minutes" (N derived from the flag at expiry time).

### 10.2 Ship order (within E.2)

1. **`_upsert` helper + six-call-site refactor** — mechanical, independently testable, lands first (the plan's "at latest now" note).
2. `queued_runs` table + TramDB helpers (schema + rowcount UPDATEs).
3. Controller: `TriggerResult`, enqueue sites, claim/commit/revert/expire, lifecycle hooks (`delete`/`stop`/`update`/`_boot_load`), claim-phase `queued` skip.
4. Reconciler drain + nudge; `WorkerPool.on_health_restored` hook; app wiring.
5. API contract + read-merge + UI badges; metrics.

### 10.3 Rollback

Flag off → enqueue and drain are both gated; behavior is today's verbatim (including the `no_workers` FAILED path). The table is additive; no data migration to reverse. **Residual rows:** a rollback mid-queue leaves `queued`/`dispatching` rows inert (no drain, no expiry pass). The read-merge still renders them truthfully as queued. Runbook: either re-enable the flag once and let the TTL expire them, or `DELETE FROM queued_runs WHERE status IN ('queued','dispatching')` — dropping them silently is acceptable because no run ever started. Changelog entry required (manual-run API now returns 202 in the no-capacity case).

## 11. Test plan (deterministic)

New file `tests/unit/test_queued_runs.py`; additions to `test_reconciler.py`, `test_pipeline_controller.py`, `test_db.py`, and the API tests; concurrency cases follow `test_controller_concurrency.py` patterns (mocked `WorkerPool`, real controller + temp SQLite). All API-tier runs outside the sandbox (AGENTS.md caveat).

**Enqueue (§4):**
- `test_trigger_no_capacity_enqueues_returns_queued` — flag on, worker mode, `healthy_workers() == []`: `TriggerResult(run_id, "queued")`, one `queued_runs` row (run_id, yaml_snapshot == state.yaml_text, expires_at = now+TTL), pipeline status `queued`, no run_history row, `MGR_DISPATCH_TOTAL{no_workers}` + `MGR_QUEUE_ENQUEUED_TOTAL` incremented.
- `test_trigger_with_capacity_submits_normally` — healthy worker present: 200-path `disposition="dispatched"`, no queue row, `_run_batch` submitted with `origin="manual"`.
- `test_dispatch_failed_never_enqueues` — `dispatch_with_result` returns `DISPATCH_FAILED`: FAILED run-history row with the truthful error, no queue row (**bug-inheritance guard #1**).
- `test_single_probe_flap_does_not_enqueue` — one failed health probe (below `health_failures_to_down`): worker still healthy → normal trigger (**guard #2**).
- `test_fallback_enqueue_on_mid_dispatch_capacity_loss` — trigger with capacity, `dispatch_with_result` in the submitted `_run_batch` returns `DISPATCH_NO_CAPACITY`: row enqueued (the RCA branch), status `queued`, no FAILED row.
- `test_scheduled_fire_never_enqueues` — APScheduler-path `_run_batch` (default origin) with `DISPATCH_NO_CAPACITY`: today's FAILED result verbatim.
- `test_standalone_never_enqueues` — `worker_pool=None`: local path untouched.

**Dedupe (§2.3):**
- `test_second_trigger_returns_existing` — pipeline `queued`, second `trigger_run` → same run_id, `disposition="queued"`, still exactly one row.
- `test_concurrent_triggers_single_row` — two threads in `trigger_run` with a `threading.Barrier` inside the lock-holding region: one enqueues, one returns the existing; one row total.

**Drain (§6):**
- `test_drain_dispatches_when_capacity_returns` — queued row + healthy worker mock: `dispatch_with_result` called with the **queued run_id and yaml_snapshot**; row `dispatched`, `_active_batch_runs` lease recorded, status `running`, wait histogram observed, `MGR_QUEUE_DISPATCHED_TOTAL` incremented.
- `test_drain_single_claim` — `claim_queued_run` twice: rowcount fence makes the second `None`; plus a Barrier inside a mocked dispatch hook asserting only one dispatch per run per pass.
- `test_drain_skips_running_pipeline` — pipeline has an active lease: excluded from `drainable_queued_runs`.
- `test_drain_reverts_on_dispatch_failure` — `DISPATCH_FAILED` on drain: row back to `queued`, `MGR_QUEUE_DRAIN_RESULT{failed}`, no run_history churn; a second pass retries.
- `test_drain_reverts_on_no_capacity_race` — healthy at pre-check, `DISPATCH_NO_CAPACITY` at dispatch: reverted, retried next pass.
- `test_drain_noop_when_unhealthy` — `healthy_workers() == []`: zero dispatch calls (flap-safety of the pre-check).
- `test_expiry_writes_failed_run_with_distinct_error` — `expires_at` in the past: FAILED run-history row with error containing "no worker capacity within", row `expired`, pipeline `error`, `MGR_QUEUE_EXPIRED_TOTAL`.
- `test_worker_restored_nudge_wakes_loop` — `on_health_restored` callback sets the nudge event → `run_once` executes before the interval (Event-based, no sleeps).

**Lifecycle (§7):**
- `test_delete_purges_queued_runs` — `delete()` mid-queue: rows gone, drain never dispatches, no expiry FAILED row for the deleted pipeline.
- `test_stop_purges_queued_runs`, `test_update_refreshes_yaml_snapshot` — updated YAML is what a subsequent drain dispatches.
- `test_boot_resets_dispatching_and_requeues` — row stuck at `dispatching` pre-restart: boot resets to `queued`; drain (mocked healthy worker) dispatches it (**Decision 4**).
- `test_boot_expires_past_ttl` — row past `expires_at` at boot: first drain pass writes the FAILED row.
- `test_scheduled_fire_skipped_while_queued` — claim phase sees status `queued`: skip (one-active-run invariant).

**API (§8.1):**
- `test_run_endpoint_202_queued` / `test_run_endpoint_200_triggered`; `test_run_endpoint_202_idempotent`.
- `test_runs_list_merges_queued` — merged row shape matches `RunResult.to_dict()` (queued status, `finished_at: None`), pagination boundaries honored.
- `test_get_run_returns_queued_run` — 200 with queued view, not 404.
- `test_pipelines_list_carries_queued_run` — `queued_run` field present only when a non-terminal row exists.

**`_upsert` refactor (§3.2):**
- `test_upsert_dialect_sql` — parametrized over sqlite/postgresql/mysql engines (mocked conn capturing `text()` statements): correct ON CONFLICT / ON DUPLICATE KEY / delete+insert shapes; `update_columns=()` yields DO NOTHING.
- `test_upsert_roundtrip_sqlite` — insert-then-conflict update on a real temp DB.
- **All six existing call-site tests stay green unchanged** (the refactor's acceptance gate); parametrized round-trips per call site where coverage is thin.

## 12. File-by-file change summary

| File | Change |
|---|---|
| `tram/persistence/db.py` | `_upsert` helper + refactor of the six dialect-branching upserts (§3.2); `queued_runs` DDL + 11 helpers (§3.3) |
| `tram/pipeline/controller.py` | flag + TTL config; `TriggerResult`; `trigger_run` sync enqueue site; `_run_batch` `origin` param + fallback site + `queued` claim-phase skip; `_enqueue_manual_run`, `drainable_queued_runs`, `claim_queued_run`, `commit_queued_dispatch`, `revert_queued_claim`, `expire_queued_run`; `delete`/`stop_pipeline` purge, `update` snapshot refresh, `_boot_load` dispatching reset |
| `tram/agent/reconciler.py` | `BatchReconciler._drain_queued_runs` + `_drain_nudge` event + loop wake (§6.5) |
| `tram/agent/worker_pool.py` | `on_health_restored` constructor hook fired on down→up transition in the poll loop (§6.5) |
| `tram/api/routers/pipelines.py` | 202/`queued` response; `queued_run` field on list/detail payloads |
| `tram/api/routers/runs.py` | queued-run merge on list/get (§8.1) |
| `tram/metrics/registry.py` | six `MGR_QUEUE_*` metrics + `_NoOp` fallbacks (§9) |
| `tram/ui/src/utils.js` + pipelines/detail/runs pages | `queued` badge entry; button/disabled states; runs-table rendering (§8.2) |
| `tram/core/config.py`, `tram/api/app.py`, `.env.example`, `docs/deployment.md`, `helm/values.yaml`, manager StatefulSet | `TRAM_QUEUE_MANUAL_RUNS`, `TRAM_QUEUE_TTL_SECONDS` plumbing (§10.1) |
| `tests/unit/…` | per §11 |

**Not changed:** run_history schema; `on_worker_run_complete`/`_finalize_batch_result` (reused as-is — the expiry path and post-dispatch lease reuse them); placement machinery; stream paths; standalone mode; `dispatch_with_result` internals.

## 13. Risks and open edges

- **Stale-YAML dispatch after downtime** — mitigated by Decision 5 (update refreshes the snapshot) and the TTL bound; a queued run whose pipeline was edited *while the manager was down* dispatches the snapshot as requested — the run-history row links the run_id, and the TTL caps the staleness window at N minutes.
- **Rollback leaves inert rows** — documented runbook (§10.3).
- **`_upsert` refactor regression surface** — the six call sites are production-critical (pipeline save, placement save, file tracking); mitigated by landing it alone first behind green tests.
- **MySQL `VALUES()` deprecation** (8.0.20+) — the helper matches the existing codebase's MySQL usage; a future cleanup, not E.2 scope.
- **Pagination skew on the runs merge** — bounded by dedupe (≤ 1 queued row per pipeline); documented in the endpoint description.
- **Reconciler tick cost** — the drain adds one indexed DB read per 10s pass (plus one per non-terminal row only when healthy workers exist); negligible next to the existing `live_streams` fan-out.

## 14. Open questions

1. **TTL default** — 15 min proposed (long enough to ride a node drain + pod reschedule; short enough that an expired request isn't a surprise). Confirm against operational expectations.
2. **202 vs 200+status-field** — 202 chosen (semantically correct, matches the webhooks precedent, `webhooks.py:33`). Confirm no external automation treats non-200 as failure (the UI's `res.ok` accepts 202).
3. **Per-pipeline unique enforcement in SQL** — a partial unique index (`WHERE status='queued'`) would harden dedupe beyond the RLock, but it isn't portable (no MySQL equivalent without generated columns). Deferred; the lock + conditional check is sufficient for single-manager. Revisit opportunistically with parked B9's unique-constraint work.
4. **Disabled pipelines mid-queue** — `enabled: false` does not purge (trigger works on disabled pipelines today; consistency). Confirm this is the desired reading.
5. **Queue depth > 1** — if #21's "optionally" is later read as a backlog, the schema needs a position column and the dedupe gate changes; explicitly out of scope now, but the design keeps `requested_at` ordering so the extension is additive.

---

Key judgment calls for the approver: return-existing dedupe over replace (§2.3), queued runs survive manager restart with a live TTL clock (§2.4), update-refreshes-snapshot over dispatch-as-requested or purge (§2.5), drain dispatch-failure reverts to queued rather than failing (§2.6), a synchronous capacity check in `trigger_run` as the primary enqueue site so the 202 contract is honest — with the RCA's `_run_batch` branch retained as the race-window fallback (§2.2) — and the `_upsert` helper landed first as an isolated, testable refactor (§10.2).
