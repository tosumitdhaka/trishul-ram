# TRAM v1.1.0 Roadmap

**Theme**: Operational Visibility & Day-2 Productivity
**Target**: v1.1.0 (follows v1.0.9)

## Features

| # | Feature | Doc | Complexity | New API endpoints |
|---|---------|-----|------------|-------------------|
| 1 | [Pipeline Setup Wizard](./01-wizard.md) | [01-wizard.md](./01-wizard.md) | High | `POST /api/ai/suggest` (optional) |
| 2 | [Live Metrics on Dashboard](./02-live-metrics.md) | [02-live-metrics.md](./02-live-metrics.md) | Medium | `GET /api/stats` |
| 3 | [Alert Rules UI](./03-alert-rules-ui.md) | [03-alert-rules-ui.md](./03-alert-rules-ui.md) | Medium | 4 alert sub-routes |
| 4 | [YAML Diff Viewer](./04-yaml-diff-viewer.md) | [04-yaml-diff-viewer.md](./04-yaml-diff-viewer.md) | Low | — |
| 5 | [Connector Connectivity Test](./05-connector-test.md) | [05-connector-test.md](./05-connector-test.md) | Medium | `POST /api/connectors/test` |
| 6 | [Pipeline Templates Library](./06-templates.md) | [06-templates.md](./06-templates.md) | Low | `GET /api/templates` |
| 7 | [Pipeline Import / Export](./07-import-export.md) | [07-import-export.md](./07-import-export.md) | Low | — |

## Build Order

Items 1–3 below can be built in parallel (independent, no shared state):

```
Phase 1 (parallel)
  ├── 6. Templates Library   — read-only, pure UI + one endpoint
  ├── 7. Import / Export     — zero new backend code
  └── 4. YAML Diff Viewer    — client-side only

Phase 2 (sequential)
  └── 2. Live Metrics        — new /api/stats + dashboard polling

Phase 3 (sequential, builds on phase 2)
  └── 3. Alert Rules UI      — new alert sub-routes + detail tab

Phase 4 (sequential, builds on alert rules)
  └── 5. Connectivity Test   — new /api/connectors/test + plugin interface

Phase 5 (sequential, builds on connectivity test)
  └── 1. Wizard              — most complex; assembles YAML, uses connectivity test
```

## Constraints

- No breaking changes to existing API
- No pipeline YAML schema changes (alert rules already supported in 1.0.x)
- No new DB columns
- No new required env vars or Helm chart values
- No new npm dependencies (charts via Canvas API; diff algorithm in plain JS)
- AI assist (`POST /api/ai/suggest`) requires `TRAM_AI_API_KEY` env var; silently
  disabled when absent — all other wizard functionality unaffected
- AI provider selectable via `TRAM_AI_PROVIDER` (`anthropic` or `openai`) and
  `TRAM_AI_MODEL`; custom `TRAM_AI_BASE_URL` enables any OpenAI-compatible gateway
  (Rakuten AI, Ollama, Azure OpenAI, vLLM, etc.)
- `anthropic` / `openai` packages are optional extras: `pip install tram[ai-anthropic]`
  or `tram[ai-openai]`
