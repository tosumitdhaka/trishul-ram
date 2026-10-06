# Sizing-calculator updates — v1.6.0 re-measure (2026-10-06)

Old→new per affected cell, for the refresh of `docs/ideas/perf-sizing-calculator-2026-10.md`.
Two classes of change: (a) genuine v1.6.0 code deltas (streams, transforms), and
(b) **baseline corrections** — the study's H-profile file-batch cells were ~2× inflated
(duration-attribution artifact; see RESULTS.md §3), so those "old" values must be
re-baselined to the #86 A/B / rerun values, not treated as regressions.

## Values that change because of v1.6.0 code

| quantity | old (study) | new (v1.6.0) | basis |
|---|---|---|---|
| kafka stream capacity / 500m worker | ~1,000 msg/s (946 lag-0 / 1,049 lag-growing) | ≥2,000 clean, 3,943 no-lag | s6 ladder, single-placed worker |
| webhook stream sustained | ~360 rps/worker (mw plateau 1,089 / 3) | ~535/worker plateau (1,605 / 3); 800 offered at p95 12 ms | s1 ladder (telemetry-limited; see RESULTS §2.1) |
| 5-transform chain (t5) | 7.8k rec/s @M | ~24k single-M / ~29k mw-M (3.1–3.7×) | matrix C |
| 1-transform chain (t1) | 19k rec/s @M | ~28k single / ~41k mw | matrix C |
| add_field | 30.8 µs/rec | 3.8 µs/rec (8.1×) | microbench |
| window_aggregate @500 groups | 56.6 µs/rec | 14.9 µs/rec | microbench |
| window_aggregate group scaling | ~8× cost growth 10→1k groups | 1.64× growth 10→5k groups | probe |
| mid-tier transforms (rename/cast/drop/value_map/coalesce) | 7–11 µs/rec | ~1–1.3 µs/rec | microbench |
| file→kafka (s7 class) | broken (out=0 >1 MB batches) | functional, 4.0–10.4k rec/s @H both topologies | matrix A |
| kafka-stream local-sink part consumption | 1 part per record (cap at 99,999) | 1 part per flush (~500 records) | s6 cells to 220k records, out==in |

## Values that change because the baseline was wrong (re-baseline)

| cell (single unless noted) | study value | re-baseline to | basis |
|---|---|---|---|
| s2csv H | 111,111 | ~50k | A/B 50.2k/52.7k; rerun 49.2k |
| s2csv M | 51,587 | ~44k | A/B ~47k; rerun 42.6k |
| s2pmxml H | 38,462 | ~18k | A/B 16.0k/16.8k; rerun 19.3k |
| s2pmxml M | 17,712 | ~14k | A/B ~15k; rerun 12.9k |
| s3 H | 11,458 | ~5k | A/B 5.2k/5.4k; rerun 4.4k |
| s3 M | 6,417 | ~5k | A/B ~4.9k; rerun 5.0k |
| fsweep csv H | 111,111 | ~77k (rerun) | rerun only (no A/B fsweep) |
| fsweep json H | 166,667 | ~127k (rerun) | rerun only |
| fsweep parquet M/H | 77.4k / 183.3k | ~30.8k / ~50.5k (rerun) | rerun; pyarrow-version delta unproven — controlled re-measure if parquet sizing matters |
| fsweep protobuf M | 10.4k | 9.5k (unchanged class) | rerun |
| fsweep avro/msgpack/xml/ndjson M–H | study values | ×0.8–0.92 (rerun) | rerun; treat study values as ~10–20% high |
| mw s2pmxml/s3 M | 9.8k / 3.3k | 16.8k / 5.3k | #86 parity (contaminated study cells) |

## Derived planning numbers affected

- **Per-500m-worker stream capacity** (kafka): 1,000 → ≥2,000 msg/s.
- **Transform-heavy pipelines**: the "json + t5-class chain" row (~7,800 rec/s @M)
  becomes ~24k rec/s @M.
- **Webhook per-CPU figure** (~726 rps/CPU in the study): recompute only after a clean
  re-ladder with fixed CPU telemetry — the 1.47× plateau is not trustworthy enough to
  publish a per-CPU number.
- **File-batch sizing rows** (s2/s3/s5 class): recompute from the re-baselined values
  above; do NOT compute "v1.6.0 regression" deltas against the study's H cells.
