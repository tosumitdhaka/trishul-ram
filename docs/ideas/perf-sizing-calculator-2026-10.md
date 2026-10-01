# TRAM Sizing Calculator — 2026-10

Derived sizing model from the capacity study (v1.5.1 @ 326d9dd). Inputs:
`perf-capacity-analysis-2026-10.md` (measured medians + validated scaling
factors), `perf-improvement-candidates.md` (known caps that bound these
numbers). All rates are **per 500m-CPU worker pod** (mgr+worker topology)
unless noted. Model cells marked **[M]** are measured medians; **[D]** are
model-derived (validated factors, combination not separately measured).

## 1. Model

```
batch_rate(format, chain) = 87.1k / (format_factor × chain_factor)   rec/s @ 500m
final_rate               = min(batch_rate, source_cap, sink_cap)
cpu scaling              : L (250m) ≈ 0.5× · M (500m) = 1× · H (2cpu) ≈ 2×  (verified linear)
streams                  : per-message path dominates — use measured rates, not the model
```

- **Base**: json→json local file batch = 87.1k rec/s @ 500m [M]
- **Format factors** (divide the json rate): json 1.0 · msgpack 0.6 ·
  parquet 1.1 · csv 1.5 · ndjson 1.6 · avro 2.9 · pmxml 6.9 · xml 7.5 ·
  protobuf 8.4 [M — each format's standalone fsweep]
- **Chain factors** (divide again): none 1 · t3-class (dedup) 1.13 ·
  t2-class (flatten+enrich) 3.1 · t1-class (project+filter) 4.6 ·
  t5-class (5-transform chain) 11.1 [M — each chain measured on json]

The multiplicative model is conservative: the executor's fixed ~7 µs/rec
overhead is shared, so real heavy combos tend to run slightly *better* than
the model. Discrepancy at t5-on-protobuf scale (~1k rec/s) is within the
run-to-run spread.

## 2. Combined table — batch rec/s per 500m worker (local → local)

| format \ chain | none | t3 (dedup) | t2 (flat+enrich) | t1 (proj+filter) | t5 (5-chain) |
|---|---:|---:|---:|---:|---:|
| msgpack | 146k [M] | 128k [D] | 47k [D] | 32k [D] | 13k [D] |
| **json** | **87k [M]** | **77k [M]** | **28k [M]** | **19k [M]** | **7.8k [M]** |
| parquet | 77k [M] | 70k [D] | 26k [D] | 17k [D] | 7.1k [D] |
| csv | 57k [M] | 51k [D] | 19k [D] | 13k [D] | 5.2k [D] |
| ndjson | 56k [M] | 48k [D] | 18k [D] | 12k [D] | 4.9k [D] |
| avro | 30k [M] | 27k [D] | 9.7k [D] | 6.5k [D] | 2.7k [D] |
| pm_xml | 12.6k [M] | 11k [D] | 4.1k [D] | 2.7k [D] | 1.1k [D] |
| xml | 11.6k [M] | 10k [D] | 3.7k [D] | 2.5k [D] | 1.0k [D] |
| protobuf | 10.4k [M] | 9.2k [D] | 3.3k [D] | 2.3k [D] | 0.9k [D] |

## 3. Source/sink path caps (apply `min` against §2)

| path | cap @ 500m | binding constraint |
|---|---:|---|
| local file → local file | none (§2 governs) | CPU |
| sftp source | ~13k rec/s aggregate (flat M→H) | network/SFTP server |
| rest → rest | ~3.3k (mw) / ~6.4k (single) | per-batch bookkeeping (candidate #12) |
| → kafka sink (post-fix, ≤1k-rec chunks) | ~6.4k (12.8k @ 2cpu) | produce + chunking |
| snmp poll (walk) | 1.65 rows/s per pipeline | RTT pacing — size by walk-duration × frequency, never CPU |
| webhook stream → * | ~360 rps per worker | per-message path (json; format is second-order, unmeasured) |
| kafka stream → * | ~1,000 msg/s per worker | per-message path (single placement per pipeline) |
| → local sink (stream) | 99,999 records per placement, then silent skip (candidate #2) | product cap |

## 4. Streams — measured rates (do not use §2)

| stream class | rate per 500m worker | ceiling behavior | workers for 10k/50k/100k rps |
|---|---:|---|---|
| webhook → local json | ~360 rps | CPU-linear in workers; latency cliff above ~⅔ ceiling; no drops | 28 / 139 / 278 |
| kafka → local json | ~1,000 msg/s | lag growth past ceiling (no error signal); one placement per pipeline | 10 / 50 / 100 |

Topology adjustments: single pod = 280–300 rps webhook (manager tax ~23%);
batch = parity (except rest/pm_xml many-small-batch: single 1.8–2×, pending
candidate #12 re-run). H (2cpu) stream ceilings were NOT verified — do not
assume 2× without re-measurement.

## 5. Worked examples

| target workload | calc | sizing |
|---|---|---|
| 40k rec/s json + dedup | 40k / 77k | 1 worker (1 pipeline) |
| 100k rec/s csv plain | 100 / 57 | 2 sharded pipelines @ 500m (or 1 @ 2cpu) |
| 25k rec/s pm_xml + project/filter | 25 / 2.7 | 10 sharded pipelines @ 500m |
| 10k rec/s json + 5-transform chain | 10 / 7.8 | 2 pipelines @ 500m |
| 5k rps webhook ingest | 5,000 / 360 | 14 workers @ 500m (~5 @ 2cpu, unverified) |
| 3k msg/s kafka consume | 3,000 / 1,000 | 3 pipelines (distinct groups) — single-placement cap |
| sftp→local 20k rec/s | net cap 13k | impossible per pipeline — shard files across 2+ pipelines |
| 50k-row SNMP table / 15-min | 50k × 0.6s/row | 5.2h per walk — 1 pipeline is fine (idle CPU), schedule accordingly |

## 6. Rules of thumb

1. Streams cost 40–100× the batch path per record — prefer file/batch
   sources wherever latency allows.
2. A batch pipeline uses exactly ONE worker — scale by sharding input
   across pipelines, never by adding workers.
3. Network/RTT-bound sources (sftp, snmp) ignore CPU — parallelize by
   pipeline, not resources.
4. Stay under ~⅔ of a stream's measured ceiling to keep p95 in single-digit
   ms (webhook cliff starts ~400–800 offered per 500m worker).
5. Long streams (>100k records/placement) hit the local-sink part cap
   today — fix candidate #2 or size runs below it.
