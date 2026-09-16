# Counter Delta & Windowed Aggregation — Design (F.1)

**Status:** proposed (requires approval before implementation)
**Branch:** `wave-a-stopgaps` · **Plan ref:** `docs/plans/issue-implementation-plan.md` Wave F, item F.1 (W-5.1)
**Sources:** `docs/reviews/telecom-domain-review.md` (counter finding — the #1 ranked domain gap) · plan Wave F table · code as of commit `2614c97`
**Prerequisites:** none hard; soft-depends on the E.2 `_upsert` helper (already landed, `370fddc`) and reuses the D.2 config-hash convention (`33e38b5` §6.1 pattern).

---

## 0. Problem statement

TRAM's SNMP poll source correctly fetches raw Counter32/Counter64 values and even classifies them into metrics (`tram/connectors/snmp/source.py:418-454`), but **no transform computes `v_now − v_prev` with wrap correction** — the defining feature of PM mediation. Every PM platform computes per-interval deltas and rates from cumulative counters before a KPI is meaningful; TRAM forces users to compute them downstream, so "erlangs per cell per 15 minutes" is not expressible in-platform. Likewise, the existing `aggregate` transform is batch-local only (`tram/transforms/aggregate.py:69-106`): it collapses the records of one chunk and forgets everything — there is no time-windowed aggregation with watermark/late-arrival semantics, so a 15-min PM period spanning multiple polls cannot be computed.

Both features need **previous-value state that outlives a single chunk**. That requirement collides with TRAM's execution architecture in a non-obvious way (§3.1), which is the real design problem F.1 must solve. The plan sketch ("`rate`/`delta` transform with Counter32 wrap correction + windowed aggregate backed by a state store", plan line 165) already names the shape; this design resolves the state store placement, the counter/window semantics, and the execution-mode constraints.

## 1. Goals and non-goals

**Goals**

- `counter_delta` transform: per-key `v_now − v_prev` with Counter32 wrap correction, reboot/reset detection, optional rate, correct across polls in every supported execution mode.
- `window_aggregate` transform: tumbling time windows (15-min telecom standard) keyed by event time, with bounded lateness, watermark-driven finalization, and a defined flush story.
- State placement that is correct in: standalone (all schedule types) and manager+worker mode for interval/cron and count=1 stream pipelines — with every other combination explicitly rejected or documented, never silently wrong.
- Restart semantics bounded and documented: what is lost on manager restart, worker restart, stream redispatch, and config update.
- Config surface following the existing transform conventions (`models/pipeline.py` typed configs, `extra: "forbid"`, `docs/transforms.md`).
- Deterministic unit/integration test plan covering wrap, reset, partition, and restart cases.

**Non-goals**

- Sliding/hopping windows, session windows, event-time reordering of arbitrary streams — tumbling windows only (the telecom PM use case).
- A general KPI-formula library or unit management (telecom review's separate roadmap item).
- Backfill of polls missed during outages (telecom review: "polls lost in an outage are gone forever"). This design makes the *delta* over an outage correct (counters are cumulative); the KPI *windows* still have gaps. That is a scheduling property, not a transform property.
- Manager HA / multi-manager state contention — single-manager architecture, same assumption as `broadcast_placements` and `queued_runs`.
- Counter64 in the SNMP trap *sink* (parked F-wave hardening item).
- Making `workers: count=N` batch runs meaningful (see §3.4 — batch dispatch is already single-slot today; changing that is out of scope).

## 2. Current data shapes (what the sources actually emit)

Verified by reading the connectors; the transforms must consume exactly these.

**SNMP poll, `classify: true` + `yield_rows: true`** (the shape that preserves per-interface counters — one record per table row, all rows yielded as one JSON array payload so the executor processes them as one chunk, `source.py:630-648`):

```json
{
  "_metrics":  {"ifInOctets": 4135975078, "ifOutOctets": 2295619590, "ifSpeed": 1000000000},
  "_labels":   {"ifDescr": "eth0", "ifType": "ethernetCsmacd"},
  "_index":    "1",
  "_polled_at": "2026-09-16T09:15:02.114321+00:00"
}
```

- `_polled_at` is stamped once per `read()` call (`source.py:595`), i.e. per poll — this is the collection timestamp for every row of that poll.
- `_classify_bindings` (`source.py:424-454`) **discards the SNMP type name** after converting to `int` — the record carries no evidence whether `ifInOctets` is Counter32 or Counter64. F.1 adds a small additive source change to recover it (§4.4).
- Note (pre-existing sharp edge, documented not fixed here): `classify: true` **without** `yield_rows` collapses all table rows into one record (base-name collision in `_classify_bindings` at `source.py:437-441`). Counter pipelines must use `yield_rows: true`.

**SNMP poll, plain mode** — flat `{oid_or_name: value, "_polled_at": ...}` or per-row `{col: val, "_index": ..., "_polled_at": ...}` (`source.py:655-685`).

**gNMI** — per-update records with the device timestamp as nanosecond epoch int (`gnmi/source.py:76-90`):

```json
{"path": "/interfaces/interface[name=eth0]/state/counters/in-octets",
 "val": {"uint64": 4135975078}, "timestamp": 1760613302114321000}
```

Both timestamp forms parse with the existing `_parse_timestamp` (`tram/transforms/timestamp_normalize.py:23-91`, sec/ms/us/ns auto-detection) — **reused, not re-implemented**. Dotted field access (`_metrics.ifInOctets`) is available via `tram/transforms/path_utils.py`. Per-chunk runtime metadata (source host, operation) already reaches transforms that opt in via `set_runtime_meta` (`executor.py:304-309`).

**Execution facts that drive the design** (evidence for §3.1):

- Transforms are built **once per run** (`executor.py:834` batch, `:1133` stream) and reused across all chunks of that run — transform-local state survives across chunks but not across runs.
- Interval/cron/manual ticks are **separate runs**: the controller's `_run_batch` claim (`controller.py:954-981`) executes or dispatches one run per tick. In manager+worker mode the dispatch always goes through `dispatch_with_result`, which **hardcodes `WorkersConfig(count=1)`** (`worker_pool.py:798-805`) — and `resolve()` picks the **least-loaded healthy worker with no affinity** (`worker_pool.py:400-408`). A pipeline's consecutive ticks can land on different workers; even the same worker rebuilds transforms per run.
- Stream runs hold one transform set for the process lifetime (`executor.py:1133`, `controller.py:1536-1555` local / agent stream thread `agent/server.py:403-447`). A stopped SNMP-poll stream *ends* (finite `read()`), so poll sources are interval-scheduled in practice.
- `multi_dispatch` (`worker_pool.py:665-756`) posts the **entire pipeline** to N worker slots — there is no record-level partitioning at dispatch time. With `count > 1`: Kafka spreads *partitions* across the N full-pipeline consumers (each worker sees a disjoint, arbitrary subset of the stream); push sources (syslog/trap NodePort) load-balance connections (disjoint subsets, no key affinity); gNMI/CORBA each open their own upstream session (**duplicated** streams). None of these guarantee that the same counter key lands on the same worker on consecutive polls.

## 3. State placement — the core decision

### 3.1 The constraint, stated explicitly

Counter delta and windowed aggregation require `previous value + timestamp` per counter key. Where that state lives determines correctness per execution mode:

1. **Stream run, single slot** — transform-local in-memory dict is correct for the run's lifetime (`executor.py:1133` builds once; the run never rebuilds).
2. **Interval/cron/manual batch runs** — transform-local state is **useless**: every tick is a fresh run with freshly-built transforms, in manager+worker mode potentially on a different least-loaded worker (`worker_pool.py:402-408`). The primary use case (SNMP poll, interval schedule) lives exactly here.
3. **Broadcast dispatch (`count>1`/`all`/`list`, streams)** — each worker sees a partial (Kafka/syslog) or duplicated (gNMI) stream with **no key affinity**. Per-worker local state computes silently wrong or duplicated deltas. There is no dispatch key to partition on — `multi_dispatch` dispatches whole pipelines, not records.

### 3.2 Decision

**A per-pipeline durable state blob, accessed through a small store interface, plus strict mode gating.** Four parts:

**(a) New `transform_state` table** (broadcast-placement conventions — TEXT columns, ISO timestamps, `CREATE TABLE IF NOT EXISTS`, E.2 `_upsert` for writes):

```sql
CREATE TABLE IF NOT EXISTS transform_state (
    pipeline_name TEXT PRIMARY KEY NOT NULL,
    state_json    TEXT NOT NULL,           -- {state_key: transform-specific blob}
    config_sha256 TEXT NOT NULL,           -- D.2 6.1 convention
    updated_at    TEXT NOT NULL,
    updated_by     TEXT NOT NULL           -- run_id (audit)
)
```

One row per pipeline holding a JSON blob of every stateful transform's state, keyed by a stable `state_key` (transform type + position in the transforms list). **Why a single blob, not per-key rows:** write rate. A router walk with 1,000 interfaces × 4 configured counter fields = 4,000 keys; per-key upserts at a 5-min poll = 48,000 rows/hour/pipeline, each a dialect-aware upsert on the manager DB. A blob is **one row per pipeline per tick** (~12/hour) carrying ~200-250 KB of JSON (≈50 B/key) — ~0.7 KB/s mean, two HTTP requests of bounded size per tick. A 50k-key walk yields a ~2.5 MB blob: acceptable, and the mitigation is explicit — only *configured* counter fields are keyed, so users bound the blob by listing what they need. (If this ever becomes hot, the follow-up is per-key columnar compaction — open question 2.)

**(b) `TransformStateStore` interface with two implementations:**

- `DbTransformStateStore(db)` — standalone mode. The controller already owns the `TramDB` handle; the executor gains an optional `state_store` constructor param (like `file_tracker`, `executor.py:201`).
- `HttpTransformStateStore(manager_url, api_key)` — worker mode. The agent already knows `TRAM_MANAGER_URL` (`agent/server.py:266-276`) and already authenticates to `/api/internal/*` with the internal API key (the same auth mode `run-complete` and `pipeline-stats` use, `api/routers/internal.py:54,95`, `middleware.py:30-47`).

New internal endpoints on the existing internal router: `GET /api/internal/transform-state/{pipeline}` and `PUT /api/internal/transform-state/{pipeline}` (body: the blob + `config_sha256`).

**Why this does not add a new SPOF:** in worker mode a run can only exist because the manager dispatched it (`/agent/run`). A worker that cannot reach the manager would never have started the run. The state GET/PUT rides the same availability envelope as dispatch itself. A PUT failure at run end is logged and swallowed (the counter property below bounds the damage).

**(c) Load/save points in the executor** — the only places transforms are built:

- `batch_run`: load once at run start (before the chunk loop), hydrate stateful transforms (`set_state(blob)`), **save only on the success path**. A failed run persists nothing — the retry rebuild (`executor.py:882-900`) re-hydrates from the *same in-run snapshot*, so a failed attempt's partial writes are discarded. This is safe for counters for a beautiful domain reason: **counters are cumulative**, so a lost state update merely makes the next delta span a longer interval (a correct, longer-window average) — never a wrong value. For `window_aggregate` a lost update re-accumulates the open window from the last good snapshot (undercount bounded by one poll; documented).
- `stream_run`: load at start; **`persist_interval_s` (default off)** periodically PUTs the blob (timed with the chunk loop, not a new thread); save in `finally` on graceful stop. Watermark and windows live in the blob, so a D.2 redispatch that hydrates recovers the open window up to the last snapshot.

**(d) Config-currency guard (D.2 §6.1 pattern):** the row carries `config_sha256` (sha256 of the pipeline YAML, computed exactly as D.2 6.1 does for agents). Hydration discards the blob on mismatch (→ one first-sight interval after a config change). Belt and braces: `controller.update()` deletes the row outright — a changed transform list may change key semantics, and stale keys are more dangerous than one re-primed interval.

### 3.3 Single-writer argument

One active run per pipeline is already an invariant: the `_run_batch` status claim (`controller.py:964-971`, extended by E.2 to treat `queued` like `running`) and the D.2 placement CAS for streams. Therefore at most one run reads-modifies-writes a pipeline's state blob — no merge protocol, no versioning beyond `config_sha256`. The PUT overwrites the row; last writer wins by construction because there is only ever one writer.

### 3.4 Rejected alternatives (and why)

| Alternative | Why rejected |
|---|---|
| Transform-local in-memory dict, all modes | Silently wrong for interval polls (fresh transforms per tick, no worker affinity) — the main use case. |
| Worker-local SQLite/file state keyed by pipeline | Breaks on least-loaded rotation (`worker_pool.py:402-408`): worker B has no visibility of worker A's state → wrong deltas with no signal. Fixing it needs dispatch affinity — a larger change to `resolve()`/`multi_dispatch` than the state store itself. |
| Per-key DB rows | 10³–10⁵ upserts/hour/pipeline on the manager DB; the blob is 2 requests + 1 row per tick (§3.2a). |
| State embedded in the dispatch/callback envelopes only | Saves two HTTP calls but couples state to the dispatch machinery and leaves streams without a periodic channel; the dedicated endpoint serves both. (Envelope piggyback remains a possible optimization later.) |
| Partition-aware state (hash counter keys to workers) | Requires a record-routing layer that does not exist — `multi_dispatch` sends whole pipelines. Building it is far beyond F.1; instead broadcast+stateful is rejected (§6). |

**Note on batch `workers: count=N`:** batch runs are *already* single-slot regardless of the pipeline's `workers` config (`dispatch_with_result` hardcodes `count=1`, `worker_pool.py:798-805`). F.1 does not change that; it documents it, because it is what makes interval-mode state tractable.

## 4. Counter semantics (`counter_delta`)

### 4.1 Delta and wrap math

For a counter sample `(v_prev, t_prev)` → `(v_now, t_now)` with counter width `W` (2³² or 2⁶⁴):

```
raw   = (v_now - v_prev) mod W          # wrap-corrected difference
delta = raw if v_now >= v_prev else (   # no decrease → plain difference
           v_now + W - v_prev            # wrap path — same value as raw
       )
```

- **No decrease** → `delta = v_now − v_prev`. Counter64 never wraps in practice; a decrease can only be a reset (below).
- **Decrease** → the wrap-corrected candidate `v_now + W − v_prev`. **Wrap vs. reset is distinguished by gap size:** a genuine wrap between two polls yields a *small* corrected delta (≈ rate × interval). A device reboot resets the counter to near zero, making the corrected delta huge (≈ W). Rule (rrdtool semantics): if the corrected delta exceeds `reset_threshold × W` (default `0.5`, i.e. > W/2), classify as **reset**: `delta = v_now` (bytes accumulated since boot — the standard, honest value), set `_counter_reset: true` on the record, increment `TRANSFORM_COUNTER_RESETS_TOTAL`. Otherwise it is a **wrap**: corrected delta stands, increment `TRANSFORM_COUNTER_WRAPS_TOTAL`.
- **Missed polls do not need special handling** — cumulative counters make the delta over any gap exact; the *rate* uses the actual elapsed time, so it is a correct long-window average. Optional `max_gap_seconds` (default `None`): if `t_now − t_prev` exceeds it, emit `delta = v_now`-style reset treatment (a device that was down and rebooted during the gap otherwise yields a plausible-but-wrong wrap-corrected delta). Operators with known outage windows set this to ~2× poll interval; default off because counters are self-correcting.
- **Rate** = `delta / (t_now − t_prev)` seconds (per-second units), from the timestamps actually on the records (§2): `timestamp_field` accepts a string or list of candidates, default `["_polled_at", "timestamp"]` (SNMP first, gNMI second), parsed with the reused `_parse_timestamp`. A missing/unparseable timestamp follows the transform `on_error` policy (`raise | null | keep`, same contract as `timestamp_normalize`).

### 4.2 Keys and first sight

- **Counter identity** = (source identity, `key_fields` values, field path). `key_fields` (default `["_index"]`) covers the table row; source identity comes from the per-chunk runtime meta (`source_host` for SNMP, `source.py:618-625`) via the existing `set_runtime_meta` hook — so two hosts polled by the same pipeline never cross-contaminate. A record missing a key field follows `on_error`.
- **First sight** (no `v_prev`): default **pass-through** with `delta`/`rate` set to `None` — keeps the record schema uniform, preserves label context downstream, and lets `window_aggregate` simply skip nulls. `first_sample: "drop"` is available for KPI-only streams. Justification for pass-by-default: a dropped first sample hides the counter's existence and label context from every downstream consumer for one interval; a null-valued sample is filterable by one `filter_rows` condition if unwanted.

### 4.3 Output shape

Per configured field `f` (dotted paths supported): `f_delta` and/or `f_rate` set alongside the original (`keep_raw: true` default; `false` drops the raw cumulative value after the delta is computed — usually wanted to avoid double-counting downstream). Example: `_metrics.ifInOctets` → `_metrics.ifInOctets_delta`, `_metrics.ifInOctets_rate`. `output: "delta" | "rate" | "both"` (default `both`).

### 4.4 Width: authoritative where possible, heuristic otherwise

The SNMP source discards the type name (`source.py:439-441`), so **F.1 adds a small additive source change**: `_classify_bindings` also emits `_snmp_widths: {field: 32|64}` for Counter32/Counter64 fields (the type name is in hand at `source.py:436`). `counter_delta` consumes it when present → authoritative.

Fallback `width: "auto"` (the default) heuristic: if either `v_prev` or `v_now` ≥ 2³² → 64, else 32. Documented edge (accepted, avoidable with explicit `width: 64`): a 64-bit counter reset where both values sit below 2³² can be misread as a 32-bit wrap if the corrected delta lands under 2³¹ — the wrap correction then produces a spike instead of a reset. gNMI counters (`uint64` val) are always width 64.

## 5. Windowed aggregation (`window_aggregate`)

Separate transform, not folded into `counter_delta` — the composable mediation pattern (`counter_delta` per poll → `window_aggregate` over the rate samples) and disjoint state payloads. It reuses the `aggregate` transform's operation parser (`_SUPPORTED_OPS`, `aggregate.py:12`: sum/avg/min/max/count/first/last).

- **Windows:** tumbling, **epoch-aligned UTC** (`window_seconds`, default 900 — the 3GPP 15-min PM standard). A record at 23:47:12 belongs to 23:45:00–00:00:00. Alignment choice (epoch vs. first-record) is deliberate: telecom PM periods are wall-clock aligned (TS 32.411 granularity periods); first-record alignment would drift with restarts.
- **Event time** from `timestamp_field` (same candidate-list default as `counter_delta`).
- **Lateness:** `allowed_lateness_seconds` (default 60). Watermark = max event timestamp observed − allowed lateness. A window finalizes (emits with `window_complete: true`) when the watermark passes `window_end`. Records arriving for a finalized window are dropped and counted in `TRANSFORM_WINDOW_LATE_DROPPED_TOTAL` (policy `on_late: "discard"`; no update of emitted windows — sinks are append-only).
- **Open windows live in the state blob** — in interval mode they round-trip tick to tick (§3.2c), accumulating partial aggregates (not raw samples: per group+window, keep only the op-relevant accumulators — running sum/count for avg, max, last, etc. — so the blob stays O(groups × windows × ops), not O(samples)).
- **Output record shape:**

```json
{"ifDescr": "eth0", "_index": "1",
 "window_start": "2026-09-16T09:15:00Z", "window_end": "2026-09-16T09:30:00Z",
 "mean_rx_rate": 84213.7, "peak_rx_rate": 91002.1, "last_rx_rate": 88450.0,
 "sample_count": 4, "window_complete": true}
```

- **Flush-on-stop semantics** (a stopped pipeline must not silently lose its open window):
  - **Batch/interval:** open windows persist in state — they finalize naturally when the pipeline resumes and the watermark advances. If the pipeline is **disabled/deleted** with a window open, that window is lost — **bounded by one window per group, documented** — with two escapes: (1) `flush_on_close: false` default for batch runs means the executor never flushes partials per tick (flushing per tick would emit a partial window record and then *re-emit* the same window after state rehydration — double counting; this is why the default is off), and (2) an explicit **manual flush run**: `POST /api/pipelines/{name}/run?flush=true` — a normal manual run (E.2 machinery intact) whose `RunRequest` carries `flush: true` (`agent/server.py:40` gains an additive field); the executor passes it down and stateful transforms' close emits open windows with `window_complete: false` **and clears them from the saved state**. Standalone mode takes the local-path equivalent.
  - **Stream:** `flush_on_close: true` default — the stop event is graceful and there is no next tick, so `stream_run`'s `finally` calls a new optional transform `close()` hook (mirroring the sink `close()` contract, `executor.py:331-351`) which emits the partial windows before the final state PUT. Ungraceful stream death (crash, OOMKill) loses the open window minus whatever `persist_interval_s` snapshotted — the documented bound; a D.2 redispatch hydrates from the last snapshot.

## 6. Execution-mode compatibility matrix

| Mode | counter_delta | window_aggregate | Mechanism |
|---|---|---|---|
| Standalone, interval/cron | ✅ correct | ✅ correct | DB state round-trip per tick (`DbTransformStateStore`) |
| Standalone, stream | ✅ correct in-memory | ✅ + flush on graceful stop | transform-local dict (run-lifetime); `persist_interval_s` optional |
| Standalone, manual | ✅ (stateless single poll: first-sight or flush run) | flush runs only | — |
| M+W, interval/cron/manual batch | ✅ correct | ✅ correct | HTTP state round-trip per run; batch dispatch is single-slot by construction (§3.4) |
| M+W, stream `count=1` | ✅ correct in-memory; redispatch → first-sight gap or hydration | ✅ same | D.2 1-slot placement; `persist_interval_s` bounds redispatch loss |
| M+W, stream `count>1` / `all` / `list` | ❌ **rejected** | ❌ **rejected** | Partial/duplicated stream per worker, no key affinity (§3.1.3). Runtime guard in `_start_stream` when `worker_pool is not None` → pipeline `error` with an explicit message; linter rule warns at `tram validate` (validation cannot know deployment mode) |
| Any mode, `thread_workers > 1` + stateful transform | ❌ **rejected at validation** | ❌ same | Chunks process concurrently and possibly out of order → `v_now` can pair with an older `v_prev`. Deterministic `PipelineConfig` validator error. Cost is nil in practice: counter pipelines are single-chunk per poll (SNMP yields one payload per `read()`). |
| Stateful transform in sink-level `transforms` | ❌ **rejected at validation** | ❌ same | Per-sink transforms would fork state per sink; the typed config validator rejects the types there |

## 7. Config surface

Following `models/pipeline.py` conventions — typed Pydantic configs, `extra: "forbid"`, added to the `TransformConfig` union (line 665):

```yaml
transforms:
  - type: counter_delta
    fields: ["_metrics.ifInOctets", "_metrics.ifOutOctets"]   # required, dotted paths
    key_fields: ["_index"]                # counter series identity (+ source host from meta)
    timestamp_field: ["_polled_at", "timestamp"]  # str or list; default shown
    width: "auto"                          # "auto" | 32 | 64  (SNMP _snmp_widths wins when present)
    output: "both"                         # "delta" | "rate" | "both"
    keep_raw: true
    first_sample: "pass"                   # "pass" | "drop"
    reset_threshold: 0.5                  # wrap-corrected delta above this × width ⇒ reset
    max_gap_seconds: null                  # optional outage guard (§4.1)
    on_error: "raise"                      # "raise" | "null" | "keep" — existing convention

  - type: window_aggregate
    window_seconds: 900                    # telecom 15-min default
    allowed_lateness_seconds: 60
    timestamp_field: ["_polled_at", "timestamp"]
    group_by: ["_labels.ifDescr", "_index"]
    operations:                            # aggregate-transform spec syntax, reused
      mean_rx_rate: "avg:_metrics.ifInOctets_rate"
      peak_rx_rate: "max:_metrics.ifInOctets_rate"
    flush_on_close: false                  # batch default; streams default true (§5)
```

`CounterDeltaTransformConfig` / `WindowAggregateTransformConfig` carry these fields; new `PipelineConfig` model validators implement the §6 rejections (thread_workers, sink-level, plus the stateful-type registry list). `docs/transforms.md` and the README transform catalog gain both entries; `docs/connectors.md` documents `_snmp_widths`.

## 8. Restart semantics — bounds and mitigations

| Event | counter_delta | window_aggregate |
|---|---|---|
| Manager restart (batch mode) | No gap — state is in the DB; polls missed while the manager was down produce a delta spanning the outage (correct, long-window average) | Open windows restored from DB; the outage interval remains a sample gap (inherent to poll loss) |
| Worker restart / death | No impact — state is not worker-resident (least-loaded rotation safe) | Same |
| Stream redispatch (D.2 stale slot) | One first-sight interval per key — unless `persist_interval_s` hydrated the last snapshot | Open window lost back to the last snapshot |
| Pipeline update (config change) | State row deleted (§3.2d) → one first-sight interval per key | Open window lost (bounded by one window per group) |
| Batch run failure / retry | Failed attempt never PUTs; counters self-heal over the longer interval (§3.2c) | Re-accumulates from the last good snapshot (undercount ≤ one poll's samples, documented) |
| Manager down entirely | No runs are dispatched at all (pre-existing platform property); the next poll's delta still spans the outage correctly | Window sample gaps only |

Write-rate justification for the DB (per pipeline): interval mode = 1 GET + 1 PUT + 1 row upsert per tick (e.g. 12/h at a 5-min poll); stream mode = ≤ 1 PUT per `persist_interval_s` (only when configured). The blob size bound and its mitigation are in §3.2a.

## 9. Rollout and rollback

- **Feature flag `TRAM_STATEFUL_TRANSFORMS`** (default ON, `TRAM_*` pattern from D.2 9.1 — plumbed through `tram/core/config.py`, `api/app.py`, `.env.example`, `docs/deployment.md`, `helm/values.yaml` + manager/worker envs per AGENTS.md). Flag OFF = transforms fail validation with "stateful transforms disabled", the internal endpoints 404, and the `_start_stream` guard is inert. Rationale for default-ON despite house style: the surface is strictly opt-in (new transform types nobody's YAML references); existing pipelines are untouched. The flag exists to revert the *guard on `_start_stream`* (the only line this feature adds to an existing hot path) without an image rollback.
- **Schema:** one additive `CREATE TABLE IF NOT EXISTS` in `_create_tables` (`db.py:71` convention). No migration, no column changes.
- **Rollback:** set flag `0` (immediate, no restart of workers required — dispatch guard and endpoints vanish; pipelines using the transforms go to `error` with a truthful message), or redeploy the previous image; `DROP TABLE transform_state` is optional cleanup. The `_snmp_widths` source field is additive and ignored by older stacks.
- **Ship order:** (1) state store + endpoints + table; (2) `counter_delta` (incl. SNMP `_snmp_widths`); (3) `window_aggregate` + flush run; (4) guards/linter/docs. Steps 2 and 3 are separable reviews.

## 10. Test plan (deterministic)

New files `tests/unit/test_counter_delta.py`, `tests/unit/test_window_aggregate.py`, `tests/unit/test_transform_state.py`; additions to `test_executor.py`, `test_agent_server.py`, `test_api_internal.py`, `test_pipeline_controller.py`, linter tests. API-tier runs outside the sandbox (AGENTS.md caveat).

**Counter semantics (§4):**
- `test_wrap32_correction` — prev=4294967190, now=100, width 32 → delta=206; wrap metric +1.
- `test_wrap_vs_reset_by_gap_size` — decrease with corrected delta < W/2 → wrap; corrected delta > W/2 → reset (`delta = v_now`, `_counter_reset: true`, reset metric +1).
- `test_counter64_reset` — decrease with width 64 → reset (no wrap possible).
- `test_rate_uses_actual_elapsed` — jittered intervals (295s, 305s) → exact rates.
- `test_first_sample_pass_and_drop` — both policies; null delta/rate on pass.
- `test_key_isolation` — two `_index` values + simulated second `source_host` via runtime meta never cross-contaminate (the classic mediation bug).
- `test_width_snmp_authoritative` — `_snmp_widths` present wins; heuristic fallback; the documented 64-as-32 misread edge asserted as a known behavior with explicit-width escape.
- `test_missing_timestamp_on_error_policies` — raise/null/keep parity with `timestamp_normalize`.

**Windows (§5):**
- `test_window_alignment_epoch_utc` — 23:47:12 sample lands in the 23:45 window.
- `test_finalize_on_watermark` — record at `window_end + lateness + ε` closes the prior window.
- `test_late_within_lateness_included` / `test_late_after_finalize_dropped_and_counted`.
- `test_state_carries_open_windows_across_ticks` — two `batch_run` calls over a fake `DbTransformStateStore`: samples from both ticks land in one emitted window.
- `test_stream_close_flushes_partial` (`flush_on_close: true`) and `test_batch_close_does_not_flush` (default; no partial per tick, no double emission after rehydration).
- `test_flush_run_emits_and_clears` — `?flush=true` manual run emits `window_complete: false` and the saved state has the windows cleared.
- `test_group_by_isolation` and `test_accumulator_not_samples` (blob size O(groups×ops)).

**State / restart (§8):**
- `test_state_roundtrip_two_runs` — consecutive batch runs produce a correct second delta.
- `test_retry_rehydrates_from_same_snapshot` — failed attempt never PUTs; rebuild uses the in-run snapshot.
- `test_config_sha_mismatch_discards_state` — hydrate drops the blob (D.2 6.1 pattern).
- `test_update_deletes_state_row`.
- `test_stream_persist_interval_hydrates_redispatch` — snapshot at t, redispatch at t+Δ, first delta correct from snapshot.
- `test_http_store_auth_and_failure` — internal API key used; PUT failure logged, run result unaffected.

**Mode gating (§6):**
- `test_validation_rejects_thread_workers_gt_1`, `test_validation_rejects_stateful_in_sink_transforms`.
- `test_start_stream_guard_rejects_broadcast_with_stateful` — manager mode, count=2 stream → `error` with message; standalone (`_worker_pool is None`) starts fine.
- `test_batch_dispatch_ignores_broadcast` — documents count=N interval pipelines still dispatch single-slot (pre-existing behavior, now asserted).
- `test_flag_off_disables_transforms_and_endpoints`.

**Observability:** `TRANSFORM_COUNTER_WRAPS_TOTAL{pipeline,field}`, `TRANSFORM_COUNTER_RESETS_TOTAL{pipeline,field}`, `TRANSFORM_WINDOW_LATE_DROPPED_TOTAL{pipeline}`, `TRANSFORM_WINDOWS_EMITTED_TOTAL{pipeline,complete|partial}`, `TRANSFORM_STATE_IO_TOTAL{op=get|put, result=ok|error}` (worker/standalone side).

## 11. File-by-file change summary

| File | Change |
|---|---|
| `tram/transforms/counter_delta.py` | new — transform + wrap/reset logic |
| `tram/transforms/window_aggregate.py` | new — transform + watermark/accumulator logic |
| `tram/transforms/stateful.py` | new — `StatefulTransform` protocol (`state_key`, `get_state`, `set_state`, `close(flush)`) |
| `tram/pipeline/executor.py` | `state_store` ctor param; hydrate/save in `batch_run`/`stream_run`; transform `close()` in finally; flush plumbing |
| `tram/pipeline/state_store.py` | new — `DbTransformStateStore` + `HttpTransformStateStore` |
| `tram/persistence/db.py` | `transform_state` table + `load_transform_state` / `save_transform_state` (via E.2 `_upsert`) |
| `tram/api/routers/internal.py` | GET/PUT `/api/internal/transform-state/{pipeline}` |
| `tram/api/routers/pipelines.py` | `?flush=true` on the run endpoint |
| `tram/agent/server.py` | `RunRequest.flush` field; construct `HttpTransformStateStore` (manager URL already known); pass to executor |
| `tram/pipeline/controller.py` | pass `DbTransformStateStore` in standalone; delete state row in `update()`/`delete()`; `_start_stream` broadcast guard (flag-gated) |
| `tram/connectors/snmp/source.py` | additive `_snmp_widths` in `_classify_bindings` (both call sites) |
| `tram/models/pipeline.py` | `CounterDeltaTransformConfig`, `WindowAggregateTransformConfig`, union entry, mode validators |
| `tram/pipeline/linter.py` | new rule: stateful transform + broadcast stream warning |
| `tram/metrics/registry.py` | §10 observability metrics |
| `docs/transforms.md`, `docs/connectors.md`, `docs/deployment.md`, `.env.example`, `helm/values.yaml` + manager/worker env | flag + docs |
| `tests/unit/…` | per §10 |

## 12. Open questions

1. **Stats-payload piggyback for stream persistence** — fold the periodic stream state PUT into the existing 30s `pipeline-stats` postback instead of a separate timer? Saves one HTTP channel but inflates stats payloads; recommend keeping the dedicated PUT for v1 and revisiting if the channel count bothers anyone.
2. **Blob compaction for very large walks** — 50k keys ≈ 2.5 MB/tick is fine on kind, possibly not at fleet scale. Per-key columnar encoding (arrays, not dicts) is the natural follow-up; deferred until a real deployment hits it.
3. **Window alignment to NE-local time** — epoch-UTC alignment is chosen (§5); an `align_timezone` knob would interact with the parked F.4 source-timezone work. Revisit after F.4 lands.
4. **`max_gap_seconds` default** — counters self-correct across gaps, so the guard is optional; whether an operator-sensible default (e.g. 2× interval) should be recommended in docs vs. enforced deserves one round of operator feedback.
5. **Dispatch affinity for stateful batch pipelines** — pinning interval dispatches to the `updated_by` worker would enable a worker-local cache and eliminate the HTTP round-trip. Not needed at current volumes (§3.2a write rates); noted as the escape hatch if the state endpoint ever shows up in profiles.

---

Key design decisions for the approver: **state placement** — interval ticks are fresh runs dispatched to a *least-loaded, non-affine* worker, so transform-local state is useless in exactly the mode SNMP polling lives in; the design routes state through a per-pipeline JSON blob in a new `transform_state` table, reached via `TramDB` directly in standalone mode and via two internal-API calls per tick in worker mode (no new availability dependency — a worker run cannot exist without a manager dispatch). **Broadcast rejection, not partition-awareness** — `multi_dispatch` sends whole pipelines with no record-level key, so `count>1` + stateful transform is rejected at runtime/lint rather than silently computing wrong deltas; `thread_workers>1` + stateful is rejected at validation because chunk ordering isn't guaranteed. **A domain property does heavy lifting** — cumulative counters make lost state updates self-healing (deltas span the gap correctly), which lets "save only on success, best-effort PUT" be safe without any transactional protocol. **One small source fix unlocks correctness** — `_classify_bindings` discards the SNMP type name, so wrap width can't be inferred authoritatively today; the additive `_snmp_widths` record field fixes that for the primary counter source.
