# TRAM — Improvement Recommendations

> **Status:** Recommendations derived Aug 2026 from the [TRAM vs Telegraf comparison](./tram-vs-telegraf-comparison.md).
> **Basis:** Telegraf gap analysis, the 8 open GitHub issues on `tosumitdhaka/trishul-ram`, roadmap cross-check against code, and code-verified gap status (G1–G9).
> **Cross-references:** Comparison summary stored as project memory ID 7 (`ARCHITECTURE`).

---

## Verified gap status (code-confirmed)

Each gap below was verified against the current source — not assumed from the roadmap.

| Gap | Verdict | Evidence |
|---|---|---|
| **G1 — no hot-loadable custom logic** | Confirmed (partially addressed) | Only sandboxed/declarative escapes: `add_field`/`filter_rows` (simpleeval), `template` (str.format), `jmespath`. No subprocess/exec/Starlark/Lua. Full-fidelity scripting requires writing a plugin + decorator + `__init__.py` import + Pydantic model. |
| **G2 — thread-based execution** | Confirmed | Entire exec is `threading`/`ThreadPoolExecutor` (`executor.py`, `controller.py` `ThreadPoolExecutor(max_workers=10)`). asyncio only at FastAPI edge. |
| **G3 — smaller plugin catalog (83 vs 370+)** | Confirmed | Live registry = 24 src + 20 sink + 27 xform + 12 ser = 83. |
| **G4 — no CRD/operator** | Confirmed | Only `tram/pipeline/k8s_service_manager.py` creates plain Services. No CRD/controller/informer/watch. |
| **G5 — no exactly-once / checkpointing** | Confirmed | Zero matches for `checkpoint|exactly.?once|transactional` in `tram/`. Kafka source uses `enable_auto_commit=True` + best-effort commit on close. |
| **G6 — no manager HA / leader election** | Confirmed | Only in-process `threading.Lock` mutexes in `tram/agent/`. No distributed election. SQLite+RWO PVC forces single manager. |
| **G7 — no RBAC** | Confirmed | `APIKeyMiddleware` = single global key OR any session token. Zero matches for role/scope/permission/admin/viewer. |
| **G8 — no DLQ viewer / live log streaming** | Confirmed | `tram/ui/src/pages/runs_table.js:137` renders only a DLQ count badge. No WebSocket log tail. |
| **G9 — stale docs/numbers** | Confirmed | `docs/architecture.md:104` says Transforms=21 (live=27). `README.md:7` badge claims Python 3.11, `pyproject.toml:10` requires `>=3.13`. |

## Open GitHub issues (8, all unlabeled)

| # | Title | Theme |
|---|-------|-------|
| 22 | Dashboard stats and chart for batch runs are completion-based, not live | Ops/UI |
| 21 | Optionally queue manual run requests when no healthy workers available | Ops |
| 20 | UI: Templates page action row and preview modal do not match shared detail components | UI |
| 19 | enh(asn1): split_path + split_path_context for single-dict BER inputs | Perf/ASN.1 |
| 18 | perf: json_flatten/_apply_explodes deepcopy is O(n²) for large nested records | Perf |
| 17 | Cluster/Detail stream visibility drops for long-running manager-mode streams | Ops/UI |
| 16 | Worker pods retain large anonymous heap after heavy CDR batch runs → OOM risk | Perf/Memory |
| 3 | Alert cooldown consumed even when delivery fails [v1.3.0] | Alerts |

---

## Tier 1 — Quick wins (low effort, do now)

Concrete, isolated, low-risk fixes. No architectural decisions needed.

| # | Improvement | Evidence | Effort |
|---|---|---|---|
| **Q1** | **Fix stale docs: transform count 21→27 in `docs/architecture.md:104`** | G9 confirmed; live registry has 27 transforms | XS |
| **Q2** | **Fix README Python badge: 3.11→3.13** | `README.md:7` badge vs `pyproject.toml:10` (`>=3.13`); v1.3.3 plan notes 3.11 dropped | XS |
| **Q3** | **Triage stale issue #3** | `tram/alerts/evaluator.py:74-75` now only sets cooldown on `fired=True` — the exact behavior #3 requests. Roadmap marks it `[x]`. Verify with a regression test, then close #3 (or find the residual path). | S |
| **Q4** | **Label the 8 open issues** | All 8 issues are unlabeled despite 9 defined labels. Triage into `bug`/`enhancement`/`documentation` so the board is filterable. | XS |
| **Q5** | **Stop committing build artifacts** | `build/`, `dist/tram-1.3.3-py3-none-any.whl`, `.coverage`, `.ruff_cache/`, `.pytest_cache/`, `.codex` (0-byte) are in the tree. Add to `.gitignore` and remove. | S |
| **Q6** | **Fix dev venv mismatch** | Project venv is Python 3.12 but `pyproject.toml` requires `>=3.13`. Recreate venv on 3.13 to match runtime and catch 3.13-only issues locally. | XS |
| **Q7** | **Auto-generate plugin counts in docs from registry** | G9 drift (21 vs 27) happened because counts are hand-maintained. Add a `tram plugins --json` (exists) → docs-build hook that emits the count table. Prevents future drift. | S |

**Why now:** All seven are isolated, unblock trust in docs, and clean the repo. Q3 closes a real loop (issue claims unfixed, code says fixed). Q7 prevents the G9 class of drift permanently.

## Tier 2 — Reliability & performance (open issues, high impact)

These address open issues with concrete code evidence. Highest user-visible payoff.

| # | Improvement | Evidence | Effort |
|---|---|---|---|
| **R1** | **Fix `json_flatten` O(n²) deepcopy (#18)** | `tram/transforms/json_flatten.py` `_apply_explodes` deep-copies rows before removing the source list — 13k-row nested records take ~700s. Rewrite to build new dicts in place / use shallow copies where safe. | M |
| **R2** | **Default `post_batch_cleanup` ON for heavy pipelines (#16)** | `executor.py` `_post_batch_cleanup` (gc + malloc_trim) only runs when `config.post_batch_cleanup=true`. Worker pods retain large anonymous heap after CDR runs → OOM risk. At minimum default it on for `asn1`/`parquet`/large-batch pipelines; better, make cleanup automatic when `record_count > threshold`. | S-M |
| **R3** | **Live batch-run stats (#22)** | Dashboard traffic cards/charts only update on run completion. Wire `PipelineStats` incremental emit into the runs/stats SSE/poll path so in-flight batch runs show progress. | M |
| **R4** | **Fix long-running stream visibility in manager mode (#17)** | Streams disappear from Cluster/Detail views after a while. Matches v1.3.3 plan #17. Likely a stats-store TTL or heartbeat gap in `tram/agent/stats_store.py`. | M |
| **R5** | **Queue manual runs when no healthy workers (#21)** | Manual/batch run fails fast if no workers available. Add an optional queue-and-retry path in `WorkerPool.dispatch_to_worker` with a configurable timeout. | M |
| **R6** | **ASN.1 single-frame split (#19)** | `asn1_serializer.py` `split_processed` only splits at BER frame boundary; single-frame nested dicts can't be split. Add `split_path`/`split_path_context` per the issue. | M |

**Why these:** Every one maps to an open issue with reproducible impact (perf, OOM, visibility). R1 and R2 are the most urgent — O(n²) and OOM risk directly hurt production telecom workloads (CDR/PM batches).

## Tier 3 — Strategic gaps vs Telegraf (bigger investments)

Confirmed gaps (G1–G8). Each is roadmap-acknowledged except G1. Prioritized by impact × strategic value.

### High strategic value

| # | Improvement | Evidence | Effort |
|---|---|---|---|
| **S1** | **Hot-loadable custom logic transform (closes G1)** | No equivalent to Telegraf's Starlark/execd. Today: `template` (str.format), `jmespath`, `simpleeval`-based `add_field`/`filter_rows` cover reshape/branching but NOT loops, custom I/O, or side effects — users must write a full Python plugin. **Add a `script` transform** with a sandboxed Python/Starlark interpreter (reuse `simpleeval` or add `python-Starlark`) supporting per-record functions. This is the single biggest extensibility gap vs Telegraf and unlocks user-side logic without core changes. | L |
| **S2** | **RBAC (closes G7)** | `APIKeyMiddleware` is single global key OR any session token — zero roles/scopes. Backlog item. Add role model (admin/operator/viewer) + per-pipeline key scoping in `auth.py`/`middleware.py`. Required before multi-team deployments. | M-L |
| **S3** | **DLQ viewer + live log streaming (closes G8)** | UI shows only a DLQ count badge (`runs_table.js:137`); no record browser/replay, no WebSocket log tail. Add DLQ browse/replay API + a `/api/runs/{id}/logs/stream` WebSocket. Major ops UX win — TRAM's control plane is its headline advantage; DLQ opacity undercuts it. | M |
| **S4** | **Manager HA / leader election (closes G6)** | Single SQLite+RWO PVC manager = SPOF. No leader election in `tram/agent/`. Backlog item. Add lease-based election (Kubernetes Lease or Postgres advisory lock when Postgres backend) so a standby manager can fail over. Unblocks production HA. | L |

### Medium strategic value

| # | Improvement | Evidence | Effort |
|---|---|---|---|
| **S5** | **Kafka exactly-once / offset checkpointing (closes G5)** | Zero matches for checkpoint/exactly-once/transactional. Kafka source uses `enable_auto_commit=True` + best-effort commit on close; roadmap flags "Kafka offset commit edge cases" as unshipped. Add a transactional producer option + offset state persisted to DB (commit after sink-ack). At least make offset commits sink-ack-gated for at-least-once-tight. | L |
| **S6** | **K8s operator / CRD (closes G4)** | No CRD/operator; `KubernetesServiceManager` only creates plain Services. Pipelines are ConfigMap-mounts or API-registered. A CRD (`Pipeline`) + controller would make TRAM GitOps-native (declarative `kubectl apply`) — Telegraf has `telegraf-operator` for this. Significant for K8s-first adopters. | L |
| **S7** | **Async execution path for high fan-out (closes G2)** | Entire pipeline exec is `threading`/`ThreadPoolExecutor`. For very high fan-out (many sinks, many streams), threads are heavier than goroutines. An optional asyncio executor for I/O-bound sinks (HTTP/Kafka/DB) would raise fan-out ceiling. Large effort; defer unless fan-out bottlenecks appear. | XL |

## Tier 4 — Catalog growth & ergonomics (closes G3 + friction)

| # | Improvement | Evidence | Effort |
|---|---|---|---|
| **C1** | **Ship backlog connectors: SMTP sink, gRPC sink, syslog sink, PM-XML source** | All in roadmap backlog, none present (grep = 0). Telecom-relevant; close catalog gaps vs Telegraf's 370+. | M each |
| **C2** | **Make `[all]` extra actually include everything** | `pip install tram[all]` excludes s3/gcs/azure/parquet/protobuf/corba — users assuming `[all]`=everything hit import errors. Either rename to `[all-core]` + add `[full]`, or document loudly. | S |
| **C3** | **Bundle/automate CORBA (omniORB) install** | `[corba]` is system-only, excluded from images/wheels — every user must pre-bake omniORB. Ship a build script or a slim Docker image variant. | M |
| **C4** | **Schema-validated `AppConfig`** | 70+ `TRAM_*` env vars via `core/config.py` with no schema-validated surface — typos surface only at runtime. Add a Pydantic `AppConfig` with strict mode + startup validation warnings for unknown vars. | M |
| **C5** | **Reduce sink-config duplication** | `condition`/`transforms`/`serializer_out` are flat optional fields on every `*SinkConfig` — a `FileSinkConfigMixin` would cut duplication (internal, not user-facing). | S |

## Suggested sequencing

1. **Now (1–2 days):** Tier 1 (Q1–Q7) — docs, stale issue, repo hygiene. Unblocks trust.
2. **Next (1–2 weeks):** Tier 2 R1 + R2 (perf/OOM — production-critical), then R3–R6.
3. **Next quarter:** S1 (script transform — biggest extensibility win), S3 (DLQ viewer — leverage control-plane advantage), S2 (RBAC).
4. **Strategic:** S4 (HA), S5 (Kafka EOS), S6 (operator) — sequence by customer demand.
5. **Continuous:** C1 connector growth + C2/C4 ergonomics.

## Three things NOT to do

- **Don't chase Telegraf's plugin count (G3).** TRAM's 83 telecom-focused plugins serve its niche; matching 370+ general plugins would dilute focus. Grow selectively (C1) for telecom-relevant gaps only.
- **Don't rewrite the executor to asyncio just because Telegraf uses goroutines (G2/S7).** Thread-based exec is fine for telecom batch/medium-stream workloads. Only invest if real fan-out bottlenecks appear. Python asyncio + blocking libs (pysnmp, paramiko) is a quagmire.
- **Don't abandon the control-plane advantage.** TRAM's REST API + UI is its clearest edge over Telegraf. Tier 3 (S2 RBAC, S3 DLQ viewer, S4 HA) all *reinforce* that advantage — prioritize them over catalog breadth.

---

## Appendix — Verification evidence

- Registry counts: `tram/connectors/__init__.py`, `tram/connectors/*/sink|source.py` (`@register_*`), `tram/transforms/__init__.py`, `tram/serializers/__init__.py`.
- Execution model: `tram/pipeline/executor.py` (`batch_run`, `stream_run`, `_stream_run_threaded`, `_process_records`), `tram/pipeline/controller.py` (`ThreadPoolExecutor(max_workers=10)`, `_start_stream`, `_add_interval_job`).
- No checkpoint/offset machine: zero matches for checkpoint/exactly-once/transactional in `tram/`; Kafka source `tram/connectors/kafka/source.py` (auto-commit, `consumer.commit()` on close).
- No HA: `tram/agent/*` all in-process `threading.Lock`; no leader election.
- No RBAC: `tram/api/middleware.py` `APIKeyMiddleware` + `tram/api/auth.py` (single key / session token).
- K8s: only `tram/pipeline/k8s_service_manager.py` Services; no CRD/operator.
- Alert cooldown code: `tram/alerts/evaluator.py:74-75` (`_set_cooldown` only on `fired`).
- Doc stale: `docs/architecture.md:104` "Transforms 21" vs registry 27; `README.md:7` badge 3.11 vs `pyproject.toml:10` (>=3.13).
- Open issues: 8 open, all unlabeled (verified via `gh issue list`).
- Lint: `ruff check .` → `All checks passed!` (zero issues).
- Tests: `pytest tests/unit/ -q --co` collects 1610 tests across 99 files.
- Committed artifacts: `build/`, `dist/tram-1.3.3-py3-none-any.whl`, `.coverage`, `.ruff_cache/`, `.pytest_cache/`, `.codex`.

---

*Document generated Aug 2026. Update the gap-verification table when roadmap items ship.*
