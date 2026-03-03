# TRAM Transforms Reference

Transforms receive and return `list[dict]`. They are applied in order, each receiving the output of the previous.

## rename

Rename fields in each record.

```yaml
- type: rename
  fields:
    old_name: new_name
    ne_id: network_element_id
    ts: timestamp
```

Fields not in the mapping are passed through unchanged. Missing source fields are silently ignored.

## cast

Convert field values to a target type.

```yaml
- type: cast
  fields:
    rx_bytes: int        # str → int
    ratio: float         # str → float
    active: bool         # "true"/"1"/"yes" → True
    timestamp: datetime  # ISO-8601 string → Python datetime
```

**Supported types:** `str`, `int`, `float`, `bool`, `datetime`

**Bool truthy values (case-insensitive):** `true`, `1`, `yes`, `on`

**Datetime parsing:** Uses Python's `datetime.fromisoformat()`. Supports ISO-8601 strings including timezone offsets.

## add_field

Add computed fields using safe expression evaluation (`simpleeval`).

```yaml
- type: add_field
  fields:
    rx_mbps: "rx_bytes / 1_000_000"
    load_pct: "round(rx_mbps / 1000 * 100, 2)"
    label: "'high' if rx_mbps > 100 else 'normal'"
```

**Expression context:** All current record fields are available as variables.

**Allowed operators:** arithmetic (`+`, `-`, `*`, `/`, `//`, `**`, `%`), comparison, logical, ternary

**Allowed functions:** `round`, `abs`, `int`, `float`, `str`, `len`, `min`, `max`

**Security:** Uses `simpleeval` — no `eval()`, no builtins, no imports, no exec

## drop

Remove fields from each record.

```yaml
- type: drop
  fields: [debug_flag, internal_id, raw_bytes]
```

Fields not present in a record are silently ignored.

## value_map

Replace field values using a lookup table.

```yaml
- type: value_map
  field: severity
  mapping:
    "1": CRITICAL
    "2": MAJOR
    "3": MINOR
    "4": WARNING
    "5": INDETERMINATE
  default: UNKNOWN      # used when value not in mapping (optional)
```

If `default` is omitted and a value is not in the mapping, the original value is preserved.

## filter

Remove rows that don't match a condition.

```yaml
- type: filter
  condition: "rx_mbps > 0"
```

```yaml
- type: filter
  condition: "status == 'active' and rx_mbps > 0.5"
```

Uses the same safe `simpleeval` sandbox as `add_field`. Rows where the condition evaluates to falsy are dropped.

## Transform Ordering Tips

1. **rename** early — rename before other transforms that reference field names
2. **cast** before arithmetic — ensure numeric types before `add_field` expressions
3. **drop** late — keep fields available for expressions, drop at the end
4. **filter** last — filter after all transformations to use computed fields

## Example Pipeline Transform Chain

```yaml
transforms:
  # 1. Normalize field names first
  - type: rename
    fields:
      ne_id: network_element_id
      ts: timestamp

  # 2. Cast to correct types
  - type: cast
    fields:
      rx_bytes: int
      timestamp: datetime
      active: bool

  # 3. Compute derived fields
  - type: add_field
    fields:
      rx_mbps: "rx_bytes / 1_000_000"
      load_pct: "round(rx_mbps / 1000 * 100, 2)"

  # 4. Map coded values to labels
  - type: value_map
    field: severity
    mapping:
      "1": CRITICAL
      "2": MAJOR
    default: UNKNOWN

  # 5. Drop internal fields
  - type: drop
    fields: [rx_bytes, debug_flag]

  # 6. Filter out zero-traffic rows
  - type: filter
    condition: "rx_mbps > 0"
```
