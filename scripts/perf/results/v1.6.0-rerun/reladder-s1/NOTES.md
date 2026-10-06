# S1 webhook saturation re-ladder — v1.6.0 release verification (2026-10-06)

Driver: `scripts/perf/ladder_s1.py` on `release/v1.6.0` @ 684869b (tree clean,
no code changes; perf-concurrency default 400/process). Images
`local-20261006045709` (mw) / `local-20261006064414` (single) — built from the
same tree; layer-identical to the re-run's `local-20261001rr` (fully cached
build, same image Created timestamp).

## Layout
- `mw/` — clean mw-M ladder (7 steps, `saturation-s1.csv` + steady samples +
  per-step `results/<run_id>/bench-summary.json` + loadgen summaries). This is
  the measurement of record for the exit target.
- `mw/controls/` — controls run to separate server-bound from loadgen/path-bound:
  - `lg-direct-800.json` (+ `run.log`) — 800 rps / 800 conns aimed directly at
    the worker NODE IP 172.19.0.3:30002, bypassing the host `docker-proxy`
    (NodePort 30002 listener): collapses identically → not host-path-bound.
  - `lg-conc50-*` (+ `steady/`, `runone-conc50/`) — re-run-shaped control:
    3,200 offered, 6 procs × conc 50. 5 of 6 procs completed (one straggler
    died without writing its summary): 1,346.7 rps @ p95 ~500 ms with workers
    pinned at the 500 m limit → extrapolates to ~1,610 for 6/6, reproducing the
    v1.6.0 re-run's 1,605 plateau with working telemetry.
  - `contaminated-first-attempt/` — first full mw ladder, INVALID: the WSL2 host
    was oversubscribed (three concurrent opencode sessions + a pytest, load
    avg 10–24, swap in use); p95 inflated 3 orders of magnitude at ≥200 rps
    offered. Kept for provenance only.
  - `env-validation-probe.csv` — 3-step probe after the host drained; steps
    1–3 match the re-run (96.9/193.9/387.8 vs 96.9/193.9/386.4), validating
    the clean window before the full ladder.
- `single/` — single-M ladder (6 steps, stopped on <95% 2xx at step 6).
  CAVEAT: steps 3–6 ran under renewed host contention (~2–3 foreign CPU cores
  from other sessions, load avg 7–13). Steps 1–2 are clean and match the re-run.
  Treat steps 3–6 as a contaminated lower bound; the collapse onset at step 3 is
  qualitatively consistent with the mw pattern.

## Known measurement artifacts (both ladders)
- `4xx == concurrency` per loadgen process at step start: the first in-flight
  burst arrives before the webhook source's placement registers; the GH #82
  10 s hold covers later arrivals but not the t=0 cohort. Same artifact existed
  in the v1.6.0 re-run (4xx == 50 == its concurrency). Not a regression.
- `pod_mem_peak_mi` in the CSVs is the max over ALL namespace pods (kafka-0
  ~720 Mi dominates); it is not the TRAM-subject pod's memory. The workers'
  actual peak was ~75–85 Mi (see `steady/*/samples.csv`).
- In-cluster steady telemetry (pod_cpu_peak_m/peak_pod) verified >0 from the
  first probe row — the v1.6.0 re-run's telemetry defect is confirmed fixed.

## Deployment facts (kind cluster `tram-dev`, ns `trishul-ram`)
- mw-M: manager + 3 workers, all 500 m / 1 Gi (values-mgrworker + values-res-M),
  helm rev 151, chart includes the env-leak fix (b311be3). Verified live: every
  worker pod has exactly one `TRAM_MANAGER_URL=http://trishul-ram:8765`.
- single-M: standalone pod 500 m / 1 Gi, `TRAM_MANAGER_URL=localhost:8765`
  (required in single mode), rev 152.
