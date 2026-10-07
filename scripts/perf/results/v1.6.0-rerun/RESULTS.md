# v1.6.0 Harness Re-run — Results & Exit-Target Verification

- **Date:** run 2026-10-05 13:07 → 2026-10-06 13:16 JST (WSL2 kind host, same class as the baseline study)
- **Code:** `release/v1.6.0` @ `e9efb52` (waves 1–3 merged: #76 #77 #78 #79 #80 #81 #82 #83 #84 #85), images `local-20261001rr`, helm revs 145–150
- **Methodology:** same matrices as the 2026-10 capacity study (Matrix A s1–s7 × L/M/H × both topologies, Matrix B fsweep, Matrix C t-chains, serializer/transform microbenches, s1/s6 saturation ladders), 2 reps per cell, staged values/drivers under this directory
- **Hygiene:** 1 enabled (bench) pipeline at matrix time — the #86 leftover contamination did not recur
- **Raw data:** `csv/` (per-cell rows), `results/` + `steady/` (per-cell dirs), `logs/`, orchestration in `run_combo.sh`, `drivers/`, `lg-*.sh`, `ladder*.py`

## Executive summary

**6 of 8 exit targets PASS. Two MISS: webhook stream (#78) and protobuf E2E (#83).**
Additionally, the apparent broad "regression" of file-batch sources (s2/s3/s5/fsweep at
0.4–0.9× the study numbers) is **not a v1.6.0 regression** — it is ~2× inflation in the
baseline study's H-profile cells, proven by cross-check against the controlled #86 A/B
(v1.5.1, clean host, 2026-10-01), which the rerun matches (see §3).

## 1. Exit-target table

| # | Target (plan) | Baseline | v1.6.0 measured | Ratio | Verdict |
|---|---|---|---|---|---|
| 1 | webhook stream ≥2× (design 1,000+ rps/worker) | ~360 rps/worker (mw ladder plateau 1,089; single ~280) | mw plateau 1,605 (step6); single ~350 | 1.47× mw / 1.25× single | **MISS** (confounders, §2.1) |
| 2 | kafka stream ≥2× (design 2,500+) | 946–1,049 msg/s no-lag (single worker) | 2,000/s clean no-lag; 3,943/s consumed==produced | 2.0–3.9× | **PASS** |
| 3 | t5 chain ≥1.5× | 7,722 rec/s (single-M) | 23,942 (single-M median) | 3.10× | **PASS** |
| 4 | add_field ≥5× | 30.8 µs/rec | 3.798 µs/rec | 8.1× | **PASS** |
| 5 | high-card window_aggregate ≥4× | ~99 µs/rec @1k groups (56.6 @500) | 18.6 µs @1k (14.9 @500); growth 10→5k groups 1.64× (was ~8× to 1k) | 5.3× | **PASS** |
| 6 | protobuf E2E ≥2× (design 30–60k) | 10,363 rec/s (fsweep-M median) | 9,527 | 0.92× | **MISS** (cost model, §2.2) |
| 7 | s7 file→kafka functional at any batch size | records_out = 0 at every cell | out==in==100,000 at all 12 cells, 0 errors | functional | **PASS** |
| 8 | mw s2pmxml/s3 re-baseline at #86 parity values | (study, contaminated) | 16,817 / 5,311 @M | matches A/B parity (16.9k / ~5.9k) | **PASS** |

## 2. Misses and confounders

### 2.1 Webhook (#78) — MISS, resolved by clean re-ladder

**Final verdict (reladder-s1/, 2026-10-06, telemetry + loadgen fixed):**
mw-M plateaus at **~1,284 rps total (~428 rps/worker)** at client concurrency 400 —
0.59× of the ≥2,184 target. At the re-run's conc-50 shape the same server does
~1,605 (1.47× baseline), and 775 rps at p95 12 ms at conc 100. The plateau is
**server-CPU-bound** (workers pinned at the 500m limit) with a
CFS-throttle-amplified queueing collapse that begins well below CPU saturation
(step 4: 335 m, p95 3.6 s). Controls: not path-bound (direct worker-IP collapse
identical); the 1,605 figure is reproducible. The single-M ladder's steps 3–6 ran
under host contention (foreign load) — clean reference stays the re-run's ~357.

- The earlier confounders are closed: ladder CPU telemetry fixed (live values on
  every row) and the loadgen genuinely offered 6,400 rps (connection-limited
  flags now distinguish server latency from generator ceilings).
- 2xx ≥98% at every mw step; 0 5xx.
- **Capacity is concurrency-shaped** (~1,600 low-conc vs ~1,284 high-conc @ M) —
  sizing should quote both and recommend sizing at the low-conc figure with a
  concurrency note.

### 2.2 Protobuf (#83) — E2E flat; the issue's cost model was wrong

- fsweep_protobuf-M: baseline 10,309/10,417 → rerun 9,709/9,346 (0.92×).
- Back-to-back serializer microbench (same host, same conditions, v1.5.1 vs v1.6.0,
  138 B/rec payload, n=10k, median of 5): **parse 23.0 → 15.0 µs/rec (1.53× faster)**;
  serialize 31.4 → 30.2 µs (flat). `preserve_keys=True` changes nothing (16.5 vs 15.0
  parse; 32.5 vs 30.2 serialize).
- Conclusion: the batch framing/message-reuse (#83) removed only the construction
  overhead; the dominant remaining cost is the per-record `MessageToDict`/`ParseDict`
  dict conversion, which #83 deliberately kept (wire compatibility). The 2–5× estimate
  assumed framing/construction dominated — it does not.
- Further gains need a different lever (e.g. message→message copy without the dict
  round-trip) — descope-order candidate #1 as planned.

## 3. Baseline study H-cell inflation (~2×) — no v1.6.0 batch regression

Cross-check of v1.5.1 on a clean host (the #86 A/B, 2026-10-01, sum-of-runs basis)
against the study and this rerun (single topology):

| cell | study (baseline) | #86 A/B (v1.5.1 clean) | v1.6.0 rerun | rerun/A/B |
|---|---|---|---|---|
| single-s2csv-H | 111,111 | 50,244 / 52,721 | 49,228 | 0.96× |
| single-s2pmxml-H | 38,462 | 16,030 / 16,816 | 19,308 | 1.18× |
| single-s3-H | 11,458 | 5,203 / 5,435 | 4,425 | 0.84× |
| single-s2csv-M | 51,587 | 46,904 / 47,324 | 42,573 | 0.90× |
| single-s2pmxml-M | 17,712 | 14,857 / 15,032 | 12,873 | 0.87× |
| single-s3-M | 6,417 | 4,805 / 4,921 | 5,000 | 1.02× |

The rerun matches the controlled A/B everywhere; the study's numbers are the outliers
(note the round values: 111,111.1 = 100k/0.9 s, 166,666.7 = 100k/0.6 s — a duration
attribution artifact in the study's H cells). The same artifact inflates the study's
fsweep H cells and plausibly its M cells by ~10–20%. **The sizing calculator must be
re-baselined (see sizing-updates.md); none of the 0.4–0.9× "regressions" in the
comparison table are code regressions.**

Parquet specifically measured 0.28–0.43× the study value (30.8k vs 77.4k @M) — likely
the same inflation plus a pyarrow-version delta (study used an ad-hoc
`pip install --target` pyarrow; images now bake `pyarrow>=16,<20` from #79). Rerun
parquet (30.8k @M) sits in the avro class (27.4k) — plausible, but if parquet
throughput matters for sizing, run one controlled A/B-style parquet cell pair before
trusting either number.

## 4. Qualitative issue verification

- **#76 (kafka chunking):** s7 out==in==100k at all 12 cells with 0 errors (baseline
  out=0 everywhere); kafka sink offsets advanced on every run.
- **#77 (part cap):** the 99,999 pinning is gone — every s6/s7 cell has out==in
  (largest: 220,554 records, ~441 parts at #78's per-flush granularity); zero cap errors.
- **#78 (stream batching):** kafka ladder 2,000/s clean (2.0×), 3,943/s no-lag;
  webhook see §2.1. Matrix s6 throughput up (mw-M 1.63×, mw-H 1.72×, single-M 1.49×).
- **#80 (transforms):** t1 1.4–2.3×, t5 3.1–4.6× E2E; microbench add_field 8.1×,
  window_aggregate 5.3× @1k groups with near-flat group scaling (1.64× over 10→5k groups).
- **#81 (stream run history):** stream cells record 6–8 run-history rows each
  (baseline recorded none).
- **#82 (webhook 404 race):** 4xx reduced to exactly 50 per loadgen process per ladder
  step (~0.2–0.5%, start-of-step registration residue) vs the study's steady 3–3.5% 4xx.
- **#84 (counters):** skipped=0 with in==out across every matrix and ladder row; no
  spurious "no sink wrote successfully" errors. (One non-skipping error in the entire
  rerun: mw-s2csv-M-rep1.)
- **#79 (image extras):** parquet, msgpack, and protobuf fsweep cells all ran in-cluster
  end-to-end with out==in — the baked extras are functionally proven. Image sizes were
  not captured (deploy logs only).

## 5. Harness defects observed (fix before next campaign)

1. Ladder collector lost `pod_cpu_peak_m`/`mem_peak`/`peak_pod` on every rerun ladder
   row (matrix rows unaffected) — CPU-bound conclusions for ladders are currently
   impossible; restore before re-triaging webhook.
2. `deploy-mw-L.log` rollout wait hung for 5,263 minutes (Oct 1 21:24 → Oct 5 13:07);
   the single-H s4 cell stalled overnight similarly (~14 h). Detached deploys +
   poll-the-log are now mandatory (both were used for the surviving runs).
3. Bench hygiene: assert "no enabled non-bench pipelines" before matrices (the #86
   follow-up — reaffirmed; the contamination did not recur this time only because the
   leftovers were disabled on Oct 1).

## 6. What this means for the release

- Wave-1 integrity targets (#76 #77 #84 s7/cap/counters) fully verified green.
- Kafka stream, transforms, and window targets pass with margin.
- Two misses need maintainer re-triage: webhook (1.47×, confounded by lost telemetry +
  possible loadgen ceiling — a clean re-ladder with fixed collector is cheap) and
  protobuf (0.92× — cost-model finding; further gains are new scope, descope-order #1).
- The baseline study's H cells (and fsweep) need re-baselining per §3 — affects the
  sizing calculator, not the code.
