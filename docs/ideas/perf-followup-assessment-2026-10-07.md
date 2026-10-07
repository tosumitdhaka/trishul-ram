# Performance follow-up assessment — 2026-10-07

The best immediate implementation candidates are faster timestamp handling,
compile-once sink conditions, and avoiding redundant Kafka sink parsing for
already bounded payloads. Webhook capacity needs a separate ingress/CPU
experiment. Protobuf needs a record-representation change to achieve a large
gain; another framing optimization is unlikely to help much.

This assessment reviews the corrected v1.6.0 results and current code at
`fdcf371`. Application behavior is unchanged. New measurements below use
isolated prototypes, with raw timings and a reproducer committed alongside
the assessment.

## Results that should guide the next work

The [corrected comparison](perf-v160-vs-v151-comparison-2026-10.md) and
[rerun evidence](../../scripts/perf/results/v1.6.0-rerun/RESULTS.md) supersede
the original capacity study's inflated H-profile batch figures and contaminated
manager/worker cells. The earlier thirteen improvement candidates have already
been implemented or resolved; they are not a new backlog.

| Workload | Current measured result | Implication |
|---|---|---|
| Kafka consumption, one 500m worker | 2,000 messages/s lag-free; 3,943/s matched production | Upper capacity remains unknown: the strongest test was producer-limited |
| Webhook, three 500m workers | About 1,605 requests/s at lower concurrency; 1,284 at higher concurrency | Concurrent requests materially change usable capacity |
| Five-transform JSON chain, 500m | 23,942 records/s standalone; 29,040 manager/worker | Existing expression/copy improvements already delivered roughly 3× |
| Protobuf format sweep, manager/worker M | 9,527 records/s | Dictionary conversion dominates despite faster parsing |
| Same format sweep: JSON / MessagePack | 72,917 / 133,929 records/s | If the contract permits a format change, test MessagePack first; this is a measured 1.84× difference in these cells |

Matrix values are medians of two recorded repetitions, not saturation limits.
The webhook plateau is also not a suitable low-latency operating point:
the high-concurrency ladder recorded 388 successful requests/s at reported
p95 3.4 ms, 614/s at 3.6 seconds, and 1,284/s at 9.8 seconds. A separate
lower-concurrency control reached about 775/s at reported p95 12 ms. Choose
an explicit latency target and concurrency shape when sizing.

## New controlled CPU experiments

Run from the repository root:

```sh
.venv/bin/python scripts/perf/results/followup-2026-10-07/bench.py
```

[Reproducer](../../scripts/perf/results/followup-2026-10-07/bench.py) and
[raw measurements](../../scripts/perf/results/followup-2026-10-07/measurements.json).
Same host, Python 3.12.3, canonical CDR corpus, seven alternating paired
repetitions, medians, warmup/equality checks outside timing. Stateful instances
are fresh per invocation. Deployment images use Python 3.13: repeat there
before treating these as deployment numbers. Transform timings use a 10,000
record `apply()` call; the executor's per-record dispatch adds its own overhead.

| Local operation | Current | Prototype | Local speedup |
|---|---:|---:|---:|
| Sink condition `duration_s > 30 and event_type == "VOICE"` | 15.49 µs/record | 1.71 µs/record | 9.1× |
| Timestamp normalization, two ISO fields, parser change | 18.42 µs/record | 4.13 µs/record | 4.5× |
| Same, also native ISO output formatting | 18.30 µs/record | 2.54 µs/record | 7.2× |
| Counter delta, two fields | 10.35 µs/record | 3.47 µs/record | 3.0× |
| Window aggregate, 500 groups | 13.01 µs/record | 5.53 µs/record | 2.4× |
| JSON parse, stdlib versus installed `orjson` | 1.58 µs/record | 0.87 µs/record | 1.8× |
| JSON serialize, stdlib versus installed `orjson` | 1.63 µs/record | 0.37 µs/record | 4.4× |

These are operation-level gains, not pipeline speedups. Output equality was
checked on this corpus. JSON equality is semantic: encoded bytes differ.
The prototypes do not establish compatibility for every accepted input.

### 1. Timestamp parser and formatter — first implementation priority

`tram/transforms/timestamp_normalize.py::_parse_timestamp` tries seven
`strptime` formats before `datetime.fromisoformat`. Even the corpus's normal
`...Z` timestamps pay a failed attempt followed by expensive format parsing.
Try the ISO parser first for eligible auto-detected strings, retaining explicit
formats, numeric epoch detection, and timezone conversion. `_format` can use
`isoformat(timespec="milliseconds")` for the default ISO output.

This is a shared kernel: `counter_delta` and `window_aggregate` import the
same parser. The latest transform microbench ranks timestamp normalization
at 19.52 µs/record, window aggregation at 15.08, and counter delta at 12.03.
The heap and shallow-copy work already landed; timestamp handling is now
a substantial remaining cost in all three.

Before shipping, verify explicit formats, naive timestamps with configured
timezones/DST, offsets, fractional precision, all epoch units, invalid inputs,
error policies, and exact default output strings. Moving `fromisoformat`
earlier can change the interpretation of strings accepted by both parsers,
including compact numeric ISO forms; preserve existing precedence deliberately.
Repeat real executor chains, not only direct transform calls.

### 2. Compile sink conditions once — small change with strong evidence

`tram/pipeline/executor.py::_filter_by_condition` still constructs a fresh
simpleeval evaluator and parses the condition for every record. The top-level
`filter` transform already has a parsed AST and a thread-local evaluator.
The measured prototype uses that existing implementation for the same valid
condition and produces identical selected records.

Extend the compile-once approach to sink routing. Bind a fresh names mapping
for each record and retain thread isolation and current routing-error policy.
This benefits conditional multi-sink pipelines; the plain webhook and t5
benchmarks do not establish its end-to-end benefit. Measure routed pipelines
with one, two, and four sinks and verify invalid-condition handling.

### 3. Kafka sink — avoid repeated parsing and sizing

The executor already supplies `output_record_count` after sink transforms
and filename partitioning. `KafkaSink.write` nevertheless constructs a
serializer, parses the payload, and serializes every record individually to
decide whether chunking is necessary.

For an executor-supplied positive count within `chunk_records`, exact payload
length within `chunk_bytes`, and no key extraction requirement, send the
original bytes immediately. Retain the existing path when metadata is absent,
the caps are exceeded, or a key must be derived.

An immediate-ack fake producer measured **2.326 ms → 0.647 µs per 500-record
sink write**, avoiding roughly **4.65 µs CPU per record**. The 264,021-byte
payload and sent-message arguments were identical. The large ratio describes
removing almost all local work from this fake-producer call; it does not
predict a Kafka throughput multiplier. The 1,000-record control was 527,656
bytes, above the 524,288-byte cap: the prototype correctly fell back and
showed no gain.

For larger payloads, explore a sink interface that accepts records plus their
serializer, eliminating the bytes→records round trip. Size actual serialized
chunks: the current sum of separately serialized records is not an exact
size model for every supported format. Preserve byte caps, framing, keys,
partition ordering, partial-delivery errors, and retry behavior.
Bounded concurrent sends can reduce acknowledgement waits later, but need
explicit ordering and failure accounting. Do not relax `acks` to claim a gain.

## Separate capacity investigations

### Webhook: measure HTTP overhead and throttling before changing execution

Worker ingress and pipeline processing run as threads in the same Python
process and compete for the same 500m CPU quota. The clean ladder supports
CPU saturation at the plateau and severe concurrency-sensitive queueing.
The stored CPU samples do not measure CFS throttling directly; the reported
throttling mechanism should be verified with throttled periods/time and
event-loop lag, especially at the earlier collapse below reported saturation.

Use a quiet host and a separate load generator. Compare:

1. Existing deployment at 500m, 1 CPU, and 2 CPU, with identical payloads.
   Record throughput per allocated core as well as total throughput.
2. Existing HTTP stack against images with `uvloop` and `httptools`, checking
   which implementations actually run. They are absent from this local venv;
   the declared dependency is plain `uvicorn`, so inspect deployment too.
3. Equal offered rates at low and high client concurrency, with bounded
   client in-flight requests and queue-depth/event-loop telemetry.
4. If clients can batch events, multiple records per JSON-array POST. Report
   records/s and requests/s separately and keep body and queue byte bounds.

Keep the existing sink micro-batching. Raising its size alone does not remove
per-request HTTP work or per-arrival parse/metrics/transform work. HTTP 202
acknowledges enqueueing, so measure sink completion latency and accepted
versus written records too. Increasing queue capacity can worsen latency;
it does not create CPU capacity. Increasing generic Uvicorn process workers
would split the in-process webhook registry and is not a drop-in fix.

### Protobuf: dictionary conversion is the architectural limit

The prior controlled comparison found parse 23 → 15 µs/record, serialize
about 30 µs/record, but full-pipeline throughput stayed about 9.5k/s.
`MessageToDict` and `ParseDict` remain on every record. `preserve_keys`
did not measurably improve performance.

Pilot either a descriptor-driven conversion for known schemas or an explicit
native-message/passthrough path for matching input/output schemas and pipelines
without record-dependent work. Verify presence/defaults, enums, 64-bit integers,
bytes, maps, nested/repeated fields, unknown fields, and registry framing.
Transformed/routed pipelines still need compatible field access. A native
codec replacement alone cannot remove dictionary conversion costs.

### Other workload-specific opportunities

- REST sink creates and closes an `httpx.Client` on every write. Reuse a
  run-scoped client and close it through the existing sink lifecycle; benchmark
  connection/TLS reuse. REST source already reuses a client across pages.
- SQL sink creates/disposes an engine and reflects its table on every write.
  Reuse the engine/table per sink instance and benchmark bulk inserts/upserts
  with bounded transaction sizes. Neither opportunity has a new measured gain.
- Batch aggregate stores all per-operation samples and builds numeric lists.
  Running accumulators can reduce memory from input-sized storage to
  group-sized state for fixed operation count. Preserve null/first/last and
  numeric summation behavior; this is primarily a memory candidate.
- `hex_decode` still costs 10.45 µs/record in the rerun. Exact overrides in
  `mode: hex` could visit only selected paths; the current implementation
  traverses every leaf. Preserve wildcard/list and container isolation semantics.
- An opt-in `orjson` serializer is promising where JSON contracts allow it.
  Check Unicode escaping, indentation, float/NaN behavior, large integers and
  unsupported types. Codec speedups alone will have limited effect on an
  HTTP-bound pipeline; redundant conversions deserve attention first.

## Measurement plan and decision criteria

Implement timestamp handling, sink-condition reuse, and the eligible Kafka
fast path as separate changes, with focused semantic regression checks.
Re-run each affected full pipeline on deployment Python with identical images,
dependency versions, CPU limits, corpus, and sink configuration. Use at least
five repetitions, long enough runs to avoid short-duration attribution errors,
and record individual results rather than only a median. A useful target is
at least 20% improvement in the affected pipeline at equal CPU and latency;
the operation-level multipliers above are not acceptance promises.

Before the next capacity campaign:

- Assert exactly the intended enabled pipelines and warm placement before load.
- Attribute CPU and RSS to the subject TRAM pods. Existing ladder CSV memory
  peaks include the Kafka pod (~720 MiB); documented TRAM workers were ~75–85 MiB.
- Capture CPU seconds/record, CFS throttling, queue depth, event-loop lag,
  sink flush size, and full-pipeline completion latency.
- Capture latency histograms: the ladder currently averages each generator's
  p95, which is not the combined p95 distribution.
- Use open-loop offered-rate accounting and sufficient independent generator
  capacity; flag achieved-rate limits and generator/host contention.
- Reconcile accepted inputs, output contents, skips, errors, duplicates and
  broker offsets. Several ladder rows differ slightly between HTTP 2xx and
  recorded inputs even when pipeline in==out; resolve start/stop attribution.
- For Kafka consumption, drive beyond 4k/s with a producer that has spare
  capacity, then require stable lag. Retain commit-after-flush and strict
  at-least-once settings; extra threads are not a safe shortcut.

A Rust/PyO3 pilot becomes useful if profiles after these changes still show
a dominant Python CPU kernel. Measure batched boundary/conversion costs and
the whole pipeline before committing to a rewrite.
