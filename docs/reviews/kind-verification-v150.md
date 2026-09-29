# Kind-cluster verification — v1.5.0 SNMP swap, flag-on (GH #72 / PR #74)

> **Status (2026-09-29): flag-on runtime record for the v1.5.0 SNMP stack swap.**
> Layer 4b of the v1.5.0 plan — the live `TRAM_SNMP_STACK=trishul` verification
> on kind required by the issue #72 acceptance criteria. Committed (8e08470); a
> re-run at final HEAD including a v1 sink check follows below.

Date: 2026-09-29 · Cluster: `tram-dev` (kind, 4 nodes) · Release: `trishul-ram` @ images `local-20260929034651` (branch `release/v1.5.0`, HEAD d3a51c8, tree clean) · Mode: manager+worker (1 manager, 3 workers, 1Gi limits) · Flag: `TRAM_SNMP_STACK=trishul` on **both** planes.

## Deployment & flag plumbing

- `scripts/deploy-kind-tram-dev.sh --mode manager` with a values file adding
  `env.TRAM_SNMP_STACK: trishul`: build + load + Helm upgrade in 5m33s; all
  pods Running (the chart's generic `.Values.env` map renders into both the
  manager and worker StatefulSets — **no chart change was needed**).
- `kubectl exec` on manager + all 3 workers: `AppConfig.from_env().snmp_stack == "trishul"`
  on every plane; `TRAM_MODE` correct (manager/worker).
- No `SNMP stack mismatch` warnings in the manager log across the whole session
  (the once-per-worker rolling-upgrade guard stayed silent through 34
  `pipeline-stats` callbacks — `tram_mgr_pipeline_stats_received_total=34`,
  counter observed climbing from 0 during the test runs).
- Only manager warnings seen: transient worker-0 DNS probe failures while the
  last worker rolled (self-healed), the known dev-cluster "API auth disabled"
  notice, and a missing `pipelines/` dir note — all pre-existing, none SNMP-related.

## Wire checks — pysnmp 7.1.25 peer on the WSL2 host

Peer: adapted copies of the harness references (`pysnmp_agent.py` →
`0.0.0.0:11163` with an added 3-row ifTable under the real IF-MIB OIDs;
`pysnmp_traprecv.py` → `0.0.0.0:11180`) run from the repo venv, reached from
kind at the docker-bridge gateway `172.19.0.1`. A UDP echo roundtrip
(pod → host → pod) was verified first: the CNI-bridge NAT path passes UDP
return traffic on this host, so `hostNetwork` was not needed.

| Check | Result | Evidence |
|---|---|---|
| v1 GET | PASS | `sysDescr.0 = "PySNMP engine version 7.1.25, …"` (run success, 1 record, worker-0) |
| v1 WALK `IF-MIB::ifTable` | PASS | 3 rows, `yield_rows`, GETNEXT loop |
| v2c GET | PASS | same sysDescr value as v1 (decode-equivalent) |
| v2c WALK | PASS | 3 rows: `lo/eth0/wlan0`, ifType 24/6/71, ifMtu 65536/1500/1500, ifSpeed 10M/1G/54M, ifOperStatus 1/1/2, `_index` 1/2/3 |
| v3 GET (SHA-256/AES-128) | PASS | identical sysDescr record — RFC 7860 tag-length path live (the #28 defect class) |
| v3 WALK | PASS | byte-identical ifTable rows to v1/v2c |
| Trap source v2c | PASS | pysnmp `warmStart` → NodePort 30162 → decoded `{"sysUpTime.0": "0", "snmpTrapOID.0": "1.3.6.1.6.3.1.1.5.2", "enterprises.99999.1.0": "kind-v150-trap-v2c"}` |
| Trap source v3 (SHA-256/AES-128) | PASS | same warmStart trap decoded identically via NodePort 30163 — standard SHA-2 trap receive on the wire |
| Trap sink v2c | PASS | host receiver logged `1.3.6.1.2.1.1.3.0=10637888; 1.3.6.1.6.3.1.1.4.1.0=1.3.6.1.4.1.99999; 1.3.6.1.4.1.99999.1.0=kind-v150-sink-test` (sysUpTime + trap OID + custom varbind) |

Trap-source streams used the per-pipeline K8s Service path
(`kubernetes.enabled: true`, NodePort UDP, `workers.count: 1` with manual
Endpoints pinned to the dispatched worker) — the Services appeared on
registration and were removed on stop, as designed.

All symbolic names (`sysDescr.0`, `snmpTrapOID.0`, `ifIndex`, `ifDescr`,
`ifType`, `ifMtu`, `ifSpeed`, `ifOperStatus`) were resolved **worker-side on
the trishul stack** — the numeric-only `enterprises.99999.1.0` varbind is
expected (the synthetic subtree has no corpus MIB).

## Worker MIB sync (custom MIB, not baked into any image)

`KIND-TEST-MIB` (synthetic, mapped onto the peer's `enterprises.99999` smoke
subtree) was uploaded via `POST /api/mibs/upload` on the trishul manager:

- Compile via trishul-smi: `"compiled": ["KIND-TEST-MIB"], "stack": "trishul"`
  → `KIND-TEST-MIB.json` (tsmi JSON IR) in the manager's `TRAM_MIB_DIR`.
- Serving: `GET /api/mibs/KIND-TEST-MIB?format=json` → 200 (`?format=py` → 404,
  correctly skipped by the sync).
- A poll pipeline (`mib_modules: [KIND-TEST-MIB]`, symbolic
  `KIND-TEST-MIB::ktOne.0`) dispatched to worker-2 — whose `/data/mibs` was
  **empty** before the run — synced `KIND-TEST-MIB.json` (2331 bytes) and
  resolved the full round trip:
  `{"ktOne.0": "pysnmp-agent reference for kind v1.5.0 live wire checks", "ktTwo.0": "sysName-slot"}`.

This proves the whole chain live: manager-side tsmi compile → JSON serving →
worker sync → trishul-stack resolve of a MIB that exists nowhere else.

Standard MIBs (IF-MIB, SNMPv2-MIB) resolve from the dual-format corpus baked
into the worker image at `/mibs` (`prepend_system_mib_dirs` puts `/mibs` ahead
of `/data/mibs`), so the ifTable/sysDescr polls above already exercised the
bundled JSON bundles.

## Findings

1. **Minor (environment / upgrade-path note, no code change):** this dev
   cluster's manager runs with `manager.persistence.enabled=true`, so
   `TRAM_MIB_DIR=/data/mibs` on the PVC — which still holds the pre-v1.5.0
   `.py`-only corpus. The image's dual-format corpus at `/mibs` is shadowed,
   so `GET /api/mibs/IF-MIB?format=json` 404s and workers sync only the `.py`
   for those legacy artifacts. Harmless for standard MIBs (baked into the
   worker image in both formats) and for new uploads (compile produces the
   JSON bundle), but **custom MIBs uploaded before v1.5.0 on a persistent
   manager volume sync `.py`-only and cannot resolve on a `trishul` worker
   until re-uploaded/recompiled**. Worth a line in the v1.5.0 migration notes.
2. **Info:** `sysName.0` decoded as `""` in all GET records — the pysnmp peer
   genuinely serves an empty default; verified directly against the agent
   (faithful decode, not a TRAM bug).
3. **Info:** pre-existing dev pipelines in the persistent DB
   (`snmp-localhost-to-jsonl` etc.) were paused to keep run history clean; no
   other cluster state was modified.

## Verdict

Flag-on live verification passes in full: v1/v2c/v3 poll roundtrips (GET +
WALK, symbolic OIDs resolved worker-side on the trishul stack), v2c + v3 trap
receive, trap-sink send with receiver-side content assertion, and the
compile → serve → sync → resolve MIB chain for a non-bundled custom MIB —
all against an independent pysnmp 7.1.25 peer on the wire, with zero
stack-mismatch warnings and both planes pinned to `TRAM_SNMP_STACK=trishul`.
The issue #72 flag-on acceptance criterion (v1/v2c/v3 roundtrips + trap
paths + worker MIB sync) is met.

## Re-run at final HEAD (26a5131)

Pre-tag re-run after the post-review fix batch 5698a51 (RFC 2576 v1 Trap-PDU
mapping, value-rendering parity, `_sync_mib` bundle-content guard, dead
`_decode_trap_tsmp` removal), requested by the independent re-review (N3).

Redeploy: `./scripts/deploy-kind-tram-dev.sh --mode manager` with the same
values file (`env.TRAM_SNMP_STACK: trishul`) — images `local-20260929044450`
(app 1.4.8, branch `release/v1.5.0`, HEAD 26a5131, tree clean); all pods
Running; flag verified `trishul` on the manager and all 3 workers via pod env.

### Regression spot-checks

| Check | Result | Evidence |
|---|---|---|
| v3 poll GET (SHA-256/AES-128, symbolic) | PASS | run success on worker-0; `{"sysDescr.0": "PySNMP engine version 7.1.25, …"}` — byte-identical to the first run's record |
| Worker MIB sync (custom MIB) | PASS | post-rollout fresh emptyDir on worker-0 re-synced `KIND-TEST-MIB.json` from the manager and resolved `ktOne.0`/`ktTwo.0` with the same values as the first run |
| Trap source v3 | PASS | pysnmp `warmStart` v3 trap → NodePort 30163 → decoded `{"snmpTrapOID.0": "1.3.6.1.6.3.1.1.5.2", "enterprises.99999.1.0": "kind-v150-rerun-final"}` |
| Stack-consistency guard | PASS | zero `SNMP stack mismatch` log lines; `tram_mgr_pipeline_stats_received_total` climbing (8 callbacks observed during the spot checks) |

### New check — v1 trap sink on the wire (N3)

Two `snmp_trap` sink pipelines (`version: "1"`, post-fix tsmp path) sent to a
raw BER capture on the host (decoded per the repo wire suite's own
`test_v1_send_trap_wire_enterprise_specific` technique — `pyasn1` BER +
`pysnmp.proto.api.v1`):

- **Enterprise-specific trap** (`trap_oid: 1.3.6.1.4.1.99999`):
  `enterprise=1.3.6.1.4.1`, `generic=6`, `specific=99999` —
  `enterprise + (specific,)` reconstructs the configured trap OID **exactly**
  (the RFC 2576 §3.2 split; the pre-fix bug of stuffing the full OID into the
  enterprise field would have yielded `1.3.6.1.4.1.99999.99999`).
  Varbinds: `sysUpTime.0 = 10936782` + `1.3.6.1.4.1.99999.1.0 = rerun-v1-sink-payload`.
- **Standard warmStart trap** (`trap_oid: 1.3.6.1.6.3.1.1.5.2`):
  `enterprise=1.3.6.1.6.3.1.1.5` (the snmpTraps node), `generic=1` (warmStart),
  `specific=0` — the standard-trap 0–5 mapping live on the wire.
  Varbinds: same sysUpTime + `rerun-v1-warm-payload`.

Both sink runs succeeded on the wire (datagrams received from node
172.19.0.3, i.e. the worker's SNATed node IP) and the runs reported
`success` in run history.

### Verdict

No regressions vs the first run (poll, trap source, MIB sync, stack guard all
green with identical evidence), and the new v1 trap-sink check confirms the
5698a51 RFC 2576 Trap-PDU encoding on the wire: the enterprise/generic/specific
split reconstructs the configured trap OID exactly, standard traps map to
generic 0–5, and payload varbinds carry through. The pre-tag re-run is clean.
