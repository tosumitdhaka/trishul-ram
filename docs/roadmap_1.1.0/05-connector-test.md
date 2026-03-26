# Feature 5 — Connector Connectivity Test

## Goal

Before saving or deploying a pipeline, let the user verify that a source or sink
can actually reach its target. Catches misconfigured hostnames, wrong credentials,
and unreachable brokers before the pipeline runs.

## New API Endpoint

```
POST /api/connectors/test
Content-Type: application/json

{
  "type": "kafka",
  "config": {
    "brokers": ["kafka-host:9092"],
    "topic": "pm-raw"
  }
}
```

Response — success:
```json
{"ok": true, "latency_ms": 12, "detail": "Connected to 1/1 brokers"}
```

Response — failure:
```json
{"ok": false, "latency_ms": null, "error": "Connection refused: kafka-host:9092"}
```

The endpoint runs synchronously (not async) with a hard 10 s timeout.
Always returns HTTP 200; `ok` field indicates pass/fail.

## Backend

New file: `tram/api/routers/connectors.py`

### Plugin interface

New mixin in `tram/core/base.py`:

```python
class ConnectorTestMixin:
    def test_connection(self) -> dict:
        """
        Returns {"ok": bool, "detail": str, "latency_ms": int | None}.
        Raise any exception to indicate failure — the endpoint catches it.
        """
        raise NotImplementedError
```

Source and sink plugins that implement `ConnectorTestMixin` get a real test.
All others fall back to a generic TCP connect probe (if `host`/`port` extractable
from config) or return `{"ok": true, "detail": "no test available"}`.

### Connector test coverage (v1.1.0)

| Type | Test method |
|------|-------------|
| `kafka` | `KafkaProducer` bootstrap connect, list topics |
| `rest` | HTTP GET to base URL, check status < 500 |
| `sql` | `engine.connect()` + `SELECT 1` |
| `local` | `os.path.exists(path)` |
| `sftp` | SSH TCP connect + key/password auth handshake |
| `s3` | `boto3` list bucket (1 object, prefix='') |
| `gcs` | `google.cloud.storage` list bucket |
| `azure_blob` | `ContainerClient.exists()` |
| `mqtt` | TCP connect to `host:port` |
| `influxdb` | `GET /ping` on InfluxDB health endpoint |
| `opensearch` | `GET /` cluster info |
| `elasticsearch` | `GET /` cluster info |
| `nats` | TCP connect to `host:port` |
| others | TCP connect probe if `host`/`port` in config; else no-op |

### Endpoint implementation sketch

```python
@router.post("/api/connectors/test")
async def test_connector(request: Request) -> dict:
    body = await request.json()
    conn_type = body.get("type")
    config = body.get("config", {})

    plugin_cls = get_plugin(conn_type)   # from registry
    if plugin_cls and hasattr(plugin_cls, "test_connection"):
        instance = plugin_cls.__new__(plugin_cls)
        instance._config = config
        return run_with_timeout(instance.test_connection, timeout=10)

    # Generic TCP fallback
    host = config.get("host") or config.get("brokers", [""])[0].split(":")[0]
    port = int(config.get("port", 0) or config.get("brokers", [""])[0].split(":")[-1])
    return tcp_probe(host, port)
```

## UI

### In the Wizard (Features 1)
- Step 2 (Source): "Test Connection" button below source config fields
- Step 4 (Sinks): "Test Connection" button on each sink card
- Result shown inline: green "Reachable (12 ms)" or red "Unreachable: Connection refused"

### In the Editor
- Toolbar button "Test Connectors" → runs test for source and all sinks in parallel
- Results shown in a collapsible panel below the editor

### In the Pipeline Detail page
- "Test Connectors" button in the pipeline header actions
- Useful for diagnosing a stuck or erroring pipeline without triggering a run

## Files Changed

| File | Change |
|------|--------|
| `tram/api/routers/connectors.py` | New |
| `tram/core/base.py` | Add `ConnectorTestMixin` |
| `tram/api/app.py` | Register connectors router |
| Selected source/sink plugins | Implement `test_connection()` |
| `tram-ui/src/api.js` | Add `api.connectors.test(type, config)` |
| `tram-ui/src/pages/wizard.js` | Test buttons on steps 2 and 4 |
| `tram-ui/src/pages/editor.js` | Test Connectors toolbar button |
| `tram-ui/src/pages/detail.js` | Test Connectors header button |
