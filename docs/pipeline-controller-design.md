# Pipeline Controller — Design & Implementation Plan

> Historical design note.
> This document captures the v1.1.x controller transition plan and is not the
> source of truth for the current v1.3.0 manager/worker architecture.
> For shipped behavior, use `docs/architecture.md`, `docs/deployment.md`,
> `docs/api.md`, and `docs/roadmap.md`.

## Problem Statement

Pipeline state management is split across three classes (`PipelineManager`,
`TramScheduler`, `TramDB`) with no single authority. Multiple code paths bypass
guards, causing paused pipelines to restart, status flickering, dual execution
during rebalance, and aggressive ownership reshuffling that disrupts running jobs.

---

## 1. Consolidated State Machine

### Status values (reduced from 6 to 4)

| Status      | Meaning                                                        |
|-------------|----------------------------------------------------------------|
| `scheduled` | Has an APScheduler job / stream thread; waiting to fire        |
| `running`   | Currently executing                                            |
| `stopped`   | Not running; will **not** auto-restart (explicit user intent)  |
| `error`     | Last run failed; will **not** auto-restart until user acts     |

**Removed:** `paused` (merged into `stopped` — same runtime state, intent is
tracked by the `stopped` DB flag, see §3).

### Legal transitions

```
                      ┌─────────────────────────────────────┐
                      │            REGISTERED                │
                      │   (new pipeline written to DB)       │
                      └────────────────┬────────────────────┘
                                       │ controller.register()
                                       │  → assign owner_node
                                       │  → if enabled: schedule
                                       ▼
         ┌────────────────────────► SCHEDULED ◄──────────────────────────┐
         │                              │                                │
         │                              │  APScheduler fires / stream    │
         │                              ▼                                │
         │                          RUNNING ── crash/error ──► ERROR     │
         │                         /       \                      │      │
         │             run success /         \ explicit stop()    │      │
         │  (interval/cron/stream) ▼         ▼                   │      │
         │                     SCHEDULED   STOPPED ◄─────────────┘      │
         │                                    │                          │
         │                           start()  │                          │
         └────────────────────────────────────┘                          │
                                                                         │
         SCHEDULED ──► stop() ──► STOPPED                                │
         RUNNING   ──► stop() ──► STOPPED  (drain first)                 │
         ERROR     ──► start() ──────────────────────────────────────────┘
```

**Invariants enforced by `PipelineController`:**
- `scheduled` — set only by `_do_schedule()` internal method
- `running` — set only by `_claim_run()` DB atomic operation
- `stopped` — set only by `stop()` and `_on_run_complete()` (manual pipelines)
- `error` — set only by `_on_run_complete()` on failure
- No external code sets status directly — all transitions go through the controller

---

## 2. Architecture: `PipelineController`

Replaces `TramScheduler` and `PipelineManager` with a single class that is the
**sole authority** for all pipeline lifecycle operations.

```
PipelineController
├── owns all state transitions (register/start/stop/delete/update/trigger)
├── enforces state machine and guards at every operation
├── persists status + ownership atomically with every transition
├── delegates execution only:
│     APScheduler  → fires batch jobs
│     ThreadPool   → runs batch jobs
│     Threads      → runs stream pipelines
├── TramDB              (persistence, no business logic)
├── ClusterCoordinator  (ownership algorithm only)
└── PipelineExecutor    (batch_run / stream_run, no state knowledge)
```

### Public API (what routers call)

```python
class PipelineController:
    # Registration
    def register(name, yaml_text, source='api') -> PipelineState
    def update(name, yaml_text) -> PipelineState
    def delete(name) -> None

    # Lifecycle — all guarded
    def start(name) -> None          # clear stopped flag + schedule
    def stop(name) -> None           # set stopped flag + remove job/thread
    def trigger(name) -> str         # immediate one-shot, returns run_id
    def rollback(name, version) -> PipelineState

    # Read
    def get(name) -> PipelineState
    def list_all() -> list[PipelineState]
    def exists(name) -> bool
    def get_runs(...) -> list[RunResult]
    def get_status() -> dict          # scheduler + cluster summary

    # Internal (not called by routers)
    def _do_schedule(name) -> None    # single scheduling gate
    def _may_schedule(name) -> bool   # all guards in one place
    def _claim_run(name) -> bool      # atomic DB CAS for concurrent safety
    def _on_run_complete(name, result) -> None
    def _sync_loop() -> None          # 30s DB poll
    def _rebalance_loop() -> None     # 10s topology poll
```

### Removed from public API

`pause_pipeline()` and `resume_pipeline()` are removed. The router maps
`POST /pause` → `controller.stop()` and `POST /resume` → `controller.start()`.
The distinction is now in `last_run_status`, not in a separate status value.

---

## 3. DB Schema Changes

### `registered_pipelines` table

New columns added via `_add_column_if_missing` migration:

```sql
-- Replaces 'paused' column (same semantics, clearer name)
-- 1 = user explicitly stopped this; sync/rebalance must NOT restart it
-- 0 = pipeline is free to be scheduled based on config.enabled + ownership
stopped         INTEGER NOT NULL DEFAULT 0

-- Authoritative ownership — which node runs this pipeline
-- Empty string = unassigned (needs assignment on next sync/rebalance)
owner_node      TEXT    NOT NULL DEFAULT ''

-- Runtime status visible to all pods (written by owning pod)
-- Values: scheduled | running | stopped | error
runtime_status  TEXT    NOT NULL DEFAULT 'stopped'

-- When runtime_status was last written (stale-run detection)
status_updated  TEXT

-- Which node last wrote runtime_status (stale-run detection)
status_node     TEXT    NOT NULL DEFAULT ''
```

### Migration strategy

```sql
-- On schema init:
-- 1. Add new columns (safe, all have defaults)
-- 2. Copy paused=1 → stopped=1 (one-time data migration)
UPDATE registered_pipelines SET stopped = paused WHERE paused = 1;
-- 3. 'paused' column kept for one release cycle, then dropped
```

### `_claim_run()` — cross-pod atomic start guard

Prevents two pods from executing the same pipeline simultaneously:

```sql
-- Owning pod executes this before starting a run:
UPDATE registered_pipelines
   SET runtime_status = 'running',
       status_node    = :my_node_id,
       status_updated = :now
 WHERE name           = :name
   AND runtime_status != 'running'

-- rowcount == 1 → this pod owns the run, proceed
-- rowcount == 0 → another pod grabbed it, skip this fire
```

---

## 4. `_may_schedule()` — Single Gate

Every path that could schedule a pipeline calls this first, no exceptions:

```python
def _may_schedule(self, name: str) -> bool:
    """
    Single enforcement point. Returns True only when all conditions are met:
      1. Pipeline is registered in-memory
      2. Pipeline is not user-stopped (DB flag)
      3. Pipeline is enabled in its YAML config
      4. No other pod is currently running it (or stale run detected)
      5. In cluster mode: this pod owns the pipeline
    """
    state = self.manager.get(name)

    # Guard 1: user explicitly stopped it
    if self._db and self._db.is_pipeline_stopped(name):
        return False

    # Guard 2: YAML-level disabled
    if not state.config.enabled:
        return False

    # Guard 3: stale-run check (cross-pod crash recovery)
    if self._db:
        row = self._db.get_pipeline_runtime(name)
        if row and row.runtime_status == 'running':
            node_alive = (
                self._coordinator.is_node_alive(row.status_node)
                if self._coordinator else False
            )
            age = (datetime.now(UTC) - row.status_updated).total_seconds()
            stale_threshold = max(300, state.config.schedule.interval_seconds * 2
                                  if hasattr(state.config.schedule, 'interval_seconds')
                                  else 300)
            if node_alive or age < stale_threshold:
                return False  # genuinely running elsewhere

    # Guard 4: cluster ownership
    if self._coordinator and not self._coordinator.owns(name):
        return False

    return True
```

---

## 5. Sticky Ownership with Selective Rebalance

### Core principle

Ownership is **stored in DB per pipeline** (`owner_node` column).
Consistent hashing is used as an **assignment algorithm** only when a pipeline
needs a new owner — not as a continuous recompute on every topology change.

### Rebalance events and behaviours

#### Event A: Node failure (TTL expired or graceful deregistration)

```
Trigger: coordinator.refresh() detects a node is no longer live

Action:
  orphans = SELECT name FROM registered_pipelines
             WHERE owner_node = :dead_node AND stopped = 0 AND deleted = 0

  For each orphan:
    if runtime_status == 'running':
      mark runtime_status = 'error'   ← run was killed by pod crash
      save partial RunResult to run_history with status=ERROR

    new_owner = least_loaded_live_node()   ← uses consistent hash as tiebreak
    UPDATE registered_pipelines SET owner_node = :new_owner WHERE name = :name

  This pod checks: are any of the orphans now assigned to me?
    → _do_schedule() for each one I now own

Nothing else touched. Running pipelines on healthy nodes are undisturbed.
```

#### Event B: Node join (new pod registers)

```
Trigger: coordinator.refresh() detects a new node

Action — Phase 1 (immediate):
  Record join_time for new node in coordinator state.
  New unassigned pipelines (owner_node = '') → assign to new node preferentially.
  New node schedules its assigned pipelines.
  Existing pipelines on healthy nodes: untouched.

Action — Phase 2 (after cooling_period, default: 2 × rebalance_interval = 20s):
  overloaded_nodes = nodes with pipeline_count > ceil(total / live_node_count)

  For each overloaded node, for each excess pipeline (sorted by last_run ASC):
    Skip if runtime_status == 'running'          ← never interrupt active batch run
    Skip if schedule_type == 'stream'             ← never disrupt stream consumers
    Skip if stopped == 1                          ← user stopped, leave alone

    Reassign to most underloaded live node:
      UPDATE registered_pipelines SET owner_node = :new_node WHERE name = :name
      Old owner: remove APScheduler job (pipeline finishes naturally if running)
      New owner: detect via next sync cycle, schedules with smart next_run_time
```

#### Event C: Periodic rebalance (no topology change)

```
Trigger: rebalance_loop tick, no node change detected

Action:
  Check if any pipeline's owner_node is dead (missed in prior rebalance).
  Reassign orphans only (same as Event A).
  No load balancing — that only happens on node join.
```

### `least_loaded_live_node()` algorithm

```python
def least_loaded_live_node(self, exclude_nodes=None) -> str:
    """
    Returns the live node with the fewest owned pipelines.
    Uses consistent hash of pipeline name as tiebreak for determinism.
    All live pods compute the same result independently (no coordination needed).
    """
    counts = self._db.get_pipeline_counts_by_node()   # SELECT owner_node, COUNT(*)
    live = [n for n in self._coordinator.live_node_ids() if n not in (exclude_nodes or [])]
    # Fill in zeros for nodes with no pipelines yet
    for node in live:
        counts.setdefault(node, 0)
    # Sort: primary = count ASC, secondary = node_id (deterministic tiebreak)
    return min(live, key=lambda n: (counts[n], n))
```

### Why this is better than full consistent hashing rebalance

| Scenario | Old behaviour | New behaviour |
|----------|--------------|---------------|
| Node joins cluster | ⅓ of all pipelines reshuffled, streams killed | Only unassigned pipelines go to new node; existing streams untouched; scheduled pipelines volunteered gradually after cooling period |
| Node fails | ⅓ of all pipelines reshuffled | Only dead node's pipelines redistributed |
| Pod restart (same node) | Re-registers, triggers full rebalance | Reads `owner_node` from DB → re-acquires exactly its own pipelines; no rebalance |
| YAML updated | Pipeline stays on same node | Pipeline stays on same node (only config reloads) |
| New pipeline added | Assigned by hash position | Assigned to least-loaded node |

---

## 6. Status Visibility Across Pods

Currently `status` is in-memory and pod-local. In a cluster, the UI shows
different answers depending on which pod handles the request.

**Fix:** `runtime_status` in `registered_pipelines` is written by the owning pod
on every transition and read by all pods for display.

Non-owning pods serve `runtime_status` from DB for API responses. They do not
maintain authoritative in-memory status for pipelines they don't own — they just
cache the last DB-read value and refresh it on the `_sync_loop` tick.

---

## 7. Startup Sequence

Current startup has a race: `start()` schedules all enabled pipelines before
`_load_from_db()` applies the stopped flags. Fixed ordering:

```
1. Schema migration (add new columns, copy paused→stopped)
2. Node registers in node_registry + coordinator.refresh()
3. Disk pipelines seeded to DB (if TRAM_PIPELINE_DIR set)
4. Load all pipelines from DB into in-memory registry (PipelineState objects)
5. Load stopped flags from DB → mark matching PipelineStates
6. Load owner_node from DB:
     - If owner_node == this_node → schedule (subject to _may_schedule())
     - If owner_node == '' (unassigned) → this node claims it (least-loaded check)
     - If owner_node == other_live_node → set status from runtime_status in DB
     - If owner_node == dead_node → treat as orphan, claim if least-loaded
7. Start APScheduler + stream threads for owned, non-stopped pipelines
8. Start rebalance_loop + sync_loop
```

This eliminates the startup race entirely. No retroactive status patching.

---

## 8. File Structure Changes

```
tram/
├── pipeline/
│   ├── controller.py    ← NEW: PipelineController (replaces scheduler.py + manager.py)
│   ├── manager.py       ← KEEP: PipelineState + in-memory registry (used by controller)
│   ├── executor.py      ← UNCHANGED
│   └── loader.py        ← UNCHANGED
├── scheduler/
│   └── scheduler.py     ← REMOVED (logic moves to controller.py)
├── cluster/
│   ├── coordinator.py   ← EXTENDED: least_loaded_live_node(), is_node_alive(),
│   │                                sticky ownership helpers
│   └── registry.py      ← UNCHANGED
├── persistence/
│   └── db.py            ← EXTENDED: stopped flag, owner_node, runtime_status,
│                                     claim_run(), get_pipeline_runtime(),
│                                     get_pipeline_counts_by_node()
└── api/
    ├── app.py           ← UPDATED: wire PipelineController instead of TramScheduler
    └── routers/
        └── pipelines.py ← UPDATED: pause→stop, resume→start; remove dual manager/scheduler refs
```

---

## 9. API Endpoint Changes

| Old endpoint | New endpoint | Change |
|-------------|-------------|--------|
| `POST /pause` | `POST /stop` | Merged — both set stopped flag |
| `POST /resume` | `POST /start` | Merged — both clear stopped flag |
| `POST /start` | `POST /start` | Unchanged |
| `POST /stop` | `POST /stop` | Unchanged |
| All others | Unchanged | No API surface change |

`/pause` and `/resume` kept as **aliases** for one release cycle to avoid
breaking existing automations, then deprecated.

---

## 10. Implementation Phases

### Phase 1 — DB schema + migration (no behaviour change)
- Add `stopped`, `owner_node`, `runtime_status`, `status_updated`, `status_node`
  columns to `registered_pipelines`
- Add `claim_run()`, `get_pipeline_runtime()`, `get_pipeline_counts_by_node()`,
  `set_pipeline_owner()`, `is_pipeline_stopped()` to `TramDB`
- Add `is_node_alive()` to `ClusterCoordinator`
- Write + pass tests for new DB methods

### Phase 2 — `PipelineController` core
- Implement `PipelineController` with full state machine
- `_may_schedule()` single gate
- `_claim_run()` atomic CAS
- Fixed startup sequence (no race)
- `stop()` / `start()` replace `pause()` / `resume()`
- Wire into `api/app.py` and `api/routers/pipelines.py`
- All existing unit tests pass (controller API is a superset of old API)

### Phase 3 — Sticky ownership
- `owner_node` written on registration and assignment
- Startup reads `owner_node` from DB (§7 step 6)
- `_rebalance()` rewrites: Event A (failure), Event B (join + cooling), Event C (periodic)
- `least_loaded_live_node()` in coordinator
- DB-backed `runtime_status` written on every transition, served by non-owning pods

### Phase 4 — Cleanup
- Remove `TramScheduler` (`scheduler/scheduler.py`)
- Drop `paused` column (migration adds `stopped`, old `paused` stays for one release)
- Remove `/pause` and `/resume` endpoints (after deprecation period)
- Update `version_1.5.0.md`

---

## 11. Environment Variables (new)

| Variable | Default | Description |
|----------|---------|-------------|
| `TRAM_REBALANCE_COOL_SECONDS` | `20` | Cooling period before new node receives load from voluntary drain |
| `TRAM_STALE_RUN_THRESHOLD` | `300` | Seconds after which a `running` status from a dead node is considered stale |
| `TRAM_NODE_TTL_SECONDS` | `30` | Existing — node heartbeat expiry (unchanged) |
| `TRAM_HEARTBEAT_SECONDS` | `10` | Existing — heartbeat interval (unchanged) |

---

## 12. What Does NOT Change

- `PipelineExecutor` (`batch_run`, `stream_run`, `dry_run`) — untouched
- `ClusterCoordinator.owns()` and consistent hashing algorithm — kept as assignment helper
- `NodeRegistry` heartbeat loop — unchanged
- All connectors, transforms, serializers — untouched
- `PipelineState` in-memory object and `PipelineManager` registry — kept, used by controller
- `APScheduler` usage — kept, controller manages it internally
- Stream thread pool — kept, controller manages it
- `_add_interval_job()` smart `next_run_time` logic — kept
- All other API endpoints — unchanged

---

---

## 13. Implementation Status (post-v1.1.4)

### What was implemented as designed

- ✅ Phase 1 — DB schema: `stopped`, `owner_node`, `runtime_status`, `status_updated`, `status_node` columns
- ✅ Phase 2 — `PipelineController` core: `_may_schedule()`, `_claim_run()`, fixed startup sequence
- ✅ Phase 3 — Sticky ownership: `owner_node` in DB, `_rebalance()` Events A/B/C, `least_loaded_node()`
- ✅ `pause`/`resume` → deprecated aliases for `stop`/`start`
- ✅ UI updated: removed pause button, `scheduled` status added to filter, `paused` badge removed

### Deviations from design

**§6 Status visibility** — The design called for non-owning pods to cache `runtime_status` from
DB and refresh on `_sync_loop` tick. Instead, a simpler approach was taken: `GET /api/pipelines`
and `GET /api/pipelines/{name}` overlay DB `runtime_status` directly on API responses (one query
per request). This is the "Option A" fix. It solves the flickering problem without changing the
internal state model.

**§11 `_sync_loop` interval** — Reduced default from 30s to 10s (`TRAM_PIPELINE_SYNC_INTERVAL`).

### Bugs found during implementation (now fixed)

**`_sync_from_db` skipped stopped-flag changes** — The sync loop called
`continue` for any pipeline with unchanged YAML. When a user clicked Start (clearing the stopped
flag), the owning pod never saw the change and never re-scheduled. Fixed: stopped-flag state
is checked even for YAML-unchanged pipelines.

**`_get_newly_joined_nodes` caused infinite cooling-period cycles** — After a cooling-period
drain completed, the node was removed from `_node_join_times`. On the next rebalance tick
it re-appeared as "newly joined", triggering another cooling period indefinitely. Fixed: added
persistent `_seen_nodes` set; nodes are only removed from it when confirmed dead.

### Known limitations (accepted for v1.1.4, resolved in v2.0.0)

- DB `runtime_status` read on every list/get API call adds one round-trip per request.
- The coordination machinery (`_rebalance_loop`, `_sync_loop`, `_seen_nodes`,
  `ClusterCoordinator`, `claim_run()`) is inherently complex because N-equal-peer
  distributed state has no clean solution at this abstraction level.
- Status shown by `GET /api/pipelines` is DB-authoritative but internal scheduling
  decisions still use in-memory status, which can diverge temporarily.

**The permanent fix** is the Manager + Worker architecture described in
`docs/roadmap_1.2.0.md`. The controller design in this document is considered
feature-complete for v1.x.

---

*Created: 2026-04-09*  
*Status: Implemented — v1.1.4 (with Option A status-visibility fix applied in v1.2.0 dev branch)*
