# TRAM Performance-Benchmark Harness (`scripts/perf/`)

Phase 1a of the TRAM capacity study. This harness drives the Phase 2 cluster
runs. It is a *local-first* toolchain: every generator runs on the bench host
with the repo venv (`.venv/bin/python` — **never** bare `python`/`pytest`),
talks to the TRAM cluster over its NodePort services, and writes per-run
metrics under `results/<run-id>/`.

Workspace map (other lanes own `scripts/perf/infra/` and
`scripts/perf/microbench/` — do not touch):

```
scripts/perf/
├── README.md                        ← this runbook
├── run_one.sh                       one-run orchestrator (register→loadgen→stop→collect→cleanup)
├── generators/
│   ├── gen_corpus.py                canonical CDR corpus (deterministic per seed+n)
│   ├── loadgen_webhook.py           asyncio HTTP POST flood (httpx)
│   ├── mock_rest_server.py          dual-purpose REST mock (GET /records pages, POST /collect)
│   ├── file_gen.py                  batch file materializer (jsonl/csv/xml/pm_xml)
│   ├── snmp_responder_scaled.py     scaled synthetic SNMP table responder (pysnmp)
│   └── kafka_loadgen.py             Kafka produce/consume loadgen (kafka-python)
├── templates/
│   ├── validate_templates.py        schema checker (all templates must pass)
│   ├── s1_webhook_local.yaml … s7_local_kafka.yaml    scenarios
│   ├── fsweep_{csv,json,ndjson,xml,avro,protobuf,msgpack,parquet,pm_xml}.yaml
│   └── t1_project_filter.yaml … t5_chain.yaml         transform benches
└── collector/
    └── collect.py                   kubectl top sampling + run-history summary + env meta
```

---

## 1. Canonical record schema (shared across the whole study — use EXACTLY this)

A telecom CDR, flat, 20 fields, ~523B as compact JSON (one line):

| field | type | field | type |
|---|---|---|---|
| `record_id` | uuid str | `roaming` | bool |
| `timestamp` | ISO8601 str | `charge_amount` | float (2dp) |
| `msisdn` | 10-digit str | `currency` | `"JPY"` |
| `imsi` | 15-digit str | `session_start` | ISO8601 str |
| `imei` | 15-digit str | `session_end` | ISO8601 str |
| `cell_id` | int | `apn` | str |
| `event_type` | VOICE/SMS/DATA/ROAMING/EVENT | `sgsn_addr` | IPv4 str |
| `direction` | MO/MT/FWD | `ggsn_addr` | IPv4 str |
| `duration_s` | int 0–3600 | | |
| `bytes_up` / `bytes_down` | int | | |
| `rat` | 2G/3G/4G/5G/NR_SA | | |

`gen_corpus.py --nested` wraps `session_start`, `session_end`, `duration_s`,
`bytes_up`, `bytes_down` under `session_info` (for the json_flatten/unnest
benches — T2). CSV output is always flat.

## 2. Environment & prerequisites

```bash
export TRAM_API_URL=${TRAM_API_URL:-http://127.0.0.1:30001}   # manager NodePort
export TRAM_API_KEY=...                                        # X-API-Key if apiKey is set
export TRAM_NAMESPACE=${TRAM_NAMESPACE:-trishul-ram}           # pod namespace
export PERF_PYTHON=/home/dhaka/trishul/trishul-ram/.venv/bin/python
```

- venv needs: httpx, pysnmp, kafka-python (all present in the repo venv;
  kafka-python arrived with the Phase 1b broker deploy — `kafka_loadgen.py
  --check` reports availability).
- `kubectl` on the bench host with access to the cluster.
- Templates use `${VAR:-default}` substitution exactly like the shipped
  `pipelines/*.yaml`; defaults are runnable on the local kind cluster.
- **In-pod paths**: `local` source/sink paths in the templates
  (`/data/perf/in`, `/data/perf/out`) are *inside* the worker/manager pod.
  Before a batch run, materialize the input files with `file_gen.py` and
  `kubectl cp` them into the pod, or mount a shared volume (Phase 2 infra).

## 3. Generators

### 3.1 gen_corpus.py — canonical corpus

```bash
.venv/bin/python scripts/perf/generators/gen_corpus.py \
    --seed 42 --n 1000 [--nested] --format jsonl|ndjson|csv|xml [--out FILE]
```
Deterministic given `(seed, n)` — byte-identical across runs. One compact
JSON record per line (~523B) for jsonl/ndjson; `--nested` for T2.

### 3.2 loadgen_webhook.py — HTTP POST flood

```bash
.venv/bin/python scripts/perf/generators/loadgen_webhook.py \
    --url http://127.0.0.1:30002/webhooks/ingest \
    --rate 200 --concurrency 20 --duration 180 \
    --payload-file corpus.jsonl --summary /tmp/run/summary.json
```
One record per POST body. Global rate limiter (rate is offered load
regardless of concurrency). Summary (stderr + `--summary`): sent, 2xx, 4xx,
5xx, errors, latency p50/p95/max.

> **Webhook ingress port — topology-dependent (verified on the live cluster):**
> manager+worker mode serves `/webhooks/*` on the **worker ingress** NodePort
> (`worker.ingressService.nodePort`, default **30002**), NOT the manager port
> (30001 returns 404 in worker mode). Standalone mode serves webhooks on the
> manager port (30001). Phase 2: set the loadgen URL from the deployed
> topology's NodePort.

### 3.3 mock_rest_server.py — REST source/sink mock

```bash
.venv/bin/python scripts/perf/generators/mock_rest_server.py \
    --port 18080 --corpus corpus.jsonl --page-size 100
```
- `GET /records?page=N` → JSON array page; `GET /records?offset=N&limit=M`
  matches the TRAM REST source's paginator contract exactly.
- `POST /collect` counts records + body bytes; `GET /collect/stats[?reset=1]`.

### 3.4 file_gen.py — batch files for local sources

```bash
.venv/bin/python scripts/perf/generators/file_gen.py \
    --files 10 --records 10000 --format jsonl|csv|xml|pm_xml --out /tmp/batches
```
Writes `batch_000001.<ext>` … `batch_000010.<ext>`. `pm_xml` emits a 3GPP
measData doc with one `<measValue>` per bench record. **All four formats
round-trip through TRAM's own serializers** (smoke-verified).

### 3.5 snmp_responder_scaled.py — synthetic SNMP table

```bash
setsid .venv/bin/python scripts/perf/generators/snmp_responder_scaled.py \
    --rows 1000 --port 11161 --seed 42 </dev/null >/tmp/snmp.log 2>&1 &
```
Foreground by design — **detach with `setsid`** (the wire harness uses the
same pattern). Serves a `rows × 8` table of mixed types (OctetString /
Integer32 / Counter32 / Gauge32 / TimeTicks) under `1.3.6.1.4.1.99999.2.1`
as in-process untyped instances — **no MIB files needed**. Community
`public`, v1/v2c GET/GETNEXT/GETBULK. The walk over the full 1000-row table
is the S4 workload; `s4_snmp_local.yaml` uses `yield_rows: true` so each
table row becomes one record.

### 3.6 kafka_loadgen.py — Kafka produce/consume

```bash
# produce
.venv/bin/python scripts/perf/generators/kafka_loadgen.py \
    --brokers 127.0.0.1:30092 --topic perf-cdr --rate 500 --duration 180 \
    --payload-file corpus.jsonl
# consume (+ lag)
.venv/bin/python scripts/perf/generators/kafka_loadgen.py \
    --consume --lag --brokers 127.0.0.1:30092 --topic perf-cdr --duration 180
```
Uses **kafka-python** — the exact client library TRAM's own Kafka connectors
use (`tram/connectors/kafka/*`). **Status: syntax-checked only; broker was
mid-deploy at Phase 1a smoke time (mark untested, Phase 1b owns it).**
`--check` verifies client availability.

## 4. Templates

22 templates, all validated against the Pydantic schema by:

```bash
.venv/bin/python scripts/perf/templates/validate_templates.py   # 22/22 pass
```

| template | source → sink | input corpus |
|---|---|---|
| `s1_webhook_local.yaml` | webhook (stream) → local | loadgen_webhook flood |
| `s2_local_local_csv.yaml` | local csv → local csv | `file_gen --format csv` |
| `s2_local_local_pmxml.yaml` | local pm_xml → local json | `file_gen --format pm_xml` |
| `s3_rest_rest.yaml` | rest paginate → rest | mock_rest_server |
| `s4_snmp_local.yaml` | snmp_poll walk → local | snmp_responder_scaled |
| `s4b` (stack variant) | **same YAML** — set `TRAM_SNMP_STACK=trishul` in the deployment env to run the trishul-snmp stack | same |
| `s5_sftp_local.yaml` | sftp → local | `file_gen --format csv` on the SFTP server |
| `s6_kafka_local.yaml` | kafka (stream) → local | kafka_loadgen producer |
| `s7_local_kafka.yaml` | local → kafka | `file_gen --format jsonl` |
| `fsweep_*.yaml` (9) | local → local, per serializer | see table below |
| `t1_project_filter.yaml` | project + filter | flat jsonl |
| `t2_json_flatten_enrich.yaml` | json_flatten + enrich | **nested** jsonl + `lookup.csv` |
| `t3_deduplicate.yaml` | deduplicate | flat jsonl |
| `t4_counter_delta_window.yaml` | counter_delta + window_aggregate | flat jsonl |
| `t5_chain.yaml` | rename→cast→add_field→filter→project | flat jsonl |

### fsweep serializer input formats (which generator/recipe produces the input)

| serializer | input file | how to produce |
|---|---|---|
| `csv` | `*.csv` header+rows | `file_gen --format csv` |
| `json` | `*.json`, **one JSON array** per file | wrap gen_corpus output: `[` + jsonl + `]` |
| `ndjson` | `*.jsonl` one record/line | `file_gen --format jsonl` |
| `xml` | `*.xml` `<records>` root | `file_gen --format xml` |
| `pm_xml` | `*.xml` measData doc | `file_gen --format pm_xml` |
| `msgpack` | `*.msgpack` msgpack array per file | Phase 2 recipe: `msgpack.packb(records)` |
| `avro` | `*.avro` object container + schema `/data/schemas/cdr.avsc` | Phase 2 recipe: fastavro writer |
| `protobuf` | `*.pb` length-delimited `CdrRecord` + `/data/schemas/cdr.proto` | Phase 2 recipe: protoc + framed writer |
| `parquet` | `*.parquet` flat columns | Phase 2 recipe: pyarrow `Table.from_pylist` |

Avro/protobuf/parquet need `pip install tram[avro|protobuf_ser|parquet]` in
the worker image (Phase 2 infra), plus schema files uploaded to
`/data/schemas` (manager persistence mounts it at `/data/schemas`; workers
sync assets at run time).

### T2 enrich lookup

`t2_json_flatten_enrich.yaml` joins on `msisdn` against
`${PERF_ENRICH_LOOKUP:-/data/perf/lookup.csv}` (`msisdn,region,subscriber_plan`).
Generate once per seed:
```bash
.venv/bin/python - <<'EOF'
import csv, random
rng = random.Random(42)
with open('/tmp/lookup.csv','w',newline='') as f:
    w = csv.writer(f); w.writerow(['msisdn','region','subscriber_plan'])
    for _ in range(20000):
        w.writerow([f"{rng.randint(1,9)}{rng.randint(0,10**9-1):09d}",
                    rng.choice(['kanto','kansai','chubu','kyushu']),
                    rng.choice(['lite','standard','premium'])])
EOF
```

## 5. Collector

```bash
.venv/bin/python scripts/perf/collector/collect.py \
    --run-id run-001 --duration 180 --interval 5 \
    --namespace trishul-ram --api-url http://127.0.0.1:30001 \
    --pipeline s1-webhook-local [--api-key KEY] [--helm-values infra/values-mgrw-m.yaml]
```
Writes `results/<run-id>/`:
- `samples.csv` — `kubectl top pods -n <ns>` per interval
- `summary.json` — run-history aggregation: records_in/out, bytes_in/out,
  run durations (total/avg/max), error count
- `meta.json` — TRAM version (pyproject), helm-values snapshot, node
  allocatable CPU/mem, running image tags

Run-history fetch uses `X-API-Key` when `TRAM_API_KEY`/`--api-key` is set
(manager `apiKey`/`TRAM_API_KEY`; health/webhooks are exempt).

## 6. run_one.sh — one bench run

```bash
TRAM_NAMESPACE=trishul-ram ./scripts/perf/run_one.sh templates/s1_webhook_local.yaml run-001 \
    --warmup 60 --duration 180 \
    --loadgen ".venv/bin/python scripts/perf/generators/loadgen_webhook.py \
        --url http://127.0.0.1:30002/webhooks/ingest \
        --rate 200 --concurrency 20 --duration 180 --payload-file corpus.jsonl"
```
Flow: register pipeline via API (409 → delete + re-register) → stream
pipelines auto-start / batch pipelines trigger via `/run` and poll to
completion → warmup + steady state → stop → collector → delete pipeline
(`--keep` to leave it stopped). Loadgen summary goes to stderr.

## 7. Methodology (Phase 2)

- **Warmup/ramp: 60 s** — loadgen starts and the stream reaches steady state
  before measurement.
- **Steady state: 180 s** — the measured window. Collector samples
  `kubectl top pods` at `--interval 5` across this window; run history is
  fetched after the stop.
- **Repetitions: 2 per cell** — report the **median** of the two reps.
- **Metrics captured per cell**: offered load vs achieved intake (loadgen
  sent/2xx/5xx vs run-history records_in), records_out, bytes_in/out,
  end-to-end p50/p95/max latency (loadgen), per-pod CPU/mem (samples.csv),
  run duration, error count, env meta.
- **Report**: one row per cell = median over reps of
  {records_in/s, records_out/s, bytes_in/s, bytes_out/s, p50, p95, max,
  peak pod CPU, peak pod mem, errors}.

## 8. Phase-2 run matrix (exact)

Topologies (infra lane owns `scripts/perf/infra/`; files present at Phase 1a
smoke time):
- **single** — standalone StatefulSet, 1 replica: `infra/values-single.yaml`
- **mgr+worker** — manager (fixed M) + 3 workers: `infra/values-mgrworker.yaml`

Resource profiles (worker/standalone requests — `infra/values-res-{L,M,H}.yaml`):
- **L** = 250m CPU / 512Mi
- **M** = 500m / 1Gi
- **H** = 2 CPU / 2Gi

A cell's deployment = topology values + profile values (e.g. `--values
infra/values-mgrworker.yaml,infra/values-res-M.yaml`).

### Matrix A — core scenarios (both topologies × L/M/H = 6 cells each)

| scenario | template | loadgen |
|---|---|---|
| S1 webhook | `s1_webhook_local.yaml` | loadgen_webhook 100/300/800 rps |
| S2 csv | `s2_local_local_csv.yaml` | file_gen 10×10k |
| S2 pm_xml | `s2_local_local_pmxml.yaml` | file_gen 10×10k |
| S3 rest | `s3_rest_rest.yaml` | mock_rest_server (paged read) |
| S4 snmp | `s4_snmp_local.yaml` | snmp_responder 1000 rows (+10k variant) |
| S5 sftp | `s5_sftp_local.yaml` | file_gen → SFTP server |
| S6 kafka→local | `s6_kafka_local.yaml` | kafka_loadgen 500/2000 msg/s |
| S7 local→kafka | `s7_local_kafka.yaml` | file_gen 10×10k |

`run_one.sh` per scenario per cell; 2 reps each.

### Matrix B — format sweep (mgr+worker, profile M; L/H only for csv/json/parquet)

9 fsweep templates × 1 cell each (M), plus csv/json/parquet at L and H.

### Matrix C — transforms (mgr+worker, profile M; L/H for t1/t5)

t1…t5 × 1 cell each (M), plus t1 and t5 at L and H.

### Per-run command template (any cell)

```bash
RUN_ID="${TOPOLOGY}-${SCENARIO}-${PROFILE}-rep${R}"
TRAM_NAMESPACE=trishul-ram ./scripts/perf/run_one.sh \
    scripts/perf/templates/${TEMPLATE}.yaml "$RUN_ID" \
    --warmup 60 --duration 180 \
    --loadgen "$LOADGEN" 2>&1 | tee results/$RUN_ID/run.log
.venv/bin/python scripts/perf/collector/collect.py \
    --run-id "$RUN_ID" --duration 180 --interval 5 \
    --namespace trishul-ram --helm-values scripts/perf/infra/${VALUES}.yaml \
    --pipeline "${PIPELINE}"
```

> `${VALUES}` is the topology/profile values file for the cell (see §8).

## 9. Deployment notes & workarounds (for the Phase-2 runners)

1. **Webhook ingress port**: manager+worker → worker ingress NodePort 30002;
   standalone → manager NodePort 30001. A loadgen pointed at the wrong port
   gets clean 404s ("No webhook source registered for path") — verified live.
2. **Rate limiter**: `TRAM_RATE_LIMIT` defaults to 50/window per client IP on
   `/api/*` + `/webhooks/*`. Bench deployments must set `TRAM_RATE_LIMIT=0`
   (or a high value) in the helm values, or floods will be 429'd and the
   collector's own `/api/runs` fetch will be throttled from the same host.
3. **Broadcast placement**: webhook/kafka stream pipelines default to
   `workers: all` (3 slots in the bench cluster). Run history has one row
   per worker slot — the collector sums them. Deletion is async: deleting a
   stream pipeline then immediately re-registering the same name can 409 on
   the lingering placement; `run_one.sh` handles it (delete + retry).
4. **In-pod paths**: `local` source/sink dirs live in the pod. Materialize +
   `kubectl cp` inputs (or a shared RWX volume) before batch runs; pull sink
   outputs after. `run_one.sh` doesn't copy files — the Phase-2 driver does.
5. **Stateful transforms** (T4) require `TRAM_STATEFUL_TRANSFORMS=1` (default)
   and `thread_workers: 1` (schema-enforced).
6. **SNMP stack variant (s4b)**: same template; set `TRAM_SNMP_STACK=trishul`
   on manager AND workers (mismatch is a warning, but the bench wants one
   stack). The responder serves untyped instances — no MIB files, so
   `resolve_oids: false` in the template.
7. **Template env-substitution**: the loader substitutes `${VAR:-default}`
   before YAML parse; numeric defaults (ports, page sizes) must parse as the
   target field type. `validate_templates.py` checks the substituted config,
   so it is the gate before any Phase-2 change.
8. **venv tram import caveat**: the harness venv's `tram` editable install
   points at a stale checkout; scripts that import tram add the repo root to
   `sys.path` themselves (see `validate_templates.py`). Run everything with
   `.venv/bin/python`.
9. **kafka-python**: required for S6/S7 + kafka_loadgen; present in the venv
   as of the Phase 1b broker deploy. `--check` reports availability.

## 10. Smoke evidence (Phase 1a, this tree @ 326d9dd / v1.5.1)

| tool | result |
|---|---|
| gen_corpus (jsonl/ndjson/csv/xml, 1000 recs) | pass — ~523B/rec, deterministic, nested OK |
| file_gen (jsonl/csv/xml/pm_xml 3×100) | pass — all parse via TRAM serializers |
| loadgen_webhook → mock_rest_server (60s @ 100 rps) | pass — 6000 sent / 6000 2xx / server counted 6000 recs + 3,131,300 B; p50 1.7ms p95 2.4ms max 11ms |
| snmp_responder_scaled (1000 rows) | pass — GET + GETNEXT answered through TRAM's hlapi path |
| kafka_loadgen | syntax + `--check` pass; **untested vs broker (Phase 1b)** |
| templates/validate_templates.py | **22/22 pass** |
| ruff check scripts/perf/ | clean |
| e2e run_one.sh (s1 webhook, live kind cluster) | pass — 1600 sent/202s → records_in=records_out=1600, 0 errors |

## 11. Phase-2 execution notes (corrections a re-runner must apply)

Learned during the actual Matrix A/B/C + ladder runs — the runbook above plus
these notes is the complete re-run procedure.

1. **t1/t5 filter conditions**: simpleeval conditions reference record fields
   directly (`event_type != 'EVENT'`), NOT `record.get(...)` — the original
   shipped templates lost 100% of records. `templates/t1_project_filter.yaml`
   and `templates/t5_chain.yaml` are the corrected versions (identical to the
   `results/templates-fixed/` copies used for the Matrix C runs, kept for
   provenance).
2. **Single topology ingress**: webhook runs go via NodePort **30001**
   (`/webhooks/ingest`) — single mode has no worker-ingress 30002.
3. **Single topology needs `TRAM_MANAGER_URL=http://localhost:8765`**: the
   default is empty, and with it all run-history rows are silently dropped.
   Even with it set, stream runs never reach run history in single mode —
   derive stream numbers from the pod log (`Stream run ended` JSON line).
4. **Worker images lack serializer extras** (msgpack, pyarrow, grpcio-tools):
   `pip install --target /data …` in each worker + `--set
   env.PYTHONPATH=/data`. Python 3.13 drops nonexistent PYTHONPATH entries at
   startup, so the target must be an always-existing mount (`/data`). Restage
   per profile upgrade on mgr+worker (emptyDir); standalone `/data` is a PVC
   and survives.
5. **SFTP chroot**: `PERF_SFTP_REMOTE=/upload/in` (atmoz chroot) via `--set`.
6. **s4 time-box**: raise to 1500s — the 1000-row walk is RTT-bound (~600s at
   M+; the 15-min default is too tight at L).
7. **Host suspends** (WSL2): a suspend mid-run contaminates the rep — re-run
   and mark it in the CSV notes column.
8. **s6 ladder sizing**: keep per-step record totals ≤ 90k so the local sink's
   99,999 file-part cap never masks the consumer ceiling.
9. **kafka_loadgen sync producer caps ~1,400 msg/s per process** — run k
   parallel processes for offered-rate ladders.

## 12. Phase-2 result files

| file | content |
|---|---|
| `results/matrix-a-mw.csv` (48) / `results/matrix-a-single.csv` (48) | S1–S7 × {L,M,H} × 2 reps per topology |
| `results/matrix-b-mw.csv` (30) / `matrix-b-single.csv` (2) | format sweeps |
| `results/matrix-c-mw.csv` (18) / `matrix-c-single.csv` (4) | transform chains |
| `results/saturation-s1.csv`, `saturation-s6.csv`, `saturation-s1-single.csv` | ladders @ M |
| `results/deploy-state.md` | helm revision / profile timeline |
| `results/mw-*`, `single-*`, `sat-*` dirs | per-run artifacts |

Analysis + sizing guidance: `docs/ideas/perf-capacity-analysis-2026-10.md`;
improvement candidates: `docs/ideas/perf-improvement-candidates.md`.