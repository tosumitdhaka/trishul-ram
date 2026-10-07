# v1.6.1 A/B campaign — affected pipelines on the kind cluster

Pipeline-level acceptance evidence for the v1.6.1 locked scope
(docs/roadmap.md §v1.6.1; docs/plans/perf-v161-v170-plan.md). Complements the
operation-level paired re-measure in `../v161-remeasure/`.

## Method

Same cluster, same 500m/1Gi worker profile (helm `--reuse-values`, verified
equal), same 10×10k-record canonical CDR corpus, same sink configuration —
only the images differ:

- **v160 side**: images `local-20261006092713` (release/v1.6.0 @ `fdcf371`,
  the deployed v1.6.0 build)
- **v161 side**: images `local-20261007072806` (release/v1.6.1 @ `8478b14`:
  the three perf commits + review fix; no version bump, so `/api/meta` still
  reports 1.6.0 — the image tag is the discriminator)

5 reps per scenario per side, interleaved staging (fresh `/data/perf/in`
per rep from `/data/perf/src/json`), driver `run_ab.py` (adapted from the
v1.6.0-rerun `run_cell.py` mechanics: register → run → poll → collect →
delete; run-history rows diffed against a pre-run snapshot). Throughput
metric: records_in/s of run-history wall time (t4's output is window
aggregates — records_out is 8 by design; t6's is a routed subset).

## Results (median of 5 reps, records_in/s; all reps in ab-results.csv)

| scenario | v1.6.0 | v1.6.1 | speedup | ≥20% target |
|---|---:|---:|---:|---|
| t4 counter_delta+window_aggregate (timestamp kernel) | 18,182 | 43,478 | **2.39x** | PASS |
| t6 2 conditional sinks, thread_workers 4 | 14,084 | 55,556 | **3.94x** | PASS |
| s7 kafka sink, keyed (control — unchanged path) | 6,211 | 6,024 | 0.97x | control, no regression |
| s7k kafka sink, keyless (fast-path eligible) | 6,289 | 5,952 | 0.95x | **not met end-to-end** |

Output parity: t4 records_out=8 every rep (both sides); t6 records_out=99,062
every rep; s7/s7k records_out=100,000 and **bytes_out=56,278,896 identical on
both sides** (byte-faithful). Zero errors/skips on all 40 reps.

## The s7k finding (honest negative, explained)

The fast path WAS engaged on the v1.6.1 side: default `batch_size` is 500
(`tram/models/pipeline.py:412`) → ~281 KB serialized payloads, within both
`chunk_bytes` (524,288) and `chunk_records` (1000), keyless — every write
eligible. Yet end-to-end throughput is unchanged because this scenario is
**ack-latency-bound, not CPU-bound**: 200 writes per run, each a synchronous
`acks=all` broker round trip (~80 ms apiece ≈ the entire 16.6 s wall). The
re-parse/re-size CPU the fast path removes is ~0.3–0.5 s per 100k records
(~2–3%), inside run-to-run noise — matching the operation-level measurement
(21.5x on the write call itself, `../v161-remeasure/`).

Conclusion: the Kafka fast path's benefit is per-write CPU (and the near-cap
single-message semantics change), visible end-to-end only when the sink write
path is CPU-bound — e.g. higher write rates with a keeping-up broker or CPU
contention. On this single-pipeline sync-acks shape it neither helps nor
harms (0.95–0.97x is within the two sides' overlapping rep ranges; the keyed
control moved the same direction).

## Artifacts

- `run_ab.py` — driver (stage → run_one.sh → run-history diff → CSV)
- `ab-results.csv` — all 20 rows (both sides × 4 scenarios × 5 reps)
- `results/<run_id>/` — per-run bench-summary.json, run-rows-raw.json,
  collector samples/meta, run.log
- Templates: `scripts/perf/templates/t6_sink_conditions.yaml` (new) and
  `s7k_local_kafka_keyless.yaml` (new); s7/t4 pre-existing

## Reproduce

Deploy one side's images (scripts/deploy-kind-tram-dev.sh), stage
`/data/perf/src/json` on all workers, then for rep 1..5 and scenario
t4/t6/s7/s7k: `.venv/bin/python scripts/perf/results/v161-ab-campaign/run_ab.py
<v160|v161> <scenario> <rep>` (from repo root, cluster at mw topology).
