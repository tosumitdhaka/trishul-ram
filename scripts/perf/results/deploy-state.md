# Matrix A (mgr+worker) — deploy state log

## Phase 2c (2026-10-01, single/standalone topology) — rev 134 @ M

- Standalone image built from the repo Dockerfile (main @ 326d9dd, v1.5.1):
  `trishul-ram:local-20260930193823`, kind-loaded. Deploy:
  values-bench + values-single + values-res-{L,M,H}, --set image.tag=...,
  env.PERF_SFTP_REMOTE=/upload/in, env.PYTHONPATH=/data. Pod: trishul-ram-0
  (statefulset trishul-ram); webhooks route via the main NodePort 30001
  (/webhooks/ingest) — the worker-ingress 30002 does not exist in single.
- Standalone /data is a per-pod PVC: pip --target /data extras + batch fixtures
  SURVIVE pod restarts/upgrades (unlike mgr+worker emptyDirs) — restage only on
  PVC loss.
- NEW FIX REQUIRED: `--set env.TRAM_MANAGER_URL=http://localhost:8765`.
  Without it the run-complete callback URL is empty and ALL run-history rows are
  silently dropped in single topology (TRAM_MANAGER_URL defaults to "").
- REMAINING GAP (worked around): even with TRAM_MANAGER_URL set, STREAM runs
  still never reach run history in single topology — the local stream path
  (controller._stream_worker) never calls manager.record_run; only live stats
  are emitted (StatsStore). Batch runs DO persist via _finalize_batch_result.
  Matrix-A-single s1/s6 metrics were derived from the pod log's
  "Stream run ended" JSON line (records_in/out/skipped) — driver marks these
  rows "success(log-derived)". Product finding, not a harness artifact.
- Host fixtures restarted for s3/s4: mock_rest_server (0.0.0.0:18080) and
  snmp_responder_scaled (0.0.0.0:11161, UDP) via nohup + disown.
- Host SUSPENDED twice mid-sweep (~2h during single-s1-M-rep1; ~12h40m between
  s3-M-rep2 and s5-M-rep1): contaminated s1-M-rep1 (45771/54000, bogus 3m CPU),
  s3-M-rep2 (37s) and s5-M-rep2 (2506s post-resume SFTP stall) — all four cells
  re-run clean; re-run rows noted in the CSV.
- fsweep_json spot check needed *.json-renamed staging (json-array content in
  .json files); t1/t5 used the templates-fixed copies (broken shipped filters).
- CSVs: matrix-a-single.csv (48 rows), matrix-b-single.csv (2),
  matrix-c-single.csv (4), saturation-s1-single.csv (4 steps, stopped at 800
  offered). Drivers in /tmp/opencode/perf-c/.

## Phase 2b (2026-09-30, mgr+worker single-node kind) — rev 129 @ M

- 4xx@300rps diagnosis (pre-ladder, required): reproduced clean 300 rps steady-state
  (0 4xx over 60s). The s1-M-rep1 4xx=77 and the ladder steps' ~3% 4xx are the
  registration placement race: requests hitting a worker whose webhook source
  placement hasn't propagated yet → 404 "No webhook source registered for path"
  (~5-11s window after registration). NOT the rate limiter: the worker ingress
  has no RateLimitMiddleware (only the manager does; TRAM_RATE_LIMIT=0 via
  values-bench), and the webhook route exists only on workers. values-bench IS
  effective on the ingress path — no fix needed; loadgen should settle ~10s
  after registration before counting.
- Worker pods do NOT carry the serializer extras (fastavro OK but
  msgpack/pyarrow/grpcio-tools MISSING; manager has grpc_tools but not fastavro).
  Fix used: `helm --set env.PYTHONPATH=/data` + `pip install --target /data
  msgpack pyarrow grpcio-tools` in each worker pod (restaged after each profile
  upgrade — emptyDir wipes /data). Python 3.13 drops nonexistent PYTHONPATH dirs
  at interpreter startup, so the target dir must exist at pod start — /data (the
  emptyDir mount point) always does; a subdir like /data/pylibs does NOT work
  unless created by an initContainer.
- t1/t5 templates as shipped are broken: filter conditions
  `record.get('event_type') != 'EVENT'` reference a `record` name that
  simpleeval does not expose (names=record fields are the namespace).
  Corrected copies under scripts/perf/results/templates-fixed/ (conditions
  `event_type != 'EVENT'`, `bytes_down >= 0`) used for matrix C; as-shipped
  behavior = 100% record loss with filter condition errors.
- t4 with the random-timestamp corpus emits ~8 window aggregates per 100k
  records (windows rarely finalize); transforms still process all records, so
  E2E chain cost is measurable, records_out is not meaningful.
- fsweep protobuf round-trip converts field names snake_case → camelCase
  (serializer convention) — passes through the pipeline with camelCase keys.
- protobuf serializer needs /data/schemas/cdr.proto (message_class CdrRecord,
  length_delimited framing); avro needs /data/schemas/cdr.avsc — staged on
  manager + workers (see /tmp/opencode/perf-b/schemas).
- json input recipe (from Phase 2a s7): one JSON array per .jsonl file;
  fsweep_json additionally needs the .json extension for its *.json pattern.
- Phase 2b CSVs: matrix-b-mw.csv (30 rows), matrix-c-mw.csv (18),
  saturation-s1.csv (7), saturation-s6.csv (4). Run dirs mw-fsweep_*, mw-t*_*,
  sat-s1-M-*, sat-s6-M-*. Ladder drivers in /tmp/opencode/perf-b/.

## Step 0 bench environment (2026-09-29)

- Cluster: kind `tram-dev` (1 control-plane + 3 workers, nodes share the WSL2 host;
  aggregate treated as ~12 CPU / ~15Gi usable per infra/CLUSTER.md).
- Release `trishul-ram` (ns `trishul-ram`), chart 1.5.1, app 1.5.1.
- Images: `trishul-ram-manager:local-20260929135930` /
  `trishul-ram-worker:local-20260929135930` (main @ 326d9dd, final v1.5.1).
- Topology: manager (PVC-backed SQLite) + 3 workers, TRAM_SNMP_STACK=legacy.
- Bench values: `scripts/perf/infra/values-bench.yaml` + `scripts/perf/infra/values-res-{L,M,H}.yaml`
  applied via `helm upgrade --reuse-values` (layered; base release state from the
  v1.5.1 kind verification, rev 97).
- env additions in values-bench.yaml: TRAM_RATE_LIMIT=0 (workaround #2),
  TRAM_STATEFUL_TRANSFORMS=1, baseline env carried explicitly, plus PERF_* template
  vars: REST/SNMP mocks on the host via the kind bridge gateway 172.19.0.1
  (mock_rest_server 0.0.0.0:18080, snmp_responder_scaled 1000 rows 0.0.0.0:11161),
  SFTP = in-cluster `sftp:22` (perf/perfpw, remote dir /home/perf/upload/in).
- Host-side fixtures: canonical corpus 100,000 records (seed 42, ~523B/rec jsonl);
  file_gen batches 10 x 10,000 (csv / pm_xml / jsonl, seed 42).
- Smoke (before matrix): webhook 100 records via worker ingress NodePort 30002 —
  100 sent / 100 2xx / 0 errors; run history records_in=records_out=100 (rep-filtered);
  results/smoke-webhook-100/.
- Note: run-history rows are cumulative per pipeline name across reps — the driver
  (cell.py) filters rows by rep start time; run_one.sh's own summary.json is
  cumulative and kept only as an artifact.
- Note: local/sftp sinks with serializer_out=json run in file_mode=single
  (models/pipeline.py normalise_sinks) and the {timestamp} filename template has
  1-second granularity, so per-record writes within the same second clobber each
  other on disk. Run-history records_out is the sink-accepted count (canonical
  metric); on-disk record counts for json local sinks will be much lower. Documented,
  not fought.

| time (UTC) | helm rev | change | pods after |
|---|---|---|---|
| 2026-09-29T14:33:13Z | rev 99 | profile L (values-bench + values-res-L) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T14:44:13Z | rev 100 | profile M (values-bench + values-res-M) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T14:55:06Z | rev 101 | profile H (values-bench + values-res-H) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:06:22Z | rev 102 | profile L (values-bench + values-res-L) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:09:48Z | rev 103 | profile M (values-bench + values-res-M) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:12:31Z | rev 104 | profile H (values-bench + values-res-H) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:16:02Z | rev 105 | profile L (values-bench + values-res-L) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:20:22Z | rev 106 | profile M (values-bench + values-res-M) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:24:11Z | rev 107 | profile H (values-bench + values-res-H) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:27:59Z | rev 108 | profile L (values-bench + values-res-L) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:33:24Z | rev 109 | profile M (values-bench + values-res-M) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:37:28Z | rev 110 | profile H (values-bench + values-res-H) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T15:41:10Z | rev 111 | profile L (values-bench + values-res-L) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T16:04:50Z | rev 112 | profile M (values-bench + values-res-M) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-29T16:28:11Z | rev 113 | profile H (values-bench + values-res-H) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 
| 2026-09-30T04:28:06Z | rev 114 | profile L (values-bench + values-res-L) | pods: trishul-ram-manager-0=Running trishul-ram-worker-0=Running trishul-ram-worker-1=Running trishul-ram-worker-2=Running 

## Phase 2a resume (2026-09-30, post docker-daemon restart ~13:52 JST)

The rev-114 runner died mid-s5-L-rep1; nodes' pods restarted 14:0x JST but helm/PVCs
survived. Resumed s5–s7 (mgr+worker) with these changes/notes:

- **s5-L-rep1 (prior runner): INVALID, redone.** The only artifact was a run.log showing
  the run errored in 0.5 s: `SFTP listdir failed: [Errno 2] No such file` — run-history
  row 4ce177f1, 0 records. Root cause: the atmoz/sftp server chroots user `perf` into
  `/home/perf`, so the session-visible path is `/upload/in`, NOT `/home/perf/upload/in`
  (values-bench.yaml's `PERF_SFTP_REMOTE`). The prior runner had already uploaded the
  10 csv batches at 04:28Z — the dir was visible but unreachable by that path.
  Fix applied as `--set env.PERF_SFTP_REMOTE=/upload/in` on every upgrade below
  (values-bench.yaml itself left untouched per the write-scope constraint). Dir deleted,
  run redone; the stale failed row is excluded by the driver's per-rep run-id diffing.
- Revs 115–123 = values-bench + values-res-{L,M,H} + the SFTP `--set`, one upgrade per
  profile change (s5 L→M→H, s6 L→M→H, s7 L→M→H). Kafka topics recreated post-restart
  (broker PVC survived but topics were lost): `perf-cdr`, `perf-cdr-out`, 3 partitions RF1.
- s6 offered-rate mapping (README says only "kafka_loadgen 500/2000 msg/s"): L=500,
  M=2000, H=2000 — documented judgment call, noted in the report.
- Diagnostics kept alongside the matrix (not in the CSV): `diag-s6-distinct-H`
  (600k-record distinct corpus — proves the 99999 records_out cap is not duplicate-related)
  and `diag-s7-100x1000-H` (s7 with 100×1000-record files — proves s7 works when the
  serialized file-batch fits kafka-python's 1 MB max_request_size).

| time (UTC) | helm rev | change | pods after |
|---|---|---|---|
| 2026-09-30T05:14:52Z | rev 115 | profile L + SFTP path fix (s5) | all Running |
| 2026-09-30T05:24:50Z | rev 116 | profile M (s5) | all Running |
| 2026-09-30T05:35:36Z | rev 117 | profile H (s5) | all Running |
| 2026-09-30T05:47:10Z | rev 118 | profile L (s6) | all Running |
| 2026-09-30T06:00:10Z | rev 119 | profile M (s6) | all Running |
| 2026-09-30T06:16:58Z | rev 120 | profile H (s6) | all Running |
| 2026-09-30T06:36:04Z | rev 121 | profile L (s7) | all Running |
| 2026-09-30T06:52:16Z | rev 122 | profile M (s7) | all Running |
| 2026-09-30T07:03:15Z | rev 123 | profile H (s7) — current | all Running |
