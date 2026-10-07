# TRAM Rust migration assessment — 2026-10-06

## Recommendation

Migrate the execution plane incrementally if a benchmark pilot establishes a
material capacity or memory advantage. Keep the Python manager, REST management
API, scheduling, persistence, CLI, and existing JavaScript UI initially.

The best initial target is a Rust worker implementing the existing worker HTTP
contract, using the in-house Rust `trishul-snmp` crate directly. Start with
SNMP/local/JSON and a representative transform chain; expand to webhook and Kafka
after delivery and lifecycle compatibility are established. Keep Python workers
for unsupported pipelines. A complete backend rewrite has a much weaker initial
business case: it replaces substantial control-plane functionality that is not
the observed throughput bottleneck.

This is a source and existing-evidence study, not a Rust TRAM implementation or a
new performance experiment. All TRAM Rust speed/resource ranges below are
planning hypotheses. Published-in-repository measurements are identified separately.

## Scope and current implementation

Assessment baseline: TRAM `1.6.0`, checkout `fdcf371`. The source inventory has
191 Python files and approximately 35,700 physical lines, including comments and
blank lines, excluding UI/build artifacts. It is not an estimate of executable
code complexity. Important areas:

| Area | Python files | Physical lines | Migration implication |
|---|---:|---:|---|
| Connectors | 72 | 9,266 | Protocol breadth and SDK compatibility dominate effort |
| Pipeline execution, loading, management, controller | 8 | 6,385 | Separate executor work from lifecycle/control-plane work |
| API | 23 | 6,483 | Limited throughput benefit from rewriting management endpoints |
| Agent | 8 | 2,875 | Existing HTTP boundary supports gradual worker replacement |
| Transforms | 33 | 3,137 | Good CPU optimization target; state and expression parity matter |
| Serializers | 13 | 1,440 | Runtime schemas are harder than fixed-schema serialization |
| Config models | 2 | 1,714 | Numerous validators and defaults beyond basic YAML parsing |
| Persistence | 3 | 1,491 | Preserve DB and durable-state compatibility during coexistence |

Source decorators register **24 sources, 20 sinks, 12 serializers, and 29
transforms**. There are 128 unit-test Python files (~48,500 physical lines),
seven integration-test Python files, and a separate JavaScript browser suite.
These tests are useful behavioral specifications, but Python object mocks will
not transfer directly to Rust.

The data flow is bytes + metadata → serializer → `list[dict]` → global transforms
→ sink conditions/transforms → serialization → writes. Global transforms run
per record to isolate failures. Stream micro-batching is already implemented;
batch/stream thread queues are already bounded. Stateful transforms currently
require sequential execution. Rust must preserve these behaviors before
introducing more concurrency.

## What the existing tsnmp migration proves

The sibling checkout `/home/dhaka/trishul/trishul-snmp-rust` provides a native
Tokio-based library and CLI, with SNMP v1/v2c/v3 polling, notifications, and
compiled-JSON MIB consumption. Its manifest reports `0.1.1`, Rust edition 2024,
and MSRV 1.88. Its benchmark report measures Rust **0.1.0**, not 0.1.1.

TRAM currently pins Python `trishul-snmp[v3]==0.6.2` and `trishul-smi==0.5.3`;
`TRAM_SNMP_STACK` defaults to `legacy`/pysnmp. Even selecting `trishul` invokes
the Python package in this checkout. The Rust crate has a Rust-native API, and
the inspected manifest does not provide a PyO3 extension. Reusing it requires
a native Rust worker, a new binding, or an IPC adapter. It is not a drop-in
package update. Rust and Python package version numbers are separate streams.

Existing sibling-repository results in `docs/benchmarks.md`:

| tsnmp workload | Python | Rust | Observed advantage |
|---|---:|---:|---|
| BER decode, same v2c datagram | 9.171 µs | 0.392 µs | 23.4× codec throughput |
| In-process sequential GET | 185 µs/request | 130 µs/request | 1.43× request throughput |
| In-process mib-2 walk | 0.315 s | 0.176 s | ~1.8× faster completion |
| CLI walk peak RSS | 30.4 MB | 9.8 MB | ~68% lower peak RSS |
| CLI walk user CPU | 0.36 s | 0.03 s | 12× lower user CPU time |
| CLI single GET, including startup | 95.69 ms | 1.39 ms | ~69× faster invocation |

These results support the direction of a migration, not those multipliers for
TRAM. The measurements use WSL2 loopback and net-snmpd; library-walk and resource
figures are single-run measurements. The GET agent floor was ~101.6 µs: speeding
up client code cannot remove it. The dramatic CLI improvement mainly removes
interpreter/import startup, which a long-running TRAM daemon already amortizes.
CLI RSS excludes TRAM's records, transforms, web server, and connector buffers.

The Rust crate consumes tsmi JSON bundles; it does not compile MIBs. Retaining
manager-side compilation and shipping compatible bundles is a natural boundary.
Its SNMP BER codec is also not a general replacement for TRAM's runtime ASN.1
CDR schema compiler and BER/DER/PER/UPER/XER/JER decode surface.

## Current TRAM performance evidence

Use the corrected [v1.6.0 comparison](perf-v160-vs-v151-comparison-2026-10.md),
not the uncorrected original capacity tables. Original H-profile batch figures
were inflated, and some manager/worker measurements were contaminated by other
scheduled pipelines. A controlled rerun found topology parity, not a systematic
2× worker-mode overhead.

| Current measured workload | Relevant evidence | Implication for Rust |
|---|---|---|
| Webhook, 500m CPU per worker | ~428–535 requests/s depending on client concurrency; CPU/throttling limited | Good candidate for native HTTP ingestion and bounded async queues |
| Kafka → local stream, 500m worker | ≥2,000 messages/s sustained; 3,943 matched production in a producer-limited run | Existing batching already gained 2.0–3.9×; true ceiling needs a stronger generator |
| Five-transform JSON chain, M profile | ~24k records/s standalone; ~29k manager/worker | Good CPU pilot with meaningful business-shaped work |
| CSV file batch, H profile, standalone | ~49.2k records/s | Compare actual parse/transform/write costs; storage can become the ceiling |
| Protobuf full pipeline, M profile | ~9.5k records/s; dict conversion remains dominant | Native descriptors/records may help more than replacing the codec alone |
| Window aggregation | 14.9 µs/input record at 500 groups; 18.6 µs at 1k | State representation and per-record dispatch remain candidates |
| REST page-size diagnostic, controlled #86 rerun | ~5.9k records/s at 100 records/page → ~29.2k at 1,000 | Bigger batches produced ~5× without changing language |

v1.6.0 already reduced add-field cost from 30.8 to 3.8 µs/record and several
copy-heavy transforms from 7–11 to 1.0–1.3 µs/record. Do not reuse the old Python
costs to estimate remaining Rust gains. Python also uses native libraries already:
Pydantic v2 validation has a Rust core, and lxml, fastavro, PyArrow, and other
dependencies perform substantial work in compiled code.

The source also exposes opportunities to compare against a rewrite: the Kafka
sink parses serialized output back into records, serializes individual records
to estimate chunk sizes, and then serializes chunks again; the NATS sink creates
an event loop and connects/closes for every write; SNMP trap events become JSON
bytes before entering the serializer. Record-aware interfaces, persistent
connections, and a structured-record ingress path could remove these costs in
Python too. Their actual importance needs profiling; changing those interfaces
must preserve framing, limits and failure semantics.

## Expected benefits and limits

### Performance scenarios, not commitments

For a well-designed native worker at identical CPU quota, workload, output
guarantees, and batching:

| Workload class | Initial planning hypothesis | Confidence before a pilot |
|---|---|---|
| Python-heavy transformation/expression/state CPU slice | 2–5× throughput for the migrated slice | Medium on direction; low on multiplier |
| Complete CPU-bound transform pipeline | 1.5–3× end-to-end throughput | Low; depends on the migrated fraction and record design |
| CPU-bound webhook ingress + execution | 1.5–3× throughput | Low; queues, CPU throttling, and flush latency must be matched |
| Kafka pipeline | 1–3× throughput | Low; existing maximum is producer-limited and broker/disk can dominate |
| RTT/storage/server-bound SFTP, REST, SNMP polling | 1–1.3× with unchanged I/O pattern | Low; improvement can be negligible |
| Native-code-heavy codec or Parquet processing | No defensible multiplier yet | Must measure conversion and allocation costs separately |

Individual pure-Python kernels can exceed these ranges, as tsnmp BER decode
shows. That does not make a 10× full-daemon improvement a defensible estimate.
These are hypotheses for experiment design, not expected savings to budget against.

Amdahl's law explains the difference. If fraction `p` of current wall time is
accelerated by `s`, overall speedup is `1 / ((1 - p) + p / s)`:

| Accelerated fraction | Slice speedup | Whole-workload speedup |
|---:|---:|---:|
| 20% | 5× | 1.19× |
| 60% | 5× | 1.92× |
| 80% | 5× | 2.78× |

Multi-core scaling is an additional opportunity for CPU work on conventional
GIL-enabled CPython, but only with sufficient CPU allocation. A 500m pod cannot
gain extra cores from Rust. Stateful keys, Kafka partitions, ordering, and sink
serialization still constrain parallelism. Python native extensions may already
release the GIL; free-threaded Python is an alternative to evaluate separately.

### Resource efficiency

- A native worker removes the Python interpreter/imported SDK baseline for that
  process and can represent records/state with fewer objects and copies.
- Tokio tasks and a shared runtime can replace many pipeline/listener/timer
  threads. Savings depend on actual thread stacks and active allocations; not
  all reserved stack memory is resident.
- Bounded streaming decode, shared buffers, and field/path compilation can reduce
  peak live data. Rust alone does not help if the port eagerly materializes full
  files, clones every record, or uses unbounded channels.
- Dropping Rust values releases ownership deterministically, but the allocator
  can retain pages. Lower RSS and immediate return of memory to the OS are not
  automatic. Existing Python heap trimming is evidence of a concern, not proof
  that every allocator issue disappears.
- A lean native worker image can be smaller and start faster. TLS, compression,
  librdkafka, SSH, Arrow, and optional native libraries still add runtime/build
  weight. A hybrid worker that embeds Python retains much of its baseline.

A useful **pilot acceptance target**, rather than a forecast, is ≥2× throughput
per CPU on a production-representative CPU-bound pipeline or ≥30% lower peak
worker memory at matched throughput, with no correctness regression. Measure
idle RSS, active working set, peak file-batch memory, high-cardinality state,
allocation retention after repeated runs, and image size independently.

CPU improvement need not equal cluster-cost improvement. If workers account for
70% of total CPU and require half as much CPU after migration, the total CPU
reduction is 35%. Managers, brokers, databases, storage, minimum replicas, and
availability requirements still cost the same. At fixed CPU, a throughput gain
can instead be spent on headroom; savings require actually reducing allocations.

## Architecture options

| Option | Benefit | Cost and limitation | Assessment |
|---|---|---|---|
| Optimize current Python | Fast ROI from connection reuse, record-aware sinks, batching, native clients, targeted profiling | Does not remove object/dispatch overhead throughout the engine | Establish this comparison baseline |
| Batched Rust extension via PyO3 | Retains existing Python plugins/control plane; accelerates a proven kernel | Binding/wheel maintenance, copying, GIL/lifetime boundaries; async tsnmp bridge needs work | Good if profiling identifies a narrow dominant slice |
| Rust worker + Python manager | Native ingestion→transform→write path; direct tsnmp reuse; process isolation and rollback | Two worker implementations, contract/capability compatibility | Recommended strategic route |
| Full Rust backend | Consistent runtime, native deployment, type checking across manager and worker | Broad functional rewrite; limited additional execution benefit | Reconsider after worker results justify it |

Calling the tsnmp CLI once per request is unsuitable for the sustained hot path:
process spawning, JSON transport, credentials, cancellation, and lifecycle would
need management. If an IPC prototype is used, make it long-lived and batched.
For an extension, pass batches/bytes and keep the chain in Rust; crossing the
Python boundary once per field or transform can consume the benefit.

Candidate building blocks are Tokio, Axum, serde, reqwest, a Kafka client such as
rust-rdkafka, CSV/JSON/XML libraries, and Arrow/Parquet. These are starting points,
not a completed dependency audit. Validate supported authentication, compression,
schema behavior, native dependencies, and licenses before selection. Dynamic
Protobuf requires runtime descriptors/reflection, not only generated `prost`
structs; XML XPath and SSH/SFTP semantics need explicit compatibility checks.

## Difficult compatibility requirements

1. **Expressions and dynamic records.** `simpleeval` conditions/add-field
   expressions use Python-like semantics. Preserve missing/null distinctions,
   truthiness, short-circuiting, casts, number behavior, functions, errors, and
   registration-time validation. Python integers can exceed fixed-width Rust
   integers; telecom counters must not overflow silently. A generic JSON value
   tree is a useful compatibility starting point but still allocates heavily.
   Avoid requiring a fixed CDR schema for every user pipeline.
2. **Runtime schemas.** ASN.1 currently compiles user-provided `.asn` files and
   supports multiple encodings/message fallbacks and incremental BER splitting.
   Static Rust ASN.1 derives do not reproduce this. Retain a Python worker for
   these pipelines until a runtime compiler/decoder strategy is proven. Protobuf
   also accepts runtime `.proto` files and has observable framing/key conventions.
3. **Delivery and failure semantics.** Preserve Kafka commit-after-flush,
   partition/chunk ordering, partial-delivery retries and duplicates, per-record
   transform failures, DLQ envelopes, circuit breakers, source-finalize timing,
   staged-file cleanup, counters, and graceful stop behavior. HTTP acknowledgments
   remain bounded-buffer acknowledgments with a crash window; Rust does not make
   webhook delivery durable or exactly-once.
4. **Durable state and restart.** Preserve transform identities/blobs, late-data
   and watermark behavior, counter wrap/reset, placement leases, restart adoption,
   stats callbacks, completion retry, and configuration hashes. Start with
   sequential stateful execution; sharding by key is separate product work.
5. **Config and APIs.** Match environment substitution, YAML defaults, aliases,
   rejected extra fields, validation, routes, payloads, auth, ports, error codes,
   and asset synchronization. Serde parsing alone does not implement Pydantic's
   validation rules. Existing raw-YAML hash behavior must stay compatible.
6. **Plugin extensibility.** Python decorator plugins cannot load directly into
   a native worker. Use a built-in Rust registry initially and Python fallback.
   Future external-process/WASM plugins require a defined API; Rust dynamic-library
   ABI compatibility should not be assumed.

The worker scheduler must use capabilities, not just load, when both runtimes
coexist. Match connector/serializer/transform combinations and relevant options;
do not dispatch unsupported YAML and discover failure after starting the run.
Preserve SNMP stack reporting or introduce an explicit, compatible runtime field.
Until capability routing exists, isolate cohorts through explicit placement.

## Effort estimate

These are engineering estimates with roughly ±50% uncertainty, assuming engineers
experienced in Rust, TRAM's telecom semantics, and production operations. They
include implementation, representative compatibility tests, build/deployment
changes, and documentation. One person-week is five engineering days. Calendar
time assumes dedicated effort; on-call/support and learning extend it.

| Scope | Total effort | Plausible elapsed time | Deliverable |
|---|---:|---|---|
| Profile/baseline + decision pilot | 4–8 person-weeks | 4–8 weeks, one engineer | Narrow native path, same-workload measurements, go/no-go decision |
| Selected batched extension production rollout | 8–16 person-weeks | 2–4 months, one engineer | A few proven kernels plus bindings/packaging; not a native worker |
| Production Rust worker for a selected pipeline cohort | 20–40 person-weeks | 3–6 months, two engineers | Agent/lifecycle, essential connectors/formats/transforms, capability routing, canary/rollback |
| Full worker feature parity | 50–90 person-weeks | 7–12 months, two engineers | Broad connector/format/transform coverage; runtime-schema risk dominates |
| Complete backend parity, retaining JS UI | 90–160 person-weeks | 12–20 months, two engineers | Worker plus manager/API/CLI/scheduling/persistence parity |

Rows are alternative total scopes, not costs to add together. The pilot can feed
the worker implementation. Estimates are not derived by translating Python lines
one-for-one. Native Rust APIs can require more code: the sibling tsnmp report
lists ~18.8k Rust library lines versus ~9.2k Python reference lines.

A selected-cohort worker budget of 20–40 person-weeks can be allocated roughly as:
contracts/config and agent lifecycle 4–7; execution, transformations, state and
error handling 5–9; selected connectors and serializers 5–10; differential/failure
testing, observability, packaging and rollout 6–14. These boundaries overlap and
must be revised after the pilot. Runtime ASN.1 and CORBA parity can push the full
rewrite beyond the range; leaving those on Python is a deliberate scope reduction.

## Pros and cons

| Pros | Cons/tradeoffs |
|---|---|
| Lower CPU overhead in interpreted paths; efficient concurrency | Little latency gain when remote systems or disk dominate |
| Potentially smaller worker memory/image baseline | Dynamic value trees, clones and native SDKs can retain high memory |
| Direct reuse of the in-house Rust tsnmp library | Its Rust API still needs a TRAM adapter and record/MIB parity tests |
| Compiler-checked ownership, types and Send/Sync boundaries | Rust learning, longer compile times, async/lifetime complexity |
| Safe Rust reduces memory and data-race bugs | Python is already memory-safe at the language level; Rust does not prevent semantic loss, deadlocks, panic or resource exhaustion; unsafe/FFI dependencies need review |
| Native binaries and fewer Python runtime dependencies in workers | New target/libc/OpenSSL/native-client build and supply-chain work |
| Easier multi-core CPU execution with appropriate partitioning | Stateful/order-sensitive processing cannot be parallelized blindly |
| Stable compiled execution engine | Slower experimentation for dynamic plugins and expressions; two-language maintenance during transition |

## Proposed decision and migration gates

1. **Baseline and profile.** Re-run a clean v1.6.0 benchmark on deployment Python
   3.13+, recording exact revisions, CPU limits/throttling, memory, dependency
   versions, batches, sink durability, and request concurrency. Existing host
   microbenchmarks use Python 3.12 and should not be the sole production baseline.
   Establish time in decode, transforms, allocation/copy, metrics and I/O.
2. **Pilot in-process Rust execution.** Build JSON/local + representative t5
   transforms, then integrate tsnmp polling/traps with unchanged records/MIB
   output. The pilot covers only the chosen expression subset and advertises that
   limit. Benchmark existing Python, optimized Python, and Rust where practical.
3. **Verify parity before expansion.** Use common input/output golden corpora and
   black-box tests against both implementations. Include malformed data, nesting,
   null/missing fields, large integers, multi-sink mutation isolation, retries,
   partial delivery, restart, stop, state hydration, and counters. Compare typed
   values and exact bytes where the wire contract requires them.
4. **Measure sustained capacity.** Reuse the corrected perf scenarios: JSON/CSV,
   t5, window cardinality, webhook, Kafka, and SNMP. Include v3 crypto/trap bursts,
   real RTT/device behavior, slow sinks, flush-interval latency, and large files.
   Count durable/accepted output, loss, duplicates, and Kafka lag rather than
   intake alone. Use at least five repetitions, saturation-capable independent
   load generation, confidence/spread reporting, and a 24–72-hour soak.
5. **Adopt only for meaningful benefit.** Require functional parity for the
   selected cohort and a useful ≥2× CPU-capacity or ≥30% peak-memory result at
   matched service guarantees. A smaller gain can still be justified by specific
   edge-device constraints, but should have an explicit cost case.
6. **Canary and preserve rollback.** Keep existing worker callbacks and UI/API
   contracts. Use capability-aware cohorts and one runtime owner per pipeline.
   Shadow only to isolated sinks; never duplicate destructive source-finalize
   actions or competing production consumer groups. Confirm Python can rehydrate
   state and resume before widening rollout. Apply existing release gates plus
   Rust lint/test and mixed-runtime compatibility checks.

Annual net infrastructure savings should be compared to migration cost plus
incremental maintenance. For example, a $100k migration saving $2k/month has a
50-month simple payback before maintenance; $10k/month savings gives 10 months.
These are illustrative arithmetic, not TRAM cost estimates. Actual fleet size,
pipeline mix, CPU/RAM prices, required throughput/latency and staffing rates are
needed to produce a financial business case.

## Evidence references

- [Architecture and execution semantics](../architecture.md)
- [Corrected v1.6.0 performance comparison](perf-v160-vs-v151-comparison-2026-10.md)
- [Capacity study and corrections](perf-capacity-analysis-2026-10.md)
- [Controlled #86 rerun](../../scripts/perf/results/ab-86-2026-10-01/RESULTS.md)
- [Pipeline executor](../../tram/pipeline/executor.py)
- [Worker agent](../../tram/agent/server.py)
- [Config models](../../tram/models/pipeline.py)
- [Stateful transform protocol](../../tram/transforms/stateful.py)
- [Runtime ASN.1 serializer](../../tram/serializers/asn1_serializer.py)
- [Runtime Protobuf serializer](../../tram/serializers/protobuf_serializer.py)
- [SNMP source](../../tram/connectors/snmp/source.py) and [dependency pins](../../pyproject.toml)
- Sibling Rust tsnmp: `/home/dhaka/trishul/trishul-snmp-rust/Cargo.toml`,
  `src/lib.rs`, `README.md`, and `docs/benchmarks.md` (read locally; measurements
  reported there were not independently rerun for this assessment).
