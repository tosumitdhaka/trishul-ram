# Kind-cluster verification — PR #25 (Waves A–F)

> **Status (2026-09-23): historical record of the v1.4.0 kind-cluster verification.** All wave
> exit criteria passed at the time; v1.4.0 shipped as planned.

Date: 2026-09-16 · Cluster: `tram-dev` (kind, 4 nodes) · Release: `trishul-ram` @ image `local-20260916044939` (branch `wave-a-stopgaps`, commit 74957a9) · Mode: manager+worker (1 manager, 3 workers, 1Gi limits).

All wave exit criteria that require a live cluster were exercised. Every gate passed.

## Deployment & smoke

- `scripts/deploy-kind-tram-dev.sh --mode manager`: build + load + Helm upgrade in 3m07s; all pods Running.
- `MALLOC_ARENA_MAX=2` present in the worker environment (A.4).
- API auth enforced end-to-end: 401 without `X-API-Key`, 200 with (release retained `apiKey` from pre-Wave-C values via `--reuse-values` — the rotated default is gone from the chart).
- Worker `/agent/health` probe green via the ingress NodePort (C.1 probe exemption path).
- Note: the release's stored values still carry the old committed key; a production rollout must set a fresh key in the same upgrade (already documented in the Wave C rollout notes).

## E.2 — queued manual runs (GH #21)

Sequence: workers scaled to 0 → manual trigger → **202 `{status: "queued", run_id, expires_at}`** (+15 min TTL), `queued_run` present in the pipeline detail → manager pod deleted and recreated → **queued row survived with the same run_id** (durable DB row, absolute TTL clock) → workers restored → **auto-dispatched ~5 s after capacity returned** (worker-restored nudge). Run history truthfully records the outcome — including the failure case (`failed / Local source path does not exist`, correct node attribution). Metrics: `queue_depth=0`, `queue_dispatched_total=1`, `drain_result{dispatched}=1`, `wait_seconds_sum=51.6` matching the exact request→dispatch timestamps.

## F.1 — stateful transforms

Pipeline `vf1-counter` (local source → `counter_delta` → local sink), manager+worker mode:

- Run 1 (first sample): `*_delta: null` with `first_sample: pass`; `{run_id}` filename token carries the full UUID.
- Run 2 (higher counters): exact deltas — node-1 `bytes_in_delta=500` (1500−1000), node-2 `200`; rates over the true 720 s elapsed (500/720 = 0.694/s).
- Internal state endpoint: 4 counter identities with `v`/`t` matching run 2 + `config_sha256`.
- Worker deleted, next run dispatched to a **different** worker: deltas continued against the previous run's state (1100 = 2600−1500) — state lives on the manager, not the pod.

Pipeline `vf1-window` (webhook stream → `window_aggregate` 60 s/5 s → local sink), `workers.count: 1`:

- Registration with the chart's default broadcast placement was **rejected at runtime**: "stateful transforms cannot run with broadcast placement" — the F.1 guard firing in a real deployment.
- Watermark finalization: records in window [05:01, 05:02) emitted with `window_complete: true` (exact sums 250/110, samples 2) the moment a later-timestamped tick advanced the watermark; the tick's own record stayed in the open window.
- Graceful stop with `flush_on_close: true`: open window emitted with `window_complete: false` (sum 420, samples 2) and the state blob's `windows` cleared — exactly per design §5.

## D.2 — count=1 stream placement (GH #17)

Pipeline `vd2-stream` (webhook stream, `workers.count: 1`):

- Durable 1-slot placement row with live per-slot stats (records/rates/uptime merged into the view).
- **Worker-death recovery**: hosting pod deleted → stream recovered on another worker in ~30 s, `restart_count=1`, placement status never left `running`.
- **Manager-restart adoption**: manager pod deleted → same `placement_group_id` survived, `slot_count` stayed 1 (**no double-dispatch**), owner and restart count unchanged, stream healthy throughout.
- **Config update**: PUT with changed config → stop → deregister → register → redispatch with a fresh run_id, confirmed in manager logs. (The reconciler's `config_drift_redispatch` safety net is unit-covered; the update path is the primary mechanism and worked.)

## #16 — RSS soak, threaded path (A.4/A.5 + B.3, GH #16)

Pipeline `v16-soak`: 42 MB JSON / 300 k nested records → `flatten` → local sink, **`thread_workers: 2`**, 13 consecutive manual runs on a single worker (1Gi limit):

- **All 13 runs succeeded**, 300 k/300 k records each, 0 restarts, 0 OOMKills.
- Peak RSS per run: ~585–606 MiB — **stable, no per-run growth**.
- Settled post-run RSS: ~150 MiB plateau across runs 2–13 (idle floor ~105 MiB vs 55 MiB pristine baseline — bounded allocator retention, not accumulation). `post_batch_cleanup` + `MALLOC_ARENA_MAX=2` hold.
- Run durations 2.2–3.7 s: the GH #18 `json_flatten` O(n²) fix observable live (this workload class took ~700 s in the issue report).

## Verdict

All cluster-verifiable exit criteria for Waves A–F are met: E.2 queue lifecycle, F.1 stateful-transform semantics + guards, D.2 durable placement lifecycle, #16 soak plateau. PR #25 is clear to merge from a runtime-behavior standpoint.
