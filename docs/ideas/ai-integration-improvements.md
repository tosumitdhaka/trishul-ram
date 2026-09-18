# AI Integration Improvements — Proposals & Ranked Ideas

**Date:** 2026-09-17
**Companion to:** `docs/reviews/ai-support-review.md` (inventory, bugs, security findings for the *existing* AI surface). This document covers (a) concrete improvements to existing features and (b) new integration points, each grounded in a specific code surface.

**Standing assumption:** the blocking-execution and secret-redaction fixes from the review ([BUG-1]/[SEC-1]) are prerequisites — every idea below sends more operational data through the same `_call_ai` path (`tram/api/routers/ai.py:65–160`), so redaction and a non-blocking call should land first.

---

## Part A — Improvements to existing features

### Quick wins

**A1. Un-block the event loop + explicit timeouts** — effort: XS (~10 lines)
`ai_suggest`/`ai_test` are `async def` but call the synchronous SDKs and `urllib` directly (`ai.py:224–316`), stalling all API traffic for the duration of one LLM call (no timeout set at `ai.py:81, 107`; Bedrock already uses 60 s at `ai.py:147`). Wrap `_call_ai` in `asyncio.to_thread` and pass `timeout=` to both client constructors. Nothing else in the feature changes.

**A2. Stop the Settings key-wipe** — effort: XS
`settings.js:80–86` always posts `api_key: ""` when the (never-prefilled, `settings.html:50–52`) password field is blank, and `ai_save_config` deletes on empty (`ai.py:216–220`). Omit blank fields from the payload (or treat absent ≠ empty server-side). This is a silent production breakage for anyone with a DB-stored key.

**A3. Validate model YAML before returning it** — effort: S
After `_strip_fences` in generate/fix/modify (`ai.py:268, 300, 316`), run `yaml.safe_load` + `load_pipeline_from_yaml` (already used by the dry-run endpoint, `tram/api/routers/pipelines.py:46–49`) and return `{"yaml": …, "valid": bool, "issues": […]}`. The editor can then warn immediately instead of the user discovering truncation at dry-run. Also check `stop_reason`/`finish_reason` for truncation (`max_tokens=1024` at `ai.py:265, 297, 313` is tight for multi-sink pipelines).

**A4. Secret redaction in explain/fix/modify** — effort: S–M
The metadata to do this already exists: `SCHEMA_FIELDS[category][type][field]["secret"]` is computed from field names containing password/token/secret (`tram/api/config_schema.py:160`). Parse the incoming YAML server-side, mask secret fields (keep `${VAR}` references intact — the loader substitutes them at runtime, `tram/pipeline/loader.py:18–23`), and send the redacted copy to the provider. The operator's pipeline on disk is untouched; only the outbound prompt changes. Removes the biggest AI security exposure ([SEC-1] in the review).

**A5. Revert path for AI actions in the editor** — effort: S
Generate (editor.js:176), modify (editor.js:200), and fix (editor.js:423) all overwrite the textarea with no undo. Snapshot the pre-AI text before each AI write and offer "Undo AI change" next to the status message until the user types or saves. The modify path already auto-opens a diff (editor.js:203–204); extend that to generate/fix too, so the model's changes are always visually diffed before save.

**A6. Template-grounded generation (few-shot from the template library)** — effort: S
`GET /api/templates` (`tram/api/routers/templates.py:75–84`) already serves fully worked pipeline YAMLs with tags (source/sink/schedule, templates.py:44–57). In `generate` mode, pick the template whose `source_type`/`sink_types` best match the prompt and include it in the system prompt as a worked example alongside the schema context from `build_ai_context` (`ai_docs.py:42–120`). This attacks the actual weakness of generation today: the model knows field *names* but not idiomatic *usage* (condition strings, oid lists, filename templates). No new endpoints; one function in ai_docs.py.

**A7. Fix iteration loop for `fix` mode** — effort: S
Currently one shot: YAML + error in, YAML out (`ai.py:286–300`). Make the server iterate: call `_call_ai`, validate with `load_pipeline_from_yaml`, and if invalid, feed the new error back for at most one retry before returning the best attempt plus the validation issues. The dry-run feedback the UI already renders (editor.js:280–293) then shows real errors rather than model hallucinations.

**A8. Docs/Helm sync + label fix** — effort: XS
- `docs/api.md:598–642` documents the wrong response shapes (`available` vs `enabled`, `result` vs `yaml`/`explanation`) and omits `fix`/`modify`/config/test endpoints.
- `helm/values.yaml` documents ~25 `TRAM_*` vars but no `TRAM_AI_*` (see its env block around L160–216); add them, plus an `envSecret` example for the key.
- `settings.html:60` says Base URL is "(OpenAI-compatible only)" but it is honored for Anthropic (`ai.py:78–80`) and required for Bedrock (`ai.py:133–134`).
- Validate the provider string in `ai_save_config` (`ai.py:210–221`) instead of failing later with a 502.

### Larger efforts

**A9. Streaming output** — effort: M
Pipeline YAML generation at `max_tokens=1024` takes tens of seconds with the textarea frozen and only a "Generating…" label (editor.js:171). Stream via SSE or chunked JSON from `suggest` (both SDKs support streaming; the Bedrock urllib proxy would need chunk handling or can stay non-streaming). The UI already has a pattern for progressive DOM updates. Value: perceived latency and fewer timeout-driven double-clicks (which today each block the event loop — see A1).

**A10. Audit trail for AI actions** — effort: M
ai.py contains no logging at all — no record of who invoked which mode with what payload size or token cost. Add: (a) a per-call log line (mode, user, model, tokens when the SDK reports them), and (b) optionally a `ai_usage` table or entries in the existing settings-style KV store (`tram/persistence/db.py:784–807` shows the pattern). Also the natural place to record "AI Fix applied and saved" against a pipeline version — the versions surface (`tram/api/routers/pipelines.py:446–459`) would then show which config versions were AI-authored.

**A11. Base URL allowlist + scheme enforcement** — effort: M
User-configurable `base_url` with the API key attached (Bearer on the Bedrock path, `ai.py:142–145`) is an exfil/SSRF vector ([SEC-3]). Enforce `https` (with an explicit carve-out for loopback/private ranges to keep Ollama/LiteLLM local use working), and optionally let `TRAM_AI_ALLOWED_BASE_URLS` restrict what `POST /api/ai/config` accepts.

---

## Part B — New integration points (ranked)

Each candidate names the exact plug-in point and the data already available there. Honest value/effort calls included; the deliberately-excluded gimmicks are listed at the end.

### Tier 1 — high operator value

**B1. Run-failure triage ("Explain this run")** — value: high / effort: S–M
- **Plug-in point (UI):** the run-issues expandable row in `tram/ui/src/pages/runs_table.js:64–102`. It already computes `failureReason` (top-level error), `reasonGroups` (deduped, count-sorted skip reasons, `runs_table.js:145–154`), `records_skipped`, and `dlq_count` — i.e., the exact triage context, pre-grouped.
- **Plug-in point (backend):** extend `/api/ai/suggest` with `mode: "triage"` taking a `run_id`; fetch via `controller.get_run(run_id)` (already exposed by `GET /api/runs/{run_id}`, `tram/api/routers/runs.py:103–122`), which returns `error`, `errors[]`, counters (`runs.py:16–38` `_queued_run_to_dict` shows the RunResult dict shape).
- **Why it's the top pick:** it is the operational heart of a mediation NOC — "why did last night's mediation run skip 40k records" — and the editor already proves the pattern works (dry-run explain, editor.js:403–413 uses the same `suggest` mode with an error string). The run path additionally has the pipeline YAML available server-side (join on `pipeline` name via `controller`), so the prompt can carry config + error + grouped skip reasons + counters in one shot, with the redaction from A4 applied.

**B2. Fix-and-validate loop surfaced on dry-run failures** — value: high / effort: S
Covered as A7 above; listed here because the integration point is the dry-run panel (`tram/api/routers/pipelines.py:33–56` returns `issues` + `warnings`; UI renders them at editor.js:280–296). Making "AI Fix" produce *validated* YAML on the first click is the difference between a demo feature and a tool operators trust.

**B3. MIB compilation-error explanation** — value: medium-high (telecom-specific) / effort: S
- **Plug-in point:** `tram/api/routers/mibs.py:254–256` — `MibCompileFailure` becomes `HTTPException(500, "Compilation failed: …")`. pysmi/pysnmp errors (missing imports, symbol clashes, ASN.1 syntax) are notoriously cryptic, and the endpoint *already computes* structured context that would make a great prompt: `_extract_imported_mibs` (mibs.py:235) and `_classify_imports` with `builtin_names`/availability before-and-after (mibs.py:262–269).
- **UI:** the mibs page error surface — add an "Explain" button next to the failed-upload message.
- **Effort:** one new suggest mode (`mode: "mib_error"`) receiving the compile error + import classification; the UI change is a button + inline text, identical to the dry-run explain pattern.

### Tier 2 — solid value, more plumbing

**B4. Alert-rule authoring from natural language** — value: medium / effort: S–M
- **Plug-in point:** the alert modal, `tram/ui/src/pages/detail.js:616–654` (condition/action/webhook/email/cooldown form), and the alerts API (`tram/api/routers/pipelines.py:511–561`).
- **Why it's cheap:** alert conditions use the *same simpleeval expression syntax the AI context already documents* (`ai_docs.py:61–72` — wrong/right examples, built-in functions). A `mode: "alert"` prompt is a small delta on the existing context builder: add the alert variable list (records_in, records_out, duration_s, error_count, status, dlq_count — already enumerated for users in the modal hint, detail.html:144) and ask for a single condition string.
- **Guardrail:** validate the condition the same way alert evaluation does before accepting it. Bounded output (one expression), so risk is low.

**B5. Throughput-anomaly explanation on the dashboard** — value: medium (only if joined with run history) / effort: M
- **Plug-in point:** `tram/api/routers/stats.py:34–177` — per-pipeline rows (`records_in/out`, `errors`, runs in window, stats.py:224–252) and the bucketed sparkline (stats.py:255–265, 341–378). The dashboard chart already renders these points.
- **Honest caveat:** the sparkline alone is *numbers without causes* — an LLM asked "why did throughput dip at 14:20" from a records_out series will confabulate. The version worth building joins the time window to `run_history` (same queries as `_db_per_pipeline`, stats.py:224–252, filtered to the dip window) and asks the model to correlate: "pipeline X: out/s dropped 62% in the 14:15 bucket; 3 runs failed at 14:14–14:18 with error Y; 12k records skipped with reason Z". That is genuinely useful NOC narration and reuses B1's triage mode almost verbatim. The chart-click → "explain this window" UI is the main new work. Without the run-history join, skip this — it would be a gimmick.

**B6. Connector test-failure explanation** — value: medium-low / effort: S
- **Plug-in point:** `tram/api/routers/connectors.py:17–29` (`/api/connectors/test` and `/test-pipeline`) return `{ok, error, latency_ms}` per connector; the UI renders them at editor.js:233–258 and detail.js:663–676.
- **Assessment:** auth/SASL/TLS errors from Kafka/SFTP clients are often stack-trace fragments, so an "Explain" button here has real value for junior operators. But many are self-explanatory ("connection refused"). Worth doing only as a cheap reuse of the explain mode with the error string + connector type. Do it after B1/B3.

### Tier 3 — larger efforts with prerequisites

**B7. DLQ record analysis** — value: potentially high / effort: L
- **Plug-in point (today):** DLQ is a *configured sink* (`tram/models/pipeline.py:1451, 1480–1490` — `on_error: dlq` requires a `dlq` sink), and `dlq_count` surfaces on runs (`runs.py:34`; UI at runs_table.js:94–101). There is **no API to browse DLQ record contents** — they land wherever the sink points (file, kafka, …).
- **Assessment:** before any AI value, TRAM needs a DLQ browse/inspect endpoint; once records are addressable, "cluster these 5k DLQ'd records and explain the common failure" is a strong mediation-domain feature (transform expression errors, serializer mismatches). Genuinely valuable, but it is a DLQ-browsing feature first and an AI feature second. Sequence it behind the DLQ surface work.

**B8. Template library natural-language search** — value: low-medium / effort: S
- **Plug-in point:** `GET /api/templates` (`templates.py:75–84`) already returns tags; the pipelines page template modal renders them.
- **Assessment:** with ~dozens of bundled templates, embedding-based search is overkill and keyword/tag filtering probably suffices. The better use of the library is A6 (few-shot grounding), not search. Listed for completeness; low priority.

### Deliberately not proposed (low-value gimmicks)

- **Dashboard "AI chat" assistant widget** — generic product fluff; TRAM's value is in specific, data-grounded actions, and the maintenance cost of a general chat surface (context assembly, safety, cost) buys nothing over the targeted modes above.
- **AI-generated pipeline names/descriptions** — trivial to type, zero operator pain.
- **"AI ops copilot" summarizing the whole dashboard** — a stats narration without causal join (see B5 caveat); confabulation risk with no data the operator can't see in two clicks.
- **Natural-language query over run history ("how many failed runs last week?")** — the runs page filters + CSV export (runs.py:41–100) already answer this deterministically; an LLM in the loop makes it slower and less trustworthy.

---

## Suggested sequencing

1. **Now (XS–S):** A1, A2, A8 — unblock event loop, stop the key wipe, sync docs/helm/label.
2. **Next (S):** A3, A4, A5 — validated output, secret redaction, undo. These make every existing and future AI mode safer and are prerequisites for B-tier ideas.
3. **Then (S–M):** A6, A7, B1, B3 — template-grounded generation, fix loop, run triage, MIB errors. These convert the AI feature from "editor toy" into operator tooling.
4. **Later (M+):** A9–A11, B4, B5 (with run-history join), B6, B7 (behind DLQ browsing).
