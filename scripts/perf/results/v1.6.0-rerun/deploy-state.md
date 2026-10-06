# v1.6.0 re-run — deployment state

All deployments on the kind cluster `tram-dev`, namespace `trishul-ram`, via
`scripts/deploy-kind-tram-dev.sh` with image tag `local-20261001rr` (built from
`release/v1.6.0` @ e9efb52 — includes waves 1-3: #76-#79, #80, #83 fixes).

| helm rev | time (JST)       | topology×profile | notes |
|---------|------------------|------------------|-------|
| 145     | 2026-10-05 13:07 | mw × L           | first v1.6.0-image deploy (slow rollout-wait, see deploy-mw-L.log) |
| 146     | 2026-10-05 15:04 | mw × M           | |
| 147     | 2026-10-05 17:15 | mw × H           | |
| 148     | 2026-10-05 18:25 | single × L       | |
| 149     | 2026-10-05 19:30 | single × M       | |
| 150     | 2026-10-05 21:07 | single × H       | |

Values per combo: `/tmp/opencode/v160-rerun/values/{mw,single}-{L,M,H}.yaml`
(mw = manager 500m/1Gi + 3 workers at profile; single = standalone pod at
profile; `TRAM_RATE_LIMIT=0`, `TRAM_STATEFUL_TRANSFORMS=1`, `TRAM_SNMP_STACK=legacy`).

mw topology = manager + 3 worker replicas (workers:all placements spread across
worker-0/1/2). Stream pipelines (s1) placed on all 3 workers in both baseline
and re-run (apples-to-apples); s6 kafka streams single placement.

In-pod fixtures restaged per deploy via `drivers/restage.sh` (schemas, lookup,
source batches; msgpack/avro/protobuf/parquet generated in-pod via TRAM's own
serializers — no pip/PYTHONPATH staging anywhere in the re-run, exercising the
#79 in-image serializer extras directly).
