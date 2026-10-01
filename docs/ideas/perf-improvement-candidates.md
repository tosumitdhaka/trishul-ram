# TRAM Performance Improvement Candidates — 2026-10

Prioritized candidates derived from the Phase 2 capacity study
(v1.5.1 @ 326d9dd). Companion analysis — evidence, medians and
cross-checks — is in `perf-capacity-analysis-2026-10.md`; raw data under
`scripts/perf/results/` and `scripts/perf/microbench/results/`.

Classification: **[P]** product fix (correctness/silent data loss),
**[O]** performance optimization, **[X]** packaging / ops.

This document is the input for an issue-creation decision — no issues are
proposed here. Priority order below is roughly: data-integrity first, then
capacity-per-dollar, then hygiene.

---

## 1. Kafka sink: per-record / bounded-batch sends — [P] + [O]

- **Evidence**: s7 records_out = 0 at every cell, both topologies — each
  10k-record file batch serializes to 5.6 MB, over kafka-python's 1 MB
  `max_request_size`; `MessageSizeTooLargeError` per batch, all records
  skipped, run status "success", end_offsets all 0 (`matrix-a-*.csv`,
  `diag-s7-100x1000-H`). Diagnostic with 1,000-record files: 100k/100k at
  12.8k rec/s @ H. Also, the stream kafka-sink path pays a per-message
  send (part of the ~0.5–1 ms/record stream cost, §s6/webhook below).
- **Proposal**: serialize and send in bounded chunks (record-count and/or
  byte cap ≤ client `max_request_size`, e.g. 1,000 records / 512 KB),
  document/derive `max_request_size` from config, and treat persistent
  send failures as run errors (or DLQ) instead of silent skips.
- **Expected gain**: makes local/file→kafka functional at all batch sizes;
  ~13k rec/s per 2-CPU worker for batch produce (measured working config);
  removes the silent-loss failure mode entirely.
- **Risk**: ordering / at-least-once semantics across chunk retries must
  be defined (idempotent producer or consumer dedup); partial-batch
  failure boundaries need tests.

## 2. Local sink: fail-loud or rollover at the part-index cap — [P]

- **Evidence**: records_out pins at exactly 99,999 in every s6 M/H run
  (both topologies) and single-s1-H; per-record writes on the stream path
  increment the single-mode part index, and `file_sink_common._next_path`
  raises `SinkError` past `max_index` (default 99,999,
  `tram/connectors/file_sink_common.py:541`), which `on_error: continue`
  converts into silent skipping with a "success" run. mw-s1-H-rep2
  (124,639 out over 3 placements) confirms the cap is per sink instance.
- **Proposal**: (a) when `max_index` is exhausted in single mode, roll
  over the part index instead of failing (the oldest-part deletion already
  exists for append mode), or (b) at minimum, surface the skipped count as
  a run error/warning — a stream that drops >0 records after the cap
  should not report success. Consider making the default `max_index`
  visible in sink docs given per-record part consumption on streams.
- **Expected gain**: no silent data loss on long streams (>100k records
  per placement); s6-style runs become re-measurable without the cap
  distorting records_out.
- **Risk**: rollover can overwrite old parts if the filename template
  lacks a unique token — needs the same collision guard as append mode;
  behavior change for anyone relying on the cap as a crude stop.

## 3. Stream per-message sink path → micro-batching — [O] (highest capacity ROI)

- **Evidence**: webhook ≈360 rps and kafka ≈1,000 msg/s per 500m worker
  (CPU-pinned) — ~0.5–1 ms CPU per record, versus 5–12 µs/record for the
  identical json→json work in batch mode (87k rec/s @ 500m). The kafka
  consumer's intake jumps to 1,300–1,500/s once the 99,999 cap turns sink
  writes into no-ops, isolating the per-message write as the dominant cost.
  Microbench json serialize is 2.1 µs/rec — the write path, not
  serialization, is the bottleneck.
- **Proposal**: buffer stream records and flush to sinks per batch (e.g.
  500 records or 1 s, mirroring kafka `max_poll_records`), keeping a
  bounded latency budget; reuse the batch executor's sink write path.
- **Expected gain**: 2–5× stream capacity per worker (webhook ~360 →
  ~1,000+ rps; kafka ~1,000 → ~2,500+ msg/s per 500m worker, bounded by the
  ~15–25 µs/record parse+bookkeeping floor); also relieves pressure on
  candidate #2 (part index consumed per flush, not per record).
- **Risk**: up to flush-interval end-to-end latency; crash-window loss
  semantics must stay at-least-once; interacts with per-sink error
  handling (batch errors vs record errors).

## 4. Worker image: ship serializer extras — [X]

- **Evidence**: msgpack / pyarrow / grpcio-tools missing from the worker
  image (fastavro present; manager has grpc_tools but not fastavro).
  Bench needed `pip install --target /data msgpack pyarrow grpcio-tools` +
  `PYTHONPATH=/data` per worker pod, restaged after every profile upgrade
  (emptyDir wipe). The microbench venv had the same gap — four serializers
  went entirely unbenchmarked (`microbench/results/summary.md` "Skipped").
- **Proposal**: bake the serializer extras (`tram[msgpack_ser,parquet,
  protobuf_ser,avro]`) into the worker (and standalone) images, or provide
  a documented Helm values switch for a "full-serializer" image variant.
- **Expected gain**: msgpack (146k rec/s — the fastest format measured)
  and parquet (77k) usable in-cluster with zero staging; removes a
  production foot-gun that would manifest as serializer-not-found at run
  time.
- **Risk**: image size (pyarrow ≈ +100 MB); consider keeping the slim
  default plus an opt-in variant.

## 5. window_aggregate: O(n × groups) finalize scan — [O]

- **Evidence**: microbench 56.6 µs/rec at 500 groups — the most expensive
  transform by 2–4×; group-cardinality probe 12.4 → 99.0 µs/rec for 10 →
  1,000 groups (8× cost for 100× groups), flat only because windows
  finalize and evict at bounded cardinality. Root cause:
  `_finalize_due_windows()` runs after **every record**
  (`tram/transforms/window_aggregate.py:297`) and iterates all groups ×
  open windows (`:251-269`).
- **Proposal**: maintain a global ordered structure (heap / sorted list)
  of open windows keyed by window-end; finalize only windows due under the
  watermark — O(log g) per record amortized instead of O(g).
- **Expected gain**: high-cardinality group_by (e.g. per-subscriber
  windows, 1k–5k groups) goes from ~10–17k rec/s to ~80k+ rec/s per
  worker; removes the only super-linear transform behavior found.
- **Risk**: watermark/late-data correctness must be re-verified
  (out-of-order timestamps, window eviction); needs dedicated tests
  before shipping.

## 6. add_field / filter: compile simpleeval once per pipeline — [O]

- **Evidence**: add_field 30.8 µs/rec and filter 16.6 µs/rec — #2 and #5
  microbench cost centers; both construct a fresh
  `simpleeval.EvalWithCompoundTypes` (+ names-dict merge) per field per
  record (`tram/transforms/add_field.py:87-91`,
  `tram/transforms/filter_rows.py:42`). t5 chain E2E: 7.8k rec/s @ 500m =
  0.09× the json baseline; add_field + filter are 47 of the 63 µs/rec
  microbench chain cost.
- **Proposal**: build the evaluator(s) once at transform init and bind
  only the per-record names at evaluation time (simpleeval supports
  reusing the compiled program with a fresh names dict).
- **Expected gain**: add_field ~30.8 → ~2–5 µs/rec (5–10×); t5-class
  chain E2E from ~7.8k to ~12–15k rec/s per 500m worker; benefits every
  filter-bearing pipeline (t1-class also gains ~40 %).
- **Risk**: evaluator reuse must not leak state between records or threads
  (`thread_workers > 1`); simpleeval API differences across versions need
  a compatibility check.

## 7. deepcopy floor in mid-tier transforms — [O]

- **Evidence**: 10 of 26 transforms (rename, cast, value_map,
  coalesce_fields, drop, unnest, explode, json_flatten, select_from_list,
  counter_delta) all deepcopy each record and cluster at 7–11 µs/rec —
  essentially the cost of `copy.deepcopy` of a 20-field flat dict. In t5,
  rename + cast ≈ 15 µs/rec of the chain; counter_delta (15.2 µs/rec) is
  dominated by its per-record deepcopy (`tram/transforms/counter_delta.py:221`).
- **Proposal**: replace `copy.deepcopy` with a cheaper copy strategy
  (shallow dict copy / `{**record}`) where transforms don't mutate nested
  values; alternatively document and switch to in-place mutation for the
  transforms where it is provably safe, gated per-transform behind tests.
- **Expected gain**: mid-tier transforms 2–3× (7–11 → 2–4 µs/rec); chains
  of cheap transforms (rename→cast→drop…) approach the enrich/project tier.
- **Risk**: aliasing bugs if any downstream transform mutates nested
  structures — this is why it must be per-transform with mutation tests,
  not a blanket change.

## 8. Single-topology: stream run-history + TRAM_MANAGER_URL default — [P]

- **Evidence**: standalone mode defaults `TRAM_MANAGER_URL` to "" →
  run-complete callback silently drops ALL run-history rows (batch runs
  included); fixed in the bench only via `--set`. Even with it set, stream
  runs never reach run history in single topology
  (`controller._stream_worker` never calls `manager.record_run`) — s1/s6
  single-mode metrics had to be log-derived (deploy-state Phase 2c).
- **Proposal**: default `TRAM_MANAGER_URL` to `http://localhost:8765` in
  standalone mode; make the single-topology stream path record runs (or
  persist via the local StatsStore → run-history bridge used by batch).
- **Expected gain**: observability parity — single-mode streams become
  monitorable through the API/UI instead of pod-log parsing; removes a
  whole class of "missing metrics" reports.
- **Risk**: low — local HTTP call to itself; guard for the manager being
  intentionally remote in hybrid setups.

## 9. Webhook placement race: fast-fail or await propagation — [P]

- **Evidence**: ~5–11 s window after stream registration where requests to
  workers without propagated placement 404 ("No webhook source registered
  for path"); ~3–3.5 % 4xx at every saturation-ladder step; worst case one
  placement never arrived and 28.6 % of the run's requests 404'd
  (mw-s1-H-rep1, rows=2). Verified NOT the rate limiter (worker ingress
  has no RateLimitMiddleware; clean 300 rps steady state reproduced).
- **Proposal**: block the register/restart response until placements are
  confirmed on all targeted workers (or return a readiness handle the
  ingress can gate on); alternatively have the worker ingress hold/retry
  unmatched webhook paths briefly instead of 404.
- **Expected gain**: removes ~3–6 % loss at stream start; makes saturation
  and production canary numbers trustworthy; eliminates the invalid-rep
  failure mode seen in the study.
- **Risk**: registration latency increases by the propagation window;
  needs a timeout + partial-topology fallback (workers: all vs subset).

## 10. protobuf E2E amplification — [O, low priority]

- **Evidence**: 10.4k rec/s E2E @ M — 5.5× slower than csv (57k) and 14×
  slower than msgpack (146k) despite the second-most compact input
  (280 B/rec). Per-record CdrRecord decode + the snake_case→camelCase
  field-name dict rebuild the serializer performs on every record dominate;
  payload compactness buys nothing on this path.
- **Proposal**: batch-level decode/encode where the framing allows; skip
  or make configurable the field-name conversion round-trip; expose a
  `preserve_keys` option.
- **Expected gain**: toward 30–60k rec/s per 500m worker (2–5×); protobuf
  stops being the slowest format despite being the densest.
- **Risk**: key-naming convention is wire-visible (downstream consumers
  may expect camelCase); needs a compat flag and docs.

## 11. Misleading "no sink wrote successfully" error + skipped double-count — [P, hygiene]

- **Evidence**: every single-s3 run and both t4 runs record "Records
  skipped — no sink wrote successfully (condition filtered all records or
  every sink failed/circuit-open)" although the sinks wrote all records
  (rest posts=1000, t4 out=8 aggregates by design). Separately,
  `records_skipped` is exactly 2× (in − out) on the stream skip path in
  all 10 affected runs, and batch s7 over-counts by 10 (one per file).
- **Proposal**: fix the double increment on the stream skip path; only
  emit the "no sink wrote" error when the sink-accepted count is actually
  zero for the run (or scope it per-batch with different wording).
- **Expected gain**: error columns and run summaries become trustworthy —
  a prerequisite for using them in alerting.
- **Risk**: none of substance; consumers of the current (wrong) counts
  should be identified first.

## 12. mw-vs-single 2× gap on many-small-batch sources — [O, investigation first]

- **Evidence**: s3 rest (1000 pages × 100) and s2pmxml run 1.8–2.1× faster
  on the standalone pod at identical CPU profiles (mw-s3-M pins 500 m at
  3.3k rec/s; single does 6.4k at ~217 m), while csv/jsonl/t1/t5/fsweep
  show parity. Root cause not established by this study — per-batch
  worker-mode bookkeeping (stats/manager round-trips per small source
  unit) is the leading hypothesis.
- **Proposal**: controlled A/B re-run (same host conditions, per-batch
  stats instrumentation) to root-cause before any code change; if
  confirmed, batch/coalesce the worker-mode bookkeeping for small source
  units.
- **Expected gain**: up to ~2× mw throughput on paged/polling sources
  (rest, snmp-style feeds) — the workloads telecom deployments actually
  run.
- **Risk**: investigation may show environment artifact (the two
  topologies ran a day apart on a shared WSL2 host); do not spend code
  budget before the re-run confirms.

## 13. Registration-time validation of filter conditions — [P, hygiene]

- **Evidence**: shipped t1/t5 bench templates used
  `record.get('event_type')` in filter conditions — simpleeval exposes
  field names, not `record` — producing 100 % record loss with condition
  errors as the only signal; corrected copies live in
  `scripts/perf/results/templates-fixed/`.
- **Proposal**: extend `tram validate` / registration lint to dry-run
  each filter/add_field expression against a sample record (or at least
  reject unbound names like `record`) at registration time.
- **Expected gain**: config errors surface at `tram validate`, not at
  100 % data loss mid-run.
- **Risk**: false positives for intentionally dynamic expressions; make
  it a warning unless the failure is deterministic.
