# TRAM vs Telegraf — Architecture Comparison

> **Status:** Analysis completed Aug 2026. Grounded in tram source code (verified paths, plugin counts from grep, cited symbols) and official Telegraf docs + live `influxdata/telegraf` repo (v1.39.2, plugin counts from directory enumeration).
> **Companion doc:** [`tram-improvements.md`](./tram-improvements.md) — improvement recommendations derived from this comparison.
> **Cross-references:** Comparison summary stored as project memory ID 7 (`ARCHITECTURE`).

---

## Overview

Both are plugin-driven "collect → transform → forward" data-pipeline daemons, but they target **different problem domains** and make **opposite trade-offs** on nearly every axis. TRAM is a young, telecom-specialized, control-plane-rich Python daemon; Telegraf is a decade-old, general-purpose, metric-centric Go agent with a vast plugin catalog and no native management API.

## At a glance

| Dimension | TRAM (trishul-ram) | Telegraf |
|---|---|---|
| **Domain** | Telecom data integration (PM/FM/logs, SNMP/gNMI/CORBA/CDR) | General metrics collection (TICK stack) |
| **Language** | Python 3.13 | Go (single static binary) |
| **Config** | YAML (Pydantic-validated, discriminated unions) | TOML (union-merged multi-file) |
| **Pipeline stages** | Source → serializer_in → transforms → serializer_out → sink | Inputs → processors → aggregators → outputs |
| **Plugin count** | ~83 (24 src / 20 sink / 27 xform / 12 ser) | ~371+ (249 in / 71 out / 39 proc / 12 agg) |
| **Data model** | Generic `list[dict]` (arbitrary JSON-shaped records) | Metric: measurement + tags + fields + timestamp |
| **Control plane** | REST API + web UI + CLI (start/stop/reload/rollback at runtime) | None native — config file + SIGHUP (Enterprise adds Controller) |
| **Scheduling** | APScheduler (interval/cron/stream/manual) | Agent loop (`interval` + `flush_interval`, per-plugin overrides) |
| **Concurrency** | Threads + ThreadPoolExecutor; asyncio at API edge only | Goroutines per plugin |
| **K8s** | StatefulSets + Helm, manager/worker split, per-pipeline Services | DaemonSet/Sidecar charts + separate `telegraf-operator` |
| **Version / age** | v1.3.3, ~185 commits, young | v1.39.2 (Jul 2026), since 2015, 5B+ downloads |
| **Delivery semantics** | At-least-once + DLQ + circuit breaker + file-tracking idempotency | At-least-once via metric buffer (in-memory; disk-buffer opt-in) |

---

## 1. Purpose & positioning

**TRAM** is explicitly a *telecom* tool — "Trishul Real-time Aggregation & Mediation." Its README frames the problem as replacing bespoke glue scripts between telecom source systems (SFTP PM dumps, SNMP traps, gNMI streaming telemetry, syslog, CORBA Itf-N CDRs) and analytics stores (OpenSearch, Kafka, InfluxDB, ClickHouse, S3). It is a **mediation layer** for heterogeneous telecom protocols, not a generic metrics agent.

**Telegraf** is the data-acquisition front-end of InfluxData's TICK stack — "the plugin-driven server agent for collecting & reporting metrics." It is general-purpose and metrics-first, designed to feed InfluxDB (and any other sink) from databases, systems, IoT sensors, and APIs. Its breadth ("any source to any destination") is the headline value.

**Verdict:** Different focus. TRAM optimizes for *telecom protocol coverage + runtime manageability*; Telegraf optimizes for *plugin breadth + lightweight universal deployment*. They overlap only on the abstract "move data from A to B with transforms" shape.

## 2. Language & stack

**TRAM:** Python 3.13, FastAPI + uvicorn, APScheduler (`<4.0`), SQLAlchemy Core (SQLite/Postgres/MySQL), Pydantic v2, `simpleeval` for conditions, typer CLI. Heavy optional deps per feature (paramiko, kafka-python, pysnmp, pygnmi, asn1tools, omniORBpy, pyarrow). Packaging uses extras to slim images. asyncio exists only at the API edge; pipeline execution is **thread-based**.

**Telegraf:** Go, compiled to a **single static binary with no runtime deps**. TOML config. Plugin extensibility via Go `init()` registry, plus `execd` (external process bridging), Starlark, and Lua for non-Go logic. Parsers/serializers are pluggable per-format (line protocol, JSON, CSV, Graphite).

**Verdict:** Telegraf wins decisively on **deployment footprint and operational simplicity** (one binary, minimal memory, no interpreter). TRAM pays the Python-runtime tax but gains faster prototyping, richer ecosystem for telecom-specific libraries (pysnmp, pygnmi, asn1tools), and a built-in web stack.

## 3. Pipeline model

**TRAM** — YAML pipeline, validated into Pydantic discriminated unions (`tram/models/pipeline.py`):
```
Source → (bytes, meta) → serializer_in.parse → global transforms
  → per-sink condition + transforms → serializer_out → sink
  → (error) → DLQ sink
```
Stages: **Source / Serializer / Transform / Sink** (4 plugin kinds). Each sink can override its own serializer, condition, and transform chain — a fan-out model. Schedules are first-class: `interval | cron | stream | manual`.

**Telegraf** — TOML config, four-stage pipeline per collection tick:
```
[inputs] → [processors] → [aggregators] → [outputs]
```
Stages: **Input / Processor / Aggregator / Output** (4 plugin kinds). Notable: aggregators are windowed (mean/min/max/quantile over a `period`) and `drop_original` keeps only aggregates. Documented ordering caveat (v1.17+): processors run **before and after** aggregators, so processor scripts must be idempotent.

**Verdict:** Structurally similar (4 stages, fan-out, per-stage plugins). TRAM's model is **more flexible per-sink** (independent serializer/condition/transform per sink) and treats **schedules as first-class pipeline config**. Telegraf's model is **more mature for time-series aggregation** (windowed aggregators are a dedicated stage) and its parser/serializer layer is data-format-driven rather than baked into the source/sink.

## 4. Plugin system

**TRAM:** Decorator-based self-registration (`@register_source("kafka")` etc. in `tram/registry/registry.py`). Discovery via `__init__.py` imports triggered at app startup. Adding a connector = write class + decorator + add import + add Pydantic config to the union (3 steps, **zero core-engine changes**). Counts (verified by grep): **24 sources, 20 sinks, 27 transforms, 12 serializers = 83 plugins.**

**Telegraf:** Go `init()` + `inputs.Add(...)` registry. Each plugin implements an interface (`Gather(Accumulator)`, `SampleConfig()`, `Description()`). Counts (live repo): **249 inputs, 71 outputs, 39 processors, 12 aggregators ≈ 371 plugins** (400+ per 2026 Enterprise announcement). Extensibility beyond Go: `execd` (external process), Starlark processor, Lua, and external `.so` plugins. Every plugin gets built-in metric filters (`namepass`/`namedrop`/`tagpass`/`metricpass`/`include_fields`).

**Verdict:** Telegraf's catalog is **~4.5× larger** and far more battle-tested, with multiple escape hatches for non-Go logic. TRAM's plugin authoring is arguably **simpler and more consistent** (decorator + Pydantic config = typed, validated, documented in one place) but the catalog is telecom-narrow. TRAM has no equivalent to Starlark/execd for hot-loadable custom logic — a real gap.

## 5. Execution & scheduling

**TRAM:** `PipelineController` (`tram/pipeline/controller.py`) owns lifecycle; APScheduler `BackgroundScheduler` drives `interval`/`cron` triggers → `_run_batch()`. Streams run in dedicated `threading.Thread`s with `threading.Event` stop signals and a bounded `queue.Queue` for backpressure. Concurrency: `ThreadPoolExecutor(max_workers=10)` for batch, `thread_workers` for in-pipeline parallelism, `parallel_sinks` for concurrent sink writes. Token-bucket rate limiting. Per-sink retry with exponential backoff + circuit breaker. Manager→worker dispatch via `WorkerPool` (least-loaded + round-robin), with `PlacementReconciler`/`BatchReconciler`.

**Telegraf:** The agent itself is the scheduler. Two cadences under `[agent]`: `interval` (gather) and `flush_interval` (write), both overridable per-plugin. On each tick, agent calls each input's `Gather(Accumulator)`; outputs flush buffered metrics in `metric_batch_size` batches with `flush_jitter`. Concurrency via goroutines per plugin. Slow consumers are absorbed by an in-memory **metric buffer** (oldest dropped on overflow; newer disk-buffer opt-in via TSD-005).

**Verdict:** TRAM has a **richer, more explicit execution model** — multiple schedule types, per-sink retry/circuit-breaker, manager/worker distribution, reconcilers. Telegraf is **simpler and proven at scale** but offers less fine-grained resilience (in-memory buffer drops on backpressure unless disk-buffer is enabled). TRAM's thread-based model is a weaker fit for very high fan-out than Telegraf's goroutines.

## 6. API & control plane

**TRAM:** Full REST control plane via FastAPI (`tram/api/`): pipeline CRUD + lifecycle (`start`/`stop`/`run`/`reload`/`rollback`/`dry-run`), run history, live stats, cluster nodes/streams, webhooks, Prometheus `/metrics`, connector test, schema registry, MIB management, AI assist, plus a Bootstrap 5 web UI at `/ui`. Hot-reload via file watcher or `POST /api/pipelines/reload`. Auth: API key + session tokens. This is a **first-class control plane** — you manage pipelines at runtime like a StreamSets/NiFi.

**Telegraf:** **No native REST control plane.** Runtime is CLI + config-file driven; reload via SIGHUP (or `--watch-config`). No start/stop/reload-individual-pipeline API. Fleet management is delegated to **Telegraf Enterprise / Telegraf Controller** (GA June 2026), a separate commercial product. On k8s, `telegraf-operator` provides CRD-based config injection (annotations), not runtime management.

**Verdict:** This is TRAM's **clearest structural advantage.** TRAM treats pipelines as runtime-managed objects; Telegraf treats them as static config. If you need to add/start/stop/rollback pipelines without redeploying, TRAM does this natively and Telegraf does not (without paying for Enterprise).

## 7. Data model

**TRAM:** Every record is a **plain Python `dict`** (`list[dict]`) — generic, arbitrary-schema, JSON-shaped. Not metrics-only, not logs-only. Records are shaped by the transform chain. Special meta keys (`source_path`, `run_id`, etc.) and DLQ envelopes (`{_error, _stage, _pipeline, _run_id, _timestamp, record, raw}`). Inline bytes passthrough via `{_raw, _size}`. This makes TRAM a **mediation engine** that can carry any payload shape.

**Telegraf:** Internal metric = **measurement name + tags (`map[string]string`) + fields (`map[string]interface{}`) + timestamp** — the InfluxDB data model. Canonical wire format is **line protocol**. Types are heterogeneous per field. Tags drive cardinality (with documented performance warnings). This makes Telegraf fundamentally **metric-centric**; logs/traces are second-class.

**Verdict:** Opposite philosophies. TRAM is **schema-agnostic** (good for telecom CDRs, fault events, logs, mixed payloads); Telegraf is **time-series-optimized** (good for PM counters, system metrics, IoT sensor data). For pure metrics → InfluxDB, Telegraf's model is more efficient. For heterogeneous telecom mediation, TRAM's generic dict model is more flexible.

## 8. Deployment

**TRAM:** Three Docker images (standalone / manager / worker), all `python:3.13-slim` multi-stage, non-root. `docker-compose.yml` with manager+worker profile and test SFTP. **Helm chart with StatefulSets** for stable identity + PVC affinity, manager/worker split, per-pipeline NodePort/LoadBalancer Services via `KubernetesServiceManager` (driven by pipeline `kubernetes:` block). Optional Bitnami Postgres subchart. **No CRD/operator** — pipelines are ConfigMap-mounts or API-registered. `TRAM_MODE=standalone|manager|worker`.

**Telegraf:** **Single static binary** — the simplest possible deployment unit. Docker image, rpm/deb packages, Helm charts (`telegraf-ds` as DaemonSet, `telegraf` as Deployment/Sidecar). K8s CRD-based config injection via the separate **`telegraf-operator`** (annotation-driven, not runtime management). No manager/worker split — each agent is autonomous.

**Verdict:** Telegraf wins on **simplicity and footprint** (one binary, no DB, no manager/worker topology). TRAM wins on **K8s-native topology** (StatefulSets, per-pipeline Services for push sources, manager/worker HA-ish split) and on **runtime-managed pipeline lifecycle**. TRAM is heavier to operate; Telegraf is lighter but fleet management is an afterthought (or a paid product).

## 9. Observability & ops

**TRAM:** Prometheus metrics (`tram_records_in_total`, `.._out_total`, `.._skipped_total`, `tram_errors_total`, `tram_dlq_total`, `tram_chunk_duration_seconds`, Kafka consumer lag, stream queue depth, manager-side dispatch/reconcile series). `/api/health` + `/api/ready` (db + scheduler + cluster). Structured JSON logging. OpenTelemetry tracing (`tram/telemetry/tracing.py`, `TRAM_OTEL_ENDPOINT`). **At-least-once-ish** delivery with per-record error isolation (`on_error: continue|abort|retry|dlq`), DLQ with staged envelopes, per-sink retry + circuit breaker, `ProcessedFileTracker` idempotency for file sources. Run history persisted to DB. **No exactly-once, no transactional checkpointing.**

**Telegraf:** `inputs.internal` self-monitoring plugin (memory, timing, gather/flush state). `processors.print` / `outputs.file` for debugging. In-memory metric buffer for backpressure (drops oldest on overflow; disk-buffer opt-in). At-least-once via metric tracking + batch requeue. Graceful shutdown. `--test` / `--once` / `--debug` / `--watch-config` for validation. **No DLQ, no circuit breaker, no OTel tracing in the OSS agent** — resilience is coarser.

**Verdict:** TRAM has a **substantially richer ops story** — DLQ, circuit breakers, per-sink retry, OTel tracing, run history, file-tracking idempotency. Telegraf is **simpler and adequate for metrics** but offers less granular failure handling; lost metrics on backpressure are a known limitation unless disk-buffer is enabled.

## 10. Maturity & scope

**TRAM:** v1.3.3, ~185 commits, ~99 unit test files + 7 integration, ~80% coverage (target 75%). Extensive docs (architecture, connectors, transforms, API, deployment, roadmap). Some doc staleness. Roadmap is UI/UX + cluster/placement parity, with backlog for new connectors (SMTP/gRPC/syslog sinks), RBAC, DLQ viewer, manager HA leader election, Kafka offset edge cases. **Young, focused, actively maturing.**

**Telegraf:** Since 2015, v1.39.2 (Jul 2026), 4 supported minor series, 5B+ downloads, 400+ plugins, documented release/security/FAQ process, commercial Enterprise tier (2026). Battle-tested at planet scale. **Mature, broad, industry-standard.**

**Verdict:** Telegraf is **vastly more mature and broadly adopted**. TRAM is **young but well-structured** for its niche, with a clear roadmap and respectable test/docs discipline for its age.

## Summary: when to pick which

**Pick Telegraf when:**
- You need **metrics collection** at scale (system/app/IoT metrics → InfluxDB/Prometheus).
- You want a **single static binary**, minimal footprint, no DB, no control plane to operate.
- You need **breadth of integrations** (370+ plugins) and proven reliability.
- You're already in the InfluxData/TICK ecosystem.

**Pick TRAM when:**
- You're doing **telecom data mediation** (PM/FM/logs over SNMP/gNMI/CORBA/SFTP/syslog/CDR).
- You need a **runtime control plane** — start/stop/rollback/reload pipelines via API/UI without redeploying.
- Your data is **heterogeneous** (mixed record shapes, not pure metrics) and needs per-sink transform/serializer fan-out.
- You need **K8s-native topology** with manager/worker split and per-pipeline push-source Services.
- You need **granular failure handling** (DLQ, circuit breakers, per-sink retry, file-tracking idempotency).

**Honest caveat:** TRAM's analysis is grounded in its actual source code (verified file paths, plugin counts from grep, cited symbols). Telegraf's analysis is grounded in official InfluxData docs and the live repo (plugin counts from directory enumeration). The two are not direct substitutes — they share a structural shape (plugin pipeline daemon) but solve different problems for different audiences. A shop running InfluxDB for infra metrics would not swap Telegraf for TRAM, and a telecom NMSO doing fault mediation would not find Telegraf's metric model sufficient.

---

## Appendix — Verification evidence

### TRAM (verified in source)
- Registry counts: `tram/connectors/__init__.py`, `tram/connectors/*/sink|source.py` (`@register_*`), `tram/transforms/__init__.py`, `tram/serializers/__init__.py`.
- Execution model: `tram/pipeline/executor.py` (`batch_run`, `stream_run`, `_stream_run_threaded`, `_process_records`), `tram/pipeline/controller.py` (`ThreadPoolExecutor(max_workers=10)`, `_start_stream`, `_add_interval_job`).
- No checkpoint/offset machine: zero matches for checkpoint/exactly-once/transactional in `tram/`; Kafka source `tram/connectors/kafka/source.py` (auto-commit, `consumer.commit()` on close).
- No HA: `tram/agent/*` all in-process `threading.Lock`; no leader election.
- No RBAC: `tram/api/middleware.py` `APIKeyMiddleware` + `tram/api/auth.py` (single key / session token).
- K8s: only `tram/pipeline/k8s_service_manager.py` Services; no CRD/operator.
- Alert cooldown code: `tram/alerts/evaluator.py:74-75` (`_set_cooldown` only on `fired`).
- Doc stale: `docs/architecture.md:104` "Transforms 21" vs registry 27; `README.md:7` badge 3.11 vs `pyproject.toml:10` (>=3.13).

### Telegraf (sourced)
- InfluxData Telegraf docs — `docs.influxdata.com/telegraf/v1/` (plugins, configuration, glossary, configure_plugins, aggregator/processor, input-plugins, processor-plugins, input data formats).
- GitHub `influxdata/telegraf` — README, `docs/METRICS.md`, `CHANGELOG.md`, `config/README.md`, plugin READMEs, releases.
- InfluxData blog — Telegraf: The Go Collection Agent (2018), Telegraf 1.39 Release Notes, Enterprise GA announcement.
- `endoflife.date/telegraf` — release/version table.

---

*Document generated Aug 2026.*
