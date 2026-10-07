# v1.7.0 Protobuf baseline anchor — fresh v1.6.1 measurement

Purpose: the v1.7.0 plan (`docs/plans/perf-v161-v170-plan.md`) defines the
protobuf pilot's acceptance target as **2× a fresh v1.6.1 baseline**, because
the v1.6.0 format-sweep reference (9,527 rec/s at M) had regressed 0.92× vs
v1.5.1 (10,363) for unexplained reasons. This directory records that fresh
measurement.

## Method

Same methodology as the v1.6.1 A/B campaign
(`scripts/perf/results/v161-ab-campaign/`): kind cluster (mw topology,
500m/1Gi workers — equal CPU), `fsweep_protobuf` template (local
length-delimited CdrRecord `.pb` in → protobuf out, no transforms),
10×10k-record corpus, 5 reps, per-run history from `/api/runs`.
Driver: `run_ab.py` (side `v161`, scenario `fsweep_protobuf`).

- Image: `trishul-ram-worker:local-20261007072806` (= v1.6.1 @ `84117f0`)
- Fixtures staged per `scripts/perf/results/v1.6.0-rerun/drivers/restage.sh`
  (in-pod binary conversion via TRAM's own serializers, roundtrip-verified)
- All 5 runs executed on `trishul-ram-worker-0`; 100k in / 100k out, 0 errors,
  0 skipped, every rep

## Results

| rep | wall_s | rec/s (in = out) |
|---|---:|---:|
| 1 | 9.8 | 10,204.1 |
| 2 | 9.1 | 10,989.0 |
| 3 | 9.0 | 11,111.1 |
| 4 | 9.0 | 11,111.1 |
| 5 | 9.0 | 11,111.1 |

**Median: 11,111.1 records/s** — the v1.6.0 dip did not reproduce
(1.07× v1.5.1's 10,363; 1.17× v1.6.0's 9,527).

## Implied v1.7.0 pilot test goal

2 × anchor = **≈22.2k records/s** (replaces the provisional ~19k figure,
which was anchored to the v1.6.0 number).

## Artifacts

- `run_ab.py` — the exact driver invoked (side `v161`, scenario
  `fsweep_protobuf`, rep 1–5)
- `ab-v161-fsweep_protobuf-rep{1..5}/bench-summary.json` + `run-rows-raw.json`
  — per-run metrics and raw run-history rows
