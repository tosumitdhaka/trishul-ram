# Independent Reviews of the Implementation Plan

> **Status (2026-09-23): historical snapshot — the reviewed plan shipped as v1.4.0** (waves
> A–F, GH #16–#22; see `docs/changelog.md` `[1.4.0]` and `docs/plans/v1.4.0_plan.md`).

**Date:** 2026-09-15
**Subject:** `docs/plans/issue-implementation-plan.md` (v1)
**Method:** Two independent review sessions with distinct mandates, neither sharing context with the plan's authoring session. Review 1 (technical): solution-vs-root-cause fidelity, dependency graph, completeness, effort calibration, verified against the actual code. Review 2 (execution/delivery): wave ordering, coupling, rollout coordination, planning artifacts, scope realism. Post-review, the highest-impact claims were independently spot-verified (marked below) before the plan was revised to v2.

---

# Review 1 — Technical Correctness

**Method:** Cross-checked every waves 0-4 work item against the RCA/code-review claims and the actual code (executor, controller, worker_pool, agent, routers, serializers, assets, sources, db, models, UI files). Line references in the plan survived spot-checking far better than usual — of ~40 citations checked, all but two are accurate. The problems are elsewhere: in two solution designs, one missing dependency edge, and a completeness gap in the backlog section.

## 1. Solution-vs-root-cause fidelity

### W-1.2 (A2) — root cause correct, both proposed solutions understated **[RISK]**

The RCA is verified: threaded path submits and advances the generator (`executor.py:856-886`); `_post_read`/`mark_processed` run on generator resumption after the last chunk is *yielded*, not completed (`sftp/source.py:120-124`, `local/source.py:81-83`); `enable_safe_finalize` is set only in the single-threaded branch (`executor.py:898`). But:

- **Option A ("drain the futures pool inside the source generator's lifetime... per source key") has a structural hole.** The executor cannot know a source key is finished until it pulls the *next* generator item — and the post-read runs *during* that pull, before the consumer sees any evidence. The only pure-executor-side fix is draining **all** in-flight futures before **every** `next()` call, which caps effective in-flight work at ~1 chunk per pull and largely negates `thread_workers > 1` for multi-chunk files. "Per source key" draining is not implementable from the executor side without a connector API change (deferred finalize hook), which the plan doesn't mention.
- **Option B ("set `enable_safe_finalize` and use the staged-finalize machinery") does not exist.** The staged-finalize machinery is *sink-side only*: `_finalize_source_for_sinks` (`executor.py:310-316`) calls `sink.finalize_source` to rename staged `.tmp` outputs. Nothing in it defers the *source-side* move/mark. Building that is new machinery, not "using" existing.

Effort **M** is calibrated for a fix that either (a) serializes the hot loop, or (b) requires reworking post-read semantics across file connectors. The pipelining-preserving variant is **L**-ish.

### W-1.1 (A1) — first solution option contradicts the worker image split **[RISK]**

RCA verified (`agent/server.py:354` bare `PipelineExecutor()`; `executor.py:228-229` conditional injection; `daemon/server.py:26-41` no DB). But the plan's first option — "construct a `TramDB` + `ProcessedFileTracker` in the worker branch" — collides with `daemon/server.py:22-25`, whose comment states the worker image **does not have sqlalchemy installed** and the worker branch must never touch the manager import chain. Also unmentioned: per-worker SQLite-on-PVC only preserves `skip_processed` if dispatch is sticky, but `resolve()`/least-loaded dispatch (`worker_pool.py:217-230`) is not sticky across runs. The plan's risk note honestly flags "DB access from workers — decide...", but Effort **S-M** with an architecturally contradictory first option understates it; the correct option (manager-mediated tracker API) is a new internal endpoint + executor plumbing + auth interaction with W-1.5 → **M**.

### W-3.1 (#17) — solution verified viable; this is the plan's best item

Confirmed: count=1 streams record only `_stream_run_ids` (`controller.py:756-777`); defaults assigned at `models/pipeline.py:1349-1356` exactly as claimed; `multi_dispatch` natively handles count=1 (`worker_pool.py:486-521`, `target_slots=1`); `_record_broadcast_placement`, `_restore_broadcast_placement`, and the reconciler all exist. Critically, the restart double-dispatch mechanism was verified end-to-end: `controller.stop()` deliberately leaves worker streams alive and clears `_stream_run_ids` (`:142-144`), `_boot_load` re-schedules enabled non-placement pipelines (`:188-189`), and the worker's 409 guard keys on `run_id` only (`agent/server.py:323-327`) — so a fresh UUID re-dispatch does start a second instance today, and the placement branch in `_boot_load` (`:182-187`) is exactly what prevents it for placement streams. "M-L" is fair (arguably M given the machinery exists).

### W-2.1 / W-2.2 / W-3.3 / W-3.4 / W-3.5 / W-3.6 / W-4.x — verified

- **W-2.1:** `_apply_explodes` delete-after-copy (`json_flatten.py:95-102`) vs `_apply_zip_groups` base-copy (`:142-148`) confirmed; keeping `apply()`'s upfront copy (`:75`, protecting in-place `_apply_choice_unwrap`) is the right call. Minor: the aliasing fix should cover **both** `explode.py` branches — line 39's `update(element)` *and* line 41's `set_path(..., element)` insert the original element. **[IMPROVEMENT]**
- **W-2.2:** mtime-keyed caches (`asn1_serializer.py:177,180`; protobuf ~`:73-76`) + unconditional `dest.write_bytes` (`assets.py:132,149`) both verified; content-hash + skip-unchanged addresses both ends of the churn. Correct and complete.
- **W-3.3/W-3.4:** zero `stats_store` references in `stats.py` (grep-verified); local `_run_batch` passes no `stats` (`controller.py:513`); `_stream_worker` wiring pattern at `:806-818` exists as described; D4 inflation at `executor.py:561-565` exact.
- **W-3.5:** all four references verified (`health.py:241` blocking `live_streams()` in async handler; `worker_pool.py:346-357`; `agent/server.py:146-157` DEBUG swallow; `db.py:902-926` RMW).
- **W-4.2:** enqueue point (`controller.py:478-497`) and the four #21 bugs verified; design matches RCA.
- **W-4.3:** UI refs spot-checked correct (`pipelines.html:112-114`, `style.css:563-572`, `:1201-1203`, `detail.html:191-198`).
- **W-1.3/1.4/1.5/1.6:** all verified (`manager.py:115-116` version-only save; watcher `manager.stop_pipeline` under `except: pass`; `middleware.py:28,47-48`; `webhooks.py:40`; agent app unauthenticated at `agent/server.py:273-277` vs ingress `:491-493`; `models/pipeline.py:1311`; `SimpleQueue` + `_WEBHOOK_REGISTRY[self.path] = q` overwrite).

**W-3.6 citation error (minor) [ERROR]:** the conflation point is `dispatch()` collapsing `BroadcastResult` to `accepted[0] or None` (`worker_pool.py:562-581`); the plan's ":445-454 vs :251-271" points at `_dispatch_to_worker`'s failure return and `resolve()`. Substance correct (distinguishable status does exist); citation imprecise.

## 2. Dependency graph

**[MISS] — W-1.2 ↔ W-2.3 is a hard missing dependency, and the two fixes conflict.** Both rewrite the same loop (`executor.py:856-886`) with incompatible invariants: W-2.3's "cap in-flight at ~2× `thread_workers`" does **not** fix A2 (the generator's post-read can still fire with cap−1 futures pending), while W-1.2's executor-side fix (drain-all before every pull) caps in-flight at ~1, defeating W-2.3's purpose. The plan's dependency line covers only "W-2.2/W-2.3 independent" and never links W-1.2 ↔ W-2.3. Executing them as separate wave-1/wave-2 patches guarantees the second rewrites the first.

**[ERROR] — "W-3.1 → W-3.3 benefit" is a false dependency.** W-3.3 merges *batch* live stats (which already reach the manager's StatsStore per #22 RCA) into `/api/stats`; W-3.1 is about *stream* placement records. They share no code path of consequence. (W-3.1 → W-3.2 is real, and stronger than "benefit": once count=1 streams have placements, the Detail endpoint's placement branch renders them, and W-3.2's gate-drop only remains relevant for standalone mode — W-3.2's content should be sequenced *after* W-3.1 to avoid building the manager-mode half unnecessarily. **[IMPROVEMENT]**)

**Verified as claimed:** W-3.6 → W-4.2 (real prerequisite, code-verified); "D4 decision gates W-3.3" (matches RCA). W-2.1 → W-4.1 is a value ordering, not a code dependency (#19 doesn't touch `json_flatten`) — "→" overstates it, but the ordering itself is the RCA's recommendation. Minor: W-3.2 and W-3.5 both modify `WorkerPool.status()`/probe paths — fine if sequenced within wave 3, worth a note.

## 3. Completeness

**RCA's 6 new defects — all placed** ✅ (double-dispatch → W-3.1; schema-cache → W-2.2; backpressure → W-2.3; label conflation → W-3.6; heartbeat → W-3.5; blocking fan-out → W-3.5). Both HIGHs have homes.

**Dropped code-review findings with no home [MISS]:**
- **B6** — Kafka `enable_auto_commit: true` undermines at-least-once. Flagged in *both* the code review (B6) and telecom review ("at-least-once is not actually guaranteed on the Kafka ingress path"). The plan's backlog lists B1/B2/B10 and nothing else from B. This is the most significant drop.
- **B3** (gnmi/kafka no `stop()` — RCA explicitly re-confirmed "real for stop-latency"), **B4** (kafka per-message `end_offsets`), **B5** (stale-config in-flight run), **B7** (`errors_last_window` — RCA qualified as mechanically valid), **B8**, **B9**, **B11** — all dropped without comment.
- **C5** (ClickHouse table-name interpolation) — dropped; W-1.5 covers C1-C4 but not C5.
- **D8** (`_pipeline_workers` unbounded growth / worker-id churn) — re-confirmed manager-side-real by the RCA, dropped.

**UI review [MISS] (moderate):** the plan's UI-backlog bullet lists 6 of the 13 ranked findings and omits #2 (**HIGH** — wizard disabled, raw YAML textarea is the only creation path), #6 (import-replace without diff), #10 (**HIGH** — run history no auto-refresh, silent CSV truncation at 1,000), #11-#13. The bullet points at the source table, but a consolidation plan whose stated purpose is "nothing dropped" should either list these or explicitly park them with rationale.

**Wave-5 vs telecom review:** W-5.1-5.4 correctly map the review's top gaps (including removing the PM-XML truncation hack only after file-done semantics — good). Unhoused domain findings: unverified SNMP trap communities (spoofing into ticketing sinks), SNMPv3 privacy, no Counter64 in trap-sink varbinds, APScheduler UTC-only + hardcoded `misfire_grace_time=60`. Minor individually, but a "parked with rationale" line would close the loop.

## 4. Effort/risk calibration

- **W-3.1 M-L:** fair — arguably M, since `multi_dispatch`, placement persistence, restore, and reconciliation all exist; the work is re-routing the gate at `controller.py:720-724` + idempotency.
- **W-1.1 S-M: [RISK] understated** for the correct option — and the plan's framing buries that this is an architecture decision, though its risk note does at least name the choice.
- **W-1.2 M: [RISK] understated** for the pipelining-preserving variant (connector API change); the "benchmark for throughput regression" caveat hints at it but doesn't surface the drain-vs-cap contradiction with W-2.3.
- Everything else (W-2.2 S-M, W-2.3 M, W-3.3 M, W-3.6 S, W-4.2 M-L, W-4.3 S-M, W-5.3 S) is calibrated honestly.

## 5. Technical errors in the plan text

1. **[ERROR]** W-3.6 citation: conflation lives in `dispatch()` (`worker_pool.py:562-581`), not ":445-454 vs :251-271".
2. **[ERROR]** "W-3.1 → W-3.3 benefit" — no meaningful dependency (batch stats vs stream placement).
3. **[ERROR]-lite** W-1.2 Option B: "use the staged-finalize machinery" — it's sink-side only; the source-side variant must be built (`executor.py:310-316`, `:898`).
4. **[IMPROVEMENT]** W-1.5 covers C4's body-size limit but not its other half (API key accepted via query param, `middleware.py:47`).
5. **[IMPROVEMENT]** W-2.1: fix `explode.py` aliasing in both branches (lines 38-41), not just line 39.

## Verdict: **yes-with-fixes**

The plan is unusually well-grounded — RCA claims, file:line references, and the two headline solutions (W-2.1, W-3.1) all survive code verification, and all six RCA-discovered defects including both HIGHs are placed. It is implementable after the amendments below.

**Top 5 amendments, ranked by impact:**

1. **Merge W-1.2 and W-2.3 into one work item owning the threaded-loop redesign.** They are the same code (`executor.py:856-886`) with directly conflicting invariants (drain-before-pull vs in-flight cap). The coherent design is: deferred source-finalize hook (move `_post_read`/`mark_processed` out of the generator, invoked by the executor after draining that source key's futures) + bounded in-flight cap. Re-estimate as one M-L item; the current split guarantees wave-2 rework of wave 1.
2. **Rewrite W-1.1's options.** Drop or heavily qualify "TramDB in the worker branch" (worker image lacks sqlalchemy by design, `daemon/server.py:22-25`); make the manager-mediated tracker API the primary design; flag that per-worker state breaks under non-sticky least-loaded dispatch; re-estimate M.
3. **House the dropped findings.** At minimum B6 (Kafka auto-commit — an at-least-once hole flagged by two independent reviews), plus B3/B4/B5/B7/B8/B9/B11, C5, D8, and UI ranked #2/#10. Add a "parked, with rationale" list so the consolidation is auditable against its sources.
4. **Fix the dependency line:** add W-1.2 ↔ W-2.3; delete W-3.1 → W-3.3; downgrade W-2.1 → W-4.1 to "recommended ordering"; make W-3.1 strictly precede W-3.2.
5. **Correct W-1.2's Option B and W-3.6's citation** so an implementer doesn't search for source-side finalize machinery that doesn't exist, or look for the conflation in the wrong function.

---

# Review 2 — Execution & Delivery

**Reviewer stance:** RCAs assumed correct. Judgment is on executability: ordering, coupling, rollout coordination, missing artifacts, scope realism, and ways the plan-as-written fails in production.

## 1. Wave ordering & risk

**[DISAGREE] "Dependencies: … W-2.2/W-2.3 independent" and the Wave 1→Wave 2 split of the threaded batch path.**
W-1.2 (mark-after-write) and W-2.3 (backpressure) rewrite the *same ~30 lines*: `executor.py:856-877` (the threaded submit loop), and both demand the same throughput benchmark. Doing W-1.2's drain-then-mark barrier in Wave 1, then W-2.3's bounded submit/drain loop in Wave 2, means rewriting the highest-risk hot loop **twice**, with the second rewrite invalidating the first's tests. A bounded submit/drain loop (W-2.3's design) is the natural substrate for W-1.2's ordering guarantee — mark/post-read after the last future per source key *inside* the drain. These are one work item with two acceptance criteria, not two M items in two waves. This is the single biggest structural flaw in the plan.

**[RISK] Customer-visible OOM gets no mitigation until Wave 2 completes.** #16 is a live production problem (563 MiB peak → 171-374 MiB retained, OOMKill at `thread_workers=2`). But two of the four #16 mitigations are S-effort, near-zero-risk, and touch **no Wave 1 files**: W-2.4 (`MALLOC_ARENA_MAX=2` in `Dockerfile.worker` + `post_batch_cleanup` default flip — `models/pipeline.py:1322` currently `False`) and W-2.2's cache-key fix (`asn1_serializer.py:177,180`). An operator bleeding memory today should not wait for the full correctness wave. Cherry-pick these into a patch release in parallel with Wave 1.

**[RISK] Correctness bugs are misfiled in Wave 3/5 by theme.**
- W-3.1's second half — manager-restart **double-dispatch of count=1 streams** — is labeled HIGH by the plan's own RCA. Duplicate stream writes to telecom sinks is the same severity class as W-1.1/W-1.2. It sits behind all of Wave 2.
- W-5.4 bundles **A12, a [CONFIRMED BUG]** (syslog TCP: one `recv`, no RFC 6587 framing — truncated/merged records, `syslog/source.py:199-205`) into a "strategic domain gaps" wave. Corrupted records today is a correctness fix, not a domain gap. If any deployment uses TCP syslog, this is Wave 1.6 material at M effort.

**[IMPROVEMENT] Wave 1 internal ordering is unspecified.** For a single maintainer, the critical path to production-grade correctness is: W-1.3 → W-1.4 (S, HIGH severity, trivial risk — do in week 1) → W-1.1 (needs a storage decision first) → merged W-1.2+W-2.3 → W-1.5 (coordinated rollout). The plan lists items but provides no critical path.

**Wave 1 before Wave 2 is otherwise the right call** — A2 is *permanent data loss* presented as processed, which outranks an OOM restart in a mediation product. The error is not the order, it's failing to fast-track the two S mitigations and misfiling the two correctness bugs above.

## 2. Hidden coupling & merge collisions

The master table implies waves are independent queues. They are not. Three collision zones:

**[RISK] `executor.py` batch path — 6 items, 4 waves:**
W-1.2 (threaded loop :856-867), W-2.3 (same loop :856-877), W-4.1 (must define `parse()` semantics for the threaded path that bypasses `parse_chunks` — same block), A3 (retry discards run_id, `executor.py:814`, paired with W-3.3 per backlog note), D4 (`records_out` inflation, `executor.py:561-565`, decided inside W-3.3), and W-2.2's `sink.close()` in `batch_run`'s finally. Sequential execution by one person survives this; **any attempt to parallelize waves across contributors breaks here.** If W-4.1 lands after the merged W-1.2+W-2.3 rework, its threaded-path semantics must be validated against the *new* loop — the plan only gates W-4.1 on W-2.1. Add "after the threaded-path rework" to W-4.1's prerequisites.

**[RISK] `controller.py` — 5 waves, one 1,200-line file:**
W-1.3, W-1.4, W-3.1 (rewrite `_start_stream` count=1 branch), W-3.4, W-4.2 (queue at the `controller.py:478-497` branch + BatchReconciler). Worse: the deferred backlog B1/B2/B10 fix — **one controller-level RLock around lifecycle transitions** — wraps exactly the methods W-1.3/W-3.1/W-4.2 all modify. Landing W-4.2's queue state machine on the *unlocked* controller means the queue inherits a duplicate-dispatch race. **The RLock should be pulled out of the backlog and done before W-4.2, ideally with Wave 1.**

**[RISK] `agent/server.py` — 4 waves in one ~500-line file:** W-1.1, W-1.5, W-3.5, W-2.2. Plus `db.py`: W-3.1, W-3.5/A13, W-4.2, and E2's `_upsert` refactor. [IMPROVEMENT] Do E2 before W-4.2 at latest, or the queue writes a 7th dialect-branching upsert.

**[IMPROVEMENT] Explicit parallelization map.** With a second contributor, the only clean split is: executor-batch-path cluster vs controller/DB cluster — never both. The plan should state which items are merge-serialized.

## 3. Deployment / upgrade coordination gaps

**[RISK] W-1.5 as written breaks run finalization and stats, and kills worker probes.** Three concrete failures, all verified in code:
1. `_post_run_complete` and `_post_stats` (`agent/server.py:97-143, 146-157`) post to `/api/internal/*` **with no `X-API-Key` header** — only `sync_assets` passes the key today. Enforce auth on `/api/internal/*` without first patching these two clients and every worker-dispatched run becomes a phantom FAILED via BatchReconciler, and the live-stats channel #17 depends on goes dark.
2. The plan says the agent gets "the same `APIKeyMiddleware` pattern as ingress". `EXEMPT` in `middleware.py:26-28` covers `/api/health`, `/api/ready`, `/metrics`, etc. — **not `/agent/health`**. The worker StatefulSet's liveness/readiness probes hit `/agent/health` with no key (`worker-statefulset.yaml:118-127`) → 401 → CrashLoopBackOff on rollout.
3. No upgrade ordering: old workers cannot send the key even with the env set (the code doesn't send it). Manager-first rollout with enforcement breaks all in-flight runs. The plan's only note is "set the env before upgrading." It needs either a warn-only/optional enforcement phase or a workers-drain-then-manager ordering, spelled out.

**[RISK] W-1.5 locks in a publicly committed secret.** `helm/values.yaml:275` ships `apiKey: "tram-internal-2026"` and `:287` `authUsers: "admin:tram@2026"` as defaults. Extending this key to internal surfaces + agent "secures" them with a value in the repo. The plan must add: rotate the default, fail-closed guidance, and remove the plaintext defaults (chart already supports `envSecret`, `manager-statefulset.yaml:100-105`).

**[MISS] W-1.1's storage decision is unmade but effort is already estimated, and every option has unlisted deploy coordination.** The worker's `/data` is an **emptyDir** (`worker-statefulset.yaml:135-138`) — no PVC. So: "SQLite-on-PVC" requires adding `volumeClaimTemplates` (Helm change + storage class decision, unlisted); "SQLite-on-emptyDir" silently reprocesses all files after every worker pod restart; "manager-mediated tracker calls" adds per-file RPCs on the batch hot path *on the same fragile manager↔worker channel that causes #17*; "per-worker Postgres" needs credentials/network reach. Until the option is chosen, "Effort: S-M" is not a credible number, and the chosen option needs a Helm/values/docs row in the plan.

**[MISS] W-2.4 deploy coordination detail absent.** `post_batch_cleanup` default flip is a silent behavioral change to every existing pipeline → needs changelog + docs; `MALLOC_ARENA_MAX` is image-level → rollback means image rollback. Per AGENTS.md, any new env/config surface needs `.env.example` + `docs/deployment.md` + Helm values updates — no work item in the plan carries this checklist.

## 4. Missing planning artifacts

- **[MISS] Owners/assignments** — with one maintainer, the master table's real function is *release cut points*, which the plan never defines.
- **[MISS] Per-wave exit criteria and rollback** — no wave has a definition of done beyond the sum of its items, and no item has a rollback story (the `post_batch_cleanup` default flip, W-1.5's two-phase enforcement, W-2.4's image-level env all need one).
- **[MISS] Branch/release strategy** — recommend: patch release for the #16 stopgaps; a minor per wave 1-3; W-3.1 and W-4.2 ship feature-flagged.
- **[MISS] Test strategy mapping** — (a) no mapping to *existing* guard tests: `test_thread_workers.py` mocks `_process_chunk` and **cannot catch W-1.2**, `test_processed_files.py` is standalone-only, no watcher-delete test, no alert-restart test; (b) the perf verifications are one-shot manual runs with no repeatability home — kind infra exists (`scripts/deploy-kind-tram-dev.sh`) but nothing institutionalizes them as regression gates; (c) no mention of the AGENTS.md constraint that full pytest must run outside the sandbox.
- **[MISS] Design-doc gates.** W-3.1 and W-4.2 are the two items where "implemented exactly as written" can regress at-least-once semantics; both need a short design doc approved before code — the treatment W-4.1 (inconsistently) already gets.

## 5. Scope realism

Single maintainer, ~21 items in Waves 0-4 plus strategic Wave 5. Honest throughput math: Waves 0-1 ≈ **3-5 weeks**; Wave 2 ≈ 2-3 weeks; Wave 3 ≈ **4-6 weeks** (W-3.1 alone is a week+ of coding and its verification is days more); Waves 4-5 are a quarter. Total: a **multi-month program for one person** — a fine roadmap, but the plan reads as a sprint queue; elapsed-time expectations and interim release points should be stated.

Specific estimate challenges: W-1.2 "M" is optimistic (drain option M, staged option L, and it should absorb W-2.3 → **L for the combined item**); W-1.1 "S-M" is not estimable until the storage decision is made; W-3.1 "M-L" and W-4.2 "M-L" are honest; W-3.5 "M" quietly includes A13, which touches the same placement persistence W-3.1 reworks.

## 6. Failure modes of the plan itself

- **[RISK] W-3.1 as written can regress at-least-once and K8s rollout behavior.** Making count=1 streams placements subjects them to `PlacementReconciler` staleness/lease semantics: (a) the reconciler's stale-slot redispatch — the A13 bug fixed in W-3.5, *a different work item* — would now apply to *every* stream; landing W-3.1 before W-3.5's A13 fix widens the duplicate-redispatch blast radius; (b) `_boot_load` deliberately keeps count=1 streams running during manager restart — making them placements changes what a restart does to live streams mid-rollout (adopt? redispatch? stop?). Needs a design doc covering poll-vs-push source semantics under placement, lease/adoption rules, and the A13 ordering (fix A13 *first*).
- **[RISK] W-4.2 inherits the unlocked controller.** B1's TOCTOU sits directly on the enqueue branch. Sequence: RLock → W-3.6 → W-4.2.
- **[RISK] W-2.4's default flip is a silent contract change.** gc + `malloc_trim` latency on high-frequency scheduled batches; needs a changelog entry and a perf sanity check on short-interval schedules.
- **[IMPROVEMENT] W-3.3 is the best-specified risky item** — use it as the template for the W-3.1/W-4.2 design docs.
- **[IMPROVEMENT] W-5.2's "then remove the PM-XML truncation auto-close hack"** inverts the safety dependency: removing existing (imperfect) protection should be gated on file-done semantics being *deployed and verified*, not merged in the same item. Split it.

## Verdict

**Yes — with fixes.** The plan is unusually well-grounded (every item carries RCA evidence and a verify bullet), but as an *execution* artifact it has one structural flaw (the W-1.2/W-2.3 split), one genuinely dangerous rollout (W-1.5 as written causes an outage, not security), and missing operational scaffolding (exit criteria, rollback, releases, test mapping, the unmade W-1.1 storage decision).

**Top 5 amendments:** (1) merge W-1.2 + W-2.3; (2) rebuild W-1.5 as a two-phase coordinated rollout with key-sending clients, probe exemptions, and rotated defaults; (3) fast-track the #16 stopgaps as a patch release; (4) reclassify the misplaced correctness bugs (double-dispatch guard, A12) and make the W-1.1 storage decision a blocking prerequisite; (5) add execution scaffolding (exit criteria, rollback, release vehicles, test mapping, design-doc gates, controller RLock pulled ahead of W-4.2).

**Recommended re-cut:** theme-based waves become **risk-ordered, release-anchored waves** — A stopgaps (~1 wk patch: W-0.x, W-2.4, W-2.2 cache keys + `sink.close()`, W-3.6) → B correctness core (W-1.3, W-1.4 → W-1.1 post-decision → merged W-1.2+W-2.3 → A12 → controller RLock → double-dispatch guard) → C security two-phase rollout → D visibility & stats (A13 first, then W-3.1 with design doc, W-3.2-3.5, W-2.1) → E enhancements (W-4.1 after the loop rework, W-4.2, W-4.3) → F domain gaps.

---

# Reconciliation (editor)

**Independent convergence (both reviews found these without coordination — highest confidence):**
1. Merge W-1.2 + W-2.3 — both ranked it their #1 amendment; same code, conflicting invariants.
2. W-1.1 understated and constrained by the worker image design (no sqlalchemy) / storage topology (emptyDir, non-sticky dispatch).
3. W-1.2's Option B (staged-finalize) is misleading — the machinery is sink-side only.
4. Misplaced correctness bugs (double-dispatch HIGH, A12 syslog framing) and unfast-tracked #16 stopgaps.
5. Controller RLock (B1/B2/B10) must precede W-4.2.
6. Missing execution scaffolding (exit criteria, rollback, release vehicles, test mapping) and dropped findings (B6 above all).

**Spot-verification after the reviews (all confirmed):** worker image comment excludes sqlalchemy (`daemon/server.py:22-25`); no `X-API-Key` headers anywhere in `agent/server.py`; committed default secrets (`helm/values.yaml:275,287`); worker `/data` is an emptyDir (`worker-statefulset.yaml:110-112`); `post_batch_cleanup: bool = False` (`models/pipeline.py:1322`); the W-3.6 conflation point is `dispatch()` collapsing `multi_dispatch` to `accepted[0] or None` (`worker_pool.py:562-581`).

**No contradictions between the lanes** — they examined different axes (technical vs delivery) and their findings compose. Both verdicts: yes-with-fixes.

**Decision:** apply all convergent amendments + the recommended re-cut; revise the plan to v2 (`docs/plans/issue-implementation-plan.md`).
