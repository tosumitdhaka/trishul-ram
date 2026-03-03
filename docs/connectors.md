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

### local
| Parameter | Default | Description |
|-----------|---------|-------------|
| `path` | required | Local directory |
| `file_pattern` | `"*"` | Glob filter |
| `recursive` | false | Recurse subdirectories |
| `move_after_read` / `delete_after_read` | — | Post-read action |

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
| `group_id` | `tram` | Consumer group |
| `auto_offset_reset` | latest | `latest` / `earliest` |
| `security_protocol` | PLAINTEXT | SASL/SSL options |

### ftp
| Parameter | Default | Description |
|-----------|---------|-------------|
| `host` | required | FTP server |
| `username` / `password` | required | FTP credentials |
| `remote_path` | `/` | Remote directory |
| `passive` | true | Passive mode |

### s3
| Parameter | Default | Description |
|-----------|---------|-------------|
| `bucket` | required | S3 bucket |
| `prefix` | `""` | Key prefix filter |
| `endpoint_url` | — | Override (e.g. MinIO) |
| `aws_access_key_id` / `aws_secret_access_key` | — | Credentials |

### syslog
UDP/TCP syslog receiver. `host`, `port` (514), `protocol` (udp/tcp).

### snmp_trap
SNMP trap receiver. `host`, `port` (162), `community`, `version`.

### snmp_poll
SNMP GET/WALK polling. `host`, `oids: list[str]`, `operation` (get/walk).

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
| `queue_group` | `""` | Load-balancing group |

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

### azure_blob
| Parameter | Default | Description |
|-----------|---------|-------------|
| `container` | required | Container name |
| `connection_string` or `account_name`+`account_key` | required | Auth |

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
| `avro` | fastavro | avro serializer |
| `parquet` | pyarrow | parquet serializer |
| `msgpack_ser` | msgpack | msgpack serializer |
| `protobuf_ser` | protobuf, grpcio-tools | protobuf serializer |
| `jmespath` | jmespath | jmespath transform |
| `metrics` | prometheus-client | /metrics endpoint |
| `all` | everything above | all features |
