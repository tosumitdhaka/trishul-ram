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
