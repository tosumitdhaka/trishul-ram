# Feasibility: Reusing the treq AI Component in TRAM

Date: 2026-09-17
Status: Assessment complete — reuse is feasible. **Scope decision: no shared-library extraction for now**; this document records the analysis so the effort can be picked up later without redoing discovery.

## Purpose

TRAM's AI support today is a single self-contained router (`tram/api/routers/ai.py`, 318 lines) with a sync `_call_ai` provider dispatch for anthropic / openai / bedrock-proxy. A sibling project, treq (`/home/dhaka/trishul/trishul-pre-req-agent`), contains a substantially more mature LLM client layer. This document assesses whether that component can be incorporated into TRAM.

## What treq has

The reusable part is the provider layer, isolated under `treq/_providers/`:

| File | Lines | Role |
|---|---|---|
| `treq/_providers/base.py` | 438 | `ProviderAdapter` ABC, canonical message/tool types, `build_provider` factory, cost engine |
| `treq/_providers/anthropic.py` | 144 | Anthropic SDK adapter |
| `treq/_providers/openai.py` | 624 | OpenAI Chat Completions adapter |
| `treq/_providers/bedrock.py` | 408 | Bedrock httpx + SSE adapter (Bearer-token proxy mode) |
| `treq/_providers/_anthropic_format.py` | 253 | Anthropic wire-format converters + response parsers |

~1,900 source lines, backed by ~5,473 lines of provider-focused tests. Everything else AI-related in treq (`agent.py` tool loop, `context_builder.py`, `compaction.py`, workflows, config tables) is treq-domain and out of scope.

## What TRAM would gain

TRAM's `ai.py` is a strict subset in capability. Incorporating the treq layer brings:

1. **Async throughout** — `stream()`/`complete()` are coroutines. TRAM's `ai_test`/`ai_suggest` handlers are already `async def`; they would `await provider.complete(...)` directly. This **fixes the [SEC/high] event-loop-blocking finding** from the AI-support review (sync SDK calls currently stall all API traffic, including `/api/ready`).
2. **Real timeouts** — httpx `Timeout(connect=10, read=1800, write=60)` on OpenAI (`openai.py:422`), connect/read/write/pool on Bedrock (`bedrock.py:64-66`); TRAM currently has none on anthropic/openai paths.
3. **Retries with backoff** — Bedrock retries HTTP 500/502/503/529 and transport errors up to 3× (`bedrock.py:110-128`).
4. **Streaming** — normalized `text_delta`/`thinking`/`done` events across all three providers.
5. **Cost engine** — per-model `compute_cost_usd` with provider-aware cache accounting (`base.py:326-368`).
6. **`list_models()`** and structured-output support (JSON schema on OpenAI, forced tool on Anthropic/Bedrock).

## Fit against TRAM's requirements

| TRAM need | treq equivalent | Notes |
|---|---|---|
| anthropic / openai / bedrock-proxy providers | Yes (all three) | Same provider set |
| Custom `base_url` | openai ✅, bedrock ✅, **anthropic ❌** | TRAM supports base_url on anthropic today (`ai.py:78-80`); must be added to treq's adapter or it's a regression |
| Config: DB-over-env | ✅ equivalent pattern | treq: pydantic env Settings + SQLite `settings_overlay`; TRAM: env + DB `settings` table — config plumbing stays TRAM-side either way |
| Masked-key status | ✅ same idea | TRAM's endpoint logic stays |
| generate / explain / fix / modify modes with YAML | ❌ not present | Stays in TRAM; treq is JSON-structured-output shaped |
| "Test connection" call | ❌ (only a de-facto `list_models` probe) | Keep TRAM's `/api/ai/test`; trivially implementable over `complete()` |

## Coupling: what must change to incorporate

The `_providers/` package is domain-free except four couplings (verified by direct inspection):

1. **Global settings singleton** — `openai.py:214,338` imports `treq.config.settings` for user-agent and cost rates. Must be constructor-injected.
2. **Hardcoded Rakuten gateway hostname** in `infer_display_name` (`base.py:385,397`). Parameterize or drop.
3. **Bedrock auth is Bearer-token proxy mode only**, not standard AWS sigv4 (`bedrock.py:1-9`). For TRAM this matches its existing "bedrock-compatible proxy" usage, so acceptable as-is; parameterize only if real sigv4 is ever needed.
4. **Anthropic adapter lacks `base_url`** (`anthropic.py:20-21`). Add it (TRAM parity requirement).

Dependency shape: treq declares `anthropic`, `openai`, `pydantic-settings` as hard deps. TRAM gates SDKs behind `tram[ai-anthropic]` / `tram[ai-openai]` extras with lazy imports — the incorporated layer must preserve lazy/optional imports to keep that behavior.

Known quirks to fix during incorporation (not blockers):
- Inconsistent structured-output error contract: Anthropic/Bedrock `complete()` raises `RuntimeError` on missing schema block (`_anthropic_format.py:233-234`) while OpenAI returns `CompleteResult(text=None, parse_error=...)` (`openai.py:592-611`).
- Streaming hard-codes `max_tokens=32000` vs `complete()` default 8192.
- Unknown models compute cost $0 (unlisted in `cost_rates`).

## What stays in TRAM regardless

- The four suggest modes and their prompts (generate/explain/fix/modify), the schema-context builder (`ai_docs.py`), YAML fence-stripping.
- All security fixes from the AI-support review, which are orthogonal to the provider transport: secret redaction before sending (reusing `config_schema.py:160` secret metadata), server-side YAML validation of model output, and `base_url` restrictions on `POST /api/ai/config`.

## Incorporation options (given no library extraction)

- **Vendor the `_providers/` package into TRAM** (e.g. `tram/ai/providers/`), adapting the four couplings above and porting the relevant tests. This is the practical path under the no-extraction constraint. Downside: divergence from treq over time; mitigation is a `VENDORED-FROM` note and periodic diff.
- **Depend on treq as a package** — not recommended: treq is an agent application, not a library; it would drag its full dependency set and domain code into TRAM.
- **Do nothing now** — acceptable; TRAM's current layer works, and the security fixes (redaction, validation, async wrapping via `asyncio.to_thread`, timeouts) can land independently against the existing `ai.py`.

## Effort estimate

- Vendor + adapt `_providers/` (4 couplings, lazy imports, tests): ~2 days
- Port `ai.py` to consume it (async handlers, keep modes/prompts/test endpoint): ~1 day
- Total: ~3 days, at which point `ai.py` shrinks to prompt-building + validation and inherits async/timeouts/retries/streaming/costs.

## Verdict

**Feasible and recommended when AI work is next prioritized** — the provider layer drops in with a small, well-defined adaptation list, and it structurally fixes the highest-severity AI finding (event-loop blocking). Deferred for now per scope decision; no library carving.
