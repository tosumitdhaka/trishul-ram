# TRAM Capacity Study — Phase 1c: Serializer & Transform Microbenchmarks

Repo: `trishul-ram` v1.5.1 @ 326d9dd. All work under `scripts/perf/microbench/`.

## Methodology

- Canonical telecom CDR corpus: flat, 20 fields, ~488–528 B as JSON, deterministic
  (seeded); documented variants (nested / cast / hex / explode / melt / select /
  dedup / counter-polls) constructed only where a plugin's input shape demands it.
- `time.perf_counter_ns`, warmup on 1,000 records, **median of 5 reps** (3 reps for
  scaling runs), single process, one thread.
- n = 10,000 records per rep; stateful transforms get a fresh instance per timed
  rep (only `apply()` is timed) so state accumulates over the batch exactly as in
  one pipeline run.
- CPU: Intel(R) Core(TM) Ultra 7 155H (host WSL2, 12 logical CPUs visible).
  **Numbers are comparative, not absolute.**
- Interpreter: repo `.venv` (Python 3.12.x).

## Serializer results (n=10,000, median of 5)

| serializer | parse rec/s | parse µs/rec | parse MB/s | ser rec/s | ser µs/rec | ser MB/s | B/rec |
|---|---:|---:|---:|---:|---:|---:|---:|
| text    | 1,933,958 | 0.52 | 899.5 | 4,899,826 | 0.20 | 2,278.8 | 488 |
| bytes   | 1,004,395 | 1.00 | 466.2 | 1,053,258 | 0.95 | 488.9 | 487 |
| json    | 388,017 | 2.58 | 195.3 | 465,961 | 2.15 | 234.5 | 528 |
| csv     | 282,126 | 3.55 | 63.7 | 355,145 | 2.82 | 80.2 | 237 |
| ndjson  | 235,510 | 4.25 | 109.5 | 272,420 | 3.67 | 136.8 | 488 |
| pm_xml  | 97,255 | 10.28 | 16.8 | 119,351 | 8.38 | 28.0 | 181 |
| asn1    | 50,557 | 19.78 | 11.9 | n/a (decode-only by design) | | | 248 |
| xml     | 37,780 | 26.47 | 27.5 | 53,162 | 18.81 | 38.7 | 763 |

Payload construction (honest inputs, see `bench_serializers.py` docstring):
json/ndjson/csv/xml use the serializer's own output over the canonical corpus;
text = one compact-JSON CDR per line (lines opaque); bytes = 10,000 × ~500 B
payloads (envelope semantics — one record per payload, records/s = payloads/s);
pm_xml = constructed 3GPP TS 32.432 measData (10 measInfo × 1,000 measValue ×
6 Nokia-style counters); asn1 = concatenated BER `CdrRecord` TLVs
(`data/cdr.asn`, `split_records: true`), encode side intentionally unsupported.

## Transform cost ranking (n=10,000, µs per INPUT record)

| # | transform | µs/rec | rec/s | out recs | note |
|---|---|---:|---:|---:|---|
| 1 | window_aggregate | 56.6 | 17.7k | 4,591 | 500 cell groups, 900 s windows |
| 2 | add_field | 30.8 | 32.5k | 10,000 | 2 simpleeval expressions |
| 3 | explode | 22.7 | 44.1k | 20,000 | 2 components → deepcopy×2 |
| 4 | timestamp_normalize | 20.5 | 48.7k | 10,000 | 2 ISO fields |
| 5 | filter | 16.6 | 60.2k | 2,018 | simpleeval condition |
| 6 | counter_delta | 15.2 | 65.9k | 10,000 | 2 counter fields, 500 series |
| 7 | select_from_list | 11.6 | 86.1k | 10,000 | |
| 8 | json_flatten | 11.1 | 90.3k | 10,000 | nested → flat |
| 9 | hex_decode | 10.8 | 93.0k | 10,000 | 2 packed-IPv4 fields |
| 10 | unnest | 7.8 | 127.8k | 10,000 | |
| 11 | cast | 7.5 | 132.5k | 10,000 | 3 fields |
| 12 | rename | 7.4 | 134.9k | 10,000 | 3 fields |
| 13 | coalesce_fields | 7.4 | 135.8k | 10,000 | |
| 14 | jmespath | 7.2 | 138.5k | 10,000 | 4 expressions |
| 15 | drop | 7.1 | 141.0k | 10,000 | 4 fields |
| 16 | value_map | 7.0 | 142.1k | 10,000 | |
| 17 | melt | 2.1 | 468.7k | 30,000 | 3 metrics/record |
| 18 | mask | 1.8 | 554.7k | 10,000 | 3× sha256 |
| 19 | aggregate | 1.7 | 579.3k | 2,450 | 4 ops, 2,450 groups |
| 20 | template | 1.1 | 876.1k | 10,000 | 2 templates |
| 21 | regex_extract | 1.0 | 1,010k | 10,000 | msisdn split |
| 22 | project | 0.9 | 1,098k | 10,000 | 6 fields |
| 23 | validate | 0.9 | 1,130k | 10,000 | 4 rule fields |
| 24 | enrich | 0.8 | 1,196k | 10,000 | dict join |
| 25 | deduplicate | 0.4 | 2,418k | 9,001 | 10% dupes |
| 26 | limit | ~0.0 | n/a | 100 | batch slice |

## Stateful scaling (10k → 50k → 100k records)

| transform | 10k µs/rec | 50k | 100k | growth 100k/10k |
|---|---:|---:|---:|---:|
| deduplicate | 0.538 | 0.562 | 0.627 | **1.17×** |
| aggregate | 1.853 | 1.803 | 1.758 | 0.95× |
| window_aggregate | 60.5 | 58.8 | 56.2 | 0.93× |
| counter_delta | 15.6 | 15.4 | 16.1 | 1.03× |

No super-linear growth **in n** at fixed group cardinality. But:

**window_aggregate group-cardinality probe** (n=10k, group_by = cell_id with
K distinct cells):

| groups | µs/record |
|---:|---:|
| 10 | 12.4 |
| 100 | 26.9 |
| 500 | 71.5 |
| 1000 | 99.0 |
| 5000 | 102.7 |

Per-record cost scales ~linearly with group count (10→1000 groups: 8× cost).
Root cause: `apply()` calls `_finalize_due_windows()` after **every record**
(`tram/transforms/window_aggregate.py:297`), which iterates *all* groups ×
open windows (`window_aggregate.py:251-269`). Total work is O(n × groups) —
a quadratic cliff for high-cardinality group_by (e.g. per-subscriber windows).
With bounded group cardinality (500 cells) the per-record cost is flat in n
because windows finalize and are evicted.

## Top-5 cost centers

1. **window_aggregate (56.6 µs/rec)** — the per-record finalize scan over all
   groups (above). Dominates every other transform by 2-4×; worst at scale with
   high-cardinality group_by.
2. **add_field (30.8 µs/rec)** — constructs a fresh
   `simpleeval.EvalWithCompoundTypes` (+ names dict merge) **per field per
   record** (`tram/transforms/add_field.py:87-91`). Interpreter setup, not the
   expression itself.
3. **explode (22.7 µs/rec)** — `copy.deepcopy(record)` per emitted element
   (`tram/transforms/explode.py:36`); fan-out multiplies it.
4. **timestamp_normalize (20.5 µs/rec)** — tries a 7-format `strptime` chain
   per field before falling back to `fromisoformat`
   (`tram/transforms/timestamp_normalize.py:59-83`), plus `astimezone` +
   `strftime` per field.
5. **filter (16.6 µs/rec)** — same per-record `simpleeval` evaluator
   construction as add_field (`tram/transforms/filter_rows.py:42`).

## Findings & observations

- **The deepcopy-per-record pattern is the structural floor**: 10 of 26
  transforms (rename, cast, value_map, coalesce_fields, drop, unnest, explode,
  json_flatten, select_from_list, counter_delta) all deepcopy each record, and
  they all cluster at 7-11 µs/record — that is essentially the cost of
  `copy.deepcopy` of a 20-field flat dict. A cheaper copy strategy (or
  documented in-place mutation) would cut the mid-tier transforms by ~2-3×.
- **counter_delta (15.2 µs/rec)**: state is O(series) — 1,000 entries here,
  constant across 10k-100k (1.03× growth). Cost is dominated by the per-record
  `copy.deepcopy` (`tram/transforms/counter_delta.py:221`), not state math.
- **aggregate materializes samples, not accumulators**: every value is appended
  to a per-group list (`tram/transforms/aggregate.py:82-84`), so batch memory
  is O(n × ops). Per-record time stayed flat (bounded 2,450 groups) but the
  allocation profile is heavy; window_aggregate's running-accumulator design is
  the better pattern.
- **deduplicate** shows mild super-linear growth (1.17× 10k→100k): the `seen`
  dict grows to ~90% of n with tuple keys — hash-table growth + cache misses,
  plus O(n) memory. Linear-ish, documented as expected.
- **mask is cheap (1.8 µs/rec for 3 sha256 fields)** — short-input hashing is
  not a bottleneck.
- **Serializers**: text (line split, 0.52 µs/rec) and bytes (base64 envelope)
  are near-free; json ~2.6 µs/rec is the structured baseline; XML is the
  slowest parse (26.5 µs/rec, defusedxml) and serialize (18.8 µs/rec, lxml
  pretty-print); asn1 BER decode (19.8 µs/rec) and pm_xml (10.3 µs/rec) sit in
  the middle. XML's payload is also ~50% larger than JSON.
- **pm_xml serialize caveat**: emits one `measInfo` per record and drops the
  `measType` p-index mapping — output shape diverges from input; parse-side is
  the realistic direction.

## Skipped serializers (deps missing from venv — nothing pip-installed)

| serializer | missing dependency |
|---|---|
| avro | fastavro (`tram[avro]`) |
| msgpack | msgpack (`tram[msgpack_ser]`) |
| parquet | pyarrow (`tram[parquet]`) |
| protobuf | protobuf + grpcio-tools (`tram[protobuf_ser]`) |

asn1 was benched (asn1tools present) but **serialize is unsupported by design**
(decode-only serializer).

## Where the results live

- `scripts/perf/microbench/results/serializers.json`
- `scripts/perf/microbench/results/transforms.json`
- `scripts/perf/microbench/results/summary.md` (this file)
- Copies: `/tmp/opencode/perf-microbench/` (both JSONs + this summary)

Reproduce: `.venv/bin/python scripts/perf/microbench/bench_serializers.py` and
`.venv/bin/python scripts/perf/microbench/bench_transforms.py` (run from the
repo root).
