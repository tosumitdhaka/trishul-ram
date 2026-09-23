# Feasibility: Schema Identifier + Registry for AI-Assisted Adaptation (Issue #24)

> **Status (2026-09-23): implemented (Option A) in v1.4.3** — the content-hash `schema_version`
> identity shipped on `/api/config/schema`, `/api/plugins`, and `/api/ai/status`, the AI prompts
> carry the schema hash, and the registry↔union cross-check (`schema_mismatch`) landed. Issue #24
> is closed. The analysis below is the historical record.

Date: 2026-09-21
Status: Assessment complete — a derived registry with a content-hash identifier is feasible and recommended (~1–2 days); explicit versioned files and full JSON Schema generation are rejected for now. This document records the analysis so v1.4.3's L2 design and v1.4.4's A6 (template-grounded generation) can build on it without redoing discovery.

## Purpose

TRAM's UI and AI features need reliable knowledge of connector schemas. Today that knowledge is derived at import time from the Pydantic models (`tram/api/config_schema.py`), served through two endpoints with different projections (`/api/plugins`, `/api/config/schema`), fed into AI prompts (`tram/api/routers/ai_docs.py`), and duplicated by hand in `docs/connectors.md`. Nothing identifies *which* schema any of these consumers are looking at. This study assesses whether TRAM needs a schema identifier + registry, which form it should take, and what it unlocks for L2 (structured creation, v1.4.3) and A6 (template-grounded generation, v1.4.4).

## 1. Ground truth — how schema knowledge flows today

**Source of truth.** `tram/models/pipeline.py` defines one Pydantic model per connector type, combined into four discriminated unions: `SourceConfig` (23 members, pipeline.py:400–404), `TransformConfig` (pipeline.py:788), `SinkConfig` (20 members, pipeline.py:1195–1199), `SerializerConfig` (pipeline.py:1326). All models inherit `model_config = {"extra": "forbid"}` (pipeline.py:13) — a `type` not in the union cannot pass config validation anywhere. The `PipelineConfig.version: str = "1"` field (pipeline.py:1432) is a document version that is written but never read for any adaptation logic.

**Derivation.** `tram/api/config_schema.py` walks the unions at import time (`_build_schema_cache()`, config_schema.py:194–243, invoked at module import, line 243) and produces two parallel projections: `SCHEMA_LINES` (compact text blocks) and `SCHEMA_FIELDS` (structured field descriptors: name, type, kind, choices, required, default, secret, multiline — config_schema.py:140–164). The `secret` flag is a name heuristic (`password`/`token`/`secret`, config_schema.py:160); it is consumed by v1.4.1's AI redaction (`tram/api/routers/ai.py:467`). Current scale: 24 sources, 20 sinks, 12 serializers, 28 transforms (84 types; canonical JSON of `SCHEMA_FIELDS` ≈ 87 KB).

**Second, parallel registry.** `tram/registry/registry.py` keeps four runtime class dicts populated by `@register_source`/`@register_sink`/… decorators (registry.py:15–18, 24–57). The Pydantic unions and the registry are **two hand-maintained lists of the same type names that nothing cross-checks**.

**Consumers:**
- `/api/plugins` (`tram/api/routers/health.py:124–138`) lists registry keys plus `details` built server-side from `SCHEMA_FIELDS` (`_build_plugin_details`, health.py:173–193) — but `_field_descriptors` (health.py:160–170) deliberately drops `choices`/`secret`/`multiline`.
- `/api/config/schema` (`tram/api/routers/schemas.py:46–49`) returns `build_config_schema_payload()` (config_schema.py:226–240) — the full descriptors, no wrapper, no version.
- Plugins page (#27): fetches **both** endpoints and merges client-side (plugins.js:62–64), because the choices/secret/multiline metadata only exists in the second payload (`_enrichedFields`, plugins.js:151–157). The comment on the cache variable says "best-effort" (plugins.js:12).
- Editor pills (#26): names only, from `/api/plugins` (editor.js:295–311), passed into AI suggest calls as the `plugins` scope (editor.js:323–324).
- AI prompt context: `build_ai_context` (ai_docs.py:42) renders `SCHEMA_LINES`, selecting full blocks for types mentioned in the prompt (`_detect_types`, ai_docs.py:13–22), with a `# (no schema available)` fallback for registry-listed types missing from the schema cache (ai_docs.py:29).
- Templates: `/api/templates` (templates.py:75–84) serves raw worked YAMLs from the templates dir — no binding to any schema version.
- Hand-written duplicate: `docs/connectors.md` (57 `###` sections) restates the same knowledge for humans and already requires a second edit for every connector change.
- Audit trail: v1.4.1's `ai_usage` rows record mode/provider/model/tokens (`tram/persistence/db.py`, `append_ai_usage`) — but not which schema the prompt was built from.

## 2. What problem would a registry solve

Confirmed failure modes of the derived-at-runtime, identity-less approach:

1. **No identity on any schema payload.** `/api/config/schema` returns a bare dict (schemas.py:49). A consumer cannot tell whether two responses are the same schema, whether a cached copy is stale, or whether the AI prompt it is about to send was built against the schema the daemon is currently running.
2. **Stale-UI-after-upgrade.** The SPA is static files served by the manager; a browser tab left open across a manager upgrade renders forms and pills from the old schema with no way to detect the skew (stale-tab scenario is inference, but the mechanism — no change-detection signal — is confirmed).
3. **Untraceable AI outputs.** When an AI-generated YAML hallucinates or omits a field, nothing in the request, response, or audit row records which schema knowledge the model was given. The v1.4.1 audit trail answers "who/what model/how many tokens" but not "what did the model know."
4. **Registry ↔ union divergence.** A plugin registered via `@register_source` but not added to the `SourceConfig` union appears in `/api/plugins` and in the AI context's plugin list (editor passes registry names into suggest), yet any pipeline using it fails Pydantic validation with a discriminator error, and the AI context renders it as `# (no schema available)` (ai_docs.py:29). The reverse skew — a union member with no registered class — passes validation and only fails at run time (`get_source` raises `PluginNotFoundError`, registry.py:63–66). Nothing in the codebase detects either direction; the two lists are synchronized only by developer discipline.
5. **No adaptation target.** Issue #24's headline use case — "adapt this pipeline written under an older schema to the current one" — has no anchor: pipeline YAML's own `version: "1"` is a dead constant (pipeline.py:1432), and there is no notion of "the schema this YAML was valid under."
6. **Dual-fetch inconsistency.** The plugins page needs two endpoints to render one table (plugins.js:62–64); the two payloads carry no shared marker, so a response pair can't even be checked for same-version consistency.

Manager-vs-worker skew (roadmap context) is largely a non-issue for this design: the UI and AI paths talk only to the manager; workers consume registry classes, not the schema API. The registry↔union cross-check (mode 4) is the skew that actually matters.

## 3. Options

### Option A — Derived registry with content-hash identifier (recommended)

Compute a stable content hash over the canonical JSON of `SCHEMA_FIELDS` (e.g. `sha256(json.dumps(..., sort_keys=True))[:12]` — measured at ~87 KB, sub-millisecond). Expose it as `schema_version`:

- in the `/api/config/schema` and `/api/plugins` responses (one field, backward-compatible),
- in `/api/ai/status` and embedded as one line in every AI system prompt ("TRAM schema v: eecd1712ea4d"),
- in the `ai_usage` audit rows (one column),
- with a registry↔union cross-check in `_build_plugin_details` flagging types present in one list but not the other.

Drift risk: none by construction — the hash is derived from the same object the endpoints serve and the prompts embed. No new files, no second source of truth.

### Option B — Explicit versioned schema files (rejected)

Hand-maintained `schema-1.4.yaml` files per release. TRAM already has the drift cautionary tale: `docs/connectors.md` (57 sections) is a hand-maintained duplicate that must be re-edited on every connector change, and option B adds a *machine-consumed* duplicate with ~84 types × ~10 fields of entries that must stay in lockstep with pipeline.py. It would enable offline/third-party consumers, but no such consumer exists today. Estimated 2–3 days to generate the initial file plus a permanent double-edit tax on every connector PR. The current system's key virtue — schema and models cannot diverge — would be given up for a capability nobody has asked for.

### Option C — Full JSON Schema generation + registry endpoint (rejected for now)

`PipelineConfig`'s unions can emit standard JSON Schema (`model_json_schema()` with discriminators), servable as a versioned registry endpoint. Honest assessment: it is more standard and more tool-consumable, but (a) `SCHEMA_FIELDS`' UI metadata (kind, choices, secret, multiline — config_schema.py:152–163) has no native JSON Schema equivalent and would need custom `x-` keywords, so the current payload remains necessary anyway; (b) `ai_docs.py` consumes the compact `SCHEMA_LINES` text format — switching prompts to JSON Schema means re-tuning the AI context builder for no demonstrated quality gain; (c) L2 is hand-rolled vanilla-JS forms (per the L2 design constraint, ui-ux-improvements.md:99–103) — a JSON-Schema form renderer would be a new dependency or significant new code; the existing descriptors already drive those forms (the dead wizard consumed exactly this shape, wizard.js:46–58). Estimated 4–6 days, breaks both current consumer shapes. Revisit only if an external/form-library consumer materializes.

| | A: hash of derived | B: versioned files | C: JSON Schema registry |
|---|---|---|---|
| Complexity | XS | M | M–L |
| Drift risk | none (derived) | high (hand-maintained) | none (derived) but new projection to maintain |
| Backward compatible | yes | new surface | breaks consumers |
| Enables L2 | schema-pinned forms, cache invalidation | same, plus offline | form-library reuse |
| Enables A6 | prompt+audit traceability | same | overkill |

## 4. Recommendation and effort

**Adopt Option A.** Effort: **1–2 days** including tests (hash function + endpoint fields + prompt line + audit column + cross-check + a contract test that the hash changes when a model field changes).

What it unlocks:

- **v1.4.3 / L2:** the structured creation path renders forms from `/api/config/schema`; with `schema_version`, a long-lived SPA tab can detect that the manager was upgraded underneath it (hash mismatch on poll) and force a reload instead of submitting forms built from a stale schema. It also settles L2's design question cheaply: the wizard-revival path can trust the existing descriptor payload — no new schema infrastructure is required for L2, which argues for wizard revival (~1 week) over the editor form layer (~2 weeks).
- **v1.4.4 / A6:** template-grounded generation sends schema context plus worked examples; embedding the hash in the prompt and the `ai_usage` row makes every AI output attributable to the exact schema knowledge that produced it — the difference between "the model hallucinated" and "the model was told a field that no longer exists."
- **Future adaptation (the #24 namesake):** the hash gives "adapt to current schema" a concrete target without committing to pipeline-migration machinery now. A later release can compare `PipelineConfig.version` (currently dead, pipeline.py:1432) against schema history if real migration demand appears; that work should stay out of scope until then.

## 5. Risks and unknowns

- **Hash stability across dependency upgrades** (inference, not yet observed): `SCHEMA_FIELDS` type strings are rendered by `_type_name` (config_schema.py:18–48) and defaults by `_serialize_default` (config_schema.py:130–137, which stringifies non-scalar defaults); a Pydantic version bump could change those renderings and rotate the hash with no semantic change. Mitigation: the hash is used only for equality, never parsed; document that it is an identity token, not a semantic version.
- **Cross-check semantics** (confirmed feasible): the registry↔union mismatch check needs `_build_plugin_details` (health.py:173) to compare `SCHEMA_FIELDS[category]` keys against registry keys; both are already in memory in that function's caller.
- **What the hash does not fix** (confirmed): it does not version *pipeline documents* — a hash says what the daemon knows now, not what a given saved YAML was written against. Closing that gap (per-YAML provenance) is the future adaptation work deliberately deferred above.
- **Unknown consumer set**: no consumers outside the SPA, AI prompts, and docs have been identified in this repo; if an external API consumer exists in the field, Option C's calculus changes — no evidence of one today.

## Verdict

Feasible and worth doing, in the narrow form: **derive a content-hash schema identifier from the existing `SCHEMA_FIELDS` cache, expose it on the two schema-serving endpoints and `/api/ai/status`, embed it in AI prompts and `ai_usage` audit rows, and add a registry↔union cross-check to `/api/plugins` — ~1–2 days, no drift risk, and it unblocks L2's design decision (favor wizard revival) and A6's traceability.** Explicit versioned files and a JSON Schema registry are rejected: the former reintroduces the drift the current architecture already solved, the latter is over-engineering for hand-rolled vanilla-JS forms and text-shaped AI prompts.
