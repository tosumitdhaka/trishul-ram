# v1.6.1 deployment-Python re-measure (paired, real old vs real new)

Decision evidence for the v1.6.1 locked scope (docs/roadmap.md §v1.6.1;
docs/plans/perf-v161-v170-plan.md). Complements the pre-implementation
prototype bench in `../followup-2026-10-07/` — this run measures the REAL
implementations on both sides, on deployment Python 3.13 with deployment
dependency versions, inside the v1.6.0 worker image environment.

## Method

`driver.py` alternately spawns `child.py` child processes whose PYTHONPATH
points at the OLD code (`fdcf371`, v1.6.0, mounted from a git worktree) and
the NEW code (release/v1.6.1 HEAD). Each child runs one rep of the affected
operations with the real code paths (no prototypes, no patches); the parent
asserts old-vs-new OUTPUT EQUALITY on every rep (SHA-256 over results, incl.
Kafka sent-message bytes) and reports medians of 7 interleaved reps.
Individual raw reps are recorded in `remeasure.json`.

Kafka timings use an immediate-ack fake producer: they exclude broker/network
costs, exactly like the pre-implementation bench. The 1,000-record payload
(527,656 B) exceeds `chunk_bytes` (524,288 B), so both sides take the legacy
path there — it is a fallback-parity control, not a gain claim.

## Reproduce

```sh
git worktree add /tmp/tram-v160-baseline fdcf371
docker run --rm \
  -v /home/dhaka/trishul/trishul-ram:/new:ro \
  -v /tmp/tram-v160-baseline:/old:ro \
  -v <this-dir>:/bench -w /bench \
  --entrypoint python3 trishul-ram-worker:<v1.6.0-image-tag> driver.py
```

## Result (medians, µs per unit)

| operation | v1.6.0 (old) | v1.6.1 (new) | speedup |
|---|---:|---:|---:|
| sink condition, compile-once (10k recs) | 16.584 | 2.074 | 8.00x |
| timestamp_normalize, 2 ISO fields | 22.535 | 5.059 | 4.45x |
| counter_delta, 2 fields | 13.149 | 5.284 | 2.49x |
| window_aggregate, 500 groups | 17.233 | 8.576 | 2.01x |
| kafka sink write, 500-record in-cap payload | 3,480.466 | 162.077 | 21.47x |
| kafka sink write, 1,000-record over-cap control | 9,771.846 | 10,036.192 | 0.97x |

Operation-level gains, not whole-pipeline forecasts (the locked scope's ≥20%
target applies to affected pipelines at equal CPU — that is the cluster
campaign's to establish). Output hashes matched old-vs-new on every rep.

Full data: `remeasure.json` (host CPU, Python version, raw per-rep timings).
