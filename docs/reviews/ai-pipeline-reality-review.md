# AI pipeline generation reality review

Scope: 1.8.2 hardening work; historical dev verification before release publication. Reviewed 2026-10-12 against
deployed manager source candidate `604c1bd`, schema identity `a59d0f62a810`.

Six live generation requests used the configured Bedrock provider and
`claude-haiku-4-5` through `/api/ai/suggest`, with the actual `/api/plugins`
inventory. Prompts used synthetic local paths, reserved `.invalid` hosts and
placeholder credentials. No generated pipeline was registered or scheduled.
An independent Sol medium subagent reviewed the first five outputs and checked
real connector/transform behavior with synthetic records.

| Requested behavior | AI schema result | Deployed dry-run | Reality check |
| --- | --- | --- | --- |
| Hourly cron, filter up records with positive rx | Invalid | Invalid | Generated `cron_expr`; TRAM requires `cron`. Filter expression itself was correct. |
| SNMP interface WALK, classify rows, save category-partitioned NDJSON to SFTP | Valid | Invalid | Generated GET instead of WALK, unsupported filename tokens, an undefined expression variable and projection paths inconsistent with classified rows. |
| First matching interface from nested list, project fields with default rx | Invalid | Invalid | Invented nested selector fields, omitted required output mapping, read the original first list item and used an expression where project requires a path. |
| Sum bytes and count records by category | Valid | Invalid | Generated `sum(bytes)` / `len(bytes)` operation specs; real aggregate constructor rejects them. |
| Authenticated gNMI polling every 300 seconds | Valid | Valid | Omitted requested username/password and selected continuous streaming. |
| Simple manual local JSON filter to NDJSON | Valid | Valid | Unmodified generated pipeline executed locally: three input records, two expected up records in the output, exact record match. |

These are one-response probes, not a statistical success-rate estimate. Four of
six responses passed AI's schema-only check; two of those failed deployed
dry-run. One dry-run-passing response still contradicted the requested behavior.
The simple control demonstrates actual successful input/output execution.

## Confirmed findings

1. **Incorrect cron guidance.** `tram/api/routers/ai.py` explicitly supplies
   `schedule.cron_expr` in its system prompt. The model followed that guidance;
   configuration validation rejects the extra field. Replacing only that field
   name with `cron` makes the minimal schema reproduction pass.
2. **Incomplete nested grounding.** The generated selector uses `condition` and
   `action`, while `SelectFromListSelectionConfig` requires `output` and exactly
   one of `match` or `first_item`. Its subsequent add-field step selects element
   zero from the original list, which the selector does not replace. Its project
   default is a Python expression string, but project uses dotted path lookup.
3. **AI validation is weaker than dry-run.** `_validate_yaml` loads Pydantic
   configuration only. It does not instantiate aggregate operations or validate
   sink filename tokens. Consequently the response and editor can report that AI
   YAML passed validation while deployed dry-run rejects it. Aggregate requires
   `sum:bytes` or `{op: sum, field: bytes}`, not `sum(bytes)`.
4. **Missing source output semantics.** The SNMP response sets `classify: true`
   but projects flat interface fields. The actual classified sample contains
   `_metrics`, `_labels`, `_snmp_widths`, `_index` and `_polled_at`. Applying its
   project independently to that sample produces null interface fields and drops
   the index. The original transform chain also fails earlier because the
   expression `interface` is an undefined variable, rather than a quoted literal.
   It additionally uses regex-style classification patterns where TRAM accepts
   globs, and `{ifType}` / `{ifIndex}` instead of supported record-field token
   syntax such as `{field._labels.ifType}`.
5. **Intent is not checked by structural validation.** The SNMP pipeline chooses
   `operation: get` for a requested WALK. The gNMI response passes dry-run but
   actual source construction confirms empty username/password and mode
   `stream`. `poll_interval_seconds` does not turn stream mode into polling.
   Interval snapshots should use `once`; continuous periodic snapshots use
   `poll`. No connection to the synthetic external hosts was attempted.

The earlier schema review also confirmed that global exclusions remove filter's
required `condition`, Literal rendering loses enum choices, nested models expose
class names/ForwardRefs, and empty-default fields plus existing descriptions are
absent from AI text. The filter omission did not prevent correct generation in
these two filter examples; observed failures do not establish sole causation.

## 1.8.2 resolution

- Correct the cron skeleton and make field exclusions category-specific.
- Preserve enum choices, optional fields, nested definitions and descriptions in
  a shared authoritative schema representation; derive availability server-side.
- Add compact source-record examples and transform semantics for classification,
  projection, selection, aggregation, expressions and file partition tokens.
- Align AI result validation with the existing no-I/O dry-run and lint checks.
- Use these six prompts plus fixture-based expected outputs as regression cases;
  test requested behavior separately from structural validity.

Raw responses, generated YAML, validation results and synthetic runtime evidence
are retained locally at `/tmp/tram-ai-reality-review/`. The control's initial
standalone dry-run preceded serializer registration; the final local dry-run was
repeated with plugins loaded as at application startup, and deployed dry-run
independently passed. That baseline probe made no application fixes or release metadata changes.


## Implementation and verification

The patch uses the server registry as the authoritative inventory and
a shared descriptor for UI and AI schemas, including nested fields, enum choices,
defaults and descriptions. Generated YAML must pass configuration loading, real
constructor/wiring checks and lint before `valid: true`. These checks do not
connect sources or write sinks. Generate, modify and fix may retry once with
sanitized feedback; secret-bearing candidate values are redacted. Partial sink
construction now closes earlier sinks if a later constructor fails.

Independent Sol medium review approved the schema/validation changes and task
guidance. The first repeat made all six outputs structurally valid, but fixture
execution found remaining selection, null-default, count and MIB issues. A second
repeat fixed selection and count expressions and exposed an additional execution
detail: top-level transforms run per record; sink transforms receive a parsed
chunk, so batch grouping belongs in the sink transform chain. This is per chunk,
not a total across arbitrary files or an entire run. SNMP categorization requires
loadable IF-MIB/IANAifType-MIB assets and successful resolution. The deployed
manager resolves `ifType.1` with those modules explicitly selected.

### Final live repeat

All six unchanged prompts generated valid YAML on the first attempt with the
configured Bedrock `claude-haiku-4-5` provider. All six deployed dry-runs passed.
Four unmodified generated transform chains were executed through the local CLI
with synthetic files and exact expected output: cron filtering, first matching
interface with missing-rx default, category sums/counts and the simple control.
The generated SNMP chain passed synthetic numeric-OID resolution through actual
IF-MIB assets, enum classification, index/counter preservation and distinct
category filename rendering. The gNMI constructor retained the requested
credentials, transport, path and periodic mode. These latter two checks do not
prove live device connectivity or SFTP delivery, and six probes do not establish
a statistical generation success rate.

Dev manager image: `trishul-ram-manager:local-v182-ai-final-20261012`; schema
identity: `a41e45651d38`; runtime-module SHA256:
`1dd4dde2a2d2ea2af4b544480ac0194cb2748c25e164f6e0149d632ebb753561`.
The local image derives from the existing dependency-complete dev manager and
changes four Python modules; existing UI assets remain. Only the manager was
rolled out. The existing SNMP-to-SFTP demo remains enabled on its 300-second
schedule. No public image, version or tag was published during this dev verification.
Subsequent release status is recorded in the [v1.8.2 release review](v1.8.2-release-review.md).

Final affected suites: **327 passed**. Ruff and `git diff --check` passed.
The final backend snapshot, including the real aggregation regression, passed
**3581 tests, 38 skipped, 87.02% coverage**. The full suite ran outside the
sandbox from a tracked-source snapshot plus the new regression file, excluding
the pre-existing untracked demo and artifacts. Raw final captures and fixture results:
`/tmp/tram-ai-reality-review/post-final-guidance/`; combined dry-run/behavior log:
`/tmp/tram-ai-final-oracle.log`.
