# TRAM — Telecom Mediation Platform Domain Review

> **Status (2026-09-23): most sections fixed v1.4.0** — file-done semantics
> (`file_stability_seconds`/`file_min_age_seconds`/`file_done_suffix`), `counter_delta` /
> `window_aggregate` stateful transforms, gNMI subscription modes + reconnect, CORBA dedupe
> window, and `source_timezone` shipped in v1.4.0 (see `docs/changelog.md` `[1.4.0]`). This
> review is the historical record.

**Date:** 2026-09-15
**Reviewer role:** Senior telecom design engineer (PM/FM collection, OSS buyer's lens)
**Scope reviewed:** `tram/connectors/{snmp,gnmi,sftp,syslog,corba,kafka}`, `tram/pipeline/{executor,controller,manager,k8s_service_manager}`, `tram/models/pipeline.py`, `tram/transforms/{aggregate,timestamp_normalize}`, `tram/serializers/pm_xml_serializer.py`, `tram/persistence/file_tracker.py`, `helm/templates/`, `docs/roadmap.md`, README.

Findings are ranked by impact on a telecom operator (highest first). Every finding is grounded in file:line evidence read from the repo.

---

## 1. PM file collection has no "file-done" semantics — partial-file reads

**[DOMAIN GAP] — highest impact.** The SFTP source lists the remote directory and immediately reads everything matching the glob: `tram/connectors/sftp/source.py:59-66` and `:91-115`. There is no:
- mtime age / size-stability check (the standard "is the NE still writing this file?" guard)
- `.done`/`.fin`/`.tmp` suffix convention support
- skip for files below a minimum age

Telecom PM dumps (15-min/30-min/1h granularity per 3GPP TS 32.411/32.435) arrive as growing files or temp names. TRAM will read a half-written file mid-transfer. The codebase itself proves this bites in production: the PM-XML serializer contains an auto-close hack that appends missing `</measValue>`/`</measInfo>`/`</measData>` tags to truncated files (`tram/serializers/pm_xml_serializer.py:52-61`). That is a symptom patch — the correct fix is file-completion detection at the source. Partial CSV/JSON files have no such rescue.

**[DOMAIN RISK]** Whole-file read into memory is the default (`sftp/source.py:110`, `read_chunk_bytes: 0` in `models/pipeline.py:46`). Multi-MB/hundreds-of-MB PM XML from large RANs will stress worker memory; `read_chunk_bytes>0` chunks *raw bytes*, which splits records mid-line unless the serializer supports `parse_chunks` (only the ASN.1/CSV paths do per roadmap v1.3.2, `docs/roadmap.md:105`).

## 2. No counter delta/rate computation — polled KPIs are unusable as-is

**[DOMAIN GAP]** `snmp_poll` correctly fetches raw Counter32/Counter64 values (`tram/connectors/snmp/source.py:418-454` even classifies them into `_metrics`), and Python's arbitrary-precision ints handle Counter64 values fine. But nowhere in the 27 transforms (`README.md:313`) is there a `rate`/`delta` transform that computes (v_now − v_prev) with 32-bit wrap correction (2³²) and 64-bit wrap handling. Mediation 101: cumulative counters → per-interval deltas. Every PM platform (NetAct, ENM export, older OSS) does this before a KPI is meaningful. TRAM delivers raw cumulative values and forces users to compute deltas downstream — meaning the platform cannot compute a KPI like "erlangs per cell per 15-min" from SNMP input at all. Same story for the `aggregate` transform: it is batch-local only (`tram/transforms/aggregate.py:69-106`), with no time-windowed aggregation (window, watermark, late-arrival handling). Aggregating across a 15-min PM period spanning multiple polls or multiple late files is not expressible.

**[WELL-DESIGNED]** SNMP poll table handling itself: index-splitting with auto/specified `index_depth` (`source.py:252-293`), symbolic OID resolution with clear failure warnings (`source.py:395-415`), `_polled_at` stamping, and the `_metrics`/`_labels` type classifier (`source.py:424-454`) show real understanding of SNMP table semantics.

## 3. CORBA source is a DII toy, and its idempotency key breaks scheduled PM collection

**[DOMAIN RISK]** The connector does a single synchronous Dynamic Invocation with scalar-only args (`tram/connectors/corba/source.py:122-133`), yet the docstring claims coverage of "3GPP Itf-N, TMN X.700, Ericsson ENM, Nokia NetAct, Huawei iManager" (`source.py:4-6`). Real 3GPP IRP integration requires the CORBA Notification Service push-consumer model (structured events, `StructuredPushSupplier` subscriptions, QoS props) — none of which exists here — or file-based IRP over FTP/SFTP. IRP operations take complex typed arguments (structs, sequences); `_add_in_arg() <<= scalar` cannot express those. Read as: **CORBA PM/FM collection against a live NMS is not achievable with this connector.**

Worse, `skip_processed: true` on a scheduled CORBA pipeline is a functional bug: the invocation key is `operation + args` (`corba/source.py:158-161`). The README's own example (`README.md:170-188`) passes constant `args: {granularity: 15min}` on a 900-second interval — the first run marks that (pipeline, endpoint, operation+args) tuple processed, and **every subsequent scheduled run is silently skipped** (`corba/source.py:163-169`). There is no time-window component in the key.

## 4. gNMI: no once/poll modes, no reconnect — telemetry pipelines silently die

**[DOMAIN RISK]** Per-path `SAMPLE`/`ON_CHANGE`/`TARGET_DEFINED` with ns `sampleInterval` is supported (`tram/connectors/gnmi/source.py:57-68` — good), but the top-level subscription mode is hard-coded to `"STREAM"` (`source.py:66`); gNMI `once` (initial-state dump) and `poll` modes are absent, and there's no heartbeat/suppression config.

More serious: there is **no reconnect loop**. When the gNMI session terminates, the `subscribe_stream` iterator in `read()` simply ends (`gnmi/source.py:71-92`), `stream_run` returns, and the controller sets the pipeline to `stopped` — not `error` — with no restart (`tram/pipeline/controller.py:836-840`). A router reload or transient TCP break silently kills a telemetry pipeline until a human notices. Compare: Kafka and NATS sources have first-class reconnect options (`models/pipeline.py:101-102, 212-213`); gNMI has none. Only username/password auth, no client certificates (`gnmi/source.py:48-55`).

**[DOMAIN RISK]** Also, the README's flagship gNMI example (`README.md:106-110`) configures `serializer_in: type: protobuf` with a `Notification` schema — but pygnmi's `subscribe_stream` yields decoded Python dicts, so that example can't work as written; a sign the gNMI path wasn't validated against real devices (roadmap backlog `docs/roadmap.md:132` confirms: "gNMI source — subscription modes, path encoding, TLS" still open).

## 5. Delivery-guarantee coherence: good skeleton, leaky edges

**[WELL-DESIGNED]** The core at-least-once story is genuinely coherent for files: source yields → sinks write with per-sink retry + exponential backoff + circuit breaker (`tram/pipeline/executor.py:456-541`); failures fall to DLQ; `mark_processed` fires only *after* the executor has finished processing the yielded chunk in the single-threaded path (generator resumption ordering, `sftp/source.py:115-124`); tracker failures fail open (`tram/persistence/file_tracker.py:42-48` → re-read, i.e., duplicate not loss). A worker crash mid-file leaves the file unmarked → re-collected. This is the right shape for PM file SLAs.

> **Reconciliation note (added after initial review):** the "safe mark ordering" above holds **only in the single-threaded execution path**. With `thread_workers > 1`, the executor submits chunks to a thread pool and advances the source generator immediately (`executor.py:856-867`), so `_post_read`/`mark_processed` run after the last chunk is *submitted*, not after its futures complete — a crash in that window is permanent data loss. See finding A2 and the "Reconciled contradiction" section in `docs/reviews/code-review.md`.

Where it breaks:

- **[DOMAIN RISK]** DLQ writes are fire-and-forget: `_write_dlq_envelope` logs and swallows DLQ sink errors (`executor.py:162-168`). If the DLQ sink is the same Kafka/OpenSearch that's down, records are lost with only a log line — there's no DLQ retry, no spooling, no DLQ viewer/replay (backlog: `docs/roadmap.md:143`). Telecom SLA accounting will not accept "DLQ also failed."
- **[DOMAIN RISK]** Kafka source defaults `enable_auto_commit: true` (`models/pipeline.py:93`). With `thread_workers > 1` (stream mode puts chunks on a queue and polls ahead, `executor.py:1006-1081`), offsets can be committed for records not yet written to sinks. Crash = loss. At-least-once is not actually guaranteed on the Kafka ingress path unless the operator knows to flip this default.
- **[DOMAIN RISK]** In-flight work during update/rollback: `update()` stops the *scheduler job* but does not abort a running batch (`controller.py:214-239` → `_stop_execution` at `controller.py:873-885` only removes the APScheduler job for non-stream pipelines). The old-config run continues while the new config is scheduled; since status is now `scheduled` (not `running`), the guard in `_run_batch` (`controller.py:454-458`) doesn't prevent a new run from starting concurrently with the old one — two runs reading the same SFTP directory simultaneously can double-deliver files not yet marked processed. Acceptable-ish (still at-least-once) but a NOC will see duplicate records after every mid-run hot-reload.
- **[WELL-DESIGNED]** Worker-crash handling for batch runs is thought through: lease tracking, `mark_active_batch_run_lost` synthesizing a FAILED run (`controller.py:594-630`), plus v1.3.2 batch reconciliation (`docs/roadmap.md:104`). Interval pipelines self-heal on the next tick, and missed PM files remain on the NE for re-collection — the file-tracker design makes the outage window recoverable in a way pure-stream platforms aren't.

## 6. SNMP trap & syslog ingestion: transport-level only, some correctness holes

- **[DOMAIN RISK]** SNMPv3 traps: decoding is BER-decode of a v2c `Message()` (`snmp/source.py:179-192`) — an SNMPv3 trap with privacy can't be decrypted by this path (model comment admits "best-effort", `models/pipeline.py:152`). Also, the community string of v1/v2c inbound traps is **never verified** (`source.py:137-168` — it's only echoed into meta), so any host can inject traps. For an FM feed entering a ticketing sink (`README.md:79-84`), that's a spoofing vector. MIB view is also rebuilt per-packet (`source.py:148`) — avoidable CPU burn at trap volume.
- **[DOMAIN RISK]** Syslog over TCP does exactly one `recv()` per accepted connection then closes (`tram/connectors/syslog/source.py:199-200`): messages longer than one buffer, or senders pipelining multiple messages per connection, are truncated or lost. No RFC 6587 framing, no TLS. UDP path is fine, but NEs configured for TCP syslog (common for reliable FM delivery) will lose data.
- **[WELL-DESIGNED]** The RFC 3164/5424 parser with PRI→facility/severity expansion (`syslog/source.py:18-79`) is clean and correct, and per-pipeline K8s NodePort Services for UDP push sources with `count: all` broadcast (`models/pipeline.py:1336-1356`, `k8s_service_manager.py:71-85`) is a genuinely operable answer to "how do 500 NEs send traps to my collector fleet."

## 7. Mediation transform layer: breadth is real, depth is missing

- **[WELL-DESIGNED]** The 27-transform catalog covers more record-shaping ground than most first-generation mediators: `json_flatten` with zip-groups/choice-unwrap (ASN.1-flavored), `hex_decode` with bit-level mapping overrides, `project` with `source_any` fallbacks, `coalesce_fields`, `enrich` from file joins, `inject_meta`, dotted-path support added specifically for CDR shaping (`docs/roadmap.md:103`). The per-sink condition + per-sink transform + per-sink serializer fan-out (`executor.py:377-544`) is exactly how mediation routing should work.
- **[DOMAIN GAP]** `timestamp_normalize` has **no source-timezone parameter**: naive timestamps are assumed UTC (`tram/transforms/timestamp_normalize.py:44-47, 63-65`). NEs routinely emit local-time PM timestamps (e.g., `endTime` in NE-local time per vendor); labeling them UTC shifts every KPI by the tz offset, and DST transitions shift it seasonally. There's no `source_timezone: "Europe/Berlin"` option. For a tool whose whole pitch is normalization, this is the single most surprising omission.
- **[DOMAIN GAP]** No hot-loadable custom logic (no Starlark/embedded-script/exec transform — grep for starlark/execd/custom returns nothing). Vendor-quirk normalization beyond rename/value_map/regex_extract requires shipping a new Python plugin and restarting. Telecom mediation lives on per-vendor quirk tables; a NOC can't patch "Ericsson field X is off-by-one DST week" without an engineering cycle.
- **[DOMAIN RISK]** Unit handling (counters vs. ratios, unit conversion, KPI formula definitions) doesn't exist above raw arithmetic in `template` conditions — every KPI must be recomposed per pipeline YAML with no library or reuse.

## 8. Scheduling: adequate for PM windows, with caveats

- **[WELL-DESIGNED]** interval/cron/stream/manual with `max_instances=1` (`controller.py:417`), next-run realignment from `last_run` to avoid thundering post-outage catch-up (`controller.py:403-421`), on-demand `trigger_run` that works even while stopped (`controller.py:310-319`), and version rollback (`manager.py:222-237`). `*/15` cron aligns fine with PM windows, and because collection is file-based + `skip_processed`, a 2-hour manager outage self-heals on the next run (late files still on the NE get picked up). That synergy is the platform's best domain property.
- **[DOMAIN RISK]** APScheduler runs UTC-only (`controller.py:110`); cron expressions evaluate in UTC with no per-pipeline timezone. NOC operators write "collect at 5 past each hour local" — they'll get it wrong across DST. Also `misfire_grace_time=60` is hard-coded (`controller.py:419,438`): a busy manager that delays a job >60s drops the whole PM window tick (recoverable only because files persist). No missed-window backfill for poll-based (SNMP) pipelines — polls lost in an outage are gone forever, leaving KPI gaps.

## 9. K8s ops story: solid mid-tier, not NOC-grade yet

- **[WELL-DESIGNED]** Clean manager(1-replica StatefulSet)/worker(stateless StatefulSet, agent `:8766` + ingress `:8767`) split with load-aware dispatch, placement reconciliation with re-dispatch on stale slots, DB-backed placement restore on manager restart (`controller.py:901-976`, roadmap v1.3.0 section), per-pipeline NodePort Services with RBAC role (`helm/templates/role-k8s-services.yaml`), versioning + one-command rollback, Prometheus metrics + OTel. This is a real, coherent control plane.
- **[DOMAIN RISK]** Manager is a single-replica SPOF (`helm/templates/manager-statefulset.yaml:13`); manager HA is explicitly backlog (`docs/roadmap.md:164`). During a manager outage streams keep running on workers, but no scheduling or reconciliation occurs — acceptable degradation, but NOC procedures need to know it.
- **[DOMAIN GAP]** Observability gaps that matter at 3 a.m.: `/metrics` is process-local and workers must be scraped separately (acknowledged at `docs/roadmap.md:100`); DLQ viewer/replay, per-sink record counts, audit log, and role-scoped auth (viewer/operator/admin) are all backlog (`docs/roadmap.md:143-154`). A single admin API key is not a telecom operations model — SOX/audit expectations in most operators will fail on this alone. Alert rules evaluate only at run completion (`manager.py:143-155`); no continuous/standalone evaluation of FM-related alert conditions.

## 10. FM domain modeling: transport only

**[DOMAIN GAP]** There is no alarm lifecycle model anywhere: no raise/clear correlation, no dedup/flapping suppression, no alarm enrichment against inventory, no clearing semantics (syslog/SNMP trap → forward). `value_map` severity mapping (README:72-74) is the extent of it. That's fine if TRAM is positioned purely as a collector feeding an alarm-processing layer, but the README's ticketing example implies more. A buyer should budget for a separate alarm-correlation engine downstream.

## 11. Northbound formats

- **[WELL-DESIGNED]** A 3GPP PM-XML serializer with proper `measInfo/measType p=/measValue/r p=` index mapping and `measObjLdn`/`granPeriod` extraction (`pm_xml_serializer.py:87-145`) — this is the correct TS 32.435 shape — plus an ONAP VES sink (`models/pipeline.py:806-824`) which will make ONAP shops happy.
- **[DOMAIN RISK]** The SNMP trap *sink* (northbound FM) varbind types are `Integer32, OctetString, Counter32, Gauge32, TimeTicks` — **no Counter64** (`models/pipeline.py:849`). PM counters forwarded as traps can't use 64-bit values.
- **[DOMAIN GAP]** No 3GPP JSON PM output (TS 28.550), no Kafka northbound with schema-registry-enforced standards payload beyond generic Avro.

## 12. CDR throughput and concurrency

**[DOMAIN RISK]** The engine is a Python ThreadPoolExecutor (`executor.py:856-886, 1006-1081`) with per-record transform loops and a GIL. ASN.1 BER `split_records` with `record_chunk_size` bounded-memory decoding (`models/pipeline.py:156-172`, roadmap v1.3.2) is a serious and correct design for large CDR files — better than most hobby collectors. But there are no benchmarks in the repo, and sustained 10k+ CDR/sec (a modest mediation node) through pure-Python per-record processing is unproven. A buyer must PoC this with real Ericsson/Nokia CDR files before committing. File-based CDR mediation (batch, overnight) is comfortably in range; real-time CDR streaming is a risk.

---

## Verdict

**Usable today? Yes, as a PM/FM *collection and forwarding* layer — with conditions.** TRAM is a well-engineered generic pipeline daemon with above-average domain instincts (file tracker semantics, per-sink routing, PM-XML, broadcast UDP collection, placement reconciliation). It is **not** yet a full mediation platform: it moves records but can't compute the things telecom actually mediates *into* (rates, windows, KPIs), and several connectors (CORBA, gNMI) are thinner than their marketing. An operator could deploy it today for SFTP PM-file collection → Kafka/OpenSearch with `skip_processed`, and for syslog/SNMP-trap fan-out at moderate scale. They could not deploy it for CORBA IRP collection, SNMP-counter-based KPIs, timezone-sensitive PM normalization, or audited NOC operations without accepting the gaps below.

## Top 5 domain-level shortcomings (ranked)

1. **No counter delta/rate or time-windowed aggregation** — cumulative SNMP counters and multi-poll 15-min KPI windows cannot be computed in-platform (`transforms/aggregate.py`, transform registry). This is the defining feature of PM mediation.
2. **No file-done semantics on PM file collection** — partial-file reads with a symptom-level truncation hack (`sftp/source.py:59-115`, `pm_xml_serializer.py:52-61`), plus default whole-file-in-memory reads.
3. **No source-timezone/DST handling in timestamp normalization** — naive local NE timestamps silently mislabeled UTC (`timestamp_normalize.py:44-65`), corrupting every downstream time-bucketed KPI.
4. **FM/collector reliability edges** — gNMI pipelines silently stop on disconnect (`gnmi/source.py:71-92` + `controller.py:836-840`), syslog-TCP truncation (`syslog/source.py:199`), unverified SNMP trap communities, DLQ write failures swallowed (`executor.py:162-168`), Kafka auto-commit default undermining at-least-once (`models/pipeline.py:93`).
5. **Connector depth + NOC operations maturity** — CORBA is nonviable against real IRP (and its skip-processed key bricks scheduled PM collection, `corba/source.py:158-169`); no hot-loadable vendor-quirk logic; single-admin API key with no RBAC/audit; single-replica manager; no DLQ viewer/replay. All acknowledged in `docs/roadmap.md:123-167` — the team knows; buyers should weight the roadmap's honesty accordingly.
