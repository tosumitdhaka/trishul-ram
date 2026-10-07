# v1.7.0 Pilot A: HTTP accelerated runtime — BLOCKED (invalid A/B, see findings)

Image local-20260711937 (http_accel baked). s1_webhook_local, corpus_100k, 40s steps.

## Findings
1. CONFOUND — flag-off is NOT a true baseline: with uvloop installed in the image,
uvicorn's loop/http auto-selection picks uvloop+httptools even with TRAM_HTTP_ACCELERATED=0
(runtime line 'HTTP runtime: loop=uvloop http=httptools' logged by a flag=0 pod). The plan's
'installed != active' rule was violated by auto-selection. FIX REQUIRED: flag-off must
explicitly pin loop=asyncio/http=h11 before any A/B is valid.
2. BLOCKER — TRAM_HTTP_ACCELERATED=1 (explicit uvloop/httptools) hangs ALL POSTs with bodies
on the worker ingress (GET / answers 404 fast; manager's main-thread server POSTs fine
400@4ms; in-cluster ClusterIP + external NodePort both hang). Root cause NOT isolated —
suspect the worker's dual-thread uvicorn topology under explicit accel kwargs. Needs local repro.
3. Numbers captured (auto-uvloop, pipeline running, zero errors): 500rps p50 1.3ms p95 2.3ms;
plateau ~640rps @conc50 (connection-limited), 486rps @conc200 (p95 1.18s).
4. Cluster restored: flag=0 + s1 running -> POST 202 in 2.5ms; s1 stopped after.

run.sh + all loadgen summaries attached. Runtime evidence lines in pod logs.

## Post-investigation addendum (2026-10-07, local repro in `repro/`)

The "BLOCKER" above is **root-caused and cleared — it was not the runtime**:

- `receive_webhook` (tram/api/routers/webhooks.py) holds unmatched POST paths
  for up to `DEFAULT_PLACEMENT_WINDOW_SECONDS = 10.0` s awaiting placement
  propagation (GH #82). Unmatched GETs return 404 immediately — exactly the
  observed GET-fast / POST-hang asymmetry.
- Local repro with the real `serve()` worker (dual-thread agent+ingress,
  explicit `uvloop`+`httptools`): POST to an UNREGISTERED path "hangs" under
  BOTH runtimes (10 s window); POST to a REGISTERED webhook queue returns
  **202 in 1–17 ms under both flag=0 and flag=1** (`repro/worker_serve2.py`).
- Conclusion: during the flag=1 window the s1 pipeline was not re-adopted on
  the worker ingress after the rollout, so every POST hit the placement hold.
  The accelerated runtime itself is healthy in the worker dual-server
  topology.

Campaign re-run pending with the corrected runbook: after each rollout,
verify webhook registration (canary POST → fast 202) BEFORE loadgen; baseline
side now runs the explicit asyncio/h11 pin (a4ac731) so it is a true default
runtime even with the extra installed.

## Final A/B re-run (run2/, images local-20261007121103 with the a4ac731 pin)

Both sides fresh-registered (symmetric); s1_webhook_local, 500m workers, 40s steps.

| shape | base asyncio/h11 | accel uvloop/httptools | delta |
|---|---|---|---|
| 500 rps c50 p50/p95 (ms) | 1.75 / 3.05 | 1.23 / 1.76 | −30% / −42% |
| plateau 1000–2500 c50 (rps) | 573–658 | 627–678 | ±6% |
| 2000 rps c200 (rps) | 452 | 447 | ~0 |
| errors | 0 | 0 | — |

Verdict: latency target met (p95 1.76 ms ≪ 50 ms, −42%); ≥25% sustainable-throughput
target NOT met — the plateau is bound downstream of HTTP parsing. Ships default-off.

Adoption measurement: no stream re-adoption after worker rollout under EITHER
runtime (900 s flag=0 / 600 s flag=1) — flag-independent pre-existing gap
(roadmap backlog). The accelerated runtime serves registered webhook POSTs in
1–2 ms and completes fresh dispatch/adoption-cycle operations normally.
