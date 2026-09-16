# Design Doc — D.2: Durable Record for count=1 Streams (GH #17)

**Status:** proposed (requires approval before implementation)
**Branch:** `wave-a-stopgaps` · **Plan ref:** `docs/plans/issue-implementation-plan.md` Wave D, item D.2
**Sources:** RCA `docs/reviews/issue-rca.md` #17 · Wave B review annotations (B.5/B.6 lanes) · code as of commit `299645e`
**Prerequisites:** D.1 (A13 per-slot `update_slot_run_id` fix) **must land first** — see §8.

---

## 0. Problem statement

count=1 stream pipelines (the default for every non-push source: kafka, gnmi, mqtt, websocket, sql, rest, corba, snmp_poll — `models/pipeline.py:1348-1356`) are dispatched by `controller._start_stream` (`controller.py:946-979`) through `dispatch_with_result()` and recorded **only** as `_stream_run_ids[name] = [run_id]` in manager memory. Consequences traced in RCA #17 and re-confirmed on this branch:

1. **No durable liveness record** — visibility rests on the 90s-TTL StatsStore plus an on-demand live probe, both riding the manager↔worker channel. Interrupt both >90s → the stream vanishes from every runtime view while status says "running".
2. **Manager restart double-dispatch** — `controller.stop()` deliberately keeps worker-side streams alive (`controller.py:159-163`); `_boot_load` re-schedules enabled streams. B.6 added the `_adopt_live_stream_if_any` guard (`controller.py:215-265`), which fixes the double-start but leaves two holes:
   - **Worker death post-adoption leaves the pipeline stuck "running"** — the PlacementReconciler iterates `get_active_broadcast_placements()` only, which is empty for adopted count=1 streams. No re-probe, manual recovery.
   - **Adoption preserves stale config** — the worker runs the pre-restart YAML; nothing detects or corrects drift.
3. **No reconciler-driven restart** — broadcast placements get stale-slot detection + redispatch (`reconciler.py:174-207`); count=1 streams get nothing.

The broadcast placement machinery already does everything count=1 streams need — `multi_dispatch` natively handles `target_slots=1` (`worker_pool.py:590-594`, `:625` sets `slot_run_id = placement_group_id`), `_record_broadcast_placement` / `_restore_broadcast_placement` / `PlacementReconciler` are count-agnostic, and the placement view renders rows unconditionally (`_stream_views.py`). D.2 routes count=1 dispatch through that machinery.

---

## 1. Goals and non-goals

**Goals**

- count=1 stream dispatch produces a persisted 1-slot placement row; UI visibility, manager-restart behavior, and worker-death recovery all derive from that row.
- Manager restart adopts a live worker run into the placement row (no double-start, no interruption).
- Worker death after adoption (or any time) is recovered by reconciliation within a bounded window.
- Stale-config adoption is detected and resolved by policy.
- All re-dispatch paths are idempotent under the B.5 RLock.
- Broadcast (count=all/N/list) flows are untouched in behavior.

**Non-goals**

- D.3 (view-layer gate drop for standalone mode) — separate item; D.2 changes no router code.
- D.6 (event-loop offload of `live_streams()` fan-out from async endpoints) — the reconciler already probes from its own thread; D.2 adds no probe cost (probes are per-worker, not per-stream).
- Standalone mode (`_worker_pool is None`) — unchanged; count=1 placement applies to manager+worker mode only.
- Any asyncio rewrite of the dispatch path (constraint: thread-based).
- Manager HA, E.2 queue, worker-side stop latency for stop-less sources (parked B3).

---

## 2. Core decision — reuse, don't fork

**Decision: reuse `multi_dispatch` + `_record_broadcast_placement` unchanged. No slimmed 1-slot variant, no new table.**

Rationale:

- `dispatch_with_result` **already** routes through `multi_dispatch` internally (`worker_pool.py:710-719`) with `WorkersConfig(count=1)` — it only discards the `BroadcastResult` slots and the controller skips `_record_broadcast_placement`. The delta is small and the machinery is proven.
- `resolve()` handles count=1 (`worker_pool.py:352` — falls through to `[candidates[0][0]]`), `target_slots` computes to 1, and the 1-slot `slot_run_id = placement_group_id` convention (`:625`) gives redispatch run-ids for free (`{prefix}-r{n}`, `controller.py:1470`).
- The `broadcast_placements` schema (`db.py:174-183`) needs **zero migration**: `target_count TEXT NOT NULL` stores `"1"`, and `PlacementReconciler._target_count` (`reconciler.py:43-51`) already parses digit strings.
- A slimmed variant would fork the placement state machine — the exact copy-paste drift pattern this codebase has been burned by twice (A6 alert persistence, A7 watcher method).

**Schema impact: none.** One new helper query (§7.5).

---

## 3. Dispatch path changes

### 3.1 Factor the broadcast gate

The predicate `workers_cfg.count == "all" or (isinstance(count, int) and count > 1) or worker_ids is not None` is duplicated at `controller.py:910-914` and `:239-243`. Extract:

```python
def _is_broadcast_workers(workers_cfg: WorkersConfig | None) -> bool:
    """True when the config selects more than one worker slot (or pins a list)."""
```

Both call sites use it. (Workers is never None post-validation — `apply_workers_default` assigns it, `models/pipeline.py:1349-1356`.)

### 3.2 `_start_stream` manager+worker branch (`controller.py:892-979`)

Add a controller flag (§9.1): `self._single_stream_placements: bool`.

**Flag ON (new path):**

```python
with self._lock:
    if config.name in self._stream_run_ids or config.name in self._active_placement_group:
        logger.debug("Stream already dispatched", extra={"pipeline": config.name})
        return
    placement_group_id = self._make_placement_group_id(config.name)
    result = self._worker_pool.multi_dispatch(
        placement_group_id=placement_group_id,
        pipeline_name=config.name,
        yaml_text=state.yaml_text,
        workers_cfg=config.workers,          # count=1 resolves to one slot
        schedule_type="stream",
        callback_url=callback_url,
    )
    if not result.accepted:
        # distinguish exactly like the legacy branch (worker_pool.py:595-607, :643-644):
        #   result.rejected non-empty  -> dispatch_failed (slot carries .error)
        #   result.rejected empty      -> no_capacity
        ...set_status("error"), MGR_DISPATCH_TOTAL{no_workers|dispatch_failed}...
        return
    MGR_DISPATCH_TOTAL.labels(pipeline=config.name, result="accepted").inc()
    self._record_broadcast_placement(config.name, placement_group_id, result, config.workers)
    self.manager.set_status(config.name, result.status)   # "running" for 1 accepted slot
    self._activate_kubernetes_service(config)
```

The existing `if workers_cfg is not None and (…broadcast…)` gate **disappears** on this path: with the flag on, all worker-mode streams (count=1, N, all, list) go through the same branch. count=1 is just the degenerate 1-slot case:

- `multi_dispatch` returns `accepted=[url]`, `slots=[{worker_index: 0, …}]`, `status="running"`.
- `_record_broadcast_placement` (`controller.py:1175-1215`) persists the row with `target_count=1` and syncs `_stream_run_ids` via `_sync_stream_run_ids_from_slots` — so `on_worker_run_complete`'s `_remove_stream_run_id` / stats-store cleanup logic (`controller.py:874-880`) works unchanged.
- 1 accepted + 0 rejected → `status="running"` (`worker_pool.py:660-662`); a rejected slot → `status="degraded"` with `slot["error"]` persisted in `slots_json` — strictly better diagnostics than the legacy path.

**Flag OFF (legacy path retained one release):** the count=1 branch (`controller.py:946-979`) stays verbatim, including `dispatch_with_result` and the B.6 adopt guard.

### 3.3 Stop / update / restart paths — no changes required

`_stop_stream` (`controller.py:1066-1094`) already pops `_active_placement_group`, calls `stop_pipeline_runs(name)` (probe-all fallback that doesn't depend on `_assignments`), and marks the row `stopped` in DB. `update()` / `delete()` / `restart_pipeline()` go through `_stop_execution` → `_stop_stream` under the RLock; a subsequent `_do_schedule` creates a fresh placement group. The already-dispatched guard in §3.2 (checking both `_stream_run_ids` and `_active_placement_group`) prevents double entry from any scheduler path.

---

## 4. Poll-vs-push source semantics under placement

**Decision: no source-type special-casing in the placement machinery. The distinction that drives behavior is `count`, and the machinery is count-driven.** Documented consequences:

- **Push sources** (webhook, prometheus_rw, syslog, snmp_trap) default to `count="all"` (`models/pipeline.py:1352-1353`) and already use placements — D.2 doesn't touch them. A push source explicitly configured `count: 1` gets a 1-slot placement; its per-pipeline NodePort Service follows the slot's `worker_url` via `_get_dispatched_worker_ids` (`controller.py:1295-1312`), which reads placement slots — correct today, unchanged.
- **Poll sources** (the default count=1 population) under placement:
  - **Deliberate re-dispatch** (user-driven `update()` / `restart_pipeline()`) — unchanged: stop + new placement group.
  - **Manager restart** — adopt into the restored placement (§5.1); the same logical stream keeps running; no re-read of the source, no duplicate consumption.
  - **Worker death** — reconciler redispatch (§5.3). Kafka offsets are committed per poll batch (B.7), so a redispatch duplicates at most one poll batch — consistent with the platform's at-least-once contract. gNMI/CORBA/sql/rest have no committed consumer position; redispatch starts a fresh subscription/invocation — same semantics a manual restart has today.
- **Stop latency** for stop-less sources (gnmi, kafka — parked B3) is unchanged: `_stop_stream`'s HTTP stop sets the agent-side stop event; the source observes it on its next read cycle. Out of D.2 scope.

---

## 5. Manager restart: restore, adoption, and liveness reconciliation

### 5.1 Restore path (flag on) — placement row exists

`_boot_load` (`controller.py:201-206`) already takes the placement branch before `_adopt_live_stream_if_any` is consulted: `_restore_broadcast_placement(placement)` → status `reconciling`, `dispatched_at` refreshed (re-arming the first-stats grace, `reconciler.py:34-40`), K8s service activated. What happens next is existing reconciler behavior, now covering count=1:

- **Worker still runs the stream** → the reconciler's live-first slot match (`reconciler.py:88-102`, run-id then (pipeline, worker-key)) or the next 30s stats payload (`on_pipeline_stats`, `controller.py:1407-1446` — transitions `reconciling` → `running` when all slots report) adopts the run **durably**. This is B.6's adoption, performed by the reconciler, recorded in the placement row. **No dispatch HTTP call is made** — the double-start bug is structurally gone.
- **Worker died while the manager was down** → slot stats absent past the grace window → slot marked `stale` → `_select_replacement_worker` (`reconciler.py:53-81`) picks any healthy worker (for 1-slot, `excluded` is empty; a `pinned_worker_id`, only set for `workers.list`, pins to that worker) → `redispatch_broadcast_slot` with `{prefix}-r1`.

**One gap in shared restore code, fixed here (benefits all placements):** `_restore_broadcast_placement` (`controller.py:1149-1173`) never re-registers the worker-pool run assignments, so `stop_run(run_id)` after a restart can't find the worker (today masked by the `stop_pipeline_runs` probe-all fallback, which works but is slow and log-noisy). Add after the slot sync:

```python
for slot in placement_copy["slots"]:
    if slot.get("current_run_id") and slot.get("worker_url"):
        self._worker_pool.adopt_stream_assignment(
            pipeline_name, run_id=str(slot["current_run_id"]), worker_url=str(slot["worker_url"]),
        )
```

`stop_pipeline_runs` remains as belt-and-braces.

### 5.2 No-placement fallback (flag off, or upgrade transition)

`_adopt_live_stream_if_any` (`controller.py:215-265`) stays, with its role narrowed:

- **Flag off:** sole mechanism, exactly as B.6 shipped it.
- **Flag on:** it becomes the **migration bridge**. A count=1 stream running at upgrade time has no placement row; on the first post-upgrade restart the guard fires, and now it must **materialize the placement** so the stream joins the new regime without a restart:

```python
def _materialize_placement_from_adoption(self, config: PipelineConfig, adopted: dict) -> None:
    """Create a 1-slot placement row from a worker-reported live run (B.6 → D.2 bridge).
    Caller holds the lock."""
    placement_group_id = self._make_placement_group_id(config.name)
    run_id = str(adopted["run_id"]); worker_url = str(adopted["worker_url"])
    slot = {
        "worker_index": 0, "worker_url": worker_url,
        "worker_id": self._worker_pool.worker_id_for_url(worker_url) or "",
        "pinned_worker_id": None,
        "run_id_prefix": run_id,            # redispatch will use f"{run_id}-r{n}"
        "current_run_id": run_id,
        "dispatched_at": datetime.now(UTC).isoformat(),
        "status": "running", "restart_count": 0,
        "adopted": True,                    # provenance marker (also skips first-stats grace)
    }
    # register in _broadcast_placements / _active_placement_group /
    # _stream_run_ids (existing helpers), then db.save_broadcast_placement(
    # placement_group_id, pipeline_name, [slot], target_count=1,
    # status="running", started_at=adopted.get("started_at") or now)
```

Notes: the adopted `run_id` becomes the `run_id_prefix`; `on_pipeline_stats`'s prefix-match (`controller.py:1421-1422`) matches it exactly (the run_id equals its own prefix). Status is set straight to `running` (the run is demonstrably live — the guard probed it), so the reconciler won't wait out the first-stats grace.

### 5.3 Liveness reconciliation — the stuck-running fix

**Decision: extend `PlacementReconciler`, do not create a new reconciler class.** The pass consumes the `live_streams()` snapshot already fetched once per `run_once` (`reconciler.py:127`) — zero additional worker probes — and the recovery semantics (redispatch vs mark-error) are placement logic. `BatchReconciler` is deliberately lease/RunResult-shaped and wrong for streams.

New method, called from `run_once` after the placement loop:

```python
def _reconcile_unplaced_streams(self, live_streams: list[dict]) -> None:
    """Streams with manager status 'running' but no placement group (flag-off
    adoption, failed placement persistence, or drift). Adopt bookkeeping if
    live; recover (redispatch or error) if gone."""
```

Controller support (single locked read; do not reach into controller internals):

```python
def stream_liveness_candidates(self) -> list[dict]:
    """[{"name": str, "has_placement": bool}] — stream pipelines, manager+worker
    mode, status == "running"."""
```

Per candidate with `has_placement == False`:

1. **Alive check:** a live-stream sighting for the pipeline in the snapshot (keyed by pipeline name — this pass is name-based; a count=1 stream has at most one live run, and extra stray runs are handled below) **or** a non-stale `stats_store.for_pipeline(name)` entry (a healthy stream keeps posting stats even when the live probe flakes — two independent liveness signals, same philosophy as RCA #17's live-first matching).
2. Alive → **self-heal bookkeeping**: `worker_pool.adopt_stream_assignment(name, run_id, worker_url)` if the assignment is missing, `_stream_run_ids.setdefault(name, [run_id])`. Additionally, if **more than one** live run for the pipeline is sighted (pre-D.2 double-dispatch residue, or a bug elsewhere), stop all but the earliest — the reconciler is the only component with a global view.
3. Not alive → hysteresis counter `self._unplaced_misses[name] += 1` (dict on the reconciler; reset on any sighting or when the pipeline leaves the candidate set). At **2 consecutive misses** (matching `health_failures_to_down`, `worker_pool.py:77`): `controller.recover_unplaced_stream(name)`:

```python
def recover_unplaced_stream(self, name: str) -> None:
    """Recovery for a running stream with no placement record and no live run.
    Idempotent; RLock-reentrant down into _do_schedule."""
    with self._lock:
        if not self.manager.exists(name): return
        state = self.manager.get(name)
        if state.config.schedule.type != "stream": return
        if name in self._active_placement_group: return   # raced with materialization
        self._stream_run_ids.pop(name, None)
        if self._may_schedule(name):      # enabled and not user-stopped
            self._do_schedule(name)       # routes via the flag → placement or legacy
            log INFO + MGR_RECONCILE_ACTION_TOTAL{action="stream_recover"}
        else:
            self.manager.set_status(name, "stopped")
```

(Nested lock acquisition is safe: `self._lock` is an `RLock` — `controller.py:94` — and the reconciler thread is the only caller on this path.)

**Worst-case recovery time:** 2 reconciler passes + stale window. With defaults: 2 × `min(30,10)s` misses + the stats grace already elapsed ≈ **≤ ~40-60s** from worker death, versus never (today).

### 5.4 Placement status machine (unchanged, now applies to count=1)

| Placement status | Entered from | Exit |
|---|---|---|
| `running` | fresh dispatch (all slots accepted) · reconciling→running (all slots report) | stop/delete → `stopped`; slot stale → `degraded` |
| `reconciling` | `_restore_broadcast_placement` at boot | → `running`/`degraded` after `2 × stats_interval` (`reconciler.py:227-230`) or when all slots running |
| `degraded` | a slot stale, or running-slots < target_count (`reconciler.py:234-241`) | redispatch → `running` |
| `error` | dispatch with zero accepted | re-dispatch via `restart_pipeline` |
| `stopped` | `_stop_stream` (row gets `stopped_at`, excluded from `get_active_broadcast_placements`, `db.py:844`) | terminal |

---

## 6. Stale-config adoption policy

**Decision: detect via config hash; on drift, stop the old instance and redispatch with the current config. Adopt-and-mark-drifted is rejected.**

Rationale: a drifted stream silently applies the wrong transforms/filters indefinitely — in a mediation platform that's wrong billing/KPI records with no signal. Stop+redispatch interrupts a healthy stream, but every count=1 source class has restart-tolerant consumption semantics (§4), so the cost is one bounded duplicate window; config currency is the stronger invariant. The comparison is cheap enough to run every reconciler tick, and it **covers broadcast placements for free** (shared code path — the same hazard exists there today, RCA #17's structural analysis applies to both).

### 6.1 Agent-side (additive, backward compatible)

- `ActiveRun` (`agent/server.py:55-69`): new field `config_sha256: str = ""`, set in `/agent/run` from `sha256(req.yaml_text.encode()).hexdigest()[:16]` before loading.
- `_active_run_status` (`agent/server.py:228-249`) includes `"config_sha256": run.config_sha256`.
- `WorkerPool.live_streams()` and `find_pipeline_runs()` pass the key through in their item dicts (both already copy chosen keys — add one).

Missing key (older agent during a rolling upgrade) → `"unknown"` → **never** triggers drift action (fail-open, adopt). Documented.

### 6.2 Manager-side

```python
def _pipeline_config_sha(self, name: str) -> str:   # under lock; sha256(state.yaml_text)[:16]
```

**Check site:** in `PlacementReconciler.run_once`'s live-slot branch (`reconciler.py:151-172`), after a live item is matched to a slot and before committing `status="running"`:

```python
if live_item.get("config_sha256") not in ("", None, "unknown"):
    expected = self._controller.pipeline_config_sha(placement["pipeline_name"])
    if expected and live_item["config_sha256"] != expected:
        self._controller.reconcile_placement_config_drift(placement_group_id)
        continue
```

**Recovery:** `redispatch_broadcast_slot` already dispatches `state.yaml_text` — the *current* config (`controller.py:1484`) — so drift recovery is exactly "stop the old runs, then redispatch":

```python
def reconcile_placement_config_drift(self, placement_group_id: str) -> bool:
    """Config drift on a live placement: stop all slot runs, then redispatch
    each slot with the current YAML (redispatch_broadcast_slot sends the
    current state.yaml_text). Under the RLock; returns True if re-dispatched."""
    with self._lock:
        placement = self._broadcast_placements.get(placement_group_id)
        if placement is None: return False
        name = placement["pipeline_name"]
        if not self.manager.exists(name): return False
        if not self._may_schedule(name): return False        # stopped meanwhile → normal stop path
        state = self.manager.get(name)
        # claim: bump restart counters under the lock so a concurrent reconciler
        # pass cannot also act on this placement
        claims = [(int(s["worker_index"]), f"{s['run_id_prefix']}-r{int(s.get('restart_count',0))+1}",
                   str(s.get("current_run_id") or "")) for s in placement["slots"]]
        ...mark slots "stale" in-memory + DB (visible during the swap)...
    # network I/O outside the lock:
    self._worker_pool.stop_pipeline_runs(name)                 # stops old instances everywhere
    for worker_index, new_run_id, _ in claims:
        self.redispatch_broadcast_slot(placement_group_id, worker_index)  # existing CAS flow
    log WARNING + MGR_RECONCILE_ACTION_TOTAL{action="config_drift_redispatch"}
    return True
```

The existing `redispatch_broadcast_slot` claim/CAS (`controller.py:1448-1521`) already serializes against `_stop_stream`/`delete()` racing the swap. Convergence: the redispatched run carries the new yaml → next tick the live hash matches → stable. A yaml edited *during* the swap triggers at most one additional bounded restart per edit — acceptable and documented.

**When the manager's pipeline is disabled while down:** no drift action — `_stop_stream` on restore is the correct path (the placement-restore branch doesn't redispatch disabled pipelines; drift only applies to live-and-expected-running placements).

---

## 7. Idempotent re-dispatch — the dedupe rules

Five layers, each independently insufficient, together closed:

1. **Entry serialization (B.5 RLock).** Every dispatch entry point (`_start_stream`, `recover_unplaced_stream`, `reconcile_placement_config_drift`) runs under `self._lock`, and `_start_stream` refuses when `name in _stream_run_ids or name in _active_placement_group` (§3.2). No scheduler or boot path can enter twice.
2. **Single redispatch authority.** `PlacementReconciler.run_once` executes serially on one thread (`reconciler.py:118-123`) — there is no concurrent reconciler redispatch within a process.
3. **CAS on commit.** `redispatch_broadcast_slot` claim → dispatch (lock released) → re-check placement/slot still exist before mutating (`controller.py:1454-1521`); `update_placement_slot` re-reads the authoritative slot under the lock (`controller.py:1380-1389`). A `delete()`/`update()` that lands mid-dispatch wins; the stale dispatch result is discarded and the worker-side orphan is cleaned by `stop_pipeline_runs` on the delete path.
4. **Run-id monotonicity.** Redispatch run-ids are `{run_id_prefix}-r{restart_count}` with `restart_count` read-and-incremented under the lock — unique per attempt, never colliding with a live run-id, so the worker's 409 duplicate guard (`agent/server.py:323-327`) is a last resort, never the mechanism.
5. **One active placement row per pipeline (new).** A crash between dispatch and stop could leave two active rows (e.g. stop HTTP failed and the row was never marked). Add to `_record_broadcast_placement`, before `save_broadcast_placement`:

```python
def deactivate_other_placements(self, pipeline_name: str, keep_placement_group_id: str) -> int:
    # UPDATE broadcast_placements SET status='stopped', stopped_at=:now
    # WHERE pipeline_name=:p AND stopped_at IS NULL AND status != 'stopped'
    #   AND placement_group_id != :keep          -- single dialect-free UPDATE
```

Also the run-complete cleanup path is unchanged-correct: `on_worker_run_complete` (`controller.py:843-888`) de-dupes on `run_id` and `_remove_stream_run_id` keeps `_stream_run_ids` slot-synced.

---

## 8. A13 / D.1 ordering — confirmed dependency

**Confirmed: D.1 must land before D.2 merges.** Grounds:

- D.2 *widens the writer population* of the slot-update path: every count=1 stream adds a 30s `on_pipeline_stats` → `update_slot_run_id` write while its placement is `reconciling` (post-restart, the steady state for restored streams until first stats). Today that path is exercised only by broadcast streams.
- `db.update_slot_run_id` (`db.py:884-908`, pre-D.1) was still the A13 read-modify-write: `get_active_broadcast_placements()` → mutate one slot in Python → write back the **whole** `slots_json`. Concurrent writers (stats payloads vs `update_broadcast_placement_status` from `update_placement_slot` / `redispatch_broadcast_slot`) lose updates. The plan's warning is accurate: a lost slot update under D.2 means a lost `current_run_id`/`restart_count`, which feeds run-id monotonicity (§7.4) — a lost update could reuse a run-id and re-trigger the duplicate-redispatch class A13 was reclassified for (RCA cross-cutting table).

**How D.2 uses the D.1-scoped update:** `on_pipeline_stats` keeps calling `update_slot_run_id(placement_group_id, worker_index, …)`; after D.1 that is a per-slot scoped UPDATE with optimistic-concurrency guard (landed on this branch: `expected_run_id` + rowcount), and no whole-`slots_json` RMW from the stats path. **Review checklist item for D.2's PR:** grep the new/changed code paths for `update_broadcast_placement_status(slots=…)` — it must appear only in paths that already hold the authoritative in-memory placement under the RLock (`_stop_stream`, `_record_broadcast_placement`, `_restore_broadcast_placement`, `update_placement_slot`, `redispatch_broadcast_slot`, `reconcile_placement_config_drift`), never from a stats callback operating on a stale copy.

---

## 9. Rollout and rollback

### 9.1 Feature flag

- Env `TRAM_STREAM_SINGLE_PLACEMENT`, values `1` (default, new behavior) / `0` (legacy count=1 path retained verbatim). Read once at controller construction (`PipelineController(..., single_stream_placements: bool | None = None)`, defaulting from env) — wired via `tram/core/config.py` `AppConfig` (pattern: `stats_interval`, `config.py:111`), `tram/api/app.py` construction site, `.env.example`, `docs/deployment.md`, `helm/values.yaml` + manager StatefulSet env (per AGENTS.md: new env vars must update all three).
- Flag off ⇒ §3.2 legacy branch + B.6 guard exactly as shipped; flag on ⇒ §3.2 unified branch + B.6 guard demoted to the no-placement bridge (§5.2).

### 9.2 Migration (no restarts, no schema change)

1. Upgrade the manager image. Existing count=1 streams keep running on workers; no placement rows exist yet.
2. On the **next manager restart** (or immediately, per §5.2's trigger — see below), `_boot_load` → no placement row → `_adopt_live_stream_if_any` fires → **materializes the placement** (§5.2). The stream converts to the new regime with zero interruption.
   - To convert without waiting for a restart: the unplaced-stream pass (§5.3) can also invoke materialization when it sights a live run for a running, placement-less, flag-on stream — same code path, driven by the reconciler's existing 10s tick. **Recommend implementing both triggers** (boot + reconciler); they converge on the same helper and are individually safe.
3. Rollback: set `TRAM_STREAM_SINGLE_PLACEMENT=0`, restart the manager. Placement rows already created for count=1 streams keep functioning (the placement machinery is generic and flag-independent — stop/restore/reconcile don't consult the flag); new dispatches use the legacy path. No data to migrate back. The agent's `config_sha256` field is additive and ignored by older managers.

### 9.3 Ship order (within D.2)

1. Agent `config_sha256` field (additive, no consumer yet) — can ride any release.
2. D.1 (separate item, prerequisite — landed on this branch).
3. D.2 core: dispatch path + materialization + restore assignment fix + `deactivate_other_placements`.
4. D.2 reconciliation: unplaced-stream pass + drift policy.
5. D.3 (view layer) — separate item, after D.2 per the plan.

### 9.4 Exit criteria (from the plan, scripted as a repeatable gate)

Extend `scripts/deploy-kind-tram-dev.sh` (or a sibling script per the plan's test-strategy note) to run, with `TRAM_STATS_INTERVAL=5`:

1. **Traffic block:** deploy a count=1 kafka or sql stream; block manager↔worker network for 2 minutes (e.g. a NetworkPolicy or scale-worker-to-0-and-back equivalent on kind); assert `GET /api/pipelines/{name}/placement` still returns the slot and status stays truthfully `degraded`/`reconciling` — never 404/absent. Placement rows render unconditionally (`_stream_views.py`), so visibility survives stats silence.
2. **Manager restart:** restart the manager StatefulSet while the stream runs; assert exactly one live run on the worker afterwards (`/agent/status` shows the original run_id or an adopted/materialized placement, `restart_count == 0`, no second instance), and the placement row reports `running` after the first stats tick.
3. **Worker death:** `kubectl delete pod` the stream's worker; assert slot → `stale` → redispatch on a healthy worker within the stale window (≤ 3×interval + one pass) and no duplicate consumer group members during the swap.
4. **Dashboard cards increment mid-run** — D.4/D.5's criterion, listed for gate completeness only.

---

## 10. Test plan (deterministic)

New file `tests/unit/test_count1_placement.py` (plus additions to `test_reconciler.py`, `test_pipeline_controller.py`, `test_worker_pool.py`); concurrency cases follow `test_controller_concurrency.py` patterns (mocked `WorkerPool`, real controller + fake DB). All API/integration-tier runs outside the sandbox (AGENTS.md caveat).

**Dispatch (§3):**
- `test_count1_dispatch_creates_placement` — flag on, `_start_stream` for a count=1 stream: placement row in DB with `target_count="1"`, one slot `worker_index=0`, `current_run_id == placement_group_id`, `_stream_run_ids` synced, MGR_DISPATCH_TOTAL labeled `accepted`.
- `test_count1_dispatch_no_capacity_vs_failed` — empty resolve → `no_workers` metric + status `error`; rejected slot → `dispatch_failed` metric + slot `error` persisted (parity with the legacy labels).
- `test_second_start_is_noop` — `_start_stream` twice while active (guard hits `_stream_run_ids` and `_active_placement_group`): exactly one dispatch call.
- `test_flag_off_is_legacy` — flag off: no placement row, legacy `_stream_run_ids` behavior, B.6 tests unchanged-green (regression).

**Boot restore / adoption (§5.1-5.2):**
- `test_boot_restore_adopts_without_redispatch` — placement row + worker still live: restore sets `reconciling`; assert **no** `/agent/run` POST; stats payload (or live probe) transitions to `running`; assignments registered (`stop_run` now reaches the worker).
- `test_boot_restore_worker_dead_redispatches` — placement row, no live run, stats beyond grace: slot → `stale`, `redispatch_broadcast_slot` called with `{prefix}-r1`, `restart_count == 1`, placement `degraded`→`running`.
- `test_restore_grace_no_premature_redispatch` — restored placement, no stats yet, inside `stats_interval+5` grace: no redispatch (guards the 2-minute-block exit criterion).
- `test_adoption_materializes_placement` — B.6 bridge: no placement row, live run sighted, flag on → 1-slot row `status="running"`, `run_id_prefix == adopted run_id`, `adopted: true`.

**Liveness reconciliation (§5.3):**
- `test_unplaced_stream_recovers_after_worker_death` — flag-off adopted stream, two consecutive passes with no live sighting and no fresh stats → `recover_unplaced_stream` re-dispatches (flag) or re-dispatches legacy (flag off); pipeline not stuck `running`.
- `test_unplaced_stream_kept_on_flaky_probe` — live probe empty but fresh stats entry present → no action (dual-signal liveness).
- `test_unplaced_stream_adopts_bookkeeping` — live sighting, missing assignment → assignment repaired, no dispatch.
- `test_unplaced_stream_double_live_run_stops_extra` — two live runs sighted for one count=1 pipeline → earliest kept, other stopped.

**Stale config (§6):**
- `test_config_drift_triggers_stop_and_redispatch` — live run with differing `config_sha256` → old runs stopped, redispatch payload carries the **new** yaml, `restart_count` incremented, action metric fired.
- `test_config_match_adopts` — equal hashes → no stop, no dispatch.
- `test_config_hash_missing_fails_open` — older-agent item without the key → adopt, no action.

**Idempotency / races (§7):**
- `test_delete_races_redispatch` — `delete()` issued while the reconciler is mid `redispatch_broadcast_slot` (dispatch hook blocks): CAS discards the slot commit, no orphan placement row active, `deactivate_other_placements` leaves exactly ≤1 active row per pipeline.
- `test_stats_callback_concurrent_with_restore` — interleaved `on_pipeline_stats` and restore writes: final slot state consistent (relies on D.1; assert no whole-slots RMW call sequence from the stats path — this is also the D.1/D.2 integration canary).
- `test_record_placement_deactivates_stale_rows` — pre-existing second active row for the pipeline is marked stopped by a fresh `_record_broadcast_placement`.

**D8 opportunistic bound (plan line 183):** `test_pipeline_workers_pruned_on_placement_stop` — when a placement transitions to `stopped` (or a slot's assignment completes via `on_run_complete`), `_pipeline_workers[name]` entries not referenced by any active placement slot are dropped.

---

## 11. Observability

- Existing: `MGR_REDISPATCH_TOTAL`, `MGR_PLACEMENT_STATUS`, `MGR_RECONCILE_ACTION_TOTAL{mark_stale|redispatch|resolve_running}`, `MGR_DISPATCH_TOTAL`.
- New labels on `MGR_RECONCILE_ACTION_TOTAL`: `stream_recover` (§5.3), `config_drift_redispatch` (§6.2), `adopt_materialize` (§5.2).
- Logs: every materialization, recovery, and drift swap at INFO/WARNING with `pipeline` extra, matching the existing structured-logging style. The 2-minute-block drill should show the `MGR_STATS_MISSED_TOTAL` WARNING (landed with the agent-side miss tracking, `agent/server.py:163-179`) — visibility into channel loss is part of the exit criterion's spirit.

---

## 12. File-by-file change summary

| File | Change |
|---|---|
| `tram/pipeline/controller.py` | `_is_broadcast_workers` helper; flag-gated `_start_stream` unification (§3.2); `_materialize_placement_from_adoption` (§5.2); assignment registration in `_restore_broadcast_placement` (§5.1); `recover_unplaced_stream`, `reconcile_placement_config_drift`, `stream_liveness_candidates`, `pipeline_config_sha` (§5.3, §6.2); `deactivate_other_placements` call in `_record_broadcast_placement` (§7.5) |
| `tram/agent/worker_pool.py` | pass `config_sha256` through `live_streams()` / `find_pipeline_runs()` items (§6.1); prune `_pipeline_workers` on placement stop / run-complete (D8, §7) |
| `tram/agent/server.py` | `ActiveRun.config_sha256` + expose in `_active_run_status` (§6.1) — additive |
| `tram/agent/reconciler.py` | `_reconcile_unplaced_streams` + hysteresis state (§5.3); drift check in the live-slot branch (§6.2) |
| `tram/persistence/db.py` | `deactivate_other_placements` (§7.5); D.1's scoped `update_slot_run_id` lands separately **before** this |
| `tram/core/config.py`, `tram/api/app.py` | `TRAM_STREAM_SINGLE_PLACEMENT` flag plumbing (§9.1) |
| `helm/values.yaml`, manager StatefulSet, `.env.example`, `docs/deployment.md` | document the flag (§9.1) |
| `scripts/` | kind-based exit-criteria gate script (§9.4) |
| `tests/unit/…` | per §10 |

**Not changed:** `broadcast_placements` schema; routers (`pipelines.py` placement endpoint already serves any placement — the `worker_pool is None` gate is standalone-only and is D.3's scope); `StatsStore`; `multi_dispatch` internals; batch paths; standalone mode.

---

## 13. Risks and open edges

- **`_select_replacement_worker` with no healthy workers** (all-worker outage): slot stays `stale`, placement `degraded`, no redispatch — recovery happens when any worker returns. Correct, but the placement view must make `degraded` loud (D.3 scope).
- **Drift redispatch during rapid config churn** (§6.2): bounded at one extra restart per edit; acceptable, documented.
- **Rolling upgrades with mixed agents:** older workers don't report `config_sha256` → drift detection silently off for their runs until upgraded (fail-open). One-line check in the upgrade runbook: agents first, managers second — same ordering Wave C prescribes for the API-key rollout.
- **`adopted: true` slots skip the first-stats grace** (§5.2): safe because the guard probed the run live before materializing; a run that dies in the next second is caught by the normal stale window.
- **Reconciler tick cost:** unchanged — live probes are per-worker (not per-stream), and the unplaced-stream pass reuses the snapshot already fetched by `run_once`.
- **`recover_unplaced_stream` vs a just-materialized placement:** guarded by the `_active_placement_group` re-check under the lock; the reconciler's next pass sees `has_placement=True` and drops the hysteresis counter.
