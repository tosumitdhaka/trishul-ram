# TRAM Connector Guide

## How to Add a New Connector

Adding a new protocol requires exactly **3 steps** and zero changes to the core engine.

### Step 1 — Create connector file(s)

```python
# tram/connectors/myproto/source.py
from tram.registry.registry import register_source
from tram.interfaces.base_source import BaseSource

@register_source("myproto")
class MyProtoSource(BaseSource):
    def __init__(self, config: dict):
        self.host = config["host"]

    def read(self):
        """Yield (bytes, meta) tuples. Finite or infinite generator."""
        for item in self._fetch():
            yield item, {"source": "myproto"}
```

### Step 2 — Add import to `connectors/__init__.py`

```python
from tram.connectors.myproto import source  # noqa: F401
```

### Step 3 — Add Pydantic config model to `tram/models/pipeline.py`

```python
class MyProtoSourceConfig(BaseModel):
    type: Literal["myproto"]
    host: str
    port: int = 9000

# Add MyProtoSourceConfig to the SourceConfig discriminated union
```

That's it. The pipeline YAML immediately supports `source.type: myproto`.

---

## Built-in Sources

### sftp
| Parameter | Default | Description |
|-----------|---------|-------------|
| `host` | required | SFTP server |
| `port` | 22 | SSH port |
| `username` | required | SSH username |
| `password` / `private_key_path` | — | Auth (one required) |
| `remote_path` | required | Directory to read |
| `file_pattern` | `"*"` | Glob filter |
| `move_after_read` | — | Move files here |
| `delete_after_read` | false | Delete after reading |
| `skip_processed` | false | Skip files already recorded in the DB for this pipeline (requires SQLite/DB persistence) |

### local
| Parameter | Default | Description |
|-----------|---------|-------------|
| `path` | required | Local directory |
| `file_pattern` | `"*"` | Glob filter |
| `recursive` | false | Recurse subdirectories |
| `move_after_read` / `delete_after_read` | — | Post-read action |
| `skip_processed` | false | Skip files already recorded in the DB for this pipeline |

### rest
| Parameter | Default | Description |
|-----------|---------|-------------|
| `url` | required | HTTP endpoint |
| `method` | GET | HTTP method |
| `auth_type` | none | `none` / `basic` / `bearer` |
| `response_path` | — | Dot-path to extract array from response |
| `paginate` | false | Enable offset pagination |

### kafka
| Parameter | Default | Description |
|-----------|---------|-------------|
| `brokers` | required | Bootstrap servers |
| `topic` | required | Topic or list of topics |
| `group_id` | pipeline name | Consumer group (defaults to pipeline name for isolation) |
| `auto_offset_reset` | latest | `latest` / `earliest` |
| `security_protocol` | PLAINTEXT | SASL/SSL options |

### ftp
| Parameter | Default | Description |
|-----------|---------|-------------|
| `host` | required | FTP server |
| `username` / `password` | required | FTP credentials |
| `remote_path` | `/` | Remote directory |
| `passive` | true | Passive mode |
| `skip_processed` | false | Skip files already recorded in the DB for this pipeline |

### s3
| Parameter | Default | Description |
|-----------|---------|-------------|
| `bucket` | required | S3 bucket |
| `prefix` | `""` | Key prefix filter |
| `endpoint_url` | — | Override (e.g. MinIO) |
| `aws_access_key_id` / `aws_secret_access_key` | — | Credentials |
| `skip_processed` | false | Skip keys already recorded in the DB for this pipeline |

### syslog
UDP/TCP syslog receiver. `host`, `port` (514), `protocol` (udp/tcp).

### snmp_trap
SNMP trap receiver. `host`, `port` (162), `community`, `version`.

**MIB integration (v1.0.0):**
| Parameter | Default | Description |
|-----------|---------|-------------|
| `mib_dirs` | `[]` | Paths to compiled (Python .py) MIB directories |
| `mib_modules` | `[]` | MIB module names to load, e.g. `["IF-MIB", "SNMPv2-MIB"]` |
| `resolve_oids` | `true` | Use symbolic names as JSON keys; `false` = numeric dotted-decimal |

### snmp_poll
SNMP GET/WALK polling. `host`, `oids: list[str]`, `operation` (get/walk).

**MIB integration (v1.0.0):** Same `mib_dirs`, `mib_modules`, `resolve_oids` fields as `snmp_trap`.

Compile raw `.mib` files to Python format with:
```bash
pip install tram[mib]
tram mib compile /path/to/IF-MIB.mib --out /mibs/compiled/
```

### mqtt
| Parameter | Default | Description |
|-----------|---------|-------------|
| `host` | required | MQTT broker |
| `topic` | required | Topic to subscribe |
| `qos` | 0 | Quality of service |
| `tls` | false | Enable TLS |

### amqp
| Parameter | Default | Description |
|-----------|---------|-------------|
| `url` | `amqp://guest:guest@localhost:5672/` | AMQP connection URL |
| `queue` | required | Queue name |
| `prefetch_count` | 10 | Prefetch limit |

### nats
| Parameter | Default | Description |
|-----------|---------|-------------|
| `servers` | `["nats://localhost:4222"]` | NATS servers |
| `subject` | required | Subject to subscribe |
| `queue_group` | pipeline name | Load-balancing queue group (defaults to pipeline name; set `""` explicitly for broadcast to all nodes) |

### gnmi
gNMI telemetry subscription. `host`, `subscriptions: list[dict]`.

### sql
| Parameter | Default | Description |
|-----------|---------|-------------|
| `connection_url` | required | SQLAlchemy URL |
| `query` | required | SQL query |
| `chunk_size` | 0 | Stream in chunks (0 = all at once) |

### influxdb
| Parameter | Default | Description |
|-----------|---------|-------------|
| `url` | required | InfluxDB URL |
| `token` / `org` | required | Auth |
| `query` | required | Flux query |

### redis
| Parameter | Default | Description |
|-----------|---------|-------------|
| `key` | required | List key or stream name |
| `mode` | list | `list` (LPOP) or `stream` (XREAD) |
| `block_ms` | 1000 | Block timeout for XREAD |

### gcs
| Parameter | Default | Description |
|-----------|---------|-------------|
| `bucket` | required | GCS bucket |
| `service_account_json` | — | Path to service account key |
| `skip_processed` | false | Skip objects already recorded in the DB for this pipeline |

### azure_blob
| Parameter | Default | Description |
|-----------|---------|-------------|
| `container` | required | Container name |
| `connection_string` or `account_name`+`account_key` | required | Auth |
| `skip_processed` | false | Skip blobs already recorded in the DB for this pipeline |

### webhook *(v0.5.0)*
| Parameter | Default | Description |
|-----------|---------|-------------|
| `path` | required | URL path (e.g. `my-events`) |
| `secret` | — | Required Bearer token |
| `max_queue_size` | 1000 | Backpressure limit |

Receives HTTP POSTs at `POST /webhooks/{path}` on the daemon port.

### websocket *(v0.5.0)*
| Parameter | Default | Description |
|-----------|---------|-------------|
| `url` | required | `ws://` or `wss://` URL |
| `reconnect` | true | Auto-reconnect on drop |
| `reconnect_delay` | 5 | Seconds between retries |

### elasticsearch *(v0.5.0)*
| Parameter | Default | Description |
|-----------|---------|-------------|
| `hosts` | required | Cluster hosts |
| `index` | required | Index name or pattern |
| `query` | `{"match_all":{}}` | Elasticsearch query body |
| `scroll` | `2m` | Scroll context TTL |
| `batch_size` | 500 | Docs per scroll page |

### prometheus_rw *(v0.5.0)*
| Parameter | Default | Description |
|-----------|---------|-------------|
| `path` | `prom-rw` | URL path for Prometheus `remote_write` target |
| `secret` | — | Bearer token |

Accepts Prometheus `remote_write` payloads (Snappy+protobuf).

### corba *(v0.9.0)*

Invokes a remote CORBA operation using the Dynamic Invocation Interface (DII). No pre-compiled IDL stubs are required. Covers 3GPP Itf-N, TMN X.700, Ericsson ENM, Nokia NetAct, Huawei iManager.

Requires: `pip install tram[corba]`

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ior` | — | Direct IOR string (mutually exclusive with `naming_service`) |
| `naming_service` | — | corbaloc URI, e.g. `corbaloc:iiop:192.168.1.1:2809/NameService` |
| `object_name` | — | Slash-separated path in NamingService, e.g. `PM/PMCollect` |
| `operation` | required | CORBA operation name to invoke |
| `args` | `[]` | Positional arguments (int, float, str, bool) |
| `timeout_seconds` | `30` | ORB request timeout |
| `skip_processed` | false | Skip if this `(operation, args)` combination has already run for this pipeline |

One of `ior` or `naming_service` must be configured.

---

## Built-in Sinks

### sftp / local / ftp / s3 / gcs / azure_blob
File-type sinks. Support `filename_template` / `blob_template` / `key_template` tokens:
- `{pipeline}` — pipeline name
- `{timestamp}` — UTC timestamp
- `{source_filename}` — original source filename (if in meta)

### rest
| Parameter | Default | Description |
|-----------|---------|-------------|
| `url` | required | HTTP endpoint |
| `method` | POST | HTTP method |
| `expected_status` | `[200,201,202,204]` | Accepted status codes |

### kafka / amqp / nats / mqtt / redis
Message-bus sinks. Config mirrors corresponding source.

### opensearch
| Parameter | Default | Description |
|-----------|---------|-------------|
| `hosts` | required | OpenSearch hosts |
| `index` | required | Index name (supports date patterns) |
| `id_field` | — | Field to use as document `_id` |
| `chunk_size` | 500 | Bulk batch size |

### sql
| Parameter | Default | Description |
|-----------|---------|-------------|
| `connection_url` | required | SQLAlchemy URL |
| `table` | required | Target table |
| `mode` | insert | `insert` or `upsert` |
| `upsert_keys` | `[]` | PK fields for upsert |

### influxdb
Writes line-protocol. `measurement`, `tag_fields`, `timestamp_field`, `precision`.

### ves
ONAP VES event sink. `url`, `domain`, `source_name`, auth options.

### snmp_trap
Sends SNMP v2c traps. `host`, `port`, `enterprise_oid`.

**Explicit varbind config (v1.0.0):**
```yaml
sink:
  type: snmp_trap
  host: manager.example.com
  port: 162
  enterprise_oid: "1.3.6.1.4.1.99999"
  mib_modules: ["IF-MIB"]
  varbinds:
    - oid: "IF-MIB::ifOperStatus"   # symbolic or numeric
      value_field: status           # record key to read
      type: Integer32
    - oid: "1.3.6.1.2.1.2.2.1.5.1"
      value_field: speed
      type: Gauge32
```
When `varbinds` is empty, falls back to auto-typing (Integer32/OctetString based on value type).

### websocket *(v0.5.0)*
Connects, sends serialized bytes, disconnects per write. `url`, `extra_headers`.

### elasticsearch *(v0.5.0)*
| Parameter | Default | Description |
|-----------|---------|-------------|
| `hosts` | required | Cluster hosts |
| `index_template` | required | Index name (supports `{pipeline}`, `{timestamp}`) |
| `id_field` | — | Document `_id` field |
| `chunk_size` | 500 | Bulk batch size |
| `pipeline` | — | Ingest pipeline name |

---

## Sink Conditions (v0.5.0)

Any sink can have `condition: <simpleeval expression>` to enable conditional routing:

```yaml
sinks:
  - type: kafka
    brokers: [kafka:9092]
    topic: all-events
    # no condition = catch-all

  - type: opensearch
    hosts: [http://os:9200]
    index: critical-events
    condition: "severity == 'CRITICAL'"

  - type: local
    path: /tmp/debug
    condition: "debug == true"
```

The same `simpleeval` sandbox is used as in the `filter` and `add_field` transforms.

---

## Per-Sink Reliability (v1.0.0)

Every sink config accepts these optional fields:

```yaml
sink:
  type: kafka
  brokers: [kafka:9092]
  topic: output
  retry_count: 3              # retry on failure (0 = no retry)
  retry_delay_seconds: 1.0   # base delay; doubled each attempt + small jitter
  circuit_breaker_threshold: 5  # open circuit after N consecutive failures for 60s
```

**Retry back-off**: `delay = retry_delay_seconds × 2^attempt + random(0, 0.1)`

**Circuit breaker**: after `circuit_breaker_threshold` consecutive failures the sink is bypassed for 60 seconds, then automatically reset on the next successful write.

---

## Optional Dependencies

| Extra | Installs | Connectors |
|-------|----------|------------|
| `kafka` | kafka-python | kafka source/sink |
| `opensearch` | opensearch-py | opensearch sink |
| `s3` | boto3 | s3 source/sink |
| `snmp` | pysnmp-lextudio | snmp_trap/snmp_poll |
| `mqtt` | paho-mqtt | mqtt source/sink |
| `amqp` | pika | amqp source/sink |
| `nats` | nats-py | nats source/sink |
| `gnmi` | pygnmi | gnmi source |
| `sql` | sqlalchemy | sql source/sink |
| `influxdb` | influxdb-client | influxdb source/sink |
| `redis` | redis | redis source/sink |
| `gcs` | google-cloud-storage | gcs source/sink |
| `azure` | azure-storage-blob | azure_blob source/sink |
| `websocket` | websockets | websocket source/sink |
| `elasticsearch` | elasticsearch | elasticsearch source/sink |
| `prometheus_rw` | protobuf, python-snappy | prometheus_rw source |
| `corba` | omniORBpy | corba source |
| `mib` | pysmi-lextudio | `tram mib compile` (raw .mib → Python) |
| `otel` | opentelemetry-sdk, opentelemetry-exporter-otlp-proto-grpc | OpenTelemetry tracing |
| `watch` | watchdog | pipeline file watcher (`TRAM_WATCH_PIPELINES`) |
| `avro` | fastavro | avro serializer |
| `parquet` | pyarrow | parquet serializer |
| `msgpack_ser` | msgpack | msgpack serializer |
| `protobuf_ser` | protobuf, grpcio-tools | protobuf serializer |
| `jmespath` | jmespath | jmespath transform |
| `metrics` | prometheus-client | /metrics endpoint |
| `all` | everything above | all features |
