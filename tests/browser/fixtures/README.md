# Browser smoke fixtures

Static API response shapes served to the Playwright suite via `page.route`
stubs (no backend involved — the suite boots the built SPA against these
payloads). Endpoint → file mapping lives in `../lib/stub.mjs`.

## Capture

Captured from the live kind dev cluster on **2026-09-22** (release/v1.4.3,
version 1.4.3). Regenerate a full capture with:

```bash
mkdir -p /tmp/api-capture
for ep in config/schema plugins templates meta ready runs runs/count \
           daemon/status stats pipelines cluster/nodes cluster/streams \
           schemas mibs ai/config ai/status health; do
  curl -s "http://localhost:30001/api/$ep" > "/tmp/api-capture/${ep//\//_}.json"
done
```

## Trimming / deviations (deliberate)

The committed fixtures are **trimmed subsets** of the full capture so the
suite stays deterministic, fast, and focused on what the smoke covers:

- `schema.json` / `plugins.json` — trimmed to the plugin set the checks
  drive (kafka/sftp sources, kafka/opensearch sinks, json/csv serializers,
  `rename` transform). The `schema_version` key is the real captured hash
  (`dfcc2f98ad48`); the checks override it when exercising the stale-schema
  guard. Field lists are the real descriptor shapes, kept deterministic.
- `templates.json` — one curated `kafka-to-os` template (the checks seed the
  wizard from `#create?template=kafka-to-os`).
- `runs.json` — two real runs (one success, one failed) with the `errors`
  arrays trimmed; `runs_count.json` keeps the real captured total. Supplemented
  (v1.8.0 states check) with a `partial` run (outcome `partial`, skipped
  records) and an `aborted` run, plus the `outcome` field on every row.
- `cluster_nodes.json` — one admitting worker plus two draining workers
  (one with runs in flight, one fully `drained`) carrying the v1.8.0
  `admission_state` / `drain` block shapes from `/agent/status`.
- `operations.json` — synthetic `lifecycle_operations` rows (v1.8.0 /
  V18-09): trigger/stop/boot_adopt/drain/restart kinds over
  complete/pending/failed states, shaped per `docs/api.md`.
- `checks/states.mjs` layers a `stopping` pipeline and a stopped `manual`
  pipeline on top of the two-row `pipelines.json` per-route (wizard-ai pins
  the shared list at two rows), overrides `POST /api/pipelines/{name}/run`
  to return the 202 receipt (`run_id` + `operation_id`), and pins the
  partial outcome toast via the run monitor.
- `daemon_status.json`, `cluster_nodes.json`, `cluster_streams.json`,
  `pipelines.json`, `stats.json`, `schemas.json`, `mibs.json` — real shapes,
  trimmed to 1-3 representative entries.
- `ai_status.json` — `enabled: false` (the live cluster had `true`). The
  suite deliberately drives the "AI not configured" path: the wizard's AI
  panel only renders when enabled, so disabling it keeps wizard steps
  deterministic and the unconfigured-note assertion meaningful.
- `auth_me.json` — the "auth disabled" shape (the live cluster 401s
  unauthenticated curl; the suite drives the no-auth boot path).
- `meta.json` — contains the released version; regenerate it whenever the
  version under gate changes (the boot check asserts the shell shows this
  version).
- `health.json`, `ai_config.json` — real captured shapes.

The full untrimmed captures are also recorded in the original ad-hoc working
dir (`/tmp/opencode/tram-pw` at capture time).