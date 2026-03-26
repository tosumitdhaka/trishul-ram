# Feature 6 — Pipeline Templates Library

## Goal

A browsable gallery of pre-built pipeline templates that can be deployed with one click.
TRAM ships 20+ production-ready YAML examples in `pipelines/` — this feature surfaces
them in the UI so operators can pick a starting point instead of writing from scratch.

## Current Templates (pipelines/ directory)

| File | Source | Sink | Schedule |
|------|--------|------|----------|
| `kafka-to-opensearch.yaml` | kafka | opensearch | stream |
| `sftp-pm-to-kafka.yaml` | sftp | kafka | interval |
| `snmp-trap-receiver.yaml` | snmp_trap | local | stream |
| `snmp-poll-ifmib-to-influxdb.yaml` | snmp_poll | influxdb | interval |
| `syslog-to-opensearch.yaml` | syslog | opensearch | stream |
| `rest-nms-to-sql.yaml` | rest | sql | interval |
| `rest-echo-receiver.yaml` | rest | local | stream |
| `webhook-alarm-fanout.yaml` | webhook | rest | stream |
| `cisco_pm_proto_to_json.yaml` | sftp | local | interval |
| `csv-ingest.yaml` | local | local | interval |
| `xml-ingest.yaml` | local | local | interval |
| `multi-format-fanout.yaml` | sftp | kafka, local | interval |
| `proto-device-event.yaml` | kafka | local | stream |
| `example_sftp_to_sftp.yaml` | sftp | sftp | interval |
| `sftp-read.yaml` | sftp | local | interval |
| `rest-pipeline.yaml` | rest | local | interval |
| `minimal.yaml` | local | local | manual |
| `all-transforms-test.yaml` | local | local | manual |

## New API Endpoint

```
GET /api/templates
```

Response:
```json
[
  {
    "id": "kafka-to-opensearch",
    "name": "Kafka → OpenSearch",
    "description": "Consume JSON records from Kafka and index into OpenSearch",
    "tags": ["kafka", "opensearch", "streaming"],
    "source_type": "kafka",
    "sink_types": ["opensearch"],
    "schedule_type": "stream",
    "yaml": "pipeline:\n  name: kafka-to-opensearch\n  ..."
  },
  ...
]
```

## Backend

New file: `tram/api/routers/templates.py`

- Reads `pipelines/` directory path (configurable via `TRAM_PIPELINES_DIR` env,
  already used by the pipeline loader)
- Parses each YAML file to extract `source.type`, `sinks[].type`, `schedule.type`
- Description and tags from optional header comments in the YAML file:
  ```yaml
  # description: Consume JSON records from Kafka and index into OpenSearch
  # tags: kafka, opensearch, streaming
  pipeline:
    name: kafka-to-opensearch
  ```
  If no `# description:` comment, derive from filename (replace `-` with spaces, title-case).
  If no `# tags:` comment, derive tags from source/sink types and schedule type.
- Result cached for 60 s (parse on first request, invalidate after 60 s)

## UI

New files:
- `tram-ui/src/pages/templates.html`
- `tram-ui/src/pages/templates.js`

Nav link: "Templates" under the Pipelines section.

### Card Grid Layout

Each template rendered as a card:
```
┌──────────────────────────────────┐
│  kafka → opensearch              │
│  stream                          │
│  ────────────────────────────    │
│  Consume JSON records from       │
│  Kafka and index into OpenSearch │
│                                  │
│  [kafka] [opensearch]            │
│                    [Preview] [Deploy] │
└──────────────────────────────────┘
```

Filter bar above the grid:
- Source type dropdown (All | kafka | sftp | rest | snmp_poll | ...)
- Sink type dropdown (All | opensearch | kafka | sql | ...)
- Schedule type toggle (All | Stream | Interval | Cron | Manual)
- Search box (filters by name/description)

### Deploy Flow

"Deploy" button:
1. Opens editor with YAML pre-filled
2. User edits pipeline name (required — must be unique) and connection details
3. User clicks Save in the editor → `POST /api/pipelines`

### Preview

"Preview" button opens a modal with the YAML in a read-only syntax-highlighted code block.

## Files Changed

| File | Change |
|------|--------|
| `tram/api/routers/templates.py` | New |
| `tram/api/app.py` | Register templates router |
| `tram-ui/src/pages/templates.html` | New |
| `tram-ui/src/pages/templates.js` | New |
| `tram-ui/src/api.js` | Add `api.templates.list()` |
| `tram-ui/index.html` | Add "Templates" nav link |
| `pipelines/*.yaml` | Add `# description:` / `# tags:` headers |
