# Perf study — kind NodePort / access map

Host → cluster: any kind node IP works for NodePorts (control-plane `172.19.0.2`,
workers `172.19.0.3`–`172.19.0.5`; all reachable from the WSL2 host).

## TRAM (release `trishul-ram`, ns `trishul-ram`)

| Port | Protocol | Service | Purpose |
|---|---|---|---|
| 30001 | TCP | `trishul-ram` (manager) | Manager API + UI — `http://localhost:30001` (host port-forwarded by kind's `extraPortMappings`; `172.19.0.x:30001` also works). API `/api/…`, UI `/ui/` |
| 30002 | TCP | `trishul-ram-worker-ingress` | Worker ingress (webhook/UDP push → workers), targetPort 8767 |

Per-pipeline push services (webhook / syslog / snmp_trap with
`kubernetes.enabled: true`) get their own ephemeral NodePort Services — check
`kubectl get svc -n trishul-ram` during runs.

## Kafka (single-broker KRaft, ns `trishul-ram` — `scripts/perf/infra/kafka.yaml`)

| Port | Protocol | Access | Purpose |
|---|---|---|---|
| 9092 | TCP | in-cluster only (`kafka:9092` / `kafka.trishul-ram.svc:9092`) | PLAINTEXT broker listener — pipelines use this |
| 30094 | TCP | host: `172.19.0.2:30094` (any node IP) | EXTERNAL listener (host-side loadgen) — advertised as `172.19.0.2:30094` |

Topic: `perf-raw`, 3 partitions, RF 1. Verified from the host: produce +
consume 1 message via `kafka-python` (`172.19.0.2:30094`).

## SFTP (`atmoz/sftp`, ns `trishul-ram` — `scripts/perf/infra/sftp.yaml`)

| Port | Protocol | Access | Purpose |
|---|---|---|---|
| 22 | TCP | in-cluster only (`sftp:22` / `sftp.trishul-ram.svc:22`) | SFTP source/sink target for in-cluster pipelines |
| 30022 | TCP | host: `172.19.0.2:30022` (any node IP) | Host-side loadgen / fixture staging |

Credentials: user `perf`, password `perfpw`, home `/home/perf`,
upload dir `/home/perf/upload` (1Gi PVC). Verified from the host with
paramiko: put/get/remove roundtrip.
