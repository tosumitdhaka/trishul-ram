# Structured Record Shaping Design

**Status:** Implemented on the `1.3.2` branch  
**Target:** v1.3.2  
**Scope:** Generic nested JSON shaping plus adjacent decode helpers, final-output projection,
cleanup helpers, and light schema-variation handling, with ASN.1/decoded-CDR use as the primary
driver

---

## Goal

Define a cleaner structured-record shaping path for nested decoded payloads. The main change is
to redesign `json_flatten` from a heuristic "guess how to explode and zip" transform into an
explicit, ordered, config-driven transform that is predictable on telecom CDR payloads and
still generic enough for any nested JSON-like input.

The immediate motivation is the current LTE/SGW/PGW exploded pipelines. They work, but the
shape is verbose and repetitive because they need multiple `jmespath` and `explode` stages to
walk nested child lists in order. The new shaping path should make that workflow explicit:

1. `json_flatten` explodes the lists the pipeline author names, in the order they name them
2. `json_flatten` optionally zips sibling lists where positional pairing is intended
3. `json_flatten` optionally unwraps ASN.1-style CHOICE objects
4. `json_flatten` flattens remaining dicts to dotted keys and drops unwanted branches
5. `hex_decode` handles byte-derived semantic decode cases that are still structurally scalar,
   including hex payloads that require a companion bit-length field
6. `project` provides a compact final extraction/rename/defaulting stage for the shaped row
7. `drop` can optionally remove fields conditionally by per-field value lists for final cleanup
8. selected transforms can accept simple path patterns so repeated schema branches do not force
   duplicated YAML
9. `project` can optionally coalesce from multiple candidate source paths
10. `hex_decode` can optionally emit decoded values, raw values, or both

The key boundary stays the same:

- `json_flatten` decides row shape
- `hex_decode` decides how to interpret byte-derived scalar values
- `project` decides the final exported field set
- `drop` handles final conditional cleanup where fields should only disappear for specific values
- simple path-pattern matching reduces duplication across schema variants

---

## Problem Statement

The current shipped `json_flatten` is convenient for simple decoded payloads, but it is not a
good fit for CDR pipelines:

- it relies on heuristic `explode_mode: auto`
- it relies on heuristic `zip_lists: auto`
- it mixes row-shaping with opinionated CHOICE handling
- it can change output shape when sibling lists appear or disappear
- it is hard to reason about when exact row semantics matter

That is tolerable for ad hoc flattening. It is not good enough for:

- CDR mediation pipelines where row multiplication must be intentional
- analytics/KPI pipelines where parent/child list semantics differ by field
- pipelines that need to preserve some nested branches and fully explode others

The desired contract is explicit:

- no auto-discovery of explode candidates
- no auto cartesian expansion
- no hidden row-count changes
- one ordered flattening plan owned by the pipeline config
- a clear place for adjacent semantic decode helpers when fields are already structurally scalar
  but still byte-derived
- a compact final projection step so pipelines do not need repeated `jmespath` or temp-field
  scaffolding just to define the exported schema
- no need for a separate `drop_empty` transform when conditional cleanup can live in `drop`
- light schema/path variation should be handled declaratively where possible instead of copying
  near-identical blocks

---

## Non-Goals

- No vendor-specific CDR flatteners in core
- No schema-specific ASN.1 intelligence in `json_flatten`
- No telecom semantic decoding in `json_flatten`
- No giant YAML expressions for byte/bit decoding that belongs in reusable code
- No automatic guessing of the "correct" row model for an arbitrary nested document
- No replacement of primitive transforms for simple cases

`unnest`, `explode`, `flatten`, `jmespath`, `select_from_list`, and `coalesce_fields` remain
the preferred tools when a pipeline only needs one or two small structural steps.

---

## Design Principles

### 1. Explicit beats heuristic

The pipeline author must state which list paths are row-generating. `json_flatten` must never
scan the tree and decide that on its own.

### 2. Structure first, meaning later

`json_flatten` produces rows and dotted keys. Transforms like `hex_decode`, `value_map`,
`timestamp_normalize`, and future semantic codecs can run afterward.

### 3. Preserve data unless asked not to

If a branch is not exploded, not dropped, and still within `max_depth`, it should survive in
the output either as dotted scalar keys or as a preserved list/object value.

### 4. Generic JSON contract

The transform works on dict/list/scalar trees. ASN.1-specific patterns such as
`{"type": ..., "value": ...}` are supported only through explicit `choice_unwrap`
configuration, not hardcoded ASN.1 behavior.

### 5. Adjacent decode helpers stay generic too

Some fields are structurally simple but semantically encoded. A good example is an ASN.1 BIT
STRING exported as a two-part value such as `["c4000000", 32]`. That does not justify
vendor-specific pipeline logic, but it does justify a generic decode helper:

- the payload is already hex
- the declared bit length matters
- the mapping from bit index to symbolic names should stay configurable

That belongs in `hex_decode`, not in `json_flatten`, and not in repeated `add_field`
expressions beyond simple field extraction.

### 6. Final schema projection should be declarative

Once a row is structurally correct, the pipeline still needs to define the exported schema:

- which fields are kept
- which fields are renamed
- which optional fields get defaults

Today that usually takes repeated `jmespath`, `rename`, `coalesce_fields`, and temp fields.
That is workable, but verbose. A small `project` transform is the right final-stage primitive
for this.

### 7. Cleanup should extend `drop`, not add `drop_empty`

Pipelines often need to remove fields only when they carry empty/noisy values such as:

- `null`
- `""`
- `[]`
- `{}`

That should not require a separate `drop_empty` transform. The existing `drop` transform is
the correct home for this behavior if it grows a conditional per-field form.

### 8. Schema variation needs light path abstraction, not custom logic

Across LTE, SGW, and PGW, the same semantic field often appears in:

- slightly different nested paths
- repeated extension branches
- sibling branches that differ only by one segment

That does not justify a new expression engine. It does justify simple path-pattern support in a
few targeted places so the pipeline does not duplicate nearly identical blocks.

### 9. Final projection should support fallback paths

Sometimes the output field is stable but the source path varies by schema or record family.
That should be handled declaratively in `project`, not by forcing a separate
`coalesce_fields` block before every final rename.

---

## Current Config

```yaml
- type: json_flatten
  explode_paths:
    - listOfTrafficVolumes
    - listOfServiceData
  separator: "."
  keep_empty_rows: true
  preserve_lists: true
  max_depth: 0

  zip_groups:
    - fields:
        measTypes: meas_type
        measResults: meas_result
      strict: true

  choice_unwrap:
    paths: [meas_result, pGWAddress]
    mode: value          # keep | value | both
    type_suffix: "_type"
    value_suffix: ""

  drop_paths:
    - diagnostics
    - recordExtensions.debugBranch
```

### Core fields

- `explode_paths: list[str]`
  Ordered list paths to explode. These are applied in sequence against the current row state
  after all prior explosions have already been applied. No implicit explosion happens outside
  this list.
- `separator: str = "."`
  Separator used when flattening nested dicts to output keys.
- `keep_empty_rows: bool = true`
  When an exploded list is missing or empty, keep the parent row instead of dropping it.
- `preserve_lists: bool = true`
  When `true`, non-exploded lists remain as list-valued fields in the output. When `false`,
  encountering a non-exploded list during flattening raises `TransformError`. This is the
  strict mode for pipelines that want scalar-only output.
- `max_depth: int = 0`
  Maximum dict-flatten depth. `0` means unlimited.

### Optional fields

- `zip_groups: list[ZipGroupConfig] = []`
  Explicit positional zipping for sibling lists that should travel together instead of causing
  separate explosions.
- `choice_unwrap: ChoiceUnwrapConfig | null = null`
  Optional CHOICE handling for objects shaped like `{"type": x, "value": y}`.
- `drop_paths: list[str] = []`
  Paths removed after explosion/zip/choice handling and after flattening has produced the final
  row keys. `drop_paths` operates on the final flattened row keys, not on the original nested
  source tree.

---

## Config Shape

### ZipGroupConfig

```yaml
zip_groups:
  - fields:
      measTypes: meas_type
      measResults: meas_result
    strict: true
```

- `fields: dict[str, str]`
  Mapping of sibling source list fields to output field names. All source fields must exist at
  the same structural level.
- `strict: bool = true`
  When `true`, all lists in the group must have identical lengths or the transform raises
  `TransformError`. When `false`, the zip group is skipped and the original lists remain.

This replaces the current heuristic `zip_lists` / `zip_mappings` split with one explicit model.

### ChoiceUnwrapConfig

```yaml
choice_unwrap:
  paths: [pGWAddress, result]
  mode: both
  type_suffix: "_type"
  value_suffix: ""
```

- `paths: list[str]`
  Paths where CHOICE-shaped dicts should be unwrapped. Empty is not allowed; if no CHOICE
  handling is needed, omit the block entirely.
- `mode: keep | value | both`
  - `keep`: leave the CHOICE object unchanged
  - `value`: replace the CHOICE object with its `value`
  - `both`: emit two fields using `type_suffix` and `value_suffix`
- `type_suffix: str = "_type"`
- `value_suffix: str = ""`

This deliberately removes the confusing `choice_mode: keep | unwrap_value | type_value`
contract from the current implementation.

---

## Execution Order

The order is fixed and must be documented because row semantics depend on it.

### 1. Explode `explode_paths` in order

Each path is applied to the current row set in sequence.

Example:

```yaml
explode_paths:
  - serviceData
  - listOfTrafficVolumes
```

This means:

1. one row per `serviceData[]` item
2. then one row per `listOfTrafficVolumes[]` item inside each selected `serviceData` item

Each entry in `explode_paths` is evaluated against the current row state after the previous
entry has already been exploded. If an earlier explosion merged dict element keys into the row,
later paths must refer to those merged/current keys, not to the original nested source path.

If the path is missing or the value is not a list:

- `keep_empty_rows: true` -> keep the row unchanged
- `keep_empty_rows: false` -> drop that row from this stage

If the element is:

- a dict: merge its keys into the row
- a scalar: write it back to the same path before later flattening

### 2. Apply `zip_groups`

At each row, if a configured zip group is present at the current level, zip those sibling lists
positionally and emit one row per zipped index.

This is for structures like:

```yaml
{
  "measTypes": ["cpu", "mem"],
  "measResults": [10, 20]
}
```

which should become:

```yaml
{"meas_type": "cpu", "meas_result": 10}
{"meas_type": "mem", "meas_result": 20}
```

and not cartesian combinations.

### 3. Apply `choice_unwrap`

Only configured paths are touched. `json_flatten` does not globally scan the tree for CHOICE
objects anymore. `choice_unwrap.paths` are evaluated against the current row state after
explosion and zipping, using the current merged key names at that point.

### 4. Flatten dicts to dotted keys

Remaining dicts are flattened recursively using `separator`.

Example:

```yaml
{"abc": {"bcd": {"cde": 123}}}
```

becomes:

```yaml
{"abc.bcd.cde": 123}
```

with the default `separator: "."`.

Non-exploded lists:

- remain list-valued fields when `preserve_lists: true`
- raise `TransformError` when `preserve_lists: false`

### 5. Apply `drop_paths`

Drops happen after flattening. `drop_paths` operates on the final flattened row keys after
explosion, zip, choice unwrap, and dict flattening are complete.

---

## Why This Helps the CDR Pipelines

The current LTE/SGW/PGW exploded pipelines repeatedly do this pattern:

1. use `jmespath` to copy a child list to a temporary field
2. `explode` that temporary field
3. repeat for the next nested child list
4. flatten or rename afterward

That works, but the row-shaping intent is scattered across many blocks.

The current `json_flatten` contract keeps the same explicitness while moving the ordered explode plan into one
transform. The pipeline stays readable:

- `explode_paths` says which child collections define rows
- `zip_groups` says which arrays are positional pairs
- `choice_unwrap` says where CHOICE objects should be simplified
- later transforms handle decode, lookups, rename, timestamps, and other semantics

---

## Migration Examples

### Example 1: LTE alternate identifiers

Current pattern:

```yaml
- type: jmespath
  fields:
    subscription_rows: serviceInformation.subscriptionID
- type: explode
  field: subscription_rows
- type: rename
  fields:
    subscriptionIDType: subscription_id_type
    subscriptionIDData: subscription_id_data
```

Current shape:

```yaml
- type: json_flatten
  explode_paths:
    - serviceInformation.subscriptionID
  separator: "."
  keep_empty_rows: true
  preserve_lists: true
```

This keeps the intended 1 -> N expansion explicit and removes the temporary field.

### Example 2: SGW traffic volumes

Current pattern:

```yaml
- type: jmespath
  fields:
    traffic_rows: listOfTrafficVolumes
- type: explode
  field: traffic_rows
```

Current shape:

```yaml
- type: json_flatten
  explode_paths:
    - listOfTrafficVolumes
```

### Example 3: PGW service segments then traffic volumes

Current pattern:

```yaml
- type: jmespath
  fields:
    service_rows: listOfServiceData
- type: explode
  field: service_rows
- type: jmespath
  fields:
    traffic_rows: service_rows.listOfTrafficVolumes
- type: explode
  field: traffic_rows
```

Current shape:

```yaml
- type: json_flatten
  explode_paths:
    - listOfServiceData
    - listOfTrafficVolumes
  keep_empty_rows: true
  preserve_lists: true
```

This captures the intended ordered child expansion directly.

---

## Interaction With Other Transforms

Recommended post-`json_flatten` transforms in CDR pipelines:

- `hex_decode` for TBCD, packed IP, text, timezone, and other byte-derived fields
- `timestamp_normalize` for epoch / `epoch_ms` output
- `value_map` for enumerations
- `project` for final field selection, rename, and defaults
- `rename` for final clean output names
- `select_from_list` for curated "pick one matching item from a child list" behavior
- `coalesce_fields` for canonical field fallback chains
- `drop` for unconditional or value-conditional cleanup
- path-pattern support where one rule should apply to repeated/variant schema branches

Recommended cases to **not** use `json_flatten`:

- preserve/raw export where the nested structure itself is useful
- curated row shaping where only one child list should be projected selectively
- pipelines that already read clearly with one `explode` and one `unnest`

### `hex_decode` support for BIT STRING values

Some decoded ASN.1 payloads expose BIT STRING values as a pair of:

- hex payload
- declared bit length

Example source value:

```yaml
service_condition_change: ["c4000000", 32]
```

The implemented shaping pattern is:

1. extract the payload and bit length with ordinary transforms such as `add_field`
2. extend `hex_decode` so it can decode the payload using the companion bit-length field
3. keep bit-name lookup mapping configurable in pipeline YAML

Example target shape:

```yaml
- type: add_field
  fields:
    service_condition_change_hex: "service_condition_change[0]"
    service_condition_change_bits: "service_condition_change[1]"

- type: hex_decode
  overrides:
    - path: service_condition_change_hex
      decode_as: bit_flags
      bit_length_field: service_condition_change_bits
      mapping:
        0: qosChange
        1: tariffTime
        5: recordClosure
```

The list indexing in `add_field` expressions above is `simpleeval` expression syntax, not
`path_utils` dotted-path syntax. This does not change the existing "no list-index support in
shared path helpers" boundary.

Why this path:

- no new `unpack` transform is required
- no new `byte_decode` transform is required yet
- the payload is already hex, so `hex_decode` is the correct home
- the implementation stays generic for any bit-field payload, not just `service_condition_change`

### Path-pattern support

Some transform configs should accept simple glob-style path patterns so one rule can match
multiple repeated branches.

Example:

```yaml
overrides:
  - path: "*.pGWAddress"
    decode_as: ip
    format: packed
```

`*` is a single-segment wildcard only. It matches exactly one path segment in that position.
So `*.pGWAddress` matches `serviceInformation.pGWAddress`, but it does not mean "match any path
ending in `pGWAddress` at any depth."

Current scope stays narrow:

- exact path matching remains the default
- support only simple glob-style matching such as `*`
- no arbitrary expressions
- no recursive query language

Implemented targets:

- `hex_decode.overrides[].path`
- `json_flatten.drop_paths`

This is meant to reduce duplicated YAML across repeated schema branches, not to become a second
query language.

### `project` transform

`project` is a declarative final-shaping transform. It is not a replacement for
`json_flatten`. It runs after row shape is already correct and answers a narrower question:
"what should the output row look like?"

Example:

```yaml
- type: project
  fields:
    record_type: recordType
    served_imsi:
      source: servedIMSI
    served_msisdn:
      source: servedMSISDN
    apn:
      source: accessPointNameNI
    user_location_information:
      source: userLocationInformation
      default: null
```

Intended semantics:

- keep only the declared output fields
- support simple rename via `output_name: source_path`
- support expanded form with:
  - `source`
  - `source_any`
  - `default`
  - `required`

This should remove a lot of repetitive final-stage YAML from LTE/SGW/PGW pipelines without
adding expression-heavy logic.

Fallback-path form:

```yaml
- type: project
  fields:
    served_imsi:
      source_any: [servedIMSI, subscription_id_data, serviceInformation.servedIMSI]
    apn:
      source_any: [accessPointNameNI, accessPointNameOI]
      default: null
```

Intended semantics of `source_any`:

- evaluate candidate paths in order
- first path that exists wins, even if its value is `null`
- if none are found:
  - use `default` if present
  - otherwise fail only when `required: true`

Compact `output: source.path` form is equivalent to an expanded `source` rule with an implicit
`default: null`. Use expanded form with `required: true` when a missing source path must fail.

This overlaps with `coalesce_fields`, but is justified here because it belongs to the final
output-schema contract.

### `drop` enhancement for conditional cleanup

Current unconditional form stays valid:

```yaml
- type: drop
  fields: [debug_blob, temp_field]
```

Conditional form:

```yaml
- type: drop
  fields:
    served_sip_uri: [null, ""]
    diagnostics.note: [null, ""]
    policy_rules: [[], {}]
```

Intended semantics:

- `fields: list[str]` -> unconditional drop, as today
- `fields: dict[str, list[Any]]` -> drop only when the field value equals one of the configured
  values
- missing field -> no-op
- dotted paths -> supported via `get_path`

This covers the useful `drop_empty` class of behavior without adding another overlapping
transform.

### `hex_decode` output modes

Schema consumers vary. Some want the decoded semantic value only. Others want the original raw
representation preserved for audit/debug. `hex_decode` should support that explicitly.

Examples:

```yaml
- type: hex_decode
  preserve_original: true
  original_suffix: _hex
```

and for bit fields:

```yaml
- type: hex_decode
  overrides:
    - path: service_condition_change_hex
      decode_as: bit_flags
      bit_length_field: service_condition_change_bits
      mapping:
        0: qosChange
        1: tariffTime
      output: both   # names | indexes | both
```

Minimum useful output control:

- in-place decoded replacement
- preserve original alongside decoded value
- for `bit_flags`: `names`, `indexes`, or `both`

---

## Edge Cases

### Empty child lists

`keep_empty_rows` controls whether empty arrays drop the row or preserve the parent row.

### Missing explode paths

Treat the same as empty child lists:

- keep row when `keep_empty_rows: true`
- drop row when `keep_empty_rows: false`

### Non-list value at an explode path

Raise `TransformError`. This is a config error, not a data-shape success case.

### Non-dict intermediate when writing exploded scalar values

Raise `TransformError`. The transform must not silently create impossible mixed structures.

### Zip length mismatch

- `strict: true` -> raise `TransformError`
- `strict: false` -> skip that zip group and leave original list fields unchanged

### `max_depth`

When `max_depth > 0`, dict flattening stops at that level and the remaining subtree is kept as
an object value.

---

## Current Model Shape

The current `json_flatten` model uses the explicit contract:

```python
class JsonFlattenZipGroupConfig(BaseModel):
    fields: dict[str, str]
    strict: bool = True


class JsonFlattenChoiceUnwrapConfig(BaseModel):
    paths: list[str]
    mode: Literal["keep", "value", "both"] = "value"
    type_suffix: str = "_type"
    value_suffix: str = ""


class JsonFlattenTransformConfig(BaseModel):
    type: Literal["json_flatten"]
    explode_paths: list[str] = Field(default_factory=list)
    separator: str = "."
    keep_empty_rows: bool = True
    preserve_lists: bool = True
    max_depth: int = 0
    zip_groups: list[JsonFlattenZipGroupConfig] = Field(default_factory=list)
    choice_unwrap: JsonFlattenChoiceUnwrapConfig | None = None
    drop_paths: list[str] = Field(default_factory=list)
```

Fields removed from the current model:

- `explode_mode`
- `zip_lists`
- `zip_mappings`
- `choice_mode`
- `rename_style`
- `keep_paths`
- `ambiguity_mode`

Those fields exist to support the heuristic version. They are not needed in the explicit one.

`HexDecodeOverrideConfig` would also grow one adjacent capability for bit fields:

```python
class HexDecodeOverrideConfig(BaseModel):
    path: str
    decode_as: str
    format: str | None = None
    bit_length_field: str | None = None
    mapping: dict[int, str] = Field(default_factory=dict)
    output: str | None = None
```

Intended semantics:

- `decode_as: bit_flags`
- `path` points at the hex payload field
- `bit_length_field` points at the sibling/root field containing the declared bit count
- `mapping` is optional; if omitted, raw bit indexes can be returned
- `output` is initially relevant for `bit_flags`

Output mode can stay a follow-up detail, but the minimum useful forms are:

- list of set bit indexes
- list of mapped names
- or both

`ProjectTransformConfig` is a separate final-stage primitive:

```python
class ProjectFieldConfig(BaseModel):
    source: str | None = None
    source_any: list[str] = Field(default_factory=list)
    default: Any | None = None
    required: bool = False

    @model_validator(mode="after")
    def validate_source_mode(self):
        has_source = self.source is not None
        has_source_any = bool(self.source_any)
        if has_source == has_source_any:
            raise ValueError("exactly one of source or source_any must be set")
        return self


class ProjectTransformConfig(BaseModel):
    type: Literal["project"]
    fields: dict[str, str | ProjectFieldConfig]
```

Validation rule:

- exactly one of `source` or `source_any` must be provided in expanded form
- `bool(self.source_any)` is intentional here: an empty `source_any: []` means "not set", so the
  validator treats it the same as omission

`DropTransformConfig` is widened from list-only to dual-form:

```python
class DropTransformConfig(BaseModel):
    type: Literal["drop"]
    fields: list[str] | dict[str, list[Any]]

    @field_validator("fields")
    @classmethod
    def validate_conditional_fields(cls, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(item, list):
                    raise ValueError(
                        f"conditional drop field {key!r} must map to a list of values"
                    )
        return value
```

---

## Compatibility / Migration Notes

This was a behavior change, so the compatibility path is explicit: replace the heuristic
`json_flatten` contract with the explicit ordered one and call that out clearly in the
changelog and transform reference.

Implemented migration behavior:

1. replace the current `json_flatten` config model with the explicit contract
2. reject legacy heuristic fields such as `explode_mode`, `zip_lists`, and `ambiguity_mode`
3. rewrite the LTE/SGW/PGW exploded example pipelines to use the new `json_flatten` syntax
4. keep primitive-transform examples in docs for users who do not need the compound transform

---

## Testing Plan

Unit coverage includes:

- ordered multi-stage explosion
- missing path vs empty list with both `keep_empty_rows` modes
- scalar element explosion back into nested path
- zipped sibling lists with strict and non-strict behavior
- CHOICE unwrap in `keep`, `value`, and `both` modes
- flattening to dotted keys with configurable separator
- preserved non-exploded lists
- strict scalar-only mode with `preserve_lists: false`
- `max_depth` subtree preservation
- `drop_paths` against nested paths and final dotted keys
- `hex_decode` BIT STRING decoding with a companion `bit_length_field`
- padding truncation based on declared bit length
- mapped-name output for configured bit indexes
- raw-index output when no mapping is provided
- `hex_decode` output mode behavior for `bit_flags`
- `project` simple rename form
- `project` expanded form with `default`
- `project` missing required field failure
- `project` dotted-path extraction
- `project.source_any` ordered fallback behavior
- `drop` unconditional list form remains unchanged
- `drop` conditional dict form drops only matching empty/noisy values
- `drop` dotted-path conditional cleanup
- path-pattern matching on supported transforms

Integration validation includes:

- LTE exploded pipeline migrated to one `json_flatten` plus decode/projection stages
- SGW exploded pipeline migrated the same way
- PGW exploded pipeline migrated the same way
- row-count verification against the already-validated local sample datasets
- one PGW/SGW case where `service_condition_change` is decoded from hex payload plus bit length
- one migrated LTE/SGW/PGW pipeline that uses `json_flatten` and `project` instead of repeated final-stage
  extraction blocks
- one migrated pipeline that uses conditional `drop` cleanup instead of manual post-processing
- one migrated pipeline where a single path-pattern rule replaces duplicated schema-branch rules

---

## Outcome

Proceed with this as a structured shaping slice:

- replace the heuristic `json_flatten` with the explicit row-shaping contract
- extend `hex_decode` for generic BIT STRING decoding when a companion bit-length field exists
- add `project` as a compact final output-schema transform
- extend `drop` with a per-field conditional form instead of adding `drop_empty`
- add light path-pattern support where repeated schema branches make exact-path rules too verbose
- add `project.source_any` for final-schema fallback extraction
- add richer `hex_decode` output controls for mixed analytics/audit needs

The current CDR pipelines already proved the desired behavior is config-driven and ordered. The
missing step is to make that contract first-class in reusable transforms instead of encoding it
through repeated `jmespath + explode` scaffolding, final-stage temp fields, and one-off YAML
decode hacks.
