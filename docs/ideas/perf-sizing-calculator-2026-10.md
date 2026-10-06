# TRAM Sizing Calculator — 2026-10

Derived sizing model from the capacity study (v1.5.1 @ 326d9dd). Inputs:
`perf-capacity-analysis-2026-10.md` (measured medians + validated scaling
factors), `perf-improvement-candidates.md` (known caps that bound these
numbers). All rates are **per 500m-CPU worker pod** (mgr+worker topology)
unless noted. Model cells marked **[M]** are measured medians; **[D]** are
model-derived (validated factors, combination not separately measured).

**Re-baselined 2026-10-06** against the v1.6.0 harness re-run
(`scripts/perf/results/v1.6.0-rerun/`, code `release/v1.6.0` @ b6c780c):
the study's H-profile file-batch cells were ~2× inflated (see the study's
Correction section), its M-profile cells ~5–20% high, and v1.6.0 changed
the transform/stream economics (#78 #80 #83). Values annotated
"(v1.6.0 re-measure 2026-10-06)" supersede the study's.

## 1. Model

```
batch_rate(format, chain) = 73k / (format_factor × chain_factor)      rec/s @ 500m
final_rate               = min(batch_rate, source_cap, sink_cap)
cpu scaling              : L (250m) ≈ 0.5× · M (500m) = 1× · H (2cpu) ≈ 2×  (verified linear)
streams                  : per-message costs were removed in v1.6.0 (#78) — use measured rates, not the model
```

- **Base**: json→json local file batch = 73k rec/s @ 500m [M — v1.6.0 re-measure
  2026-10-06 (fsweep json-M 72.9k); the study's 87.1k was ~15% high]
- **Format factors** (divide the json rate): json 1.0 · msgpack 0.54 ·
  parquet 2.4 (pyarrow-version caveat: image bakes `pyarrow>=16,<20`; the
  study used an ad-hoc newer install — controlled re-measure pending if
  parquet sizing matters) · csv 1.45 · ndjson 1.65 · avro 2.7 · pmxml 7.0 ·
  xml 8.9 · protobuf 7.7 [M — each format's v1.6.0 fsweep]
- **Chain factors** (divide again): none 1 · t3-class (dedup) 1.17 ·
  t1-class (project+filter) 1.79 · t2-class (flatten+enrich) 1.97 ·
  t5-class (5-transform chain) 2.5 [M — each chain measured on json,
  v1.6.0; #80 made project/filter cheaper than flatten+enrich]

The multiplicative model is conservative: the executor's fixed overhead is
shared, so real heavy combos tend to run slightly *better* than the model.

## 2. Combined table — batch rec/s per 500m worker (local → local)

v1.6.0 re-measure anchors: format "none" column = fsweep-M medians; json row =
matrix-C mw-M medians; the rest is the multiplicative model on those anchors.

| format \ chain | none | t3 (dedup) | t2 (flat+enrich) | t1 (proj+filter) | t5 (5-chain) |
|---|---:|---:|---:|---:|---:|
| msgpack | 134k [M] | 116k [D] | 69k [D] | 76k [D] | 54k [D] |
| **json** | **73k [M]** | **62k [M]** | **37k [M]** | **41k [M]** | **29k [M]** |
| parquet | 31k [M] | 26k [D] | 16k [D] | 17k [D] | 12k [D] |
| csv | 51k [M] | 43k [D] | 26k [D] | 28k [D] | 20k [D] |
| ndjson | 44k [M] | 38k [D] | 23k [D] | 25k [D] | 18k [D] |
| avro | 27k [M] | 23k [D] | 14k [D] | 15k [D] | 11k [D] |
| pm_xml | 10.4k [M] | 8.9k [D] | 5.3k [D] | 5.8k [D] | 4.2k [D] |
| xml | 8.2k [M] | 7.0k [D] | 4.2k [D] | 4.6k [D] | 3.3k [D] |
| protobuf | 9.5k [M] | 8.1k [D] | 4.8k [D] | 5.3k [D] | 3.8k [D] |

## 3. Source/sink path caps (apply `min` against §2)

| path | cap @ 500m | binding constraint |
|---|---:|---|
| local file → local file | none (§2 governs) | CPU |
| sftp source | ~10.5–11k rec/s aggregate (flat M→H; v1.6.0 re-measure — study's ~13k was ~15% high) | network/SFTP server |
| rest → rest | ~5k @ M, parity both topologies (the study's mw 3.3k was leftover-pipeline contamination — #86 A/B) | per-request source I/O |
| → kafka sink | ~5k @ M / ~9k @ H (v1.6.0 s7 re-measure; functional at any batch size — #76 chunking) | produce |
| snmp poll (walk) | 1.65 rows/s per pipeline | RTT pacing — size by walk-duration × frequency, never CPU |
| webhook stream → * | ~430–535 rps/worker (concurrency-shaped: 535 @ low client concurrency, 428 @ conc 400; CPU-bound plateau) | per-ingress CPU + CFS queueing collapse below saturation |
| kafka stream → * | ≥2,000 msg/s no-lag (3,943 measured, producer-limited) | single placement per pipeline |
| → local sink (stream) | no practical cap — one part per ~500-record flush (#78); 220k-record run verified out==in | — |

## 4. Streams — measured rates (do not use §2)

| stream class | rate per 500m worker | ceiling behavior | workers for 10k/50k/100k msg |
|---|---:|---|---|
| webhook → local json | ~535 rps/worker @ modest client concurrency (~428 @ conc 400) — CPU-bound plateau, clean re-ladder 2026-10-06 | latency good until the cliff (p95 12 ms at ~775/worker offered @ conc 100); CFS-throttle-amplified queueing collapse starts below CPU saturation at high client concurrency | 23 workers @ conc-400 basis for 10k msg (~19 @ conc-50) |
| kafka → local json | ≥2,000 msg/s no-lag | 3,943 consumed==produced, producer-limited (no lag signal); one placement per pipeline | 5 / 25 / 50 |

Topology adjustments: batch = parity across the board including rest/pm_xml
many-small-batch (the study's single 1.8–2× exception was leftover-pipeline
contamination on the mw side — #86 A/B). Single-pod webhook: ~357 rps
(re-run conc-50 reference; the re-ladder's single steps 3–6 were
host-contaminated and only bound it from below). H (2cpu) stream ceilings
were NOT verified — do not assume 2× without re-measurement.

## 5. Worked examples

| target workload | calc | sizing |
|---|---|---|
| 40k rec/s json + dedup | 40k / 62k | 1 worker (1 pipeline) |
| 100k rec/s csv plain | 100 / 51 | 2 sharded pipelines @ 500m (or 1 @ 2cpu) |
| 25k rec/s pm_xml + project/filter | 25 / 5.8 | 5 sharded pipelines @ 500m |
| 10k rec/s json + 5-transform chain | 10 / 29 | 1 pipeline @ 500m |
| 5k rps webhook ingest | 5,000 / ~430 | 12 workers @ 500m (conc-400 basis; ~9 if client concurrency stays modest) |
| 3k msg/s kafka consume | 3,000 / 2,000 | 2 pipelines (distinct groups) — single-placement cap |
| sftp→local 20k rec/s | net cap ~10.5–11k | impossible per pipeline — shard files across 2+ pipelines |
| 50k-row SNMP table / 15-min | 50k × 0.6s/row | 5.2h per walk — 1 pipeline is fine (idle CPU), schedule accordingly |

## 6. Rules of thumb

1. Streams still cost far more than the batch path per record — kafka
   ~36× after v1.6.0 micro-batching (down from ~87×), webhook ~170× —
   prefer file/batch sources wherever latency allows.
2. A batch pipeline uses exactly ONE worker — scale by sharding input
   across pipelines, never by adding workers.
3. Network/RTT-bound sources (sftp, snmp) ignore CPU — parallelize by
   pipeline, not resources.
4. Webhook capacity is client-concurrency-shaped (~535/worker at modest
   concurrency vs ~428 at conc 400): the collapse is CFS-throttle-amplified
   queueing that starts *below* CPU saturation once in-flight requests
   pile up — keep per-worker client concurrency low (~100) and size at the
   conc-400 figure for headroom.
5. ~~Long streams (>100k records/placement) hit the local-sink part cap~~ —
   fixed in v1.6.0 (#77/#78): streams consume one part per ~500-record
   flush, not per record; a 220k-record run completed with out==in.
