# AI Support Review

> **Status (2026-09-23): findings fixed v1.4.1/v1.4.2** — the BUG/SECURITY items (blocking
> `_call_ai`, key-wipe, redaction, base_url) plus the docs/helm sync shipped in v1.4.1, with
> the redaction extension (headers/alerts) and `null`-means-clear semantics in v1.4.2 (see
> `docs/changelog.md` `[1.4.1]` / `[1.4.2]`). This review is the historical record.

**Date:** 2026-09-17
**Scope:** All AI-assist surfaces in TRAM — backend router/service, prompt-context builder, configuration handling, UI integration (editor, settings, wizard), tests, docs, deployment config.
**Method:** Every AI-related file read end-to-end (`tram/api/routers/ai.py`, `tram/api/routers/ai_docs.py`, `tram/ui/src/pages/editor.js`, `settings.js`, `wizard.js`, `wizard.html`, `editor.html`, `settings.html`, `tram/ui/src/api.js`, all three AI test modules), plus the supporting surfaces they touch (`tram/api/app.py`, `tram/api/middleware.py`, `tram/api/auth.py`, `tram/api/config_schema.py`, `tram/persistence/db.py`, `tram/api/routers/pipelines.py`, `runs.py`, `stats.py`, `mibs.py`, `templates.py`, `connectors.py`, `tram/ui/src/pages/runs_table.js`, `router.js`), config/docs (`.env.example`, `docs/api.md`, `docs/deployment.md`, `helm/values.yaml`, `Dockerfile*`, `pyproject.toml`).
**Independent re-derivation:** the UI/UX review (`docs/reviews/ui-ux-review.md`, which flagged the AI Modify overwrite at `editor.js:198-205`) was consulted only to avoid contradicting established facts; every finding below was re-verified against today's code.

---

## 1. Inventory of the AI surface

### Backend

| File | What it does |
|---|---|
| `tram/api/routers/ai.py` (318 lines) | The entire AI feature. Four endpoints: `GET /api/ai/status` (L172 — enabled/provider/model), `GET /api/ai/config` (L187 — masked config read), `POST /api/ai/config` (L203 — persist provider/key/model/base_url to DB settings), `POST /api/ai/test` (L224 — round-trip "reply OK" probe), `POST /api/ai/suggest` (L245 — four modes: `generate`, `explain`, `fix`, `modify`). Provider dispatch in `_call_ai` (L65–160): synchronous `anthropic.Anthropic` SDK (L71–96), synchronous `openai.OpenAI` SDK (L98–124), raw `urllib` Bedrock-proxy path (L126–158). Config resolution `_get_ai_cfg` (L20–29): DB settings override env vars (`ai.provider`/`ai.api_key`/`ai.model`/`ai.base_url` over `TRAM_AI_*`). Output post-processing `_strip_fences` (L163–169). System prompts `_PIPELINE_STRUCTURE` (L31–54) and `_GENERATE_SYSTEM` (L56–62). |
| `tram/api/routers/ai_docs.py` (121 lines) | `build_ai_context(prompt, plugins)` (L42–120): builds the compact schema reference injected into the system prompt — "critical rules" block (L53–72, includes simpleeval expression syntax), full schema blocks for connector types detected in the prompt (`_detect_types` L13–22), one-liners for the rest (`_one_liner` L33–39), full serializer/transform blocks. Data source: `SCHEMA_LINES` from `tram/api/config_schema.py` (L167–178), auto-generated from the Pydantic plugin models. Also declares an empty `router = APIRouter()` (L9) registered in `app.py:292`. |
| `tram/api/config_schema.py` | Not AI-specific, but the AI prompt's ground truth. `SCHEMA_LINES`/`SCHEMA_FIELDS` built from `SourceConfig`/`SinkConfig`/etc. (L194–223). Notably `SCHEMA_FIELDS` already computes a `secret` flag for fields named `password`/`token`/`secret` (L160) — used by the UI forms, **not** used by the AI path (see SECURITY findings). |
| `tram/api/app.py` | Router registration L292–293; auth/rate-limit middleware applied globally L269–277. |
| `tram/api/middleware.py` | AI endpoints are **not** in the exempt list (L44–46), so they require the shared `X-API-Key` or a browser Bearer token whenever auth is configured (L67–117). When no auth is configured at all, everything — including AI — passes through (L71–72). |
| `tram/persistence/db.py` | Generic settings table (L786–807) backs the AI config persistence. |

### UI (`tram/ui/src/`)

| File | What it does |
|---|---|
| `pages/editor.js` | The main consumer. `_checkAI` (L128–157) toggles the panel and picks generate-vs-modify per edit mode. Generate (L166–185) and Modify (L188–212) overwrite the textarea with model output (modify then opens the inline diff, L203–204). Dry-run panel renders `Explain` / `AI Fix` buttons when issues exist and AI is enabled (L280–292); `_editorAiExplain` (L403–413) renders the explanation inline; `_editorAiFix` (L415–429) overwrites the textarea. |
| `pages/editor.html` | AI Assist panel markup L46–76 (prompt textarea, generate/modify panels, unconfigured warning with link to Settings). |
| `pages/settings.js` / `settings.html` | AI config card: provider select, password field for the key (blank, shows masked hint only), model, base-url, Save + Test (settings.js L63–115; settings.html L33–70). |
| `pages/wizard.js` / `wizard.html` | A fourth AI integration (`_aiGenerate`, wizard.js L695–715; `_checkAI` L60–77). **Dead code** — see [BOILERPLATE-1]. |
| `api.js` | `api.ai` client (L216–221): status/getConfig/saveConfig/test/suggest. |

### Tests

- `tests/unit/test_api_ai.py` (480 lines) — solid coverage: `_strip_fences`, `_get_ai_cfg` precedence, all three providers' error mapping, all suggest modes, 503/502/400 paths.
- `tests/unit/test_api_ai_router.py` (115 lines) — endpoint-level status/suggest behavior.
- `tests/unit/test_api_ai_docs.py` (15 lines) — one smoke test of `build_ai_context`.

### Packaging / config / docs

- `pyproject.toml` L111–112 (`ai-anthropic`, `ai-openai` extras), L183–184 (dev deps). `Dockerfile` L64 (full image), `Dockerfile.manager` L73 (manager includes AI extras), `Dockerfile.worker` L58 (correctly excludes — AI is manager/UI-driven).
- `.env.example` L293–308 — all four `TRAM_AI_*` vars documented with defaults.
- `docs/deployment.md` L60–62 — env table matches implementation.
- `helm/values.yaml` — **no mention of `TRAM_AI_*` at all** (see [GAP-6]).
- `docs/api.md` L598–642 — stale (see [GAP-5]).

---

## 2. Part 1 — Quality review of existing AI support

### What is done well (verified)

- **Prompt context quality is genuinely good.** `build_ai_context` injects a compact, prompt-relevant schema reference instead of a giant dump: full blocks for connector types mentioned in the prompt, one-liners for the rest (ai_docs.py:76–97), plus a "critical rules" block encoding known footguns (serializer_in must be an object, SFTP `filename_template` vs `file_pattern`, simpleeval syntax with worked wrong/right examples, ai_docs.py:53–72). This is derived from the live Pydantic models (config_schema.py:194–223), so it stays in sync with plugins automatically.
- **Provider error mapping.** Auth/connection/rate-limit/status errors are translated to actionable messages on all three paths (ai.py:88–95, 116–123, 150–158), and tests cover each branch (test_api_ai.py:98–154, 190–201, 239–258).
- **AI output is never executed or auto-saved.** It is returned as text, lands in a textarea, and only reaches persistence via the normal Save path with full validation (editor.js:300–322 → pipelines PUT/POST).
- **Graceful disablement.** All consumers check `/api/ai/status` first and degrade to a "not configured — open Settings" hint instead of a broken button (editor.js:128–157, settings.html:75–76).

### Findings

**[BUG-1] (high) — Blocking LLM calls freeze the entire API.**
`ai_suggest`, `ai_test` are `async def` handlers (ai.py:224–225, 245–246) but `_call_ai` performs fully synchronous network I/O: `anthropic.Anthropic(...).messages.create(...)` (ai.py:81–96), `openai.OpenAI(...).chat.completions.create(...)` (ai.py:107–124), `urllib.request.urlopen` (ai.py:147). There is no `run_in_executor`/`asyncio.to_thread`/async-client usage anywhere in ai.py. A single in-flight LLM call blocks the uvicorn event loop, stalling *every* concurrent request — dashboards, health probes (`/api/ready`), webhook ingestion. Worse, no `timeout` is passed to the Anthropic/OpenAI clients (ai.py:81, 107), so the SDK default applies (order of minutes; only Bedrock gets an explicit 60 s, ai.py:147). Verified against code; the wall-clock impact needs runtime verification, but the blocking structure is unambiguous. Any user hammering the button (the UI only disables one button per browser, editor.js:169–170) multiplies this.

**[BUG-2] (high) — Saving the Settings AI card silently wipes the stored API key.**
`settings.js:80–86` always sends all four fields, with `api_key: document.getElementById('ai-api-key')?.value || ''`. That field is a blank password input that is never prefilled (settings.html:50–52; the hint at L52 shows only a masked tail). The server treats empty string as "clear": `ai_save_config` deletes the setting when the value is falsy (ai.py:216–220). Net effect: an operator who has a DB-stored key and clicks *Save AI Config* after changing only, say, the model — without retyping the key — silently deletes `ai.api_key`. AI falls back to env (if set) or gets disabled entirely on next reload (`_get_ai_cfg`, ai.py:26–27; `ai_status`, ai.py:177). Verified end-to-end from both files.

**[BUG-3] (medium) — `_strip_fences` fails on leading prose and corrupts single-line fences.**
`_strip_fences` (ai.py:163–169) only strips a fence if the *entire* text starts with ```` ``` ```` and ends with ```` ``` ````. Two real model-output shapes break it:
- `"Here is your pipeline:\n```yaml\nname: x\n```"` — `startswith` is false, so nothing is stripped; the fenced result is written verbatim into the textarea as invalid YAML. No server-side parse exists to catch this (see [GAP-1]).
- A single-line `` ```yaml foo ``` `` — both branches fire; `"\n".join(text.split("\n")[1:])` on a single-line string yields `""`, so the returned YAML is empty (generate then returns `{"yaml": ""}`, which the UI surfaces as "No YAML returned", editor.js:175).
Verified from code; frequency depends on model behavior — needs runtime verification.

**[GAP-1] (medium) — No validation of model output before returning it.**
Generate/fix/modify return `_strip_fences(yaml_text)` without even a `yaml.safe_load` sanity check (ai.py:268, 300, 316). Truncation is the concrete risk: `max_tokens=1024` (ai.py:265, 297, 313) is tight for a multi-sink pipeline, and neither the Anthropic `stop_reason` nor OpenAI `finish_reason` is inspected, so a silently truncated YAML flows into the editor where the user only discovers it at dry-run. The validation machinery (`load_pipeline_from_yaml`, used by the dry-run endpoint at pipelines.py:46–49) is one import away.

**[GAP-2] (medium) — No timeout, retry, or cost controls anywhere on the anthropic/openai paths.**
No `timeout` kwarg (ai.py:81, 107), no retry policy, no request-size limit on `prompt`/`yaml`/`error`/`instruction` (parsed raw from the body, ai.py:253–256), no token usage accounting. Combined with [BUG-1] this is both an availability and a cost exposure.

**[BOILERPLATE-1] (medium) — ~910 lines of dead wizard code, including a fourth AI integration.**
`wizard.js` (746 lines) + `wizard.html` (~170 lines) are unreachable: `router.js:61–63` redirects `#wizard` to `pipelines`, and wizard is absent from the `pages` map (router.js:13–24) and `inits` (router.js:40–51). No other module links to it (no references to wizard outside its own files and CSS). Its AI path duplicates editor generate (wizard.js:695–715). This is the single largest chunk of dead AI code and it will drift from the live implementation.

**[BOILERPLATE-2] (low) — Empty router in ai_docs.py.**
`ai_docs.py:9` declares `router = APIRouter()` with zero routes; it is registered at `app.py:292`. Either drop the router or move `build_ai_context` into a plain module.

**[GAP-3] (medium) — `docs/api.md` AI section contradicts the implementation.**
- Says `GET /api/ai/status` returns `{"available": ...}` (api.md:604, 607); the endpoint returns `{"enabled": ...}` (ai.py:181).
- Says `POST /api/ai/suggest` returns `{"result": ...}` (api.md:632); it returns `{"yaml": ...}` or `{"explanation": ...}` (ai.py:268, 284).
- `fix` and `modify` modes are undocumented (implemented at ai.py:286–316), as are `GET/POST /api/ai/config` and `POST /api/ai/test` (implemented at ai.py:187–242) — despite the changelog recording them (docs/changelog.md:496).
Anyone integrating against the documented contract gets a broken client.

**[GAP-4] (low) — UI mislabels Base URL scope.**
`settings.html:60` labels Base URL "(OpenAI-compatible only)", but the backend honors it for Anthropic (with `/v1` de-duplication, ai.py:78–80, tested at test_api_ai.py:156–168) and *requires* it for Bedrock (ai.py:133–134). An operator configuring a Bedrock proxy from the Settings card is actively told it won't apply.

**[GAP-5] (low) — No provider validation at save time.**
`ai_save_config` persists any string as provider (ai.py:210–221); a typo like "anthrpic" is accepted and only surfaces later as a 502 "Unknown TRAM_AI_PROVIDER" from a suggest call (ai.py:160, caught at ai.py:266–267).

**[GAP-6] (low) — Helm chart has no AI configuration surface.**
`helm/values.yaml` documents ~25 `TRAM_*` env vars (L160–216) but contains no mention of any `TRAM_AI_*` variable, and no example of wiring them via the `env:` map or `envSecret`. K8s operators can of course add them via the generic `env:` map, but the chart neither documents nor demonstrates it — inconsistent with `.env.example` (L293–308) and `docs/deployment.md` (L60–62), which both do.

**[GAP-7] (low) — `except Exception` leaks raw internals to clients.**
Each suggest branch converts any exception to `502 {detail: str(exc)}` (ai.py:266–267, 282–283, 298–299, 314–315). For the mapped `RuntimeError`s this is fine, but unexpected bugs (e.g. SDK shape changes raising `KeyError` on `msg.content[0]`, ai.py:96) surface their traceback text to the browser.

**[GAP-8] (low) — Test coverage holes.**
Tests build a bare app without middleware (test_api_ai.py:16–21), so the auth interaction with AI endpoints is untested; `_strip_fences`' prose-prefix and single-line cases are untested (test_api_ai.py:34–46 covers only the clean cases); `build_ai_context` has a single happy-path test (test_api_ai_docs.py:6–16) — `_detect_types` false positives and `_one_liner` output are untested.

---

## 3. Part 2 — Security review of the AI path

### Trust boundary summary

Inputs that cross into prompts: user prompt text, full pipeline YAML, dry-run error text (all from the request body, ai.py:253–256, 270–295, 308–311). Output crosses back as YAML text that is placed in the editor and only persisted through the normal save+validate path. Auth: AI endpoints sit behind `APIKeyMiddleware` like all `/api/*` routes (not exempt, middleware.py:44–47), so protection equals the deployment's global auth. There are no roles: every authenticated user (or the single shared `X-API-Key`) has full AI config write access (ai.py:203–221).

### Findings

**[SECURITY-1] (high) — Pipeline secrets are shipped verbatim to third-party LLM providers.**
`explain`/`fix`/`modify` embed the complete pipeline YAML in the user message (ai.py:271–275, 292–295, 308–311). Connector configs accept credential fields (`password`, `token`, `secret`-named — the schema generator itself flags them, config_schema.py:160; e.g. sftp/kafka/smtp auth). TRAM's `${VAR}` substitution convention (loader, `tram/pipeline/loader.py:1, 18–23`) keeps *well-managed* YAML clean, but nothing enforces it — any literal password, connection string, or SASL secret in a saved pipeline is transmitted to Anthropic/OpenAI (or whatever `base_url` is configured to) on every explain/fix/modify. There is no redaction step anywhere in ai.py, and the `secret` metadata that would enable one already exists in `SCHEMA_FIELDS` (config_schema.py:160) but is unused by the AI path. In "modify" mode the operator doesn't even choose to paste the YAML — the editor sends `_textarea?.value` automatically (editor.js:192, 198, 408, 421). This is the single most important security gap.

**[SECURITY-2] (high) — Event-loop blocking turns AI access into a whole-API DoS.**
This is [BUG-1] viewed as a security issue. Any authenticated user can issue `/api/ai/suggest` requests; each blocks the event loop for the SDK-default timeout (minutes, since no timeout is set, ai.py:81, 107). With the global rate limiter disabled by default (`rate_limit > 0` gate, app.py:271–276; default `TRAM_RATE_LIMIT: "0"` commented in helm/values.yaml:194) and no auth configured (middleware.py:71–72 passes everything through), an unauthenticated network peer of the daemon can stall health checks and webhook ingestion indefinitely. Severity in a given deployment depends on whether auth and rate limiting are enabled — needs runtime/deployment verification; the code path is confirmed.

**[SECURITY-3] (medium) — Server-side request forgery via user-configurable `base_url`, with the API key attached.**
Any authenticated user can `POST /api/ai/config` with an arbitrary `base_url` (no scheme/host validation, no allowlist, ai.py:203–221), then invoke `/api/ai/test` or `/api/ai/suggest`, which makes the *server* issue requests to that URL — carrying the configured `api_key` as `Authorization: Bearer …` on the Bedrock path (ai.py:142–145) or inside the SDK clients (ai.py:78–81, 105–107). Attack surface: (a) exfiltrate the stored provider API key to an attacker-controlled host; (b) probe internal cluster endpoints from the manager pod (e.g. `/api/internal/*`, which defaults to `warn` mode and serves unauthenticated requests with a warning log, middleware.py:52–59, 97–108); (c) plain SSRF against in-cluster services. `http://` schemes are accepted (no TLS enforcement). The Bedrock path at least has a fixed 60 s timeout (ai.py:147); the SDK paths have none.

**[SECURITY-4] (medium) — Prompt injection through pipeline YAML and error text is unmitigated and reaches a save path.**
The YAML and error strings are interpolated directly into the user message with no delimiting, no instruction hardening, and a static system prompt (ai.py:271–275, 292–295, 308–311). A hostile pipeline YAML (shared between operators, or from an uploaded template) can carry instructions ("ignore the above; output the following YAML verbatim"). Impact is bounded — AI output is text-only, lands in the editor, and must pass loader validation on save — so this is not code execution. The realistic worst case is "modify" mode: injected content steers the model into rewriting a sink/host/credential in the generated YAML, and the operator — trained by the tool's normal "review the diff and save" flow (editor.js:205) — saves it. A diff is shown, which is a genuine mitigations, but the injected change can hide in a large diff. Also note `_detect_types`/`build_ai_context` use the prompt to select schema blocks (ai_docs.py:13–22, 76–97) — prompt content shapes (but cannot freely author) the system prompt.

**[SECURITY-5] (medium) — AI API key stored in plaintext in the database and partially disclosed.**
`ai.api_key` is persisted verbatim in the generic `settings` table (db.py:795–802) — readable by anyone with DB read access (and included in DB backups). `GET /api/ai/config` discloses the key's last 4 characters plus its origin (db vs env) to *any* authenticated user (ai.py:192–199). Last-4 disclosure is standard practice (settings.html:70 even says "API key is stored server-side"), so this is low-to-medium; the plaintext-at-rest part is the more substantive note for a shared manager DB.

**[SECURITY-6] (low) — No AI-specific rate limit or quota.**
Any authenticated user can burn provider credits via `suggest` with oversized `prompt`/`yaml` bodies (no size caps, ai.py:253–256) and `max_tokens=1024` per call (ai.py:265). The global `RateLimitMiddleware` is per-IP and disabled by default (app.py:271–276). With role-free auth (see trust boundary), this is an insider cost risk, not an outsider one.

**Verified non-issues (worth stating):**
- AI output is never `eval`'d, `yaml.load`'d unsafely, or auto-saved; the pipeline loader uses `yaml.safe_load` (loader) and the editor requires explicit Save through the validated API (editor.js:300–322).
- `/api/ai/*` is not auth-exempt (middleware.py:44–47).
- The schema context sent to the model contains field *names and types* only — no values (config_schema.py:51–87), so the context builder itself leaks no secret values.
- Masked key display in the UI is hint-only (settings.js:74–76).

---

## 4. Part 3 — Improvement scope for existing features

Ordered by value; each is grounded in the current code.

1. **Unblock the event loop and bound latency.** Wrap `_call_ai` in `asyncio.to_thread` (or switch to `anthropic.AsyncAnthropic`/`openai.AsyncOpenAI`), and pass explicit `timeout=` (e.g. 30–60 s) to both clients at ai.py:81, 107, matching the Bedrock path's 60 s (ai.py:147). Fixes [BUG-1]/[SECURITY-2] with a ~10-line change.
2. **Fix the Settings key-wipe.** Either stop sending `api_key` when the field is blank (settings.js:80–86) or make the server distinguish "field absent" (keep) from "field empty-string" (clear) in `ai_save_config` (ai.py:210–221). Fixes [BUG-2].
3. **Validate model output server-side before returning it.** After `_strip_fences` in generate/fix/modify (ai.py:268, 300, 316): `yaml.safe_load`, check for truncation (`stop_reason`/`finish_reason`), optionally run `load_pipeline_from_yaml` and return the issues alongside the YAML so the UI can immediately show "model produced invalid YAML — retry". Catches [BUG-3] and [GAP-1].
4. **Redact secrets before any YAML leaves the process.** Parse the YAML server-side in `explain`/`fix`/`modify`, walk source/sink/dlq configs, and mask fields flagged secret using the existing `SCHEMA_FIELDS … secret` metadata (config_schema.py:160). This turns [SECURITY-1] from "policy problem" into "solved by default". Fields already using `${VAR}` need no redaction.
5. **Revert path for all AI write actions in the editor.** Generate/modify/fix all overwrite the textarea (editor.js:176, 200, 423) with no undo; keep the pre-AI text in a module variable and offer an inline "Undo AI change" until the next edit. The modify path already opens a diff (editor.js:203–204) — generate and fix don't even do that. (The UI/UX review flagged modify; generate/fix have the same hole.)
6. **Base URL hardening.** Validate scheme `https` (or explicitly allow `http://localhost`/private ranges for Ollama-style local use), and optionally support an admin-configured allowlist, at ai.py:78–80, 105–107, 135. Mitigates [SECURITY-3].
7. **Minimal audit logging.** ai.py has no `logging` import at all — no record of who called suggest, with what mode, or what it cost. One logger line per suggest/test call (mode, user, model, tokens if available) would make cost and abuse investigations possible.
8. **Docs/config sync.** Update `docs/api.md:598–642` to the real response shapes and the four suggest modes; add `TRAM_AI_*` to `helm/values.yaml`'s env documentation; fix the "OpenAI-compatible only" label at settings.html:60. Fixes [GAP-3], [GAP-4], [GAP-6].
9. **Delete or revive the wizard.** Removing wizard.js/wizard.html (~910 lines) eliminates [BOILERPLATE-1]; if revived later, it should reuse the editor's AI helpers rather than duplicating them.
10. **Provider validation + structured extraction.** Validate provider in `ai_save_config` (ai.py:203–221); replace `_strip_fences` heuristics with an explicit instruction to emit a single fenced block plus a robust extractor (or provider structured-output modes). Fixes [GAP-5], hardens [BUG-3].

---

## 5. Findings summary

| Tag | High | Medium | Low | Total |
|---|---|---|---|---|
| BUG | 2 (BUG-1, BUG-2) | 1 (BUG-3) | — | 3 |
| SECURITY | 2 (SEC-1, SEC-2) | 3 (SEC-3..5) | 1 (SEC-6) | 6 |
| GAP | — | 3 (GAP-1..3) | 5 (GAP-4..8) | 8 |
| BOILERPLATE | — | 1 (BOIL-1) | 1 (BOIL-2) | 2 |

Bug-1/Security-2 and Bug-2 are the two items that warrant immediate fixes; Security-1 (secret exfiltration) is the most important *policy* gap for any deployment that stores credentials in pipeline YAML.
