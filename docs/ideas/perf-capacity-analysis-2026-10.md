# TRAM Performance & Capacity Analysis — 2026-10

Independent analysis of the Phase 2 cluster runs (v1.5.1 @ 326d9dd) on the
4-node kind cluster `tram-dev`. All medians and derived figures in this
document were recomputed directly from the CSVs under `scripts/perf/results/`
(pandas-free recomputation script; see §10 for the cross-check against the
runner-claimed figures).

- Runbook / methodology: `scripts/perf/README.md`
- Deploy log / caveats: `scripts/perf/results/deploy-state.md`
- Environment: `scripts/perf/infra/CLUSTER.md`, `scripts/perf/infra/PORTMAP.md`
- Raw per-run artifacts: `scripts/perf/results/<run-id>/`
- Microbenchmarks: `scripts/perf/microbench/results/`

---

## 1. Executive summary

Headline capacities (median of 2 clean reps unless noted):

| Workload | Capacity | Binding constraint |
|---|---|---|
| Webhook → local (stream, 3 × 500m workers) | **≈1,050–1,090 rps** | worker CPU (500m pinned); p95 cliff above ~400–800 offered |
| Webhook → local (standalone pod @ 500m) | **≈280–300 rps** (~¼ of the 3-worker fleet) | pod CPU (manager+worker share 500m) |
| Kafka → local (stream, per 500m worker) | **≈950–1,050 msg/s** with real sink writes | per-message sink write + consumer; lag grows beyond |
| Local → Kafka (batch) | **0 rec/s** (product bug) — **≈12.8k rec/s** with ≤1k-record file batches (single data point @ H) | 5.6 MB file-batch > kafka client 1 MB `max_request_size` |
| File batch json→json (1 × 500m worker) | **≈87k rec/s** (L 43k, H 167k) | CPU, ~linear L→M→H |
| File batch msgpack / parquet / csv | 146k / 77k / 57k rec/s @ M | CPU, linear |
| File batch protobuf / xml / pmxml | 10.4k / 11.6k / 12.6k rec/s @ M | per-record (de)serialize CPU |
| SFTP → local csv | ≈13k rec/s, flat M→H | network/SFTP-bound, not CPU |
| SNMP walk (1000 rows × 8 cols) | ≈600 s per walk at M+ (1.65 rows/s) | RTT pacing, profile-insensitive |
| 5-transform chain (t5) | 7.8k rec/s @ 500m (0.09× json baseline) | simpleeval per-record compile dominates |

Practical takeaways:

1. **Streams are expensive, batches are cheap.** The per-message stream path
   costs ~0.5–1 ms CPU per record (webhook ~360 rps per 500m worker; kafka
   ~1,000 msg/s per 500m worker) versus 5–12 µs per record for file batches.
   The dominant stream cost is the per-message sink write, not parsing.
2. **A batch pipeline runs on one worker** (all matrix-A/B/C batch rows have
   `rows=1`): fleet size does not speed up a single batch pipeline. Scale by
   sharding input across pipelines, not by adding workers.
3. **Kafka stream pipelines placed on exactly one worker** in every s6 run
   (`rows=1`) while webhook streams spread across all workers (`rows=2–3`)
   — consistent with `TRAM_STREAM_SINGLE_PLACEMENT=1`. Per-pipeline kafka
   consumption is capped at one worker until that default is changed.
4. Two product bugs destroy data silently at scale (s7 kafka sink: 100 %
   loss, run still "success"; local sink 99,999-part cap: records beyond
   99,999 per sink instance silently skipped). See §9.

## 2. Methodology recap

See `scripts/perf/README.md` §7–8 for the full protocol.

- **Workload**: canonical CDR, 20 flat fields, ~523 B as compact JSON,
  deterministic (seed 42); batch inputs are 10 files × 10,000 records.
- **Measured window**: 60 s ramp + 180 s steady state, 2 reps per cell,
  median reported. `kubectl top` sampled every 5 s; run history fetched after
  stop (rep-filtered by start time).
- **Topologies**: `mgr+worker` = manager (fixed M) + 3 workers, each at the
  profile; `single` = one standalone pod (manager+worker in-process) at the
  profile.
- **Profiles** (requests = limits): L = 250m/512Mi, M = 500m/1Gi,
  H = 2cpu/2Gi per pod. Kind nodes share one WSL2 host; cluster aggregate
  ≈12 CPU / ~15 Gi (CLUSTER.md) — profile limits, not node capacity, are the
  binding constraint below H.
- **Scenarios**: S1 webhook→local (stream), S2 local csv / pm_xml→local,
  S3 rest→rest, S4 snmp walk→local, S5 sftp→local, S6 kafka→local (stream),
  S7 local→kafka; Matrix B = 9-serializer format sweep; Matrix C = transform
  benches t1–t5.

### Caveats applied (load-bearing)

- **s1 single-topology numbers are log-derived**: stream runs never reach run
  history in single mode (`controller._stream_worker` never calls
  `manager.record_run`); intake/out were taken from the pod log's
  "Stream run ended" line. Batch runs DO persist (with
  `TRAM_MANAGER_URL=http://localhost:8765` set).
- **s6 M/H `records_out` is pinned at 99,999** (local-sink cap, §9.2) — the
  CSV's 555.5 rec/s "throughput" for those cells is meaningless. Capacity is
  read from intake rate + consumer lag (saturation ladder), not records_out.
  Post-cap intake (~1,300–1,500/s) is *not* a valid capacity figure either —
  once the sink stops writing, the skip path is cheap and the consumer runs
  ~40 % faster than it can sustain with real writes.
- **s7 records_out = 0 at every cell** — product bug (§9.1), a finding, not
  a perf number. The working-config diagnostic (100 × 1,000-record files)
  gave 12.8k rec/s @ H.
- **s4 is duration-bounded** (SNMP RTT pacing), not CPU-bound; s4-mw-L was
  time-box aborted at 1,270 s (rep2 produced no row at all) — treat mw-L as
  ">1,270 s, incomplete". Single-topology s4-L completed in ~600 s; the
  mw-L vs single-L walk discrepancy is unexplained (§3.4).
- **CPU peaks**: batch runs often finish within one metrics-server scrape;
  mid-run rep values are the only credible ones. `pod_cpu_peak_m` /
  `pod_mem_peak_mi` follow a max-across-pods convention, so **kafka-0
  (~1.05 Gi after the kafka scenarios) contaminates the mem columns** of
  phase-2a rows, and rows whose `peak_pod` is kafka-0 (all s6 matrix rows)
  report kafka's CPU — the worker's stream CPU is invisible there.
  Single-topology stream CPU readings (2–3 m at 300 rps) are physically
  impossible and were discarded.
- **`records_skipped` is double-counted on the stream skip path**: in all 10
  affected stream runs `records_skipped` = exactly 2 × (`records_in` −
  `records_out`); true skipped = in − out. Batch rows (s7) show in − out
  (+10, one per input file). Runners' summaries that echo this field
  overstate stream losses by 2×.
- **Host suspends**: two WSL2 host suspends contaminated single-s1-M-rep1,
  single-s3-M-rep2 and single-s5-M-rep2; those cells were re-run clean and
  only clean rows are in the CSVs used here. `mw-s1-H-rep1` is contaminated
  by the placement race (28.6 % 4xx, one placement never arrived — rows=2);
  rep2 is the valid rep.
- **t1 filter output 79,781/100k is correct** (filter drops ~20 %); t4 emits
  8 window aggregates (correct for the random-timestamp corpus); t3 out=100k.

## 3. Results per scenario

Medians recomputed from `matrix-a-mw.csv` / `matrix-a-single.csv`. Batch
throughput = records_in / duration (all batch runs completed 100k in = out
except where noted). Stream intake = records_in / 180 s.

### 3.1 S1 — webhook → local (stream)

Offered load per profile: L 100/s, M 300/s, H 800/s (matrix); saturation
ladders separately (§6).

| topology | profile | offered | intake (rep1/rep2) | p95 | notes |
|---|---|---|---|---|---|
| mw | L | 100/s | 100.0 / 100.0 rps | 2.5–2.6 ms | 0 errors, CPU 156–171 m |
| mw | M | 300/s | 299.6 / 296.7 rps | 2.1–2.7 ms | 0 4xx rep2; rep1 77 4xx (race) |
| mw | H | 800/s | 494.6 / 692.4 rps | 72 ms | rep1 race-contaminated; rep2 loadgen-limited (sent 124,639 of 144,000) |
| single | L | 100/s | 99.9 / 100.0 rps | — (log-derived) | |
| single | M | 300/s | 299.7 / 299.0 rps | — | all accepted |
| single | H | 800/s | 610.1 / 652.9 rps | — | records_out pinned 99,999 (sink cap); loadgen sent only 76–82 % of offered |

At M, both topologies absorb 300 rps comfortably (mw p95 2.1–2.7 ms).
The **H cell does not establish an H ceiling**: workers used only 399–464 m
of their 2-CPU limit while p95 rose to 72 ms and the loadgen fell behind
(client-side concurrency cap). True mw-H capacity is ≥692 rps and unmeasured;
do not read the mw H numbers as a regression versus M.

Scaling efficiency: from the saturation ladder at M (§6), webhook capacity is
CPU-linear in worker count — ~363 rps per 500m worker, 1,090 rps for 3
workers. Single-topology per-pod efficiency is ~280 rps per 500m
(manager + webhook ingress + worker share one CPU budget), i.e. ~77 % of a
dedicated worker.

### 3.2 S2 — local file → local file (csv, pm_xml)

| scenario | topo | L | M | H | M/L | H/M |
|---|---|---|---|---|---|---|
| s2csv (rec/s) | mw | 26,741 | 55,356 | 107,721 | 2.07 | 1.95 |
| s2csv | single | 27,778 | 51,587 | 111,111 | 1.85 | 2.15 |
| s2pmxml (rec/s) | mw | 4,598 | 9,760 | 20,601 | 2.12 | 2.11 |
| s2pmxml | single | 8,104 | 17,712 | 38,462 | 2.18 | 2.17 |

CSV scales ~linearly with the CPU profile (2× per doubling, both topologies,
parity between topologies). pm_xml is ~5× more expensive per record and, in
mw mode, runs ~1.8× slower than single at the same profile — see §7 for this
anomaly (it is workload-specific: csv shows parity, pm_xml and s3 do not).

### 3.3 S3 — rest → rest

| topo | L | M | H | M/L | H/M |
|---|---|---|---|---|---|
| mw | 1,523 | 3,315 | 6,020 | 2.18 | 1.82 |
| single | 3,126 | 6,417 | 11,458 | 2.05 | 1.79 |

mw-s3-M pins the worker at exactly 500 m while single does 1.94× the
throughput at ~217 m observed CPU — the same ~2× topology gap as pm_xml
(§7). Note every single-s3 run records 1 spurious
"Records skipped — no sink wrote successfully" error although the rest sink
wrote all 100k records (posts=1000) — misleading error, not data loss.

### 3.4 S4 — SNMP walk → local

| topo | L | M | H |
|---|---|---|---|
| mw | >1,270 s (aborted, incomplete) | 616 s | 608 s |
| single | 601 s | 592 s | 586 s |

Duration-bounded by RTT pacing (~8000 OIDs per walk), essentially
profile-insensitive at M+. The mw-L abort is a time-box artifact, but note
single-L completed the identical walk in ~600 s — the mw-L slowness is
unexplained and worth one controlled re-run before trusting mw for
SNMP-heavy schedules. CPU is idle (~60–100 m) — never size SNMP capacity on
CPU; size on walk duration × poll frequency.

### 3.5 S5 — sftp → local csv

| topo | L | M | H | M/L | H/M |
|---|---|---|---|---|---|
| mw | 10,703 | 12,840 | 13,472 | 1.20 | 1.05 |
| single | 9,854 | 9,768 | 12,908 | 0.99 | 1.32 |

Sub-linear in CPU — the atmoz/sftp server / network path saturates around
13k rec/s (~7.5 MB/s of CSV). More CPU does not help; parallelize with more
pipelines/files. single-M is 24 % below mw-M (high run-to-run SFTP
variance; rep spread 8.5k–11k).

### 3.6 S6 — kafka → local (stream)

Offered: L 500, M/H 2000 msg/s (documented judgment call, deploy-state).

| topo | profile | intake (rep1/rep2) | records_out | loadgen achieved |
|---|---|---|---|---|
| mw | L | 500 / 500 | 90,000 (real) | 500/s, lag 0 |
| mw | M | 1,397 / 1,451 | 99,999 (capped) | 1,397 / 1,451/s |
| mw | H | 1,383 / 1,359 | 99,999 (capped) | 1,383 / 1,359/s |
| single | M | 1,201 / 1,497 | 99,999 (capped) | — |
| single | H | 1,301 / 1,288 | 99,999 (capped) | — |

M and H intake are **identical within noise** (~1,300–1,500/s) — the
post-cap skip path is not CPU-bound, so these matrix numbers cannot be read
as "kafka consumption doesn't scale with CPU". The honest capacity figure is
the saturation ladder (§6): **~950–1,050 msg/s per 500m worker with real
sink writes** (worker CPU pinned ~500 m at M in sat-s6). All runs placed on
a single worker (`rows=1`).

### 3.7 S7 — local → kafka (batch)

**0 records out at every cell, both topologies** — every 10k-record file
batch serializes to 5.6 MB, exceeding kafka-python's 1 MB
`max_request_size`; all 100k records skipped per run, run status "success".
Diagnostic `diag-s7-100x1000-H` (100 × 1,000-record files) completes
100k/100k at 12.8k rec/s @ H. This is a product finding (§9.1), not a
capacity number.

## 4. Format costs — E2E vs microbench

E2E = median fsweep throughput @ M (single worker; `matrix-b-mw.csv`),
µs/rec = 10⁶/rate. Microbench = parse + serialize µs/rec from
`scripts/perf/microbench/results/serializers.json` (host CPU, no limits).
L/H for csv/json/parquet: csv 28.2k/111.1k, json 42.6k/166.7k,
parquet 34.4k/183.3k — CPU-linear.

| format | E2E rec/s @ M | E2E µs/rec | micro µs/rec (parse+ser) | E2E/micro | bytes/rec in |
|---|---:|---:|---:|---:|---:|
| msgpack | 145,833 | 6.9 | n/a (dep missing) | — | 447 |
| json | 87,121 | 11.5 | 4.7 | 2.4× | 524 |
| parquet | 77,381 | 12.9 | n/a | — | 189 |
| csv | 57,190 | 17.5 | 6.4 | 2.7× | 272 |
| ndjson | 55,556 | 18.0 | 7.9 | 2.3× | 523 |
| avro | 29,857 | 33.5 | n/a | — | 254 |
| pmxml | 12,603 | 79.3 | 18.7 | 4.2× | 305 |
| xml | 11,561 | 86.5 | 45.3 | 1.9× | 692 |
| protobuf | 10,363 | 96.5 | n/a | — | 280 |

Where they diverge and why:

- **E2E is 2–4× the microbench sum** uniformly. The gap is the executor
  (per-record transform pass-through, stats, batching) plus the 500 m CFS
  quota vs the microbench's unthrottled host CPU. pm_xml shows the worst
  amplification (4.2×) — its E2E cost is dominated by executor overhead, not
  the measInfo parser itself.
- **protobuf is the E2E disaster of the sweep**: 10.4k rec/s — 5.5× slower
  than csv (57k) and 14× slower than msgpack (146k) *despite the second-most
  compact input* (280 B/rec). The per-record path (CdrRecord decode + the
  snake_case→camelCase field-name dict rebuild the serializer performs on
  every record) dominates; payload size is irrelevant.
- **msgpack/parquet/avro/protobuf were absent from the microbench** (venv
  missing the extras — the same packaging gap as §9.3), so their only
  measured costs are these E2E numbers.
- pm_xml serialize caveat (microbench): output loses the measType p-index
  mapping — parse-side is the realistic direction; s2pmxml (pm_xml in →
  json out) is the honest E2E shape.

## 5. Transform costs — E2E vs microbench

E2E medians (`matrix-c-mw.csv`), compared against the json→json file-only
baseline (87.1k rec/s = 11.5 µs/rec). "Δ over baseline" = E2E µs/rec −
11.5. Microbench costs from `transforms.json`.

| bench | transforms | E2E rec/s @ M | Δ over baseline (µs/rec) | microbench sum (µs/rec) | Δ/micro |
|---|---|---:|---:|---:|---:|
| t3 | deduplicate | 76,923 | 1.5 | 0.41 | 3.6× |
| t2 | json_flatten + enrich | 28,175 | 24.0 | 11.9 | 2.0× |
| t1 | project + filter | 19,039 | 41.0 | 17.5 | 2.3× |
| t5 | rename, cast, add_field, filter, project | 7,843 | 103.5 | 63.2 | 1.6× |

Scaling (median rec/s): t1 9.3k (L) → 19.0k (M) → 39.9k (H); t5 3.8k → 7.8k
→ 16.0k — CPU-linear. Single-topology parity: t1 19.5k, t5 7.7k @ M
(vs 19.0k / 7.8k mw) — bulk-batch workloads show no topology gap.

- **t3 ≈ baseline confirmed** (0.88×): deduplicate adds ~1.5 µs/rec E2E.
- **t5 = 0.090× baseline confirmed** (7,843 / 87,121): a 5-transform chain
  costs ~104 µs/rec E2E vs 63 µs/rec microbench sum — the executor adds
  ~1.6–3.6× on top of raw transform cost (record passing / bookkeeping
  between transforms).
- **Microbench top-5 confirmed as the E2E cost centers**: t5's E2E is
  dominated by add_field (30.8 µs) + filter (16.6 µs) — both simpleeval
  evaluator construction per record — and rename/cast (7.4/7.5 µs each) sit
  on the deepcopy floor. t4 (counter_delta + window_aggregate) processed
  100k records in 7.1 s (records_out=8 aggregates is correct behavior);
  window_aggregate's O(n×groups) finalize scan is the top microbench cost
  (56.6 µs/rec @ 500 groups) and the group-cardinality cliff (12→99 µs/rec
  for 10→1,000 groups) is the single worst scaling behavior in the
  transform library.
- Shipped t1/t5 templates were broken (filter conditions referenced a
  `record` name simpleeval does not expose → 100 % record loss as shipped);
  corrected copies under `scripts/perf/results/templates-fixed/` were used.

## 6. Saturation analysis

### 6.1 S1 ladder, mgr+worker @ M (`saturation-s1.csv`)

| offered rps | achieved 2xx/s | p50 / p95 | peak worker CPU |
|---:|---:|---|---:|
| 100 | 96.9 | 2.3 / 3.2 ms | 179 m |
| 200 | 193.9 | 1.7 / 2.4 ms | 276 m |
| 400 | 387.8 | 1.6 / 2.5 ms | 471 m |
| 800 | 559.5 | 59 / 233 ms | 462 m |
| 1,600 | 761.8 | 87 / 351 ms | 501 m |
| 3,200 | 958.8 | 134 / 579 ms | 501 m |
| 6,400 | 1,089.1 | 220 / 1,039 ms | 501 m |

- **Ceiling ≈1,050–1,090 accepted rps for 3 × 500m workers (~363 per
  worker)**; worker CPU pins at the 500 m limit from ~400 offered upward —
  CPU-bound.
- **Failure mode = latency cliff, not errors**: p95 goes 2.5 ms → 233 ms
  between 400 and 800 offered and grows linearly beyond; no 5xx and no drops
  (the webhook source queue, max_queue_size 10,000, absorbed everything).
  First-failing component: worker CPU quota → queueing → latency.
- The loadgen itself tops out near ~1,150 sent/s (k=8 at step 7), so the
  6,400-offered step was never actually deliverable; the server-side ceiling
  is nonetheless credible because the workers were CPU-pinned with latency
  climbing.
- ~3–3.5 % 4xx at every step — the **placement race** (below), not
  throughput loss.

### 6.2 S1 ladder, single @ M (`saturation-s1-single.csv`)

| offered rps | achieved 2xx/s | p50 / p95 | CPU |
|---:|---:|---|---:|
| 100 | 96.8 | 2.5 / 3.6 ms | 235 m |
| 200 | 193.9 | 2.1 / 3.1 ms | 405 m |
| 400 | 273.3 | 106 / 470 ms | 495 m |
| 800 | 279.5 | 115 / 468 ms | 479 m |

Ceiling ≈280 rps @ 500m (CPU-pinned). The "800 offered" step only delivered
~300 sent/s (k=1 loadgen cap), and the matrix run at 300 offered sustained
299 rps — so the honest single ceiling is **280–300 rps**, i.e. ~¼ of the
3-worker fleet (§7). No H ladder exists for either topology.

### 6.3 S6 ladder, mgr+worker @ M (`saturation-s6.csv`)

| offered msg/s | consumed/s | lag at stop | steady |
|---:|---:|---:|---|
| 500 | 500 | 0 | 180 s |
| 1,000 | 946 | 0 | 90 s |
| 2,000 | 865 | 39,124 growing | 45 s |
| 1,200 | 1,049 | 11,357 growing | 75 s |

**Consumer ceiling ≈950–1,050 msg/s per worker** with real sink writes:
lag is zero at 946/s and grows at 1,049/s. Failure mode = **lag growth**
(no errors, no latency signal to the producer). Worker CPU pinned ~500 m —
the cost is the per-message path (parse + per-record sink write +
bookkeeping), not kafka fetch: the matrix runs, once the 99,999 cap made
sink writes no-ops, ingested 1,300–1,500/s at <35 m visible CPU.

### 6.4 Placement-race 4xx (explanation)

The steady-state 4xx in every s1 ladder step (~3–3.5 %) and mw-s1-M-rep1
(77) / mw-s1-H-rep1 (35,589 = 28.6 %) are **not** the rate limiter: the
worker ingress plane has no `RateLimitMiddleware` (manager only;
`TRAM_RATE_LIMIT=0` was effective — verified by a clean 300 rps steady
state with 0 4xx over 60 s). They are the **registration placement race**:
for ~5–11 s after pipeline registration, requests routed to a worker whose
webhook-source placement has not yet propagated get a clean
404 "No webhook source registered for path". mw-s1-H-rep1 hit the worst
case — one of three placements never arrived during the measured window
(rows=2). Consequence: rep1 of that cell is invalid for capacity; treat
rep2 (692 rps, 0 4xx) as the datum.

## 7. Topology comparison — single vs mgr+worker

| workload (median @ M) | mw | single | single/mw |
|---|---:|---:|---:|
| s1 webhook intake (offered 300) | 298 rps | 299 rps | 1.00 (offered-bound) |
| s1 webhook ceiling | 1,090 rps | 280–300 rps | 0.26–0.28 |
| s2csv | 55,356 | 51,587 | 0.93 |
| s2pmxml | 9,760 | 17,712 | 1.81 |
| s3 rest | 3,315 | 6,417 | 1.94 |
| s5 sftp | 12,840 | 9,768 | 0.76 |
| fsweep json | 87,121 | 80,128 | 0.92 |
| t1 | 19,039 | 19,459 | 1.02 |
| t5 | 7,843 | 7,722 | 0.98 |

- **Bulk file-batch workloads: parity** (s2csv, fsweep json, t1, t5 all
  within ±10 %) — as expected, since a batch pipeline executes on one
  worker either way.
- **Many-small-batch workloads: single is 1.8–2.1× faster** (s3 rest pages:
  1.90–2.05×; s2pmxml: 1.76–1.87× — across all three profiles). The mw
  worker burns ~2.3× the CPU per record on exactly these two workloads
  (mw-s3-M pins 500 m at 3.3k rec/s while single does 6.4k at ~217 m).
  Root cause is NOT established by this study: candidates are per-batch
  worker-mode bookkeeping (stats/manager round-trips per source page or
  measInfo group), since the effect is absent for whole-file sources. This
  deserves a controlled re-run before it drives topology decisions; it is
  the one place the raw data contradicts the "mw is never slower"
  intuition.
- **Webhook streams: single ≈ ¼ of the 3-worker fleet.** Two multiplicative
  factors: (a) 1 pod vs 3 — the fleet is 3× the CPU; (b) per-pod efficiency
  ~280 vs ~363 rps per 500m — in single mode the manager (API server, run
  history, stats loop) and the webhook ingress share the pod's CPU budget
  with the pipeline, taxing ~23 %.
- **s5**: single is 24 % slower at M (SFTP variance; parity at L/H).
- **Memory**: the standalone pod measured 740–870 Mi across the sweep —
  well above the 512 Mi L-profile limit, without OOM. Either the standalone
  StatefulSet does not receive the resource overlay's limits or
  `kubectl top` working-set includes local-sink page cache. Verify before
  running single in production at tight requests; size single pods at
  ≥1 Gi.

## 8. Capacity / sizing guidance

Per-500m-worker rates (mgr+worker, medians @ M) and worker counts for
10k / 50k / 100k rec/s targets. **A batch pipeline uses exactly one
worker**; "workers" for batch classes means shard the input across that
many pipelines. H (2cpu) batch rates are ~2× the M rates (verified linear);
stream rates at H were NOT verified (§3.1) — assume linear only with
re-measurement.

| workload class | rec/s per 500m worker | 10k rec/s | 50k rec/s | 100k rec/s |
|---|---:|---:|---:|---:|
| webhook → local json (stream) | ~360 | 28 | 139 | 278 |
| kafka → local json (stream, real writes) | ~1,000 | 10 | 50 | 100 |
| local jsonl → local json (batch) | ~87,000 | 1 | 1 | 2 |
| local csv → local csv (batch) | ~57,000 | 1 | 1 | 2 |
| msgpack / parquet (batch) | 146,000 / 77,000 | 1 | 1 | 1 |
| xml / pmxml / protobuf (batch) | ~10–13,000 | 1 | 4–5 | 8–10 |
| json + t1-class chain (2 transforms) | ~19,000 | 1 | 3 | 6 |
| json + t5-class chain (5 transforms) | ~7,800 | 2 | 7 | 13 |
| rest → rest (mw) | ~3,300 | 4 | 16 | 31 |
| sftp → local csv | ~13,000 (net-bound) | 1 | 4 | 8 |
| local → kafka (post-fix, ≤1k-rec batches) | ~13,000 @ 2cpu | 1 | 4 | 8 |
| snmp walk | 1.65 rows/s (RTT-bound) | n/a | n/a | n/a |

Scaling formulas (validated range in parentheses):

- **kafka stream**: ~1,000 msg/s per 500m worker, CPU-bound at M; with
  `TRAM_STREAM_SINGLE_PLACEMENT=1` each pipeline is capped at one worker —
  run N pipelines (distinct topics/groups) for N× capacity, or revisit that
  default.
- **webhook stream**: ~360 rps per 500m worker ≈ 726 rps per CPU
  (validated at M only; p95 stays <5 ms below ~½ of ceiling).
- **file batch**: rate ≈ 87k × (500m/worker CPU) × (1 / format factor)
  where format factor: msgpack 0.6, parquet 1.1, csv 1.5, ndjson 1.6,
  avro 2.9, pmxml 6.9, xml 7.5, protobuf 8.4 (json = 1.0).
- **transform chains**: divide the format rate by ~1.13 (t3), 3.1 (t2),
  4.6 (t1) or 11.1 (t5-class 5-chain) at M.
- **sftp / snmp**: do not scale with CPU — parallelize pipelines (sftp:
  ~13k rec/s aggregate per server observed) or accept walk duration.

Planning notes:

1. Stream workloads are 40–100× more CPU-expensive per record than batch —
   prefer batch/file sources where latency allows.
2. At H (2cpu/worker) the 3-worker kind cluster approaches the shared
   host's ~12 CPU: s1-H showed loadgen/host contention (p95 72 ms at 692
   rps with workers at <25 % CPU). Don't extrapolate H ceilings from this
   environment.
3. The 99,999-part local-sink cap bites *per worker placement* on streams:
   a 3-worker webhook pipeline can sink ~300k records per run before silent
   skipping; a single-placement kafka stream caps at ~100k. Size run
   length or fix the cap (§9.2) before any long-running stream.

## 9. Known product findings (facts only)

1. **s7 kafka sink batch-size mismatch**: each 10k-record file batch
   serializes to 5.6 MB > kafka-python 1 MB `max_request_size` →
   `MessageSizeTooLargeError` per batch → 100 % record loss
   (records_out=0, end_offsets all 0). Run status remains "success"; only
   3 error rows per run for 10 failed files. Diagnostic with 100 ×
   1,000-record files: 100k/100k at 12.8k rec/s @ H. Affects every
   local→kafka / file→kafka pipeline whose serialized batch exceeds 1 MB.
2. **local sink 99,999-part cap, silent skip**: stream paths write per
   record; the single-mode file sink increments its part index per write
   and `file_sink_common._next_path` raises once part_index > max_index
   (default 99,999, `tram/connectors/file_sink_common.py:541`). With
   `on_error: continue` every record beyond 99,999 per sink instance is
   silently skipped while the run reports success — observed pinning
   records_out at exactly 99,999 in s6 M/H (both topologies) and
   single-s1-H; mw-s1-H-rep2 (3 placements) shows 124,639 out,
   confirming the cap is per sink instance / per worker.
3. **worker images missing serializer extras**: msgpack, pyarrow and
   grpcio-tools are absent from the worker image (fastavro present;
   manager has grpc_tools but not fastavro). Bench workaround: `pip
   install --target /data` + `PYTHONPATH=/data` per worker pod, restaged
   after every profile upgrade (emptyDir). The microbench venv had the
   same gap (avro/msgpack/parquet/protobuf all skipped).
4. **single-topology observability**: `TRAM_MANAGER_URL` defaults to "" in
   standalone mode — the run-complete callback URL is empty and all
   run-history rows are silently dropped (fixed in the bench by
   `--set env.TRAM_MANAGER_URL=http://localhost:8765`). Even with it set,
   **stream runs never reach run history in single topology**
   (`controller._stream_worker` never calls `manager.record_run`; batch
   runs persist via `_finalize_batch_result`) — s1/s6 single-mode metrics
   had to be derived from pod logs.
5. **s4 SNMP walks are RTT-bound** (~600 s per 1000×8 walk at M+,
  >1,270 s at mw-L; single-L ~600 s — mw-L discrepancy unexplained), with
  worker CPU ~60–100 m. Duration, not CPU, is the sizing variable.
6. **Registration placement race**: ~5–11 s window after registering a
  webhook stream where requests to not-yet-propagated workers 404
  ("No webhook source registered for path"); worst case one placement
  never arrives within the run (mw-s1-H-rep1: 28.6 % 4xx).
7. **`records_skipped` double-counted on the stream skip path** (exactly
  2× in−out in all 10 affected runs; batch path counts in−out+10) —
  run-history/summary consumers overstate stream losses.
8. **Misleading error text**: "Records skipped — no sink wrote
   successfully (condition filtered all records or every sink
   failed/circuit-open)" is recorded once on every single-s3 run and on
   t4 runs although sinks wrote all records — noise that erodes trust in
   the error column.
9. **Shipped bench t1/t5 filter conditions are invalid** as written
   (`record.get(...)` — simpleeval exposes field names, not `record`);
   corrected copies live in `scripts/perf/results/templates-fixed/`.
   Suggests a linter/registration-time validation gap for conditions.

## 10. Cross-check appendix — recomputation vs runner claims

All figures recomputed from the CSVs with the repo venv (medians of clean
reps; contaminated reps excluded as flagged in §2).

| runner claim | recomputed | verdict |
|---|---|---|
| s1-mw ceiling ≈1,090 rps @ M, worker CPU-bound, p95 cliff at 800 | sat step 7: 1,089.1 2xx/s (records_in 192,183/180 = 1,068); CPU pinned 501 m; p95 2.5 ms @400 → 233 ms @800 offered | **Confirmed.** Caveat: "cliff at 800" is in *offered* terms — achieved at that step was only 559/s because the loadgen (k=1) can't deliver 800/s; server-side cliff begins somewhere in 400–800 delivered. |
| s1-single ceiling ≈270–280 rps @ M | ladder: 273.3 / 279.5 achieved @400/800 offered; **but matrix sustained 299 rps at 300 offered, 0 errors** | **Confirmed with refinement**: honest ceiling is 280–300 rps; the 800-offered step never delivered more than ~300 sent/s (k=1 cap), so the single pod was never pushed past ~300. |
| s6 consumer ≈1,000–1,100 msg/s per worker @ M | ladder: 946 lag-0 / 1,049 lag-growing | **Mostly confirmed**; top end 1,100 unsupported — say ~950–1,050. Critically, matrix M/H intake (~1,400/s) must NOT be read as capacity (post-cap skip path, §2). |
| msgpack 143k / json 87k / parquet 77k / csv 57k rec/s @ M | medians 145.8k / 87.1k / 77.4k / 57.2k | **Confirmed** (msgpack within rep spread 125–167k). |
| t3 ≈ baseline | 76.9k vs json 87.1k = 0.88× | **Confirmed** (12 % under, dominated by baseline variance). |
| t5 ≈ 0.09× baseline | 7,843 / 87,121 = 0.090 | **Confirmed exactly.** |
| single 1.8–2.1× faster on many-small-batch; bulk within ±10 % | s3 1.90–2.05×, s2pmxml 1.76–1.87×; s2csv 0.93–1.04, t1 1.02, t5 0.98, fsweep json 0.92 | **Confirmed, one exception**: s5-M single/mw = 0.76 (single 24 % slower) — outside ±10 %; SFTP run variance. |
| "s1-mw H numbers derive from saturation step 7 (~196k/180s)" | the saturation ladder ran at profile **M** (`sat-s1-M-*`); no H ladder exists | **Discrepancy**: the ~1,090 figure is an **M** ceiling. The H ceiling is unmeasured — matrix s1-H achieved only 494–692 rps at 800 offered (rep1 race-contaminated, rep2 loadgen-limited; workers <25 % of 2 CPU). Do not cite an H webhook capacity. |

Additional findings the runners missed (all verified against the raw rows):

1. **`records_skipped` double-counting** on stream skip paths (§9.7) —
   systematic, exactly 2× in−out in all 10 affected runs.
2. **mw-s1-H-rep1 is placement-race contaminated** (4xx = 35,589 =
   28.6 %, rows=2) — the runner caveats did not list it; the cell median
   (594 rps) understates and should be replaced by rep2 (692 rps,
   loadgen-limited).
3. **Single-topology stream CPU samples are invalid** (2–3 m at 300 rps —
   physically impossible); the collector's peak-pod convention also hides
   worker CPU whenever kafka-0 is the peak pod (all s6 matrix rows).
4. **Standalone pod memory (740–870 Mi) exceeds the 512 Mi L limit without
   OOM** — either the standalone StatefulSet ignores the `resources`
   overlay path or metrics include page cache; unverified, worth a check.
5. **s6 single-placement**: every s6 run used exactly one worker (rows=1)
   vs s1's 2–3 — consistent with `TRAM_STREAM_SINGLE_PLACEMENT=1`; this
   caps per-pipeline kafka capacity and belongs in the sizing model.
6. **s7 skipped-count off-by-10** (records_skipped=100,010 for in=100k,
   out=0) — ten extra skips, one per input file; cosmetic but confirms
   per-batch error accounting.
7. **mw-vs-single 2× gap is workload-specific** (s3, s2pmxml — not csv,
   t1, t5, fsweep) — the "many-small-batch" generalization holds, and the
   root cause (per-batch worker-mode bookkeeping suspected) is an open
   item, not a settled explanation.
