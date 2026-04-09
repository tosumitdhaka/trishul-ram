# TRAM v1.5.0 — Work in Progress

Tracking changes made after v1.1.4 that will form the v1.5.0 release.
Update this file as features are added. Delete when version is committed to CHANGELOG.md.

---

## Transforms

### New: `melt` — wide-to-long pivot transform

Pivots a dict-valued field into one record per key/value pair. The inverse of `aggregate`.

**Use case:** SNMP `classify: true` output has `_metrics` (numeric OIDs) and `_labels` (string OIDs)
grouped as nested dicts. `melt` produces one time-series row per metric per interface — the standard
format for TSDBs (InfluxDB, VictoriaMetrics, Prometheus remote-write).

**Config:**
```yaml
- type: melt
  value_field: _metrics          # dict to pivot into rows (required)
  label_fields: [_labels]        # dict fields to unnest as label columns
  metric_name_col: metric_name   # default
  metric_value_col: metric_value # default
  drop_source: true              # remove value_field + label_fields from output
  include_only: []               # optional: only melt these keys
  exclude: []                    # optional: skip these keys
```

**Input → Output example (ifTable, 1 interface):**
```
Input (1 record):
  _metrics: {ifInOctets: 516499001, ifOutOctets: 516499001, ifInErrors: 0}
  _labels:  {ifIndex: "1", ifDescr: "lo", ifOperStatus: "1"}
  _polled_at: "2026-04-09T10:00:00Z"

Output (3 records — one per metric):
  {ifIndex: "1", ifDescr: "lo", ifOperStatus: "1", metric_name: "ifInOctets",  metric_value: 516499001, _polled_at: "..."}
  {ifIndex: "1", ifDescr: "lo", ifOperStatus: "1", metric_name: "ifOutOctets", metric_value: 516499001, _polled_at: "..."}
  {ifIndex: "1", ifDescr: "lo", ifOperStatus: "1", metric_name: "ifInErrors",  metric_value: 0,         _polled_at: "..."}
```

**CSV output** (clean, no nested fields):
```
ifIndex,ifDescr,ifOperStatus,metric_name,metric_value,_polled_at
1,lo,1,ifInOctets,516499001,2026-04-09T10:00:00Z
1,lo,1,ifOutOctets,516499001,2026-04-09T10:00:00Z
1,lo,1,ifInErrors,0,2026-04-09T10:00:00Z
```

---

## Helm

### `tram-auth` secret auto-created on install

Added `helm/templates/secret-auth.yaml` — generates a stable 64-char random `TRAM_AUTH_SECRET`
on first `helm install` using `lookup` (no-op on `helm upgrade`, existing value preserved).

**Why it matters:** In cluster mode (multiple replicas) all pods must share the same HMAC secret
so that a session token issued by `tram-0` is accepted by `tram-1`/`tram-2`. Without a shared
secret each pod uses its own random key and cross-pod browser logins fail silently.

Previously required manual: `kubectl create secret generic tram-auth ...`

### `keys.secretName` defaults to empty

`helm/values.yaml`: `keys.secretName` now defaults to `""` (was `"tram-keys"`).

The statefulset already had `{{- if .Values.keys.secretName }}` guards on the volume and
volumeMount, but the non-empty default caused `CreateContainerConfigError` on fresh clusters
where the secret didn't exist yet. Fresh installs now start without any manual secret creation.

Set `keys.secretName: "tram-keys"` to re-enable SSH key mounting for SFTP connectors.

---

## Bug Fixes

### SNMP `yield_rows` — only 1 record in output file (overwrite bug)

**Root cause:** `yield_rows=true` was yielding one chunk per interface row. Each chunk triggered
a separate `sink.write()` call with the same `run_timestamp`-based filename → every write
opened the file in `"wb"` mode and overwrote the previous record.

**Fix:** Collect all rows into a single list and yield one JSON array payload per poll cycle.
Both classify and non-classify paths fixed in `tram/connectors/snmp/source.py`.

### SNMP `ifPhysAddress` — garbled binary output

**Root cause:** pysnmp `OctetString.__str__()` decodes raw bytes as latin-1, producing garbage
characters for MAC addresses and other binary OIDs.

**Fix:** Added `_snmp_val_to_str()` static method with smart serialization:
- Printable ASCII → decoded as ASCII string
- 6-byte sequences → formatted as `aa:bb:cc:dd:ee:ff` MAC address
- Other binary → `0x` hex string

### `{timestamp}` filename token showing run_id hex instead of datetime

**Root cause:** `meta["run_id"]` (short hex string) was being used as the fallback for
`{timestamp}` in `_render_filename()`.

**Fix:** Executor now injects `meta["run_timestamp"]` (human-readable `%Y%m%dT%H%M%S` string,
consistent across all chunks in a run). Sinks use `run_timestamp` for `{timestamp}` and
`run_id` for `{run_id}` as separate independent tokens.

### Pipeline status flickering between `stopped` ↔ `scheduled` after manual trigger

**Root cause:** In cluster mode, a manual trigger can land on a non-owning node (no APScheduler
job). After run success, `has_job=False` → status set to `stopped`. The owning node's next
interval fire then set it back to `scheduled`.

**Fix:** Non-owning node detects `interval`/`cron` + `enabled=true` after a successful run and
calls `_schedule_pipeline()` instead of setting `stopped`.

### Pipeline deregistered during active run

**Root cause:** `_sync_from_db()` (30s loop) detected a yaml diff and called `stop_pipeline()`
mid-run, killing the active execution.

**Fix:** `_sync_from_db()` now checks `state.status == "running"` and defers re-registration
until the run completes.

### Smart interval scheduling after pod restart

**Root cause:** After a pod restart, every interval pipeline fired immediately (APScheduler
`next_run_time` defaulted to `now`), causing a thundering-herd poll storm on restart.

**Fix:** `_add_interval_job()` queries `state.last_run` from DB and calculates remaining time:
```
delay = max(0, interval_seconds - elapsed_since_last_run)
next_run_time = now + delay
```
Pipeline fires at the correct wall-clock time after restart, not always immediately.

### `_load_from_db failed: name 'datetime' is not defined`

**Root cause:** `_add_interval_job()` used `datetime.now(UTC)` but `datetime` was not imported
at module level in `scheduler.py`.

**Fix:** Added `from datetime import UTC, datetime, timedelta` to scheduler imports.

---

*Last updated: 2026-04-09*
