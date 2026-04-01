# TRAM v1.2.0 Roadmap

Items discussed after completing v1.1.1. Priority TBD.

---

## Backend

- **Pipeline cloning** — copy pipeline as new with name prompt
- **Per-sink record counts** — run-level metrics broken down by sink
- **Scheduled alert evaluation** — cron-based alert checks, not just post-run
- **Dead-letter queue viewer** — browse and replay DLQ records via the UI

## UI / UX

- **Pipeline search/filter** — filter pipelines list by name, status, type
- **Bulk actions** — enable/disable/delete multiple pipelines at once
- **Live log streaming** — WebSocket tail for running stream pipelines
- **Dark/light theme toggle**

## Cluster / Operations

- **Node health detail page** — per-node pipeline assignments and load
- **Graceful drain** — mark node as draining, rebalance ownership before shutdown
- **Pipeline dependency graph** — visualize chain when pipeline A feeds pipeline B

## Integrations / Connectors

- ~~**ClickHouse sink — batch buffering**~~ — _implemented in v1.1.2_
- **SMTP sink** — outbound email delivery
- **gRPC sink** — generic gRPC unary call sink
- **Syslog sink** — forward records to remote syslog server (RFC 5424)
- **Kafka schema registry** — Avro with schema ID framing (Confluent wire format)

## Testing / Quality

- **Test coverage to 75%** — Tier 3 unit tests for scheduler, pipeline executor, persistence DB, pipeline watcher, and health/runs/metrics API routers; currently at 69% (threshold: 60%)

## Security

- **Role-based access** — read-only vs admin token scopes
- **Per-pipeline API key scoping** — restrict a key to specific pipelines
- **Key upload API** — `POST /api/keys/upload`, `GET /api/keys`, `DELETE /api/keys/<name>`;
  stores files on the shared RWX PVC under `/data/keys/`; UI shows a dropdown of uploaded keys
  alongside the free-text `private_key_path` field in connector forms.
  Complements the Helm `keys.secretName` pattern for self-service / non-k8s environments.
