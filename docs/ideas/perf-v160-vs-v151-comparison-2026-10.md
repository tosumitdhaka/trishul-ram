# TRAM v1.6.0 vs v1.5.1 — Measured Performance Comparison (2026-10-06)

All numbers are measured, not projected. Code bases: v1.5.1 @ 326d9dd
vs `release/v1.6.0` @ 75f06fb (waves #76–#85 + #79). Evidence:
`scripts/perf/results/v1.6.0-rerun/` (full matrices, microbenches, ladders),
`scripts/perf/results/ab-86-2026-10-01/` (controlled v1.5.1 A/B),
`scripts/perf/results/v1.6.0-rerun/reladder-s1/` (clean webhook re-ladder).

**Measurement-basis caveat:** the 2026-10 capacity study's H-profile
file-batch cells were later found ~2× inflated (duration-attribution
artifact) and its mw s2pmxml/s3 cells were contaminated by leftover
registered pipelines — see the Correction section in
`perf-capacity-analysis-2026-10.md`. Where a clean v1.5.1 reference exists
(the #86 A/B), batch comparisons use it rather than the study's original
numbers. Stream, transform, and ladder baselines were not affected.

## 1. Stream pipelines (the v1.6.0 headline targets)

| metric (per 500m worker) | v1.5.1 | v1.6.0 | change | target | verdict |
|---|---:|---:|---:|---|---|
| kafka stream consumption | ~1,000 msg/s (946 lag-free / 1,049 lag-growing) | ≥2,000 sustained; 3,943 consumed==produced (producer-limited) | 2.0–3.9× | ≥2× | PASS |
| webhook stream, modest client concurrency | ~360 rps (mw plateau 1,089 ÷ 3) | ~535 rps (1,605 total) | 1.47× | ≥2× | MISS |
| webhook stream, high client concurrency (conc 400) | untested | ~428 rps (1,284 total, CPU-pinned) | 1.19× | — | plateau is concurrency-shaped |
| webhook p95 latency @ ~800 rps offered | 233 ms | 12 ms | ~20× better latency | — | — |
| kafka→local sink capacity | 99,999 records, then silent skip | one part per ~500-record flush; 220,554-record run out==in, 0 errors | cap effectively removed | functional | PASS |

Webhook miss analysis (clean re-ladder, telemetry fixed): CPU-bound —
workers pinned at the 500m limit; the plateau mechanism is
CFS-throttle-amplified queueing collapse that begins below CPU saturation
once in-flight requests pile up. Not loadgen-bound (genuinely offered
6,400 rps), not path-bound (direct worker-IP control collapsed
identically). Sizing should quote both concurrency shapes.

## 2. Transform CPU cost (microbench)

| transform | v1.5.1 (study) | v1.6.0 | change |
|---|---:|---:|---:|
| add_field | 30.8 µs/rec | 3.8 µs/rec | 8.1× |
| window_aggregate @ 500 groups | 56.6 µs/rec | 14.9 µs/rec | 3.8× |
| window_aggregate @ 1,000 groups | ~99 µs/rec | 18.6 µs/rec | 5.3× |
| window_aggregate group scaling | ~8× cost growth 10→1k groups | 1.64× growth 10→5k groups | near-flat |
| mid-tier (rename, cast, drop, value_map, coalesce) | 7–11 µs/rec | 1.0–1.3 µs/rec | ~7–10× |

Same-host back-to-back spot-check (v1.5.1 vs v1.6.0 checkouts, identical
conditions) confirmed the microbench direction: add_field 44.2 → 3.8 µs/rec.

## 3. End-to-end transform chains (matrix, kind, json @ M profile)

| chain | v1.5.1 (study) | v1.6.0 | change |
|---|---:|---:|---:|
| t1 (project+filter) | ~19k rec/s | 28k (single) / 41k (mw) | 1.5–2.1× |
| t5 (5-transform chain) | 7.8k rec/s | 24k (single) / 29k (mw) | 3.1–3.7× |
| t3 (dedup) | 76.9k | 62.5k | parity* |

\* within the study's M-profile inflation band (~5–20%); dedup was not a
v1.6.0 target.

## 4. Batch file sources (v1.5.1 reference = #86 A/B clean-host re-measure)

| cell (single topology) | v1.5.1 clean | v1.6.0 | change |
|---|---:|---:|---:|
| s2csv-H (CSV batch) | ~51.5k rec/s | 49.2k | 0.96× (parity) |
| s2pmxml-H (PM XML) | ~16.4k | 19.3k | 1.18× |
| s3-H (REST) | ~5.3k | 4.4k | 0.84× |
| s2csv-M | ~47.1k | 42.6k | 0.90× |
| s3-M | ~4.9k | 5.0k | 1.02× |
| **s7 file→kafka (>1 MB batches)** | **records_out = 0 (100% silent loss)** | **out==in==100k at all 12 cells, 0 errors, 4–10.4k rec/s** | **broken → working** |

v1.6.0 did not target the plain batch file path — parity (±18% cell noise)
is the expected and observed result. The apparent "0.4–0.9× regressions"
against the original study's H numbers are baseline inflation, not code
(see the caveat above).

## 5. Serializers

| serializer | v1.5.1 | v1.6.0 | change |
|---|---:|---:|---:|
| protobuf parse (same-host back-to-back) | 23.0 µs/rec | 15.0 µs/rec | 1.53× |
| protobuf E2E (fsweep-M) | 10.4k rec/s | 9.5k rec/s | 0.92× — MISS |
| json/csv/ndjson/avro/msgpack/pmxml | — | parity vs clean baseline | no change targeted |
| parquet | study: 77.4k (inflated + ad-hoc pyarrow) | 30.8k (image-baked `pyarrow>=16,<20`) | unproven — needs one controlled re-measure |

Protobuf miss analysis: the batch framing (#83) removed only message
construction; the dominant cost is the per-record
`MessageToDict`/`ParseDict` dict conversion, deliberately kept for wire
compatibility. `preserve_keys` changes nothing measurably. Further gains
require message→message copy without the dict round-trip — new scope.

## 6. Operational / derived planning numbers

- Kafka stream planning figure: 1,000 → ≥2,000 msg/s per 500m worker
  (pipelines needed for 3k msg/s: 3 → 2).
- A 10k rec/s json t5-class chain: 2 workers @ 500m → 1.
- Stream runs appear in run history (single topology) — invisible in v1.5.1.
- The study's mw-vs-single "2× gap" on many-small-batch sources does not
  exist: true parity 0.94–1.15× (verified twice — #86 A/B and the rerun).
- Sizing calculator fully re-baselined: `perf-sizing-calculator-2026-10.md`.

## 7. Summary

- **6 of 8 exit targets PASS**; 2 documented misses (webhook 1.19–1.47×,
  protobuf 0.92×) with root causes established.
- The dominant v1.6.0 win is integrity, not throughput: file→kafka went
  from 100% silent loss on >1 MB batches to verified lossless, and the
  local-sink silent cap-skip (99,999) is gone (fail-loud + per-flush parts).
