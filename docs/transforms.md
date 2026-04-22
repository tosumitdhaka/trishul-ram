# TRAM Transforms Reference

Transforms receive and return `list[dict]`. They are applied in order, each receiving the output of the previous.

---

## rename

Rename fields in each record.

```yaml
- type: rename
  fields:
    old_name: new_name
    ne_id: network_element_id
```

Fields not in the mapping are passed through unchanged. Missing source fields are silently ignored.

---

## cast

Convert field values to a target type.

```yaml
- type: cast
  fields:
    rx_bytes: int
    ratio: float
    active: bool       # "true"/"1"/"yes" → True
    timestamp: datetime
```

**Supported types:** `str`, `int`, `float`, `bool`, `datetime`

---

## add_field

Add computed fields using safe `simpleeval` expressions.

```yaml
- type: add_field
  fields:
    rx_mbps: "rx_bytes / 1_000_000"
    label: "'high' if rx_mbps > 100 else 'normal'"
```

**Expression context:** all current record fields available as variables.
**Allowed functions:** `round`, `abs`, `int`, `float`, `str`, `len`, `min`, `max`

---

## drop

Remove fields from each record.

```yaml
- type: drop
  fields: [debug_flag, internal_id]
```

---

## value_map

Replace field values using a lookup table.

```yaml
- type: value_map
  field: severity
  mapping:
    "1": CRITICAL
    "2": MAJOR
  default: UNKNOWN
```

---

## filter

Keep only rows where a condition is truthy.

```yaml
- type: filter
  condition: "rx_mbps > 0 and status == 'active'"
```

Same `simpleeval` sandbox as `add_field`.

---

## flatten

Recursively flatten nested dicts.

```yaml
- type: flatten
  separator: "_"   # default
  max_depth: 0     # 0 = unlimited
  prefix: ""       # prepend to all keys
```

`{"a": {"b": {"c": 1}}}` → `{"a_b_c": 1}`

---

## json_flatten

Recursively hoist nested dicts, expand nested list-of-record structures, and optionally zip
paired lists into row-oriented output.

```yaml
- type: json_flatten
  explode_mode: auto        # auto | off | paths
  zip_lists: auto           # auto | off | mappings
  choice_mode: type_value   # keep | unwrap_value | type_value
  rename_style: snake_case  # none | snake_case
  ambiguity_mode: keep      # keep | error
```

Useful for decoded ASN.1 / XML / nested JSON payloads where a long chain of `unnest` and
`explode` transforms would otherwise be required.

Optional controls:

```yaml
- type: json_flatten
  explode_mode: paths
  explode_paths: [measData, measData.measInfo]
  zip_lists: mappings
  zip_mappings:
    - labels: measTypes
      values: measResults
  drop_paths: [measFileFooter]
```

---

## timestamp_normalize

Normalize heterogeneous timestamp strings/ints to a uniform format.

```yaml
- type: timestamp_normalize
  fields: [created_at, updated_at]
  input_format: null     # auto-detect (unix ms/us/ns, ISO-8601, etc.)
  output_format: iso     # "iso" or strftime pattern
  on_error: raise        # raise | null | keep
```

---

## aggregate

Group records and compute aggregations. Collapses batch into one row per group.

```yaml
- type: aggregate
  group_by: [network_element_id]
  operations:
    total_bytes: "sum:rx_bytes"
    avg_mbps: "avg:rx_mbps"
    max_mbps: {op: max, field: rx_mbps}
    count: {op: count}
```

**Supported ops:** `sum`, `avg`, `min`, `max`, `count`, `first`, `last`

---

## enrich

Left-join records with a static lookup file loaded once at init.

```yaml
- type: enrich
  lookup_file: /data/ne_metadata.csv
  lookup_format: csv     # csv | json
  join_key: network_element_id
  lookup_key: ne_id      # column in lookup file (defaults to join_key)
  add_fields: [region, vendor]   # null = add all
  prefix: "ne_"
  on_miss: keep          # keep | null_fields
```

---

## explode

Expand a list field into one row per element.

```yaml
- type: explode
  field: alarms
  drop_source: true
  include_index: false
  index_field: index
```

`{"alarms": [{"id": 1}, {"id": 2}]}` → two records: `{"id": 1}`, `{"id": 2}`

---

## deduplicate

Remove duplicate rows based on field values.

```yaml
- type: deduplicate
  fields: [ne_id, timestamp]
  keep: first    # first | last
```

---

## regex_extract

Extract named capture groups from a string field.

```yaml
- type: regex_extract
  field: syslog_msg
  pattern: "(?P<host>\\S+) (?P<process>\\S+)\\[(?P<pid>\\d+)\\]"
  destination: null   # null = merge into record
  on_no_match: keep   # keep | null | drop
```

---

## template

Build new string fields from existing fields using `{field}` placeholders.

```yaml
- type: template
  fields:
    display_name: "{vendor} {model} @ {location}"
    alert_id: "ALERT-{ne_id}-{severity}"
```

---

## mask

Redact, hash, or partially mask sensitive fields.

```yaml
- type: mask
  fields: [password, credit_card]
  mode: redact    # redact | hash | partial
  placeholder: "***"
  visible_start: 2   # for partial mode
  visible_end: 2
```

---

## validate

Drop or raise on records that fail schema rules.

```yaml
- type: validate
  rules:
    ne_id: required
    rx_bytes: {type: int, min: 0}
    status: {allowed: [active, inactive]}
  on_invalid: drop   # drop | raise
```

---

## sort

Sort all records by one or more fields.

```yaml
- type: sort
  fields: [timestamp, ne_id]
  reverse: false
```

---

## limit

Keep only the first N records in the batch.

```yaml
- type: limit
  count: 1000
```

---

## jmespath

Extract field values using JMESPath expressions. Requires `pip install tram[jmespath]`.

```yaml
- type: jmespath
  fields:
    first_alarm_id: "alarms[0].id"
    alarm_count: "length(alarms)"
```

---

## unnest

Lift a nested dict field's keys to the top level.

```yaml
- type: unnest
  field: metadata
  prefix: "meta_"
  drop_source: true
  on_non_dict: keep   # keep | drop | raise
```

`{"metadata": {"region": "eu", "tier": 1}}` → `{"meta_region": "eu", "meta_tier": 1}`

---

## hex_decode

Interpret scalar hex-string leaf values heuristically or via explicit codecs. This is especially
useful after ASN.1 decode, where `OCTET STRING` values are emitted as hex strings.

```yaml
- type: hex_decode
  mode: utf8_or_hex       # hex | utf8_or_hex | latin1_or_hex
  preserve_original: false
```

With overrides:

```yaml
- type: hex_decode
  mode: utf8_or_hex
  preserve_original: true
  overrides:
    - path: servedIMSI
      decode_as: digits
      format: tbcd
    - path: recordOpeningTime
      decode_as: timestamp
      format: bcd_semi_octet
    - path: mSTimeZone
      decode_as: timezone
      format: tbcd_quarter_hour
    - path: pGWAddress.value.value
      decode_as: ip
      format: packed
```

Supported built-in semantic targets / formats in `1.3.2`:
- `text` + `utf8` / `latin1`
- `digits` + `tbcd`
- `timestamp` + `bcd_semi_octet`
- `timezone` + `tbcd_quarter_hour`
- `ip` + `packed`
- `hex`

---

## melt

Pivot a dict-valued field into one record per key/value pair (wide → long format). Useful for producing time-series rows from wide SNMP/telemetry records.

```yaml
- type: melt
  value_field: _metrics          # required — dict field to pivot
  label_fields: [_labels]        # optional — dict fields to unnest as label columns
  metric_name_col: metric_name   # default
  metric_value_col: metric_value # default
  drop_source: true              # default — remove value_field and label_fields
  include_only: []               # if set, only melt these keys
  exclude: []                    # keys to skip
```

**Example:**

Input:
```json
{"_metrics": {"ifInOctets": 1000, "ifOutOctets": 2000}, "_labels": {"ifIndex": "1", "ifDescr": "lo"}, "_polled_at": "2026-04-09T10:00:00Z"}
```

Output (2 records):
```json
{"ifIndex": "1", "ifDescr": "lo", "metric_name": "ifInOctets",  "metric_value": 1000, "_polled_at": "..."}
{"ifIndex": "1", "ifDescr": "lo", "metric_name": "ifOutOctets", "metric_value": 2000, "_polled_at": "..."}
```

If `value_field` is missing or not a dict, the record passes through unchanged.

---

## Transform Ordering Tips

1. **rename** early — rename before other transforms reference field names
2. **cast** before arithmetic — ensure numeric types before `add_field`
3. **enrich** after cast — join keys should be the correct type
4. **drop** late — keep fields available for expressions, remove at the end
5. **filter** last — filter after all transformations to use computed fields
6. **limit** last — apply after all filtering/enrichment

---

## Example Transform Chain

```yaml
transforms:
  - type: rename
    fields: {ne_id: network_element_id, ts: timestamp}

  - type: cast
    fields: {rx_bytes: int, timestamp: datetime}

  - type: add_field
    fields:
      rx_mbps: "rx_bytes / 1_000_000"
      load_pct: "round(rx_mbps / 1000 * 100, 2)"

  - type: enrich
    lookup_file: /data/ne_metadata.csv
    join_key: network_element_id
    add_fields: [region, vendor]

  - type: value_map
    field: severity
    mapping: {"1": CRITICAL, "2": MAJOR, "3": MINOR}
    default: UNKNOWN

  - type: mask
    fields: [password, api_key]
    mode: redact

  - type: drop
    fields: [rx_bytes, debug_flag]

  - type: filter
    condition: "rx_mbps > 0"
```
