# Changelog

All notable changes to TRAM are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [1.1.3] — 2026-04-01

### Added

**Test coverage — Tier 1 + Tier 2 unit tests**
- 8 new unit test files covering API routers, auth utilities, and serializers:
  - `test_auth_utils.py` — token create/verify, password hash, `parse_users`, `extract_bearer`
  - `test_api_auth_router.py` — login, `/me`, change-password (all happy + error paths)
  - `test_api_pipelines.py` — pipeline CRUD, lifecycle (start/stop/run), dry-run, alerts CRUD, versions, reload
  - `test_api_connectors_router.py` — `/test` and `/test-pipeline` endpoints, host/port extraction helpers
  - `test_api_stats_router.py` — in-memory stats fallback, pipeline status counts, sparkline buckets
  - `test_api_ai_router.py` — AI status, generate/explain modes, error paths (503/502/400)
  - `test_api_misc_routers.py` — webhooks (404/401/202/503), templates (cache, YAML parse), mibs (list/delete/upload)
  - `test_serializers_text_ndjson.py` — `TextSerializer` and `NdjsonSerializer` parse + serialize + error paths
- **846 tests total** (up from 701); coverage **69%** (up from 63%); threshold 60%

---

## [1.1.2] — 2026-03-30

### Added

**ASN.1 serializer (`type: asn1`)**
- Decodes BER/DER/PER/XER/JER binary files using a user-provided `.asn` schema file — same pattern as the `protobuf` serializer (`schema_file` + `message_class`)
- Encoding selectable via `encoding: ber | der | per | uper | xer | jer` (default: `ber`)
- `schema_file` can point to a single `.asn` file or a directory of `.asn` files (compiled together for cross-file imports)
- `_to_json_safe()` converts `datetime` → ISO 8601 string, ASN.1 CHOICE 2-tuples → `{"type": x, "value": y}`, `bytes` → hex
- Schema compiled once per serializer instance and cached for its lifetime (same pattern as `protobuf`)
- Deserialize only (`serializer_in`) — encode path raises `SerializerError` with a clear message pointing to `serializer_out: type: json`
- `.asn` added to `POST /api/schemas/upload` accepted extensions (displayed as type `asn1` in the schemas list)
- New optional extra: `tram[asn1]` = `asn1tools>=0.167`; included in the standard Docker image
- Reference schema `docs/schemas/3gpp_32401.asn` for Ericsson 3GPP TS 32.401 PM statsfiles (BER, IMPLICIT TAGS); uploadable via UI, works with C\* (core) and G\* (HLR/vHLR) variants

### Fixed

**Pipeline visibility across cluster nodes (API-registered pipelines)**
- `POST /api/pipelines` on any pod now writes the pipeline YAML to a shared `registered_pipelines` table in PostgreSQL; `PUT` updates it; `DELETE` soft-deletes (sets `deleted=1`)
- On startup, after loading pipelines from `TRAM_PIPELINE_DIR` (ConfigMap), the scheduler calls `_load_from_db()` — registers any DB pipeline not already loaded from the filesystem; filesystem wins on name collision
- Background thread `_sync_from_db()` polls the DB every `TRAM_PIPELINE_SYNC_INTERVAL` seconds (default 30): registers newly added pipelines, deregisters soft-deleted ones; all pods converge without restart
- Pipeline registered via API on pod-0 becomes visible on pod-1 and pod-2 within one poll interval; status is consistent (hash-based ownership decides which pod executes it)
- New DB table `registered_pipelines(name, yaml_text, created_at, updated_at, deleted)` — auto-created on startup via existing `_create_tables()` pattern; safe on existing databases
- New config: `TRAM_PIPELINE_SYNC_INTERVAL` (integer seconds, default 30)
- SQLite (standalone, single pod): DB persistence still works, sync loop is effectively a no-op

**Balanced pipeline distribution across cluster nodes**
- Replaced simple `sha1(name) % node_count` ownership formula with rank-based assignment: all pipeline names sorted by stable hash then distributed round-robin (`rank % count == position`); guarantees at most 1 pipeline difference between any two nodes regardless of name hashes
- `rebalance_ownership(all_names)` pre-computes and caches the owned set as a `frozenset` on the coordinator; called on topology change, after startup load, and after each DB sync cycle
- `get_state()` (cluster API endpoint) uses the same rank-based formula so UI pipeline counts match actual ownership

**Reload endpoint now restores DB-registered pipelines**
- `POST /api/pipelines/reload` previously cleared all in-memory pipelines and re-scanned only the filesystem, causing API-registered pipelines to disappear until the next DB sync cycle (up to 30 s)
- Fixed: after filesystem scan, reload now calls `_load_from_db()` so all DB-registered pipelines are immediately available; `total` in the response reflects the combined count

**Cluster page — pipeline counts**
- Node count and total pipeline count added to the cluster status line: `Cluster active · N nodes · M pipelines`
- Each node accordion header now shows a badge with its assigned pipeline count, right-aligned before the expand chevron

---

## [1.1.1] — 2026-03-30

### Added

**Run History — expandable error rows**
- Runs with errors or DLQ records show a chevron (▶) in the detail page Runs table
- Clicking chevron inserts an inline sub-row with per-record error lines in monospace red; toggles closed on second click
- DLQ-only runs show "N record(s) sent to DLQ" when no inline errors; clean runs show no chevron
- Backend: `RunResult.errors: list[str]` field populated from `PipelineRunContext.errors`; persisted as `errors_json TEXT` column in `run_history` (auto-migrated on existing DBs)

**Wizard — complete connector coverage**
- `wizard.js` FIELD_SCHEMA now covers all connector types including `websocket`, `gnmi`, `snmp_poll`, `prometheus_rw`, and `corba`
- `snmp_poll` OID list rendered as a YAML sequence (added `oids` to `ARRAY_FIELDS`)
- All FIELD_SCHEMA and TRANSFORM_FIELDS entries have descriptive hint text

**Wizard — step reorder to match YAML field order**
- Steps: Info (name/schedule/on_error) → Source (type + serializer_in + Test) → Transforms → Sinks (global serializer_out + sink cards) → Review
- `serializer_in` moved to Source step; `on_error` added to Info step with inline descriptions; global `serializer_out` added to top of Sinks step
- `buildYaml` emits `serializer_in` as nested block, `on_error` only when non-default, `serializer_out` before sinks block

**Wizard — UX improvements**
- "New Pipeline" toolbar button replaced with split btn-group: **Wizard** | **YAML** (direct editor)
- Template deploy correctly loads template YAML into editor (fixed `window._editorYaml` propagation)
- YAML diff now uses `reqText()` helper in `api.js` — fixes JSON parse error on raw YAML version fetch
- "Advanced: open blank YAML editor" link properly clears editor state

**Connector Test — full coverage**
- `test_connection()` added to all remaining connectors: `amqp` source+sink (TCP probe), `s3` source+sink (`head_bucket`/`list_buckets`), `gcs` source+sink (`get_bucket`), `azure_blob` source+sink (`get_account_information`), `ves` (HTTP HEAD), `websocket` source+sink (TCP probe), `prometheus_rw` (local listener check), `webhook` (local listener check), `corba` (TCP probe on corbaloc)
- `_extract_host`/`_extract_port` TCP fallback in `connectors.py` now parses `url`/`base_url` fields for URL-based connectors

**Helm: pre-mounted connector key files**
- New `keys` section in `values.yaml`: `secretName` / `mountPath` — pre-mounts a single Kubernetes Secret at `/secrets/` on every pod
- Quickstart commands documented inline (create, rotate, reference in pipeline YAML)
- `docs/roadmap_1.2.0.md`: key upload API added to roadmap

### Fixed
- **Pipeline status on startup (cluster mode)**: non-owning nodes now set `status="scheduled"` for interval/cron pipelines instead of leaving them stuck at `"stopped"`; `_rebalance` release also sets `"scheduled"` instead of `"stopped"`
- **SPA routing**: `router.init()` called unconditionally before `checkAuth()` — hashchange listener always registered
- **Templates modal**: button uses `data-bs-toggle/data-bs-target` (not `new bootstrap.Modal().show()`) — fixes silent failure in Vite ESM context; server accepts both `sink:` (singular) and `sinks:` (list) in dry-run
- **Templates view button**: inline YAML preview panel (view-switcher) inside modal — no nested Bootstrap modal
- **Scheduled badge**: `.badge-scheduled` CSS (yellow) + dot color added to `style.css`
- **Password change**: Settings page shows Change Password card when logged in
- **Settings layout**: 3-column grid (col-4 each), no max-width cap
- **Pipeline export**: download YAML button (↓) added to Actions column
- **Detail page tabs**: isolated tab panel rendering — fixes DOM corruption when switching Runs/Versions/Config tabs
- **YAML diff modal**: `bootstrap is not defined` in `detail.js` — added `import * as bootstrap from 'bootstrap'` (Vite ESM modules don't share `window.bootstrap` reliably)
- **Version history table**: Diff and Rollback buttons now show text labels alongside icons
- **Enrich transform missing file**: `_load_lookup()` warns and returns empty dict instead of raising `TransformError` — allows dry-run to succeed for pipelines with runtime-resolved lookup paths
- **All 20 bundled pipeline templates pass dry-run**: validate rules format (`field: required` → `{required: true}`), empty defaults (`${VAR:-}` → named placeholder), `seconds:` → `interval_seconds:`, `add_field` format fixes, `:-placeholder` defaults for bare `${VAR}` env vars
- **`test_connection` port defaults**: syslog=514, snmp_trap=1162; REST connector uses 443/80 based on scheme; sftp/ftp/snmp_poll/gnmi use connector-specific defaults
- **Helm fsGroup + key file permissions**: `securityContext.fsGroup: 1000` on pod spec; `defaultMode: 0440` on keys Secret volume → `root:tram` ownership, readable by tram user without world-read

---

## [1.1.0] — 2026-03-29

### Added

**Pipeline Wizard**
- 5-step UI wizard (Name → Source → Transforms → Sinks → Review) for creating pipelines without writing YAML
- Client-side YAML assembly from wizard state; final step shows live preview and sends to Editor or saves directly
- Accessible from Pipelines page toolbar via "+ New Pipeline" button (Bootstrap modal)
- Server validates both `sink:` (singular) and `sinks:` (list) in template dry-run

**Live Metrics Dashboard**
- `GET /api/stats` — per-pipeline aggregated stats (records in/out, error rate, avg duration) for the last hour
- Dashboard page extended with 10-second polling metrics table and Canvas sparkline graphs per pipeline
- Dialect-aware SQL aggregation: `EXTRACT(EPOCH ...)` PostgreSQL, `TIMESTAMPDIFF` MySQL, `julianday` SQLite

**Alert Rules UI**
- Alert rules CRUD in Pipeline Detail page: `GET/POST/PUT/DELETE /api/pipelines/{name}/alerts`
- YAML mutation approach: rules written back into pipeline YAML config and persisted
- Alert modal uses `import * as bootstrap from 'bootstrap'` (not `window.bootstrap`) for Vite ESM compatibility

**Connector Test**
- `POST /api/connectors/test` — test connectivity for a connector config; TCP fallback for unknown connector types
- `POST /api/connectors/test-pipeline` — test all source and sink connectors in a pipeline YAML
- `ConnectorTestMixin` base class in `tram/core/base.py`; all connectors with network access implement `test_connection()`

**Pipeline Templates**
- `GET /api/templates` — returns list of bundled pipeline YAML templates from `pipelines/` directory
- Templates tab in Pipelines page: browse, preview, and load any template into the Editor
- View YAML inline in modal (no nested Bootstrap modal)
- 20 bundled templates covering SFTP, Kafka, REST, SNMP, Syslog, S3, OpenSearch, InfluxDB, ClickHouse, Protobuf, multi-format fanout, webhook alarm, and more

**AI Assist**
- `POST /api/ai/suggest` — `mode=generate` (create pipeline from description) or `mode=explain` (explain existing YAML)
- `GET /api/ai/status` — returns configured provider/model and whether AI is available
- Configurable via env: `TRAM_AI_API_KEY`, `TRAM_AI_PROVIDER` (openai/anthropic), `TRAM_AI_MODEL`, `TRAM_AI_BASE_URL`
- Editor page "AI Assist" button with textarea for prompt; result inserted into editor

**Password Change**
- `POST /api/auth/change-password` — changes password for authenticated user; stored in `user_passwords` DB table (sha256+salt hash)
- `user_passwords` table: `(username, password_hash, updated_at)`; upsert dialect-aware (SQLite/PostgreSQL vs MySQL)
- Settings page shows "Change Password" card when logged in

**Helm: pre-mounted connector keys**
- `keys.secretName` / `keys.mountPath` in `values.yaml` — pre-mounts a single Kubernetes Secret at `/secrets/` on every pod
- `securityContext.fsGroup: 1000` + `defaultMode: 0440` — key files are `root:tram` owned, readable by tram user without world-read
- Zero-restart key rotation: updating Secret contents propagates via kubelet (~60s); adding a new Secret mount requires rolling restart
- Quickstart docs in `values.yaml` `keys:` section

### Changed
- `helm/Chart.yaml`: version → `1.1.0`; `pyproject.toml`: version → `1.1.0`
- Settings page layout: 3-column grid (col-4 each) with no max-width cap
- Pipeline export: download YAML button (↓) added to Actions column in pipelines list
- Scheduled badge: `.badge-scheduled` (yellow) CSS added to `style.css`
- `tram-ui/src/pages/detail.js`: SPA router always registers hashchange listener unconditionally; Templates button moved to Pipelines page toolbar

---

## [1.0.9] — 2026-03-25

### Added

**Shared RWX storage for schemas and MIBs (cluster mode)**
- New `sharedStorage` Helm section: single `ReadWriteMany` PVC (`tram-shared`) mounted at `/shared` on every pod
- `TRAM_SCHEMA_DIR` and `TRAM_MIB_DIR` auto-pointed to `/shared/schemas` and `/shared/mibs` when `sharedStorage.enabled=true`
- Schemas/MIBs uploaded via the UI are now instantly visible to all replicas — no session pinning required
- `helm/kind/nfs-provisioner.yaml`: deploys [kubernetes-sigs NFS Ganesha server + external provisioner](https://github.com/kubernetes-sigs/nfs-ganesha-server-and-external-provisioner) (`registry.k8s.io/sig-storage/nfs-provisioner:v4.0.8`) in kind clusters; creates StorageClass `nfs-rwx`
- Supported RWX storage classes documented in `values.yaml`: `nfs-rwx` (kind), `efs-sc` (AWS), `azurefile` (Azure), `filestore-rwx` (GKE), `longhorn-rwx`

### Changed
- `persistence.enabled` defaults to `false` in cluster-mode `values.yaml` — per-pod `/data` PVCs are unnecessary when PostgreSQL + `sharedStorage` are both active
- Removed `sessionAffinity: ClientIP` workaround from Service (was pinning browsers to a single pod to paper over per-pod schema visibility; no longer needed)

---

## [1.0.8] — 2026-03-25

### Added

**Browser user authentication**
- `TRAM_AUTH_USERS` env var: comma-separated `username:password` pairs for UI login
- `tram/api/auth.py`: HMAC-SHA256 session tokens (8-hour TTL, invalidated on restart)
- `POST /api/auth/login` — returns `{"token": "...", "username": "..."}` on valid credentials
- `GET /api/auth/me` — returns current user from Bearer token (401 if unauthenticated)
- `APIKeyMiddleware` extended: accepts both `X-API-Key` (machine clients) and `Bearer` token (browser users); `/api/auth/login` added to exempt set
- Login overlay in tram-ui: full-screen login page shown when `TRAM_AUTH_USERS` is configured; 8-hour token stored in `localStorage`; logout button in topbar
- `helm/values.yaml`: new `authUsers` key (injected as `TRAM_AUTH_USERS`); recommended to use `envSecret` for production

**Multi-file upload (schemas & MIBs)**
- Schema and MIB upload zones now accept `multiple` files; uploads proceed sequentially with per-file progress hints
- Drop zone text updated to "Drop files here"

**Dashboard shortcuts**
- "Manage →" and "+ New" buttons on the Active Pipelines card navigate directly to the Pipelines and Editor pages

**Settings — restore base URL**
- Reset button (↺) next to the base URL input restores to `window.location.origin` (same-origin default)
- Removed duplicate "Reload Pipelines" button from Settings (already available on the Pipelines page)

**PostgreSQL subchart (Helm)**
- Bitnami PostgreSQL added as optional dependency (`postgresql.enabled=true` in `values.yaml`)
- When enabled, `TRAM_DB_URL` is auto-wired as `postgresql+psycopg2://<user>:<pass>@<release>-postgresql/<db>`; no manual `TRAM_DB_URL` needed
- `values.yaml`: `postgresql.auth` (username/password/database) and `postgresql.primary.persistence.size`
- Combined with `replicaCount>1` + `clusterMode.enabled=true` for a fully self-contained HA cluster

**Sample pipeline on install**
- `values.yaml` ships with a `sample-health` pipeline (interval 60 s, no-op source, writes status field to `/tmp/tram-sample`) so a fresh install has a visible running pipeline immediately

### Changed
- `pyproject.toml`, `helm/Chart.yaml`: version → `1.0.8`
- `tram-ui/package.json`, `index.html`: version badge → `v1.0.8`
- `helm/values.yaml`: `replicaCount: 3`, `clusterMode.enabled: true`, `postgresql.enabled: true` (kind dev-cluster deployment defaults)
- `helm/values-template.yaml`: new clean-defaults reference file (ClusterIP, replicaCount:1, postgresql:false, `OWNER/tram` placeholder)
- `tram/core/config.py`: added `auth_users` field

---

## [1.0.7] — 2026-03-24

### Added

**`tram-ui` — Bootstrap 5 web UI**
- New `tram-ui/` Vite + Vanilla JS project: fully self-contained frontend (no CDN), suitable for Docker embedding
- Bootstrap 5.3 dark theme base; all custom styles in `src/style.css` via CSS custom properties for full dark/light mode support
- Hash-based SPA router (`#dashboard`, `#pipelines`, `#runs`, etc.) with lazy page `init()` loading
- **Dashboard**: stat cards (total/running/errors/records-out), Active Pipelines table with inline stop/play actions, Recent Runs table
- **Pipelines**: live table with search + status/type filters, per-row start/stop/run/edit/delete, Reload from disk
- **Run History**: filtered by pipeline/status/date, expandable error rows, CSV export
- **Pipeline Detail**: summary cards (source/sinks/schedule/transforms/error policy), run history with filters, Runs/Versions/Config tab switching, version rollback
- **Pipeline Editor**: YAML editor with Tab-key indent, `new-pipeline.yaml` template for new pipelines, loads existing YAML for edits, Dry Run with inline result panel, Save (create/update)
- **Schemas**: schema file list, drag-and-drop upload zone with subdirectory support, per-row delete
- **MIB Modules**: compiled MIB list, drag-and-drop `.mib` upload, bulk download from mibs.pysnmp.com, per-row delete
- **Cluster**: accordion node list from `/api/daemon/status`, online/offline status dots, pipeline assignment per node
- **Plugins**: accordion with Sources 24 / Sinks 20 / Serializers 10 / Transforms 20
- **Settings**: connection form (base URL, API key, poll interval), Save/Test Connection, Daemon Status table, Reload Pipelines
- **Health poller**: 10s interval, sidebar dot + topbar hover card show daemon online/offline state, version, scheduler, DB status
- **Dark/light mode toggle** persisted in `localStorage`; all custom CSS uses CSS variables with full light-mode palette
- Shared `utils.js`: `relTime`, `fmtDur`, `fmtNum`, `statusBadge`, `schedBadge`, `esc`, `toast`
- Full TRAM REST API client in `src/api.js` (pipelines, runs, schemas, MIBs, daemon, health, meta, plugins)
- Build: `npm run build` → self-contained `dist/` (~82 KB gzipped total)

**Image — UI embedded in daemon**
- Multi-stage Dockerfile: new `ui-builder` stage (`node:20-alpine`) runs `npm ci && npm run build`; built `dist/` copied to `/ui` in runtime stage
- FastAPI mounts `StaticFiles` at `/ui` when `TRAM_UI_DIR` points to a valid directory; `GET /` redirects to `/ui/`
- `/ui/*` and `/` exempt from API key authentication — static assets are public
- `TRAM_UI_DIR=/ui` default env var; set to empty string to disable UI serving

**Helm — dedicated UI Service**
- New `helm/templates/service-ui.yaml` — `Service` named `{release}-ui` targeting the same pod port 8765 via a dedicated `ClusterIP:80` (or `NodePort`/`LoadBalancer`) when `ui.enabled=true`
- `values.yaml`: new `ui:` section — `enabled`, `port`, `serviceType`, `nodePort`, `serviceAnnotations`
- `statefulset.yaml`: injects `TRAM_UI_DIR=""` when `ui.enabled=false` to suppress static serving
- `NOTES.txt`: prints UI port-forward command when `ui.enabled=true`

### Changed
- `pyproject.toml`, `helm/Chart.yaml`: version → `1.0.7`
- `tram/api/middleware.py`: `EXEMPT_PREFIX` extended to cover `/ui` and `/` (root redirect)

---

## [1.0.6] — 2026-03-13

### Added
- `LICENSE` file (Apache-2.0 full text) added to repository root
- Helm `service.snmpTrapPorts` (list) replaces the former single `service.snmpTrapPort` scalar — iterate any number of UDP ports for multi-source SNMP trap deployments; each port creates one Service UDP port and one container port; adding/removing ports requires `helm upgrade`
- docker-compose SNMP trap port driven by `TRAM_SNMP_PORT_1` env var (defaulting to `1162`); additional ports can be added as numbered vars and entries in the `ports:` section

### Changed
- `pyproject.toml`: classifier `"Development Status :: 3 - Alpha"` → `"Development Status :: 5 - Production/Stable"`; added `"License :: OSI Approved :: Apache Software License"` classifier; `license` field changed from inline `{text = "Apache-2.0"}` to `{file = "LICENSE"}`
- Helm `Chart.yaml` / `values.yaml` image tag → `1.0.6`

---

## [1.0.5] — 2026-03-13

### Added

**`ndjson` serializer**
- `@register_serializer("ndjson")` — Newline-Delimited JSON (JSON Lines); each non-empty line is parsed as a JSON object
- Arrays flattened into the record stream; scalars wrapped in `{"_value": ...}` unless `strict: true`
- `strict: bool = False` — raises `SerializerError` on non-object lines when enabled
- `ensure_ascii`, `newline` config keys match the `json` serializer for consistency
- Covers Kafka consumer output, Filebeat/Fluentd/Vector JSON output, jq streams, and any source that produces one JSON object per line rather than a wrapped array
- `NdjsonSerializerConfig` in `tram/models/pipeline.py`

**Per-sink `serializer_out` override**
- Each sink config (`SFTPSinkConfig`, `LocalSinkConfig`, `KafkaSinkConfig`, … all 20) gains an optional `serializer_out: Optional[SerializerConfig] = None` field
- When set, that sink uses its own serializer instead of the global `serializer_out`
- Enables multi-format fan-out from a single pipeline: Avro→Kafka + JSON→local + CSV→SFTP
- Example:
  ```yaml
  serializer_out:          # global default
    type: json

  sinks:
    - type: kafka
      topic: pm-avro
      serializer_out:      # per-sink override
        type: avro
        schema_file: /schemas/pm.avsc
    - type: local
      path: /data/output   # inherits global → json
    - type: sftp
      host: archive.example.com
      serializer_out:
        type: csv
  ```
- `_build_sinks()` now returns a 5-tuple `(sink_instance, condition, transforms, sink_cfg, per_sink_ser|None)`
- `_write_one_sink()` resolves: per-sink serializer → global serializer
- Forward-reference resolved with `model_rebuild()` for all sink config classes (Pydantic v2 pattern)

**`serializer_out` optional at pipeline level**
- `PipelineConfig.serializer_out` changed from required to `Optional[SerializerConfig] = None`
- `None` → defaults to `JsonSerializer({})` at runtime in `_build_serializer_out()`
- Pipelines that write JSON (the vast majority) no longer need to declare `serializer_out:`

### Changed
- `tram/models/pipeline.py`: serializer section now has `NdjsonSerializerConfig`; `SerializerConfig` union extended; `_SINK_CONFIG_CLASSES` + `model_rebuild()` block added after union definition
- `tram/pipeline/executor.py`: `_build_sinks()` returns 5-tuple; `_write_one_sink()` handles 3/4/5-tuples; `_build_serializer_out()` handles `None` config
- Helm `values.yaml` / `Chart.yaml` / `image.tag` → `1.0.5`

---

## [1.0.4] — 2026-03-13

### Added

**Schema Registry consolidation**
- `TRAM_SCHEMA_REGISTRY_URL` env var is now a server-level default for both the schema registry proxy (`/api/schemas/registry/*`) and the Avro/Protobuf serializer clients — no need to repeat the URL in every pipeline YAML
- `TRAM_SCHEMA_REGISTRY_USERNAME` / `TRAM_SCHEMA_REGISTRY_PASSWORD` env vars — server-level auth defaults for registry serializers; pipeline YAML fields (`schema_registry_username`, `schema_registry_password`) act as per-pipeline overrides
- `AppConfig`: three new fields — `schema_registry_url`, `schema_registry_username`, `schema_registry_password` (all from env)
- `AvroSerializer` and `ProtobufSerializer`: `registry_url` now resolves from `config.get("schema_registry_url") or os.environ.get("TRAM_SCHEMA_REGISTRY_URL")`; same fallback for `registry_username` / `registry_password`; credentials forwarded to `SchemaRegistryClient`

**Schema Registry proxy**
- `GET/POST/PUT/DELETE /api/schemas/registry/{path}` — transparent reverse proxy to `TRAM_SCHEMA_REGISTRY_URL`; proxies all headers and query params; returns 503 when env var is not set
- Route registered before the `/{filepath:path}` catch-all so it resolves correctly

**Pipeline management**
- `PUT /api/pipelines/{name}` — update/replace a registered pipeline's YAML config in-place (stops → re-registers → restarts if enabled)

**ClickHouse connector**
- `@register_source("clickhouse")` — query ClickHouse using `clickhouse-driver`; configurable `query`, `database`, chunked via `chunk_size`
- `@register_sink("clickhouse")` — insert records into a ClickHouse table; `insert_block_size` batching
- `ClickHouseSourceConfig` / `ClickHouseSinkConfig` in `tram/models/pipeline.py`
- New optional extra: `pip install tram[clickhouse]` (`clickhouse-driver>=0.2`)
- Registered in `tram/connectors/__init__.py`

**REST connector fix (httpx 0.28)**
- `tram/connectors/rest/source.py` + `sink.py`: `verify_ssl` moved from per-request `kwargs` to the `httpx.Client(verify=...)` constructor — resolves `TypeError: Client.request() got an unexpected keyword argument 'verify'` introduced by httpx 0.28

**Example pipelines**
- `pipelines/all-transforms-test.yaml` — exercises all 20 transform types in a single webhook pipeline; documents cross-record transform behaviour in stream mode
- `pipelines/csv-ingest.yaml` — CSV serializer validation via webhook
- `pipelines/xml-ingest.yaml` — XML serializer (defusedxml) validation via webhook
- `pipelines/rest-pipeline.yaml` — REST source (poll) + REST sink (POST) end-to-end
- `pipelines/rest-echo-receiver.yaml` — companion webhook receiver for REST sink loop
- `pipelines/proto-device-event.yaml` — multi-file Protobuf schema: `device_event.proto` imports `severity.proto`, `location.proto`, `interface_stats.proto`, `identity.proto`; all compiled in one `protoc` invocation

### Changed
- `docker-compose.yml`: `TRAM_SCHEMA_REGISTRY_URL: ${TRAM_SCHEMA_REGISTRY_URL:-}` env var wired in; `1162:1162/udp` SNMP trap port exposed
- Helm `values.yaml` / `Chart.yaml` / `image.tag` → `1.0.4`

---

## [1.0.3] — 2026-03-09

### Added

**SNMP MIB management**
- `TRAM_MIB_DIR` env var (default `/mibs`) — global MIB directory; SNMP source/sink connectors auto-prepend it to `mib_dirs` at startup so OID resolution works without per-pipeline config
- `AppConfig.mib_dir` field
- `tram mib download <NAMES...> --out <dir>` — new CLI command; downloads and compiles MIB modules from `mibs.pysnmp.com` using `pysmi-lextudio` (requires `tram[mib]`)
- `tram mib compile` enhanced: now accepts a **directory** in addition to a single file; all `.mib` files in the directory are compiled in one pass so cross-file imports resolve correctly
- MIB management REST API:
  - `GET /api/mibs` — list compiled MIB modules in `TRAM_MIB_DIR`
  - `POST /api/mibs/upload` — upload a raw `.mib` file and compile it (requires `tram[mib]`)
  - `POST /api/mibs/download` — `{"names": [...]}` download+compile from `mibs.pysnmp.com` (requires `tram[mib]`)
  - `DELETE /api/mibs/{name}` — delete a compiled MIB module
- Dockerfile: **three-stage build** — new `mib-builder` stage downloads + compiles `IF-MIB`, `ENTITY-MIB`, `HOST-RESOURCES-MIB`, `IP-MIB`, `TCP-MIB`, `UDP-MIB`, `IANAifType-MIB` from `mibs.pysnmp.com` at build time; compiled `.py` files copied to runtime image; MIB download failures are non-fatal (empty `/mibs` on air-gapped builds)
- Helm: `mibPersistence` section — optional `volumeClaimTemplate` at `/mibs` for persisting runtime-downloaded MIBs across pod restarts

**Schema file management**
- `TRAM_SCHEMA_DIR` env var (default `/schemas`) — global schema directory for serialization schemas
- `AppConfig.schema_dir` field
- Schema management REST API:
  - `GET /api/schemas` — list all schema files under `TRAM_SCHEMA_DIR` recursively; returns `path`, `type`, `size_bytes`, `schema_file` (paste-ready for pipeline YAML)
  - `GET /api/schemas/{filepath}` — read a schema file's raw text content
  - `POST /api/schemas/upload?subdir=<dir>` — upload a `.proto`, `.avsc`, `.json`, `.xsd`, `.yaml`, or `.yml` file; optional `subdir` for multi-file proto packages; atomic write (`.tmp` → rename)
  - `DELETE /api/schemas/{filepath}` — delete a schema file
- Path-traversal protection on all schema endpoints (`_safe_join` with `os.path.normpath`)
- Dockerfile: `/schemas` directory created at build time, `ENV TRAM_SCHEMA_DIR=/schemas` set
- Helm: `schemaPersistence` section — optional `volumeClaimTemplate` at `/schemas` so schemas uploaded via the API survive pod restarts

**Protobuf serializer improvements**
- `framing: none` mode — each file is a single raw serialized proto message (no 4-byte length prefix); required for Cisco EMS PM binary files
- Multi-file proto compile fix: `_compile_proto()` now compiles **all** `.proto` files in the same directory in one `protoc` invocation so import statements resolve correctly at Python import time
- `ProtobufSerializerConfig`: new `framing: Literal["length_delimited", "none"]` field (default `"length_delimited"`)
- Example pipeline: `pipelines/cisco_pm_proto_to_json.yaml` — SFTP binary PM files → protobuf decode → `_pm_type` detection → JSON output on SFTP

**Dependency**
- `python-multipart>=0.0.9` added to core dependencies (required for `UploadFile` in MIB/schema upload endpoints)
- `mib` extra (`pysmi-lextudio`) now included in the default Docker image

### Changed
- Dockerfile: `pip install "${whl}[metrics,postgresql,mysql,snmp,mib]"` — `mib` added to default installed extras; connector extras (`kafka`, `s3`, `avro`, `protobuf_ser`, etc.) remain opt-in via a custom `FROM tram:1.0.3` layer
- Helm `values.yaml` / `Chart.yaml` / `image.tag` → `1.0.3`

### Fixed
- `APIKeyMiddleware`: `AppConfig.from_env()` moved from `dispatch()` to `__init__()` — config is now cached once at startup instead of re-read on every request
- `RateLimitMiddleware._windows`: periodic eviction of idle client entries when dict exceeds 500 keys — prevents unbounded memory growth in long-running daemons
- `tram/core/config.py`: all bare `int()` env var reads replaced with `_env_int()` helper — raises `ValueError` with the variable name on invalid input instead of a cryptic Python traceback
- CI (`ci.yml`): removed dead `develop` branch trigger; added `--cov-fail-under=75` coverage gate to unit test step
- Release (`release.yml`): added `test` job (ruff + unit + integration) that must pass before Docker image is built and pushed
- `docker-compose.yml`: `TRAM_DB_PATH` replaced with `TRAM_DB_URL: sqlite:////data/tram.db`

---

## [1.0.2] — 2026-03-06

### Added

**SNMPv3 USM support**
- New `build_v3_auth()` helper in `tram/connectors/snmp/mib_utils.py`: builds a pysnmp `UsmUserData` object from human-readable config; security level auto-detected (noAuthNoPriv / authNoPriv / authPriv)
- Auth protocols: MD5, SHA (default), SHA224, SHA256, SHA384, SHA512
- Privacy protocols: DES, 3DES, AES / AES128 (default), AES192, AES256; unknown strings fall back gracefully to SHA / AES128
- **`snmp_poll` source** (`SNMPPollSource`): `version: "3"` now issues GET/WALK with `UsmUserData` instead of `CommunityData`; `ContextData(contextName=...)` passed when `context_name` is set
- **`snmp_trap` sink** (`SNMPTrapSink`): `version: "3"` sends traps with `UsmUserData`
- **`snmp_trap` source** (`SNMPTrapSource`): v3 config fields accepted and stored; trap *decoding* is best-effort (falls back to raw hex for encrypted v3 packets — full USM receive engine planned)
- New v3 config fields on `SnmpPollSourceConfig`, `SnmpTrapSourceConfig`, `SnmpTrapSinkConfig`: `security_name`, `auth_protocol`, `auth_key`, `priv_protocol`, `priv_key`, `context_name`

---

## [1.0.1] — 2026-03-06

### Added

**SNMP Poll enhancements**
- `_polled_at` (UTC ISO8601) injected into every SNMP poll record payload and `meta` dict — timestamp reflects the moment the poll was issued
- `yield_rows: bool = False` on `SnmpPollSourceConfig`: when `true`, yields one record per table row instead of one flat dict for the entire WALK result
- `index_depth: int = 0` on `SnmpPollSourceConfig`: controls how the row index is extracted from WALK keys — `0` = auto (split on first dot, correct for MIB-resolved names such as `ifDescr.1`); `>0` = last N OID components form the index (for numeric OIDs or composite indexes)
- Each per-row record carries `_index` (dot-separated compound index string, e.g. `"1.192.168.1.1"`) and `_index_parts` (list of strings, e.g. `["1","192","168","1","1"]`) for downstream parsing

### Changed

**Build / versioning**
- `tram/__init__.py`: `__version__` now read from installed package metadata via `importlib.metadata.version("tram")` — `pyproject.toml` is the single source of truth; fallback to `"0.0.0-dev"` when running from an uninstalled source tree
- `release.yml`: tag push (`v*`) now automatically patches `pyproject.toml`, `helm/Chart.yaml` (both `version` and `appVersion`), and `helm/values.yaml` (`image.tag`) in the ephemeral CI workspace before building — no manual version edits required for future releases

---

## [1.0.0] — 2026-03-06

### Added

**Security**
- `APIKeyMiddleware`: protect all `/api/*` endpoints with `X-API-Key` header or `?api_key=` query param; `TRAM_API_KEY` env var (empty = auth disabled); health/metrics/webhooks paths always exempt
- `RateLimitMiddleware`: sliding-window per-IP rate limiting for `/api/*`; `TRAM_RATE_LIMIT` (req/min, 0 = disabled), `TRAM_RATE_LIMIT_WINDOW` (seconds, default 60)
- TLS support: set `TRAM_TLS_CERTFILE` + `TRAM_TLS_KEYFILE` to enable HTTPS via uvicorn `ssl_*` params
- Helm: `apiKey` and `tls` sections in `values.yaml`; TLS secret volume mount + env vars in StatefulSet

**Reliability**
- Per-sink retry: `retry_count` (int, default 0) and `retry_delay_seconds` (float, default 1.0) on all 19 sink configs; exponential back-off with jitter; DLQ still receives record after all retries exhausted
- Parallel sinks: `PipelineConfig.parallel_sinks: bool = False`; fans out to all sinks concurrently via `ThreadPoolExecutor` when true
- Circuit breaker: `circuit_breaker_threshold` (int, default 0 = disabled) on all sink configs; skips sink for 60s after N consecutive failures; resets on success
- Kafka reconnect: `reconnect_delay_seconds`, `max_reconnect_attempts` on `KafkaSourceConfig`; outer reconnect loop in `stream_run`
- NATS reconnect: `max_reconnect_attempts`, `reconnect_time_wait` passed to `nats.connect()`
- Chunked reads: `read_chunk_bytes` on `SFTPSourceConfig` and `S3SourceConfig`; yields file in N-byte chunks

**SNMP MIB Integration**
- New `tram/connectors/snmp/mib_utils.py`: `build_mib_view()`, `resolve_oid()`, `symbolic_to_oid()`, `oid_str_to_tuple()`, `get_mib_view()` (cached)
- `SnmpPollConfig` + `SnmpTrapSourceConfig`: `mib_dirs`, `mib_modules`, `resolve_oids` fields; OIDs resolved to symbolic names in output records
- `SnmpTrapSinkConfig`: `varbinds: list[VarbindConfig]` for explicit OID/type/field mapping; `symbolic_to_oid()` resolves IF-MIB-style names
- New `VarbindConfig` model: `oid`, `value_field`, `type`
- `tram mib compile <source.mib> --out <dir>`: CLI command to compile raw MIB files (requires `tram[mib]`)
- New optional extra: `tram[mib]` = `pysmi-lextudio>=1.1`

**Observability**
- OpenTelemetry tracing: `tram/telemetry/tracing.py`; `init_tracing()` + `get_tracer()`; `TRAM_OTEL_ENDPOINT` + `TRAM_OTEL_SERVICE` env vars; no-op fallback when SDK not installed; `batch_run()` wrapped in `"batch_run"` span
- Kafka lag metric: `tram_kafka_consumer_lag{pipeline,topic,partition}` Gauge updated after each message poll
- Stream queue depth metric: `tram_stream_queue_depth{pipeline}` Gauge updated in threaded stream mode
- Run history CSV export: `GET /api/runs?format=csv` returns `text/csv` via `StreamingResponse`
- Enhanced readiness: `GET /api/ready` body now includes `db`, `scheduler`, `cluster` fields; returns 503 if DB or scheduler unavailable
- New optional extra: `tram[otel]` = `opentelemetry-sdk>=1.20, opentelemetry-exporter-otlp-proto-grpc>=1.20`

**Operations / DX**
- Pipeline file watcher: `tram/watcher/pipeline_watcher.py`; `TRAM_WATCH_PIPELINES=true` watches `TRAM_PIPELINE_DIR` for YAML changes using watchdog; auto-reloads on create/modify, deregisters on delete
- Pipeline linter: `tram/pipeline/linter.py`; five rules: L001 (source+no sinks), L002 (skip+no DLQ), L003 (stream+workers>1), L004 (batch_size on stream), L005 (email alert+no SMTP); integrated into `tram validate`
- `tram pipeline init <name>`: scaffolds a minimal pipeline YAML to stdout or file
- New optional extra: `tram[watch]` = `watchdog>=3.0`

### Changed
- `tram/api/app.py`: version `"1.0.0"`, middleware registration, OTel init, pipeline watcher in lifespan
- `tram/cli/main.py`: all API calls inject `X-API-Key` header when `TRAM_API_KEY` is set; `validate` calls linter
- `helm/Chart.yaml`, `helm/values.yaml`: version 1.0.0

---

## [0.9.0] — 2026-03-05

### Added

**`thread_workers` — intra-node parallelism**
- `PipelineConfig.thread_workers: int = 1` — number of worker threads per pipeline run
- `batch_run()`: when `thread_workers > 1`, chunks from the source are submitted to a
  `ThreadPoolExecutor(max_workers=thread_workers)` so N chunks process concurrently; single-
  threaded code path unchanged for `thread_workers=1`
- `stream_run()`: when `thread_workers > 1`, a bounded `Queue(maxsize=thread_workers * 2)`
  decouples the source producer from N worker threads, providing natural backpressure
- `PipelineRunContext` is now fully thread-safe: all counter mutations go through
  `threading.Lock`-protected helper methods (`inc_records_in`, `inc_records_out`,
  `inc_records_skipped`, `record_error`, `record_dlq`)

**`batch_size` — record cap per run**
- `PipelineConfig.batch_size: Optional[int] = None` — limits records processed per batch run
- Source read loop breaks once `ctx.records_in >= batch_size`; remaining source chunks skipped
- Works in both single-threaded and multi-threaded modes
- Useful for controlling run duration on large sources (Kafka backlog, large S3 buckets)

**`on_error: "dlq"` — explicit DLQ routing**
- `on_error` Literal extended with `"dlq"` value
- Model validator raises `ValueError` if `on_error="dlq"` is set without a `dlq` sink configured
- Runtime behavior identical to `on_error="continue"` with DLQ sink present — makes intent explicit

**Processed-file tracking**
- New DB table: `processed_files (pipeline_name, source_key, filepath, processed_at)` — PRIMARY KEY on all three name fields; indexed on `(pipeline_name, source_key)` for fast lookup
- `TramDB.is_processed(pipeline, source_key, filepath) -> bool`
- `TramDB.mark_processed(pipeline, source_key, filepath)` — dialect-aware upsert; errors logged and swallowed
- `ProcessedFileTracker` wrapper in `tram/persistence/file_tracker.py` — silences DB errors, safe for use in connectors
- `skip_processed: bool = False` added to `SFTPSourceConfig`, `LocalSourceConfig`, `S3SourceConfig`, `FtpSourceConfig`, `GcsSourceConfig`, `AzureBlobSourceConfig`
- Source connectors check `is_processed` before reading and call `mark_processed` after successful yield + `_post_read`
- `PipelineExecutor._build_source()` injects `_file_tracker` into source config dict when `file_tracker` is present on the executor
- `TramScheduler` and `create_app()` wired to create and pass `ProcessedFileTracker` when DB is available

**CORBA source connector**
- `@register_source("corba")` — DII (Dynamic Invocation Interface) mode; no pre-compiled IDL stubs required
- Supports: direct IOR (`ior:`) or NamingService resolution (`naming_service:` + `object_name:`)
- `operation:` names the CORBA operation; `args:` passes positional scalar arguments via DII
- Result normalised to `list[dict]` via `_corba_to_python()` (handles structs, nested sequences)
- `skip_processed: bool` supported via `ProcessedFileTracker` — invocation key = `operation:args_json`
- `pip install tram[corba]` (pulls `omniORBpy>=4.3`)
- `CorbaSourceConfig` in Pydantic models with `model_validator` requiring `ior` or `naming_service`
- Plugin key: `corba`

**Helm: ConfigMap checksum annotation**
- `checksum/config` annotation added to the StatefulSet pod template (when `pipelines` values are non-empty)
- Value: `sha256sum` of the rendered `configmap.yaml` — changes when any pipeline YAML changes
- Kubernetes detects the pod spec diff and triggers a rolling restart automatically on `helm upgrade`

**Tests** — 62 new tests (`test_thread_workers.py` ×13, `test_batch_size_on_error.py` ×10,
`test_processed_files.py` ×15, `test_corba_connector.py` ×24); **535 total, all passing**

### Changed
- `PipelineExecutor.__init__` gains `file_tracker: ProcessedFileTracker | None = None`
- `TramScheduler.__init__` gains `file_tracker: ProcessedFileTracker | None = None`
- `executor._build_source()` injects both `_pipeline_name` and `_file_tracker` into source config
- `tram/__init__.__version__` → `"0.9.0"`

---

## [0.8.1] — 2026-03-05

### Fixed

**Kafka consumer group isolation**
- `KafkaSourceConfig.group_id` default changed from `"tram"` (shared across every pipeline) to
  `None` — resolved at runtime to the pipeline name, giving each pipeline its own consumer group
- Pipelines that set `group_id:` explicitly in YAML are unaffected
- Added explicit `consumer.commit()` before `consumer.close()` — best-effort offset flush on clean
  shutdown (supplements `enable_auto_commit=True` timer; no-ops on abrupt kill)
- Fallback chain: explicit `group_id` → pipeline name → `"tram"` (if no pipeline name available)

**NATS queue group for cluster mode**
- `NatsSourceConfig.queue_group` default changed from `""` (broadcast — all cluster nodes receive
  every message) to `None` — resolved at runtime to the pipeline name (competing consumers, correct
  for cluster mode where the same pipeline runs on all nodes)
- `queue_group: ""` in YAML still works as an explicit broadcast opt-out
- Fallback chain: explicit `queue_group` (including `""`) → pipeline name → `""` (broadcast)

**Pipeline name injection**
- `PipelineExecutor._build_source()` now injects `_pipeline_name` into the source config dict;
  connectors can use `config.get("_pipeline_name")` as a safe default for group/queue identifiers

**Helm chart**
- `helm/values.yaml` `image.tag` corrected from `"0.6.0"` to `"0.8.1"`

**Tests** — 20 new tests (`test_kafka_connectors.py` ×16, `test_nats_connectors.py` ×5 new);
**473 total, all passing**

---

## [0.8.0] — 2026-03-05

### Added

**StatefulSet self-organizing cluster**
- `tram/cluster/registry.py` — `NodeRegistry`: registers the local node in the shared DB, runs a
  periodic heartbeat thread, expires stale peers (`status='dead'`), deregisters on clean shutdown
- `tram/cluster/coordinator.py` — `ClusterCoordinator`: caches live node topology, determines
  pipeline ownership via consistent hashing: `sha1(pipeline_name) % live_node_count == my_position`
- Ownership uses **sorted position** in live node list (not static ordinal) — handles non-sequential
  ordinals gracefully when a node fails (tram-0, tram-2 become positions 0 and 1)
- Safe fallback: if no live nodes in DB (startup race), the node owns all pipelines
- `detect_ordinal(node_id)` helper: extracts ordinal suffix from StatefulSet hostname (`tram-2` → `2`)

**DB: node_registry table**
- `node_registry` table: `node_id, ordinal, registered_at, last_heartbeat, status`
- New `TramDB` methods: `register_node()` (dialect-aware upsert), `heartbeat()`, `expire_nodes()`,
  `get_live_nodes()`, `deregister_node()`
- Cluster mode requires an external DB (`TRAM_DB_URL`); SQLite is blocked with a warning

**Cluster env vars (AppConfig)**
- `TRAM_CLUSTER_ENABLED` — enable cluster mode (default: `false`)
- `TRAM_NODE_ORDINAL` — override ordinal (default: auto-detected from hostname)
- `TRAM_HEARTBEAT_SECONDS` — heartbeat interval in seconds (default: `10`)
- `TRAM_NODE_TTL_SECONDS` — seconds before a silent node is marked dead (default: `30`)

**Scheduler: dynamic rebalance**
- `TramScheduler` gains `coordinator` and `rebalance_interval` parameters
- Ownership check in `_schedule_pipeline()` — nodes skip pipelines they don't own
- Background `tram-rebalance` thread: polls `coordinator.refresh()` every N seconds; on topology
  change calls `_rebalance()` which starts newly owned pipelines and stops released ones

**Cluster API endpoint**
- `GET /api/cluster/nodes` — returns `cluster_enabled`, `node_id`, `my_position`,
  `live_node_count`, `nodes` list; returns `{"cluster_enabled": false}` in standalone mode

**Helm: always-StatefulSet design**
- `helm/templates/statefulset.yaml` — always rendered; `replicaCount=1` standalone, `N` cluster
- `helm/templates/headless-service.yaml` — always rendered; headless Service for stable pod DNS
- `deployment.yaml` and `pvc.yaml` removed — replaced by `volumeClaimTemplates` in StatefulSet
- `volumeClaimTemplates` auto-provisions `data-tram-N` PVC per pod — survives pod restarts and
  rescheduling; PVC stays bound to the same pod across node reschedules
- `helm/values.yaml` — `clusterMode.enabled: false` controls `TRAM_CLUSTER_ENABLED` env var
- `helm/Chart.yaml` — version bumped to `0.8.0`

**Tests** — 22 new tests (`test_cluster.py`); **453 total, all passing**

### Changed
- `TramScheduler.__init__` gains optional `coordinator: ClusterCoordinator | None` and
  `rebalance_interval: int` parameters (backward compatible — defaults to standalone behaviour)
- `tram/api/app.py` wires `NodeRegistry` + `ClusterCoordinator` from `AppConfig` in lifespan
- `tram/__init__.__version__` → `"0.8.0"`

---

## [0.7.0] — 2026-03-05

### Added

**SQLAlchemy Core DB abstraction**
- `tram/persistence/db.py` rewritten on SQLAlchemy Core — any backend supported via `TRAM_DB_URL`
- SQLite (default), PostgreSQL (`tram[postgresql]`), MySQL/MariaDB (`tram[mysql]`) all work out of the box
- `TRAM_DB_URL` env var (SQLAlchemy URL); falls back to `TRAM_DB_PATH` → SQLite when unset
- Connection pooling (`pool_size=5`, `max_overflow=10`, `pool_pre_ping=True`) for non-SQLite backends
- `sqlalchemy>=2.0` added to core dependencies (was previously in `[sql]` optional only)
- New optional extras: `postgresql = ["psycopg2-binary>=2.9"]`, `mysql = ["PyMySQL>=1.1"]`

**Node identity**
- `AppConfig.node_id` — from `TRAM_NODE_ID` env (default: `socket.gethostname()`)
- `node_id` stored in every `run_history` row — essential for multi-node cluster debugging
- `TramDB(url, node_id)` constructor; node_id auto-stamped on every `save_run()`

**`dlq_count` persisted**
- `RunResult.dlq_count: int = 0` field added; `from_context()` carries it from `PipelineRunContext`
- `to_dict()` now includes `dlq_count`
- `dlq_count` column added to `run_history` table
- `tram_dlq_total` Prometheus counter (`pipeline` label) incremented on every DLQ write

**Graceful shutdown**
- `TramScheduler.stop(timeout: int = 30)` — signals all stream threads, waits for in-flight batch runs via `ThreadPoolExecutor.shutdown(wait=True)`, joins stream threads with timeout
- `TRAM_SHUTDOWN_TIMEOUT_SECONDS` env var (default `30`) wired through `AppConfig` and `lifespan`
- SIGTERM handler in `daemon/server.py` converts SIGTERM → SIGINT so uvicorn gets a clean shutdown (critical for Docker / Kubernetes PID 1)

**Readiness DB check**
- `TramDB.health_check()` executes `SELECT 1`; returns `True/False`
- `GET /api/ready` returns `503` when DB is configured but unreachable

**Run history pagination**
- `GET /api/runs` gains `offset` and `from_dt` query params
- `TramDB.get_runs(offset, from_dt)` — `OFFSET` clause + `started_at >=` filter
- `PipelineManager.get_runs()` and in-memory fallback both support new params
- `TramDB.get_run(run_id)` now queries DB directly (previously only searched in-memory deque)

**Schema migration**
- `_create_tables()` is idempotent: `CREATE TABLE IF NOT EXISTS` + `_add_column_if_missing()` helper
- Existing v0.6.0 SQLite databases upgraded automatically on first start (adds `node_id`, `dlq_count` to `run_history`)

**Tests** — 25 new tests (`test_db_v07.py` ×15, `test_config_v07.py` ×6, `test_runresult_v07.py` ×4); **431 total, all passing**

### Changed
- `TramDB.__init__` signature: `path: Path` → `url: str = "", node_id: str = ""` (uses SQLAlchemy URL)
- `pipeline_versions.id` now TEXT UUID (generated in Python); fresh databases get UUID ids; existing SQLite databases keep their integer ids (SQLite flexible typing)
- `AppConfig` gains `node_id`, `db_url`, `shutdown_timeout` fields (from env: `TRAM_NODE_ID`, `TRAM_DB_URL`, `TRAM_SHUTDOWN_TIMEOUT_SECONDS`)

---

## [0.6.0] — 2026-03-05

### Added

**Dead-Letter Queue (DLQ)**
- `PipelineConfig.dlq: Optional[SinkConfig]` — any sink type can serve as DLQ; receives failed records as JSON envelopes
- Envelope schema: `{_error, _stage, _pipeline, _run_id, _timestamp, record, raw}` where `raw` (base64) is only present for parse-stage failures
- Three failure stages captured: `parse` (serializer_in failed), `transform` (global or per-sink transform raised), `sink` (sink.write() raised)
- Per-record transform isolation: global transforms applied record-by-record; a single bad record no longer aborts the entire chunk
- DLQ write errors are logged and swallowed — never propagate to main pipeline
- `PipelineRunContext.dlq_count` tracks how many records were DLQ'd in a run

**Per-Sink Transform Chains**
- Each sink config gains `transforms: list[TransformConfig]` (default empty)
- Applied **after** global pipeline transforms and **after** condition filtering, **before** serializing for that specific sink
- Sink transforms are independent: different sinks can reshape the same records differently
- Sink transform failures route to DLQ (if configured) and skip that sink; other sinks continue
- `_build_sinks()` now returns `list[tuple[BaseSink, condition, list[BaseTransform]]]`

**Alert Rules**
- `AlertRuleConfig` model: `condition` (simpleeval), `action` (webhook|email), `webhook_url`, `email_to`, `subject`, `cooldown_seconds` (default 300)
- `PipelineConfig.alerts: list[AlertRuleConfig]`
- `AlertEvaluator` in `tram/alerts/evaluator.py` — evaluated after every batch run
- Alert condition namespace: `records_in`, `records_out`, `records_skipped`, `error_rate`, `status`, `failed`, `duration_seconds`
- Cooldown persisted in new SQLite `alert_state` table — survives daemon restarts
- Webhook action: `httpx.POST` with full run payload; email action: `smtplib` STARTTLS
- SMTP configured via env vars: `TRAM_SMTP_HOST/PORT/USER/PASS/TLS/FROM`
- All action errors logged and swallowed
- `PipelineManager` accepts `alert_evaluator: AlertEvaluator | None`; `AlertEvaluator(db=db)` instantiated in `create_app()`

**Helm Chart** (`helm/`)
- `Chart.yaml` — apiVersion v2, version 0.6.0
- `values.yaml` — image, replicaCount (fixed at 1), service, persistence (SQLite PVC), env, envSecret, pipelines ConfigMap, resources, nodeSelector, tolerations, affinity, podAnnotations, serviceAccount
- Templates: `statefulset.yaml`, `service.yaml`, `headless-service.yaml`, `configmap.yaml`, `serviceaccount.yaml`, `_helpers.tpl`, `NOTES.txt`
- Storage managed via `volumeClaimTemplates` (introduced in v0.8.0; v0.6.0 used `deployment.yaml` + `pvc.yaml`)

**GitHub Actions**
- `.github/workflows/ci.yml` — triggers on push to `main`/`develop` and all PRs; runs ruff + pytest on Python 3.11 and 3.12
- `.github/workflows/release.yml` — triggers on `v*` tags; builds multi-arch Docker image (linux/amd64 + linux/arm64) → `ghcr.io/{owner}/tram:{semver}`; packages + pushes Helm chart → `oci://ghcr.io/{owner}/charts/tram`

**SQLite**
- New `alert_state` table: `(pipeline_name, rule_name, last_alerted_at)` primary key
- `TramDB.get_alert_cooldown()` / `set_alert_cooldown()` methods

**Tests** — 35 new tests (test_dlq.py ×11, test_sink_transforms.py ×8, test_alerts.py ×16); **406 total, all passing**

### Changed
- `tram/models/pipeline.py` — Transforms section moved before Sinks section to avoid Pydantic v2 forward-reference issues with `list[TransformConfig]` on sink classes
- `_build_sinks()` return type widened to 3-tuple `(BaseSink, condition | None, list[BaseTransform])`

---

## [0.5.0] — 2026-03-03

### Added

**Conditional Multi-Sink Routing**
- `sinks: list[SinkConfig]` replaces `sink: SinkConfig` (backward compat: singular `sink:` auto-wrapped by model_validator)
- Per-sink `condition: Optional[str]` — simpleeval expression evaluated per record; sink is skipped if no records match
- Catch-all sink (no condition) receives all records
- `rate_limit_rps: Optional[float]` on `PipelineConfig` — token-bucket rate limiter across all sink writes

**SQLite Persistence** (`tram/persistence/db.py`)
- `TramDB` wraps `sqlite3`; DB at `~/.tram/tram.db` (or `$TRAM_DB_PATH`)
- Tables: `run_history` (persists `RunResult`), `pipeline_versions` (auto-saved on register)
- `PipelineManager` accepts `db: TramDB | None`; `record_run()` persists to SQLite; `get_runs()` queries SQLite
- API: `GET /api/pipelines/{name}/versions`, `POST /api/pipelines/{name}/rollback?version=N`
- CLI: `tram pipeline history <name>`, `tram pipeline rollback <name> --version N`

**Prometheus Metrics** (`tram/metrics/registry.py`)
- Counters: `tram_records_in_total`, `tram_records_out_total`, `tram_records_skipped_total`, `tram_errors_total` (labeled by `pipeline`)
- Histogram: `tram_chunk_duration_seconds`
- All metrics are no-ops when `prometheus_client` is not installed
- `GET /metrics` endpoint (503 if not installed)
- New optional extra: `pip install tram[metrics]`

**Webhook Source** (`tram/connectors/webhook/source.py`)
- `@register_source("webhook")` — receives HTTP POSTs forwarded from `/webhooks/{path}` on the daemon port
- Module-level `_WEBHOOK_REGISTRY` bridges FastAPI router → source generator
- Optional `secret` for `Authorization: Bearer` validation
- New API router: `POST /webhooks/{path}` → 202 Accepted / 404 / 401

**WebSocket Connector** (`tram/connectors/websocket/`)
- `@register_source("websocket")` — background thread + asyncio loop + SimpleQueue bridge; auto-reconnect
- `@register_sink("websocket")` — `asyncio.run()` connect/send/close per write
- Optional dep: `websockets>=12.0`; new extra `pip install tram[websocket]`

**Elasticsearch Connector** (`tram/connectors/elasticsearch/`)
- `@register_source("elasticsearch")` — search + scroll API
- `@register_sink("elasticsearch")` — `helpers.bulk()` with `index_template` token substitution
- Optional dep: `elasticsearch>=8.0`; new extra `pip install tram[elasticsearch]`

**Prometheus Remote-Write Source** (`tram/connectors/prometheus_rw/source.py`)
- `@register_source("prometheus_rw")` — Snappy-decompress + protobuf `WriteRequest` → `list[dict]`
- Reuses WebhookSource global registry (path-routed via daemon)
- Optional dep: `protobuf>=4.25`, `python-snappy>=0.7`; new extra `pip install tram[prometheus_rw]`

**Schema Registry** (`tram/schema_registry/client.py`)
- `SchemaRegistryClient` — Confluent-compatible REST API (also Apicurio); in-memory cache by schema_id
- `encode_with_magic(schema_id, payload)` / `decode_magic(data)` — Confluent magic-byte `\x00` + 4-byte BE ID framing
- Avro serializer gains `schema_registry_url/subject/id` + `use_magic_bytes` config
- Protobuf serializer gains same registry config

**New Pydantic Models**
- Sources: `WebhookSourceConfig`, `WebSocketSourceConfig`, `ElasticsearchSourceConfig`, `PrometheusRWSourceConfig`
- Sinks: `WebSocketSinkConfig`, `ElasticsearchSinkConfig`
- Serializers: `AvroSerializerConfig` and `ProtobufSerializerConfig` extended with registry fields

**Tests** — 49 new tests; **371 total, all passing**

---

## [0.4.0] — 2026-03-03

### Added

**New Serializers**
- `avro` — fastavro read/write; requires `pip install tram[avro]`
- `parquet` — pyarrow read/write; requires `pip install tram[parquet]`
- `msgpack` — msgpack pack/unpack; requires `pip install tram[msgpack_ser]`
- `protobuf` — runtime .proto compilation via grpcio-tools; length-delimited framing; requires `pip install tram[protobuf_ser]`

**New Source Connectors**
- `mqtt` — paho-mqtt subscriber; TLS support; reconnect on drop
- `amqp` — pika consumer; prefetch, auto-ack configurable
- `nats` — nats-py subscriber; queue groups; credentials file
- `gnmi` — pygnmi subscription (telemetry streaming)
- `sql` — SQLAlchemy; chunked reads
- `influxdb` — influxdb-client Flux query
- `redis` — list LPOP or stream XREAD modes
- `gcs` — google-cloud-storage; blob listing + streaming
- `azure_blob` — azure-storage-blob; container listing + streaming

**New Sink Connectors**
- `amqp` — pika publisher to exchange/routing-key
- `nats` — nats-py publisher
- `sql` — SQLAlchemy insert/upsert
- `influxdb` — line-protocol write
- `redis` — list RPUSH, pubsub PUBLISH, or stream XADD
- `gcs` — google-cloud-storage blob upload
- `azure_blob` — azure-storage-blob upload

**New Transforms**
- `explode` — expand a list field into multiple rows
- `deduplicate` — remove duplicate rows by key fields
- `regex_extract` — extract named capture groups from a string field
- `template` — render Jinja-style `{field}` string templates
- `mask` — redact, hash, or partial-mask sensitive fields
- `validate` — schema validation with `on_invalid: drop|raise`
- `sort` — sort records by field list
- `limit` — keep only first N records
- `jmespath` — JMESPath field extraction
- `unnest` — lift a nested dict field to top level

**Tests** — 322 total, all passing

---

## [0.3.0] — 2026-03-03

### Added

**New Connectors**
- `ftp` source + sink — ftplib; move/delete after read; passive mode
- `s3` source + sink — boto3; endpoint_url override for S3-compatible stores
- `syslog` source — UDP/TCP listener; RFC 3164/5424 parsing
- `snmp_trap` source + sink — pysnmp trap receiver / sender
- `snmp_poll` source — GET/WALK OID polling
- `ves` sink — ONAP VES event batch sender; auth types: none/basic/bearer
- `opensearch` source (scroll) added alongside existing sink

**Tests** — 198 total, all passing

---

## [0.2.0] — 2026-03-03

### Added

**New Transforms**
- `flatten` — recursive dict flattening with configurable `separator`, `max_depth`, and `prefix`
- `timestamp_normalize` — normalizes heterogeneous timestamps to UTC ISO-8601
- `aggregate` — groupby + sum/avg/min/max/count/first/last
- `enrich` — left-join records with a static CSV or JSON lookup file

**New Connectors**
- `local` source + sink — reads/writes local filesystem files
- `rest` source + sink — HTTP polling source and POST/PUT sink (httpx)
- `kafka` source + sink — KafkaConsumer/Producer; SASL/SSL support
- `opensearch` sink — bulk-indexes records via opensearch-py

**Tests** — 124 total, all passing

---

## [0.1.0] — 2026-03-03

### Added

**Core**
- `tram.core.exceptions` — `TramError` hierarchy
- `tram.core.context` — `PipelineRunContext` + `RunResult` + `RunStatus`
- `tram.core.config` — `AppConfig` from environment variables
- `tram.core.log_config` — JSON-structured logging

**Plugin Interfaces** — `BaseSource`, `BaseSink`, `BaseTransform`, `BaseSerializer`

**Plugin Registry** — `@register_*` decorators + `get_*()` lookups + `list_plugins()`

**Pydantic Models** — `PipelineConfig` with discriminated unions; `ScheduleConfig`

**Serializers** — `json`, `csv`, `xml`

**Transforms** — `rename`, `cast`, `add_field`, `drop`, `value_map`, `filter`

**Connectors** — `sftp` source + sink

**Pipeline Engine** — `loader.py`, `executor.py` (batch/stream/dry-run), `manager.py`

**Scheduler** — `TramScheduler` (APScheduler batch + threads stream)

**REST API** — FastAPI on port 8765; health, pipelines, runs, daemon endpoints

**CLI** — Typer; direct + daemon-proxy commands

**Tests** — 69 total, all passing
