# TRAM Issue Tracker — Consolidated Summary

> **Status (2026-09-23): historical snapshot from 2026-09-15.** Every issue listed below
> shipped resolved: the GH #16–#22 cluster in v1.4.0 (see `docs/changelog.md` `[1.4.0]`) and
> #24 (schema identity, Option A) in v1.4.3. See `docs/roadmap.md` and
> `docs/ideas/consolidated-roadmap.md` for current state.

**Date:** 2026-09-15
**Sources:** GitHub issues on `tosumitdhaka/trishul-ram` (17 total: 9 open, 8 closed), `docs/issue-drafts/` (empty), `docs/roadmap.md`, `docs/ideas/tram-improvements.md`, and the code/UI/domain reviews in this directory.

---

## A. Open GitHub issues (9)

### Performance / memory (the heaviest cluster)
- **#16 — Worker pods retain large anonymous heap after heavy CDR batches → OOM risk.** Reproduced on kind: a PGW ASN.1 batch leaves a worker at ~251-374Mi idle vs ~50Mi baseline; later light work doesn't release it. Partial mitigation exists (`post_batch_cleanup: true` → `gc.collect()` + `malloc_trim`), which reduces but doesn't restore baseline. Root cause (allocator fragmentation vs decoder object lifetimes) still unconfirmed.
- **#18 — `json_flatten`/`_apply_explodes` deepcopy is O(n²).** Observed ~700s to explode a 13k-row PM statsfile (parse alone: 0.3s). Delete-source-list-before-deepcopy fix is spec'd in the issue.
- **#19 — ASN.1 `split_path`/`split_path_context` enhancement.** Single-frame BER files (TS 32.104 PM statsfiles) decode to one giant dict; `split_records` and `record_chunk_size` never engage. Full design with acceptance criteria is in the issue.

### Operational visibility
- **#17 — Cluster/Detail stream visibility drops for long-running manager-mode streams.** Streams visible at startup, disappear after minutes while still running. Post-1.3.3 hotfix item.
- **#22 — Dashboard batch stats are completion-based, not live.** Records/Bytes In/Out and the chart stay flat for the whole duration of a 15-30 min batch run.
- **#21 — Optionally queue manual runs when no healthy workers exist** (currently fails immediately; semantics TBD).

### UI
- **#20 — Templates page action row / preview modal don't match shared detail components.** RCA'd as a missing shared button/viewer contract, not a deploy/cache issue.

### Alerts
- **#3 — Alert cooldown consumed even when delivery fails [v1.3.0].** Webhook/email exceptions swallowed, cooldown set anyway.
  ⚠ **Needs triage:** `docs/ideas/tram-improvements.md` (Q3) found `evaluator.py:74-75` now only sets cooldown on `fired=True` and roadmap marks it `[x]` — likely already fixed; verify with a regression test and close, or find the residual path.

### Architecture / AI
- **#24 — Feasibility study: schema identifier + schema registry for AI-assisted pipeline adaptation.** Detailed 4-plane proposal (data/observation/control/intelligence) — deterministic schema fingerprinting + diff history feeding the AI assistant. Explicitly keeps AI out of the data path; autonomous deployment out of scope.

## B. Closed issues (8) — recent fix history

| # | Title |
|---|---|
| #4 | Version references and auth docs inconsistent across package, Helm, and docs |
| #5 | Bundled pipeline and README examples stale against current schema |
| #6 | DB-backed browser auth still requires `TRAM_AUTH_USERS` |
| #7 | PipelineController still generates truncated 8-character run IDs |
| #8 | Manager-worker callback path loses real run timestamps |
| #9 | `source_stem`/`source_suffix` tokens for file-based sink filename templates |
| #10 | Migrate SNMP connectors from pysnmp-lextudio to pysnmp 7.x |
| #11 | Push-based sources not architecture-ready in manager-worker mode (closed after v1.3.0: `workers:` block, broadcast dispatch, per-pipeline Services) |

## C. Local docs state

- **`docs/issue-drafts/2026-04-15-audit/`** — **empty directory**; no local issue drafts exist.
- **`docs/ideas/tram-improvements.md`** — cross-references "8 open issues" (now stale: #24 makes it 9), plus 9 code-verified strategic gaps (G1–G9: no hot-loadable logic, thread-based exec, 83 vs 370+ plugins, no CRD, no exactly-once, no manager HA, no RBAC, no DLQ viewer, stale doc numbers).
- **`docs/roadmap.md` backlog** — covers strategic items overlapping the domain review: DLQ viewer/replay, RBAC/multi-tenancy, gNMI hardening, manager HA, SNMP connector fixes.

## D. Cross-reference with the code review (`code-review.md`)

**Issues corroborating review findings (independent confirmation from two directions):**

| GH issue | Related review finding |
|---|---|
| #17 stream visibility | **A13** — `update_slot_run_id` read-modify-write loses concurrent slot updates → reconciler spuriously drops/redispatches live streams. Concrete candidate root cause inside #17's "areas to inspect" (`reconciler.py`, `worker_pool.py`). |
| #16 heap retention | **A9** (ClickHouse sink: leaked self-rescheduling `Timer` + never-called `close()`), **D8** (`_pipeline_workers` unbounded growth), **B7** (unbounded `errors_last_window`) — all leak-class defects on the same worker processes; rule in/out of #16's RCA. |
| #22 completion-based stats | UI review's run-monitoring findings (no live counters surfaced mid-run). |
| #8 (closed) callback timestamps | **A3** — same callback path still discards `run_id` on retry. |

**Critical delta — high-severity review findings with NO tracking anywhere** (not on GH, not in roadmap, not in ideas docs):

1. **A1** — `skip_processed` silently dead in manager+worker mode (duplicate file reprocessing)
2. **A2** — threaded runs mark/move files before sink writes complete (data-loss window)
3. **A6** — alert CRUD edits lost on restart
4. **A7** — watcher calls nonexistent `stop_pipeline`; deleted pipelines keep running
5. **C1/C2** — `/api/internal/*` and the worker agent API (:8766) are fully unauthenticated

None of the 9 open issues covers any of these. The tracker's open items are perf/UX-shaped; the correctness/security holes from the review are entirely untracked.

## E. Recommended next steps

1. File the five untracked items above as issues (A1/A2 can share one "idempotency parity across all execution modes" umbrella).
2. Triage #3 per improvements-doc Q3 (likely fixed — verify with a regression test, then close).
3. Link A13 into #17 and A9/D8/B7 into #16 as RCA candidates.
4. Update `docs/ideas/tram-improvements.md` open-issue count (8 → 9, #24 added).

## Note on #24

#24 is a feasibility-study proposal, not a defect — root-cause analysis does not apply. It should be scheduled as design work, not triaged into the RCA pass.
