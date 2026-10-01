# Perf study — cluster facts (kind `tram-dev`)

## Topology

kind cluster `tram-dev`: 1 control-plane + 3 workers, containerd, CNI kindnet
(pod CIDR 10.244.x, node subnet 172.19.0.0/16). Host access via the docker
bridge: nodes `172.19.0.2` (control-plane), `172.19.0.3` (tram-dev-worker),
`172.19.0.4` (tram-dev-worker2), `172.19.0.5` (tram-dev-worker3).

StorageClasses: `standard` (rancher.io/local-path, default) and `nfs-rwx`
(nfs-provisioner). metrics-server running (`kubectl top` works).

## Node capacity (allocatable) + idle baseline (2026-09-29)

| Node | Allocatable CPU | Allocatable memory | Pods | Idle CPU | Idle mem |
|---|---|---|---|---|---|
| tram-dev-control-plane | 12 | 16120920Ki (~15.4Gi) | 110 | 92m (0%) | 849Mi (5%) |
| tram-dev-worker | 12 | 16120920Ki (~15.4Gi) | 110 | 46m (0%) | 991Mi (6%) |
| tram-dev-worker2 | 12 | 16120920Ki (~15.4Gi) | 110 | 23m (0%) | 594Mi (3%) |
| tram-dev-worker3 | 12 | 16120920Ki (~15.4Gi) | 110 | 26m (0%) | 654Mi (4%) |

Kind nodes share the WSL2 host kernel/resources — "12 CPU" per node is the
host's total, not a per-node reservation. Treat the cluster aggregate as
≈12 CPU / ~15Gi usable (what the host reports), NOT 4×12.

## TRAM placement

The chart does **not** pin TRAM pods to specific nodes (`nodeSelector: {}`,
`affinity: {}` in `helm/values.yaml`; the standalone/manager/worker StatefulSets
all render `{{- with .Values.nodeSelector }}`, i.e. only when set). Current
placement (scheduler's choice): manager + worker-2 on tram-dev-worker3,
worker-0 on tram-dev-worker2, worker-1 on tram-dev-worker. For topology-pinned
runs Phase 2 can set `nodeSelector: {kubernetes.io/hostname: <node>}`.

## Deployed at snapshot time (2026-09-29)

- TRAM release `trishul-ram` (ns `trishul-ram`), manager+worker mode, images
  `local-20260929135930` built from main @ 326d9dd (final v1.5.1), tram 1.5.1
  on manager + all 3 workers. `TRAM_SNMP_STACK=legacy` (chart default; the
  release env previously carried `trishul` from the v1.5.0/1.5.1 kind
  verification runs — reset via a values file in the redeploy).
- Chart env of note (manager): `TRAM_MODE=manager`,
  `TRAM_DB_URL=sqlite:////data/tram.db` (manager PVC),
  `TRAM_MANAGER_URL=http://trishul-ram:8765`, `TRAM_WORKER_REPLICAS=3`,
  `TRAM_MIB_DIR=/data/mibs`. Workers: `TRAM_MODE=worker`, `/data` emptyDir
  (schemas/MIBs synced from manager). Both planes: `TRAM_QUEUE_MANUAL_RUNS=1`,
  `TRAM_STATEFUL_TRANSFORMS=1`, `TRAM_STREAM_SINGLE_PLACEMENT=1`,
  `TRAM_LOG_FORMAT=json`, `TRAM_LOG_LEVEL=INFO`.
- Kafka `kafka-0` (tram-dev-worker) and SFTP `sftp-0` (tram-dev-worker2) —
  see `PORTMAP.md`.

## Topology switching for Phase 2

- **manager+worker (3 workers)**: `./scripts/deploy-kind-tram-dev.sh --mode
  manager` (optionally `VALUES_FILE=scripts/perf/infra/values-mgrworker.yaml`).
- **single / all-in-one**: `./scripts/deploy-kind-tram-dev.sh --mode
  standalone` — builds the `trishul-ram` image (Dockerfile: manager+worker+UI
  in one container) and renders only the standalone StatefulSet
  (`manager.enabled=false`). Equivalent helm choices are codified in
  `scripts/perf/infra/values-single.yaml`. Note: switching between
  `--mode standalone` / `--mode manager` is a chart-level change (different
  StatefulSets and images) — the deploy script handles the image build +
  `--set` wiring in one shot.
- **resource levels**: layer `scripts/perf/infra/values-res-{L,M,H}.yaml`
  (L=250m/512Mi, M=500m/1Gi, H=2cpu/2Gi) — they set `manager.resources`,
  `worker.resources` (manager+worker StatefulSets) and top-level `resources`
  (standalone StatefulSet) so one overlay serves either topology.

## Caveats found

- `helm upgrade --reuse-values` (used by the deploy script) carries the env
  map forward across deploys — env set once persists until overridden. The
  perf baseline env above is what the release currently carries.
- Kafka's Service needs `publishNotReadyAddresses: true` (see
  `kafka.yaml`): the broker registers with its own controller through the
  ClusterIP before it turns Ready.
