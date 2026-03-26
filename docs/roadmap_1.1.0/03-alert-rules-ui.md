# Feature 3 — Alert Rules UI

## Goal

View, create, edit, and delete alert rules for a pipeline from the UI.
The backend engine and model already exist — this feature surfaces them visually.

## Current State

- `AlertRuleConfig` Pydantic model in `tram/models/pipeline.py`:
  `name`, `condition`, `action` (webhook/email), `webhook_url`, `email_to`,
  `subject`, `cooldown_seconds`
- `AlertEvaluator.check()` fires after every `manager.record_run()`
- Rules defined inline in pipeline YAML under `alerts:` list
- No API to read/write alert rules without editing full pipeline YAML

## New API Endpoints

All endpoints mounted under `/api/pipelines/{name}/alerts` in `tram/api/routers/pipelines.py`.

```
GET    /api/pipelines/{name}/alerts
→ list of {index, name, condition, action, webhook_url, email_to, subject, cooldown_seconds}

POST   /api/pipelines/{name}/alerts
Body:  {condition, action, webhook_url?, email_to?, name?, subject?, cooldown_seconds?}
→ 201 with created rule

PUT    /api/pipelines/{name}/alerts/{idx}
Body:  full rule object
→ 200 with updated rule

DELETE /api/pipelines/{name}/alerts/{idx}
→ 204
```

Implementation pattern: read pipeline's current YAML → parse → mutate `alerts` list →
re-save via existing `PUT /api/pipelines/{name}` path (saving a new version automatically).
No new DB tables required.

## Condition Variables

Available in `condition` expressions (evaluated by `simpleeval`):

| Variable | Type | Example value |
|----------|------|---------------|
| `records_in` | int | 1024 |
| `records_out` | int | 1022 |
| `duration_s` | float | 2.4 |
| `error_count` | int | 3 |
| `status` | str | `'error'` |
| `dlq_count` | int | 1 |

Example conditions:
```
records_out == 0
error_count > 0
duration_s > 30
status == 'error'
records_in > 0 and records_out == 0
```

## UI

New "Alerts" tab on the pipeline detail page (`detail.html` / `detail.js`),
alongside the existing Overview / Runs / Config / Versions tabs.

**Tab content:**

Table columns: Name | Condition | Action | Cooldown | [Edit] [Delete]

"Add Rule" button opens a modal:
```
Name (optional)          [___________________]
Condition expression     [___________________]
  ℹ available variables: records_in, records_out, duration_s, error_count, status
Action                   ( ) Webhook  ( ) Email
Webhook URL / Email to   [___________________]
Subject template         [TRAM Alert: {pipeline}]
Cooldown (seconds)       [300]
                         [Cancel]  [Save Rule]
```

Edit: same modal pre-filled with existing rule values.
Delete: inline confirmation ("Delete rule 'high-error-rate'?").

## Files Changed

| File | Change |
|------|--------|
| `tram/api/routers/pipelines.py` | Add 4 alert sub-routes |
| `tram-ui/src/pages/detail.html` | Add Alerts tab |
| `tram-ui/src/pages/detail.js` | Alerts tab logic, add/edit/delete modal |
| `tram-ui/src/api.js` | Add `api.alerts.list/create/update/delete` |
