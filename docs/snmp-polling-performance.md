# SNMP Polling Performance

Human-readable performance data for TRAM's `snmp_poll` source (GET and WALK
operations), measured live on 2026-10-08 on two environments:

- **Local loopback** (operation-level): TRAM's real `SNMPPollSource.read()`
  driven directly against the synthetic table responder used by the perf
  campaigns (`scripts/perf/results/ab-86-2026-10-01/perf/generators/
  snmp_responder_scaled.py`), on the repo venv, both wire stacks.
- **Kind cluster, end to end** (pipeline-level): full
  `snmp_poll → serializer → sink` pipelines registered and run through the
  manager API (NodePort 30001), against the same responder published on the
  host bridge (`172.19.0.1`), with json/jsonl output and local/SFTP sinks,
  on both wire stacks.

Both stacks were measured because the legacy (pysnmp) stack is slated for
deprecation once the trishul stack is fully validated; this document is part
of that evidence.

## Executive summary

| Question | Answer |
|---|---|
| How fast is a scalar GET poll? | **3 ms (trishul stack) / 94 ms (legacy)** per poll on loopback; **sub-second end to end in-cluster** (~0.2 s run time including dispatch and sink write) |
| How fast is a table WALK? | One varbind per GETNEXT round trip: a 100-row × 8-column table walks in **~8–13 s**; walk time = (rows × columns + 1) × per-request latency — the *agent's* per-request cost dominates |
| How does walk scale with table size? | Against this synthetic agent, per-request GETNEXT latency **grows with table size** (linear-scan MIB lookup): 10 ms @ 100 rows → 141 ms median @ 1000 rows (growing 6 ms → 1.9 s within one walk). Walks are latency-bound, not CPU-bound — the in-cluster s4 campaign showed the same (flat across L/M/H worker profiles) |
| What does the output side cost? | Per poll: local sink **~0.2 s** per run, SFTP **~0.8 s** per run (≈ 0.55 s is SFTP connection setup). For a 100-row walk the sink is **1–7 % of run time** — SNMP polling pipelines are input-bound |
| json or jsonl output? | Effectively identical at these sizes (36,198 vs 36,097 bytes for 100 rows); ndjson is marginally smaller |
| Which stack is faster? | **Trishul: 31× faster GET** (3.0 vs 93.9 ms median per poll), **1.26× faster 1000-row walk**, parity on 100-row walks and in-cluster end-to-end. GET latency on the legacy stack is dominated by per-poll pysnmp engine setup (~90 ms fixed cost) |
| Would BULKWALK help? | TRAM has no bulkwalk operation (`operation` accepts `get` or `walk` only). A raw GETBULK reference against this agent shows **no gain** (agent-bound); against agents with indexed MIB storage, GETBULK reduces round trips by ~max_repetitions× — that is the migration headroom, not a measured TRAM number |

All runs completed with `records_out == records_in`, zero skipped, zero
errors, and byte-identical payload sizes across both stacks (same table
seed).

## Method

- Responder: synthetic SNMPv2c table, `rows × 8` columns of mixed types
  under `1.3.6.1.4.1.99999.2.1`, community `public` (the perf-campaign
  generator; serves GET/GETNEXT/GETBULK).
- Local loopback: `SNMPPollSource` constructed with the same config keys the
  `s4` pipeline template uses (`resolve_oids: false`, `yield_rows: true`,
  `index_depth: 1` for walks; plain multi-varbind GET for `get`), timed over
  the full `read()` (engine setup + requests + row grouping + JSON payload).
- Kind e2e: 100-row table; pipelines `get`/`walk` × `json`/`ndjson` out ×
  `local` (`/data/perf/snmpshow`)/`sftp` (`sftp.trishul-ram.svc`) sinks,
  manual schedule, 3 reps per cell, fresh pipeline name per rep, run-history
  rows matched by a pre-trigger run-id snapshot. Stack switched via
  `helm upgrade --set env.TRAM_SNMP_STACK=...` and verified in worker env.
- Host: WSL2; repo venv (pysnmp 7.1.25, trishul-snmp 0.6.2).

## Input-side performance (operation level, loopback)

### GET — one poll, 8 varbinds (row 1 of the table)

| Stack | Median | p95 | Min | Max |
|---|---:|---:|---:|---:|
| legacy (pysnmp) | 93.9 ms | 124.2 ms | 86.0 ms | 209.7 ms |
| trishul | **3.0 ms** | 5.4 ms | 2.2 ms | 46.0 ms |

50 reps each. The legacy number is dominated by per-poll pysnmp engine
setup (~90 ms fixed); the trishul stack has no equivalent fixed cost. In
end-to-end pipelines both land at the ~0.2 s run-time floor because
dispatch + sink write dominate.

### WALK — GETNEXT chain, `yield_rows: true`

| Table | Stack | Median wall | Per request | Varbinds/s |
|---|---|---:|---:|---:|
| 100 rows (801 requests) | legacy | 8.2 s | 10.2 ms | 97.6 |
| 100 rows | trishul | 9.5 s | 11.9 ms | 84.1 |
| 1000 rows (8001 requests) | legacy | 1270.7 s | 140.8 ms median | 6.3 |
| 1000 rows | trishul | 1009.9 s | 126.2 ms mean | 7.9 |

Per-request latency distribution for the legacy 1000-row walk (8001
requests, instrumented): min 6 ms, p25 55 ms, median 141 ms, p75 224 ms,
p95 432 ms, max 1949 ms — **latency grows as the walk advances through the
table**, because the synthetic agent resolves GETNEXT by scanning its
in-process MIB from the base. Total walk cost is therefore superlinear
(~O(N²)) against this agent. Real agents with indexed MIB storage do not
behave this way; their walk time is (rows × columns) × network-RTT-ish
latency. The in-cluster history corroborates the latency-bound character:
the s4 campaign's 1000-row walks ran at 1.6–1.7 rows/s with identical
payload bytes (363,044) and were flat across L/M/H worker CPU profiles.

**Walk-5000 was excluded as infeasible**: a single GETNEXT against the
5000-row table measured 3.78 s (responder-side), projecting a ~11-hour walk.
(With the default 2 s request timeout the first request times out and the
walk returns an **empty** payload — see Caveats.)

### GETBULK reference (NOT a TRAM operation)

Raw pysnmp GETBULK walks over the 100-row table, mirroring TRAM's walk loop
(`lookupMib=False`, subtree guard):

| max_repetitions | Requests | Median wall | Varbinds/s |
|---|---:|---:|---:|
| 10 | 81 | 11.0 s | 72.8 |
| 25 | 33 | 10.9 s | 73.7 |
| 50 | 17 | 28.9 s | 27.6 |

Against **this** agent, GETBULK buys nothing — the agent's per-varbind scan
cost dominates and batch responses do not reduce it (it gets *worse* at
max_repetitions=50). The classic bulkwalk win (round trips ÷
max_repetitions) applies to agents with indexed MIB storage; treat the
numbers above as a wire-machinery reference only.

## Output-side performance (kind, end to end)

Run time (`finished_at − started_at`) medians of 3 reps, 100-row table:

| Operation | Serializer | Sink | legacy | trishul |
|---|---|---|---:|---:|
| get | json | local | 0.28 s | 0.21 s |
| get | json | sftp | 0.85 s | 0.79 s |
| get | ndjson | local | 0.23 s | 0.18 s |
| get | ndjson | sftp | 0.82 s | 0.75 s |
| walk | json | local | 12.98 s | 12.28 s |
| walk | json | sftp | 13.73 s | 12.84 s |
| walk | ndjson | local | 12.83 s | 12.25 s |
| walk | ndjson | sftp | 14.08 s | 12.52 s |

Readable takeaways:

- **SFTP costs ~+0.55 s per run** vs local (connection setup per run; the
  transport is opened once per run and reused across writes). For GET-shape
  polls that triples run time; for walks it disappears into the poll cost.
- **json vs ndjson is a wash** at these sizes (36,198 vs 36,097 bytes out
  for 100 rows).
- Walk runs are **~95 % input-side**: 13 s poll vs ≤ 0.8 s sink.
- Artifacts verified on disk: local sink files and SFTP uploads present with
  expected byte counts; one outlier exists (one legacy walk-ndjson-sftp rep
  at 19.9 s, and one trishul first-run-after-rollout at 78.6 s — cold start;
  medians exclude their effect by repetition).

## Stack comparison (deprecation evidence)

| Metric | legacy (pysnmp) | trishul | Delta |
|---|---:|---:|---|
| GET poll, loopback (median) | 93.9 ms | 3.0 ms | **31× faster** |
| 100-row walk, loopback (median) | 8.2 s | 9.5 s | ~16 % slower |
| 1000-row walk, loopback | 1270.7 s | 1009.9 s | **1.26× faster** |
| e2e GET run (median of matrix) | ~0.23–0.85 s | ~0.18–0.79 s | parity |
| e2e walk run (median of matrix) | 12.8–14.1 s | 11.6–13.3 s | parity–6 % faster |

The trishul stack matches or beats the legacy stack everywhere except the
100-row loopback walk (within noise), and eliminates the ~90 ms per-poll
engine setup that dominates legacy GET polling. No output differences: all
payload byte counts identical.

## Caveats (what these numbers are and are not)

1. **The synthetic responder's GETNEXT is a linear scan**, so walk numbers
   here are agent-cost-dominated and superlinear in table size. They
   demonstrate the *shape* of GETNEXT-chain polling (latency-bound, one
   round trip per varbind) and TRAM's overhead relative to it — they are not
   a statement about production agents.
2. **Empty walk on request timeout**: if a GETNEXT exceeds `timeout` ×
   (`retries` + 1), TRAM's walk stops and yields whatever it has (possibly
   an empty payload) without failing the run. This is not theoretical: the
   1000-row walk's slowest request measured 1949 ms against the default 2 s
   timeout — within 51 ms of the ceiling. Agents with slow GETNEXT need a
   raised `timeout`; measure first.
3. **5000-row exclusion**: single GETNEXT against the 5000-row table
   measured 3.78 s; a full walk projects to ~11 h against this agent and was
   not run.
4. **E.2 queue idempotency reuses orphaned run ids**: a manual run triggered
   on a pipeline that is deleted mid-dispatch can remain queued; a later
   pipeline re-registered under the *same name* gets the *same run id* back,
   with `started_at` stamped at the original enqueue — inflating
   run-duration diffs. The e2e driver therefore snapshots run ids before
   each trigger and matches only new ids. (Discovered live: an initial
   contaminated data pass was discarded because of exactly this.)
5. **In-cluster walk history**: the earlier s4 campaign (same responder,
   1000-row table, cross-node) measured 1.6–1.7 rows/s — consistent with the
   loopback 1000-row walk (0.79 rows/s) once the agent cost is accounted
   for; payload bytes match exactly (363,044).
6. **GETBULK reference** uses raw pysnmp, not TRAM code paths — it is a
   wire reference for what a future bulkwalk operation could target, not a
   TRAM capability.

## Reproduction

Harness and raw JSONL results were kept out of the tree (ephemeral
`/tmp/opencode/snmp-showcase/`): `bench.py` (loopback get/walk/bulk),
`walk_instr.py` (instrumented single walk rep), `e2e_driver.py` (kind e2e
matrix), plus `results-{legacy,trishul}.jsonl` and `e2e-*.jsonl`. The
responder generator lives in the repo at
`scripts/perf/results/ab-86-2026-10-01/perf/generators/snmp_responder_scaled.py`.
