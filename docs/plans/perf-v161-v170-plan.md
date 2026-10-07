# Performance scope: v1.6.1 and proposed v1.7.0

Decision date: 2026-10-07. The user locked the three targeted optimizations
for v1.6.1. The HTTP and Protobuf work below is a recommendation for v1.7.0,
pending pilot results and scope selection. This document does not represent
an implemented change or a release approval.

Evidence and reproducer: [follow-up assessment](../ideas/perf-followup-assessment-2026-10-07.md).

## Locked v1.6.1 scope

1. ISO-first timestamp handling and default ISO formatting, with numeric-epoch
   and explicit-format precedence retained ahead of the ISO fast path, and
   timezone/epoch/error semantics and byte-identical output strings preserved.
2. Compile-once sink conditions with thread-local evaluator reuse and current
   routing-error behavior.
3. Kafka sink fast path for trusted executor output counts, positive counts
   within the record cap, actual serialized bytes within the byte cap, and
   no key extraction requirement. Keep the existing path for all other cases.

Include focused compatibility checks and deployment-Python end-to-end
benchmarks. A target of at least 20% improvement applies to affected pipelines,
not every workload. HTTP tuning, JSON codec replacement, connector connection
reuse and Protobuf representation changes are outside this locked scope.

## Proposed HTTP work: optimize the current ingress first

Start with FastAPI/Uvicorn and the existing webhook API. Add an explicitly
selectable accelerated runtime using `uvloop` and `httptools` on supported
deployment platforms, retain the existing runtime as a rollback option, and
report the actual selected event loop/HTTP parser at startup. Avoid treating
an installed dependency as proof that it is active.

The first experiment compares runtime implementations at identical CPU limits,
payloads, sink configuration, offered rates and client concurrency. Test 500m,
1 CPU and 2 CPU separately; extra CPU is a sizing option, not an efficiency
gain. Capture CPU seconds per completed record, throttled time/periods,
event-loop lag, queue occupancy, HTTP latency and sink completion latency.

Then measure bounded admission/concurrency options. The observed collapse at
high concurrency suggests keeping fewer requests in flight can protect useful
latency. Queue-full rejection already exists (HTTP 503 when the in-memory
queue is full); if admission/concurrency rejection before enqueue is
introduced, make it explicit and retryable, and count admission rejections
separately from the existing queue-full 503s. The existing HTTP 202 response
means accepted into the in-memory queue; preserve that contract. Larger
queues do not solve CPU starvation and can extend the loss window on process
failure.

If event producers support it, benchmark multiple JSON records per POST.
The serializer already accepts JSON arrays. Confirm payload-size, record-count,
and queued-byte bounds and compare records/s as well as requests/s; a batch
must not hide a much larger memory or completion-latency budget.

Compatibility checks cover worker and standalone deployments, source secrets
and API keys, registration/readiness, body limits including chunked bodies,
queue-full behavior, TLS, disconnects, stop/restart, and written-record
accounting under overload. Preserve the release's supported Python/platform
matrix or document an optional acceleration profile.

**Pilot acceptance target:** at least 25% more sustainable completed records/s
than v1.6.1 at the same CPU allocation and agreed latency limits. The locked
v1.6.1 scope contains no HTTP-path changes, so the v1.6.1 HTTP baseline is
expected to equal v1.6.0's. Use an initial
HTTP p95 target of 50 ms for the canonical local benchmark and report sink
completion latency separately against its flush-interval budget. This is an
experiment target, not a production latency promise. Repeat both concurrency
shapes and require no correctness regression.

Only consider process-separated ingress if profiles still show substantial
contention between HTTP handling and execution. That design requires bounded
IPC, queue/placement ownership, backpressure and recovery semantics. Increasing
generic Uvicorn process workers would split the existing in-process webhook
registry; it cannot be the scaling design without resolving that ownership,
and the current code cannot use them as-is either (`uvicorn.run` with an app
object fails when `workers>1`; Uvicorn requires an import string for
multiple workers).
No HTTP/2 or web-framework replacement is justified by the current evidence.

## Proposed Protobuf work: explicit fast paths before a general record API

Current interfaces require `BaseSerializer.parse -> list[dict]` and
`serialize(list[dict]) -> bytes`; transforms and conditional routes depend
on those dictionaries. Replacing them all with message objects would expand
scope into plugin compatibility, routing, stateful transforms and sink APIs.

### First pilot: validated same-schema passthrough

Introduce an opt-in execution capability for pipelines that only transport
Protobuf records. Eligibility requires matching schema content and message
class, matching framing/registry requirements, and no global or sink transforms,
conditions, record-field-dependent filenames, keys, or other field-dependent
behavior. Initially limit sinks to ones that transport the payload without
parsing it internally; ordinary Kafka sink chunking is not automatically
eligible. Count-dependent limits/rate control and DLQ/error behavior must also
be supported explicitly or cause ineligibility.

Validate every frame and decode messages enough to detect malformed Protobuf
and obtain accurate counts. Preserve original record bytes where eligible;
avoiding dictionaries does not mean forwarding arbitrary unvalidated data.
Carry counts through the same metrics, retries and completion accounting.
Validate registry identifiers/schemas and preserve configured framing rather
than assuming equal URLs make the contracts identical.

Keep the optimization disabled by default until proven. Unsupported eligibility
must be reported clearly; automatic mode, if introduced, falls back to the
existing path. Describe byte preservation explicitly: unknown-field retention
and encoding details may differ from today's dictionary round trip even when
known-field values match.

**Pilot acceptance target:** at least 2× end-to-end throughput at equal CPU
on an eligible canonical Protobuf pipeline, relative to a fresh v1.6.1 run.
The v1.6.0 format-sweep reference is 9,527 records/s at M (0.92× the v1.5.1
cell in the same matrix), so roughly 19k/s is a useful test goal anchored to
that v1.6.0 number, not a predicted outcome; recompute it as 2× whatever the
fresh v1.6.1 baseline yields. Measure RSS and
transformed-pipeline fallback overhead too.

**Measured fresh v1.6.1 anchor (2026-10-07, `scripts/perf/results/v170-protobuf-anchor/`):**
median **11,111 records/s** over 5 reps (100k in / 100k out each, 0 errors;
kind, mw topology, 500m workers, image `local-20261007072806` = v1.6.1 @
`84117f0`, `fsweep_protobuf` template, 10×10k corpus). The v1.6.0 dip did
not reproduce — the anchor is 1.07× v1.5.1's 10,363 and 1.17× v1.6.0's 9,527 —
confirming the plan's requirement to re-measure rather than assume. The
recomputed pilot test goal is **≈22.2k records/s** (2× the anchor).

### Second pilot: descriptor-aware conversion for transformed workloads

If Protobuf pipelines commonly transform records, benchmark a conversion plan
compiled once from descriptors while retaining the public dictionary contract.
Start with scalar fields in known schemas; retain the established conversion
for unsupported message shapes. Match current field names and JSON-mapping
semantics, including enum names, 64-bit integer strings, bytes encoding,
presence/defaults, null/error handling and unknown fields. Check actual
runtime reflection costs before assuming this beats the native library's
established JSON conversion.

This gives existing transforms compatible records. It is a separate experiment
from passthrough, and its gains must be measured independently. Fixtures should
include nested/repeated fields, maps, oneofs, optional/default fields,
well-known types, enum edge cases and registry framing, even where the first
fast path falls back.

A general native-message representation with lazy dictionary materialization
is a later design if these pilots justify it. It would need explicit ownership,
mutation and fan-out isolation rules, capability declarations for plugins,
materialization at compatibility boundaries, and measurements of each boundary.
It should not be made a default v1.7.0 plugin contract on the present evidence.

## Scope selection

Establish the final v1.6.1 baseline first. Run the HTTP runtime and validated
Protobuf passthrough pilots independently; retain only improvements that pass
the performance targets and compatibility checks. Record misses and causes.
Choose descriptor-aware conversion only when transformed Protobuf workloads
and profiles support that investment. Follow the repository's normal review
and mandatory release gate before tagging either release.
