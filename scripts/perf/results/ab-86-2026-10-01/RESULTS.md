# TRAM #86 controlled A/B re-run — results summary

Date: 2026-10-01, 10:25Z–12:00Z (single session, shared WSL2 host, no host suspends).
Code: release/v1.6.0 @ dda5cc9 (Wave-1 merged), images local-20261001102732 built from the
clean tree and deployed with scripts/deploy-kind-tram-dev.sh.
Topologies alternated in the SAME session: mw@M → single@M → mw@M → single@M →
mw@H → single@H → mw@H → single@H (8 deployments, interleaved per task instruction).

## Raw throughput (records/s = 100000 / run-duration; run-history rows filtered per rep)

| cell            | mw rep1 | mw rep2 | single rep1 | single rep2 | mw med | single med | single/mw | ORIGINAL mw | ORIGINAL single | orig single/mw |
|-----------------|--------:|--------:|------------:|------------:|-------:|-----------:|----------:|------------:|-----------------:|---------------:|
| s2csv  @ M      | 55,643  | 50,366  | 48,476      | 50,494      | 52,905 | 49,485     | 0.94      | 55,356      | 51,587           | 0.93           |
| s2csv  @ H      | 78,277  | 106,415 | 99,207      | 94,763      | 92,346 | 96,985     | 1.05      | 107,721     | 111,111          | 1.03           |
| s2pmxml @ M     | 16,394  | 17,388  | 16,841      | 16,385      | 16,891 | 16,613     | 0.98      | 9,760       | 17,712           | 1.81           |
| s2pmxml @ H     | 31,533  | 36,153  | 34,161      | 30,127      | 33,843 | 32,144     | 0.95      | 20,601      | 38,462           | 1.87           |
| s3     @ M      | 6,023   | 5,772   | 5,975       | 6,103       | 5,898  | 6,039      | 1.02      | 3,315       | 6,417            | 1.94           |
| s3     @ H      | 7,856   | 10,060  | 10,732      | 9,807       | 8,958  | 10,270     | 1.15      | 6,020       | 11,458           | 1.90           |
| s3p1000 @ M (page_size 1000 diagnostic) | 29,230 | — | 29,387 | — | 29,230 | 29,387 | 1.01 | — | — | — |

All runs: records_in = records_out = 100,000, errors = 0, status = success.
s3 sink-side (mock /collect): records=100000, posts=1000, bytes=56,279,616 — identical both topologies.

## Verdict

The 1.8–2.1× mw-vs-single gap does NOT reproduce. Under controlled same-session
conditions the two topologies are at parity on all three scenarios (single/mw
0.94–1.15, overlapping rep spreads). The re-run's mw numbers match the ORIGINAL
study's SINGLE numbers (e.g. s3@M: mw now 5.9k vs original single 6.4k), i.e. the
original mw measurements were ~2× slow, not the original single measurements fast.

## Evidence: per-batch worker-mode bookkeeping (the leading hypothesis) — CONTRADICTED

1. Worker pod logs during an s3 run (mw, 1000 pages): exactly 1002 INFO lines —
   "Batch run started", 1000 × "REST sink wrote data" (per-chunk sink write), "Batch
   run completed". ZERO per-batch/per-page manager traffic. Manager log during the
   window: Registered pipeline / Dispatched run to worker / Deregistered / Deleted —
   4 lifecycle lines. run-complete = ONE per run (persisted row); pipeline-stats =
   periodic (30s StatsStore tick), no per-batch posts; no callback failures anywhere.
2. s2pmxml (mw): "Local source found files" + 10 × "Wrote file locally" + run
   start/end — HttpFileTracker marks are buffered and flushed once at run end.
3. Average CPU during runs (cgroup cpu.stat usage_usec deltas, executing pod):
   identical between topologies — s3@M 0.51 vs 0.51 cores; s2pmxml@M 0.53 vs 0.52;
   s2csv@M 0.60–0.69 vs 0.58; s3@H 0.84–0.85 vs 0.85; s2pmxml@H 1.06 vs 1.04.
   The original claim "mw burns ~2.3× the CPU per record" does not hold; mw and
   single burn the same CPU per record.
4. s3 page cadence (host mock request log): p50 inter-page gap 9.4/9.9 ms (mw)
   vs 9.3/9.1 ms (single) @ M; 9.1–11.5 vs 8.6–9.3 @ H — identical. The real per-page
   cost (~14.6 ms fixed, from the page-size model: 16.6 s/1000 pages vs 3.4 s/100
   pages) is the two HTTP round-trips per chunk (GET page + POST sink) through kind
   networking — topology-neutral.
5. page_size 1000 diagnostic: 29.2k (mw) vs 29.4k (single) — parity at 10× fewer,
   10× bigger batches. Batch granularity dominates s3 throughput; topology does not.

## Root cause of the ORIGINAL gap — environment artifact (mechanism identified)

Asymmetry found between the two DBs used by the original study:
- manager PVC (mgr+worker topology, original mw runs Sept 29–30): 37 registered
  pipelines, of which 6 INTERVAL-SCHEDULED and enabled — sftp-pm-to-kafka @30 s,
  snmp-walk-iftable @60 s, test @300 s, snmp-localhost-to-jsonl @300 s,
  snmp_to_jsonl_ifmib @300 s, sample-health @600 s. These dispatch as runs onto the
  same 3 worker pods and share the measured pod's CPU quota. One of them ("test",
  SNMP GET with timeouts) was directly observed firing mid-run during this re-run's
  smoke cell (6.5 s of worker wall time). None are mentioned anywhere in the
  original study's deploy notes.
- standalone PVC (single topology, original single runs Oct 1): ZERO pipelines —
  a fresh empty DB; the original single runs were measured with a clean scheduler.

This asymmetry + the day-apart sessions on a shared WSL2 host fully explains the
original "workload-specific" 2× gap pattern: short cells (s2csv ~2 s, t1/t5/fsweep)
dodge background firings; longer cells (s2pmxml ~10 s, s3 ~30 s in mw) almost always
overlap one, and a colocated background run halves the measured run inside the
pod's 500 m quota — which is also why the original study saw the mw worker "pinned
at exactly 500 m" while single showed ~217 m for the same work (single truly needed
only ~510 m-core average — see CPU table; the mw reading was the SUM of the measured
run + background run inside one 500 m pod).

## Re-run environment notes / pitfalls hit (for the record)

- The first deploy attempt was killed by a tool timeout mid kind-load; re-run with
  setsid + fixed IMAGE_TAG (local-20261001102732) — subsequent deploys ~1-3 min.
- --reuse-values carried env.TRAM_MANAGER_URL=localhost:8765 (set by Phase 2c for
  the single topology) into the mw release; because the chart appends .Values.env
  AFTER its built-in worker TRAM_MANAGER_URL (duplicate env name, last wins, and
  duplicate names break helm's strategic-merge patch), workers got the wrong
  callback URL. Fixed by pinning env.TRAM_MANAGER_URL=http://trishul-ram:8765 in the
  mw values files + deleting/recreating the STSs. (Not a factor in the original
  study — the leak only exists when upgrading from the single release.)
- All 37 leftover pipelines were disabled via API before measurement; the smoke
  cell that overlapped a background firing was discarded and re-run.
- Standalone /data (PVC) carried 10 stale Phase-2c .json input files — cleaned and
  re-staged to exactly the 20 fresh files per run set.
- Harness copied to /tmp (run_one.sh + collector with --results-dir) — no writes to
  the repo tree. Raw artifacts: /tmp/opencode/v160-86/results/ab-*/ (per run:
  run-rows.json, cpu_stat_{before,after}.txt, top-during.csv, podlogs/, mock-*.json,
  run_one.log, summary.json, samples.csv, meta.json).

## Recommended next step (NO code changes made)

Close the perf-investigation: the issue's premise (worker-mode per-batch
bookkeeping) is disproven — there is no per-batch manager round-trip and no per-batch
CPU penalty in mw mode. Do NOT spend code budget on "batching/coalescing worker-mode
bookkeeping" for #86. Two cheap follow-ups if desired: (1) a bench-hygiene note or
harness check that asserts a clean scheduler (no enabled interval/stream pipelines)
before matrix runs — the original mw numbers for s2pmxml/s3 (and possibly other
long-window cells) should be treated as contaminated and re-baselined at the parity
numbers measured here; (2) optionally, the chart-level duplicate-env / --reuse-values
callback-URL leak deserves a small fix ticket (worker TRAM_MANAGER_URL silently
overridable via values.env).
