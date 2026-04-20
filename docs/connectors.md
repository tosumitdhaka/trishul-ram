# TRAM Connector & Serializer Reference

This document covers every built-in source, sink, and serializer — full parameter tables, auth options, and a working YAML snippet for each.

For transforms, see [transforms.md](transforms.md).
For deployment and environment variables, see [deployment.md](deployment.md).

---

## Table of Contents

- [Pipeline skeleton](#pipeline-skeleton)
- [Sources](#sources)
- [Sinks](#sinks)
- [Serializers](#serializers)
- [Per-sink features](#per-sink-features)
- [Adding a custom connector](#adding-a-custom-connector)
- [Optional dependencies](#optional-dependencies)

---

## Pipeline skeleton

```yaml
version: "1"

pipeline:
  name: my-pipeline
  description: "Optional free-text description"
  enabled: true                # false = registered but never auto-scheduled

  schedule:
    type: interval             # interval | cron | manual | stream
    interval_seconds: 300      # for type: interval
    # cron: "*/5 * * * *"     # for type: cron
    # (no extra fields)        # for type: manual or stream

  source:
    type: <source-type>
    # ... source params

  serializer_in:               # how to parse raw bytes from the source
    type: json                 # json | csv | xml | avro | protobuf | asn1 | parquet | msgpack | ndjson | bytes | text

  transforms:                  # optional ordered list
    - type: rename
      fields: {old: new}

  # serializer_out defaults to json if omitted
  serializer_out:
    type: json
    indent: 2

  sinks:                       # one or many
    - type: <sink-type>
      # ... sink params

  on_error: continue           # continue | abort | retry | dlq

  dlq:                         # dead-letter queue for failed records
    type: local
    path: /data/dlq
```

**Schedule types:**

| Type | When it runs |
|---|---|
| `interval` | Every `interval_seconds` seconds |
| `cron` | On a cron expression, e.g. `"0 * * * *"` |
| `manual` | Only when triggered via `POST /api/pipelines/{name}/run` |
| `stream` | Continuously; source runs in a dedicated thread (Kafka, webhook, syslog, etc.) |

---

## Sources

### sftp

Reads files from an SFTP server. Batch mode: one run = all matching files.

| Parameter | Default | Description |
|---|---|---|
| `host` | required | SFTP hostname or IP |
| `port` | `22` | SSH port |
| `username` | required | SSH username |
| `password` | — | SSH password |
| `private_key_path` | — | Path to private key file (alternative to password) |
| `remote_path` | required | Directory to read from |
| `file_pattern` | `"*"` | Glob filter, e.g. `"*.xml"` |
| `move_after_read` | — | Move files to this remote path after reading |
| `delete_after_read` | `false` | Delete files after reading |
| `skip_processed` | `false` | Skip files already seen by this pipeline (tracked in DB) |

```yaml
source:
  type: sftp
  host: ${SFTP_HOST}
  username: ${SFTP_USER}
  password: ${SFTP_PASS}
  remote_path: /pm/counters/hourly
  file_pattern: "A*.xml"
  move_after_read: /pm/counters/processed
  skip_processed: true
```

---

### local

Reads files from the local filesystem. Batch mode.

| Parameter | Default | Description |
|---|---|---|
| `path` | required | Directory to read from |
| `file_pattern` | `"*"` | Glob filter |
| `recursive` | `false` | Recurse into subdirectories |
| `move_after_read` | — | Move files to this path after reading |
| `delete_after_read` | `false` | Delete files after reading |
| `skip_processed` | `false` | Skip already-processed files (tracked in DB) |

```yaml
source:
  type: local
  path: /data/input
  file_pattern: "*.csv"
  skip_processed: true
```

---

### rest

Polls an HTTP endpoint. Batch mode (one request per run, or paginated).

| Parameter | Default | Description |
|---|---|---|
| `url` | required | Full URL |
| `method` | `GET` | HTTP method |
| `headers` | `{}` | Additional request headers |
| `params` | `{}` | URL query parameters |
| `body` | — | Request body string or dict (for POST/PUT) |
| `auth_type` | `none` | `none` \| `basic` \| `bearer` \| `apikey` |
| `username` | — | Basic auth username |
| `password` | — | Basic auth password |
| `token` | — | Bearer token |
| `api_key` | — | API key value (for `auth_type: apikey`) |
| `api_key_header` | `X-API-Key` | Header name for API key |
| `timeout` | `30` | Request timeout in seconds |
| `verify_ssl` | `true` | Verify TLS certificates |
| `response_path` | — | Dot-path to extract from JSON response, e.g. `"data.items"` |
| `paginate` | `false` | Enable offset-based pagination |
| `page_param` | `offset` | Pagination query parameter name |
| `page_size` | `100` | Records per page |
| `total_path` | — | Dot-path to total count in response |

```yaml
# Bearer token auth
source:
  type: rest
  url: https://nms.example.com/api/v1/alarms
  auth_type: bearer
  token: ${NMS_TOKEN}
  response_path: data.alarms
  paginate: true
  page_size: 200

# API key auth
source:
  type: rest
  url: https://api.example.com/metrics
  auth_type: apikey
  api_key: ${API_KEY}
  api_key_header: X-Auth-Token    # default is X-API-Key
```

---

### kafka

Consumes messages from a Kafka topic. Stream mode.

| Parameter | Default | Description |
|---|---|---|
| `brokers` | required | List of bootstrap servers |
| `topic` | required | Topic name or list of topics |
| `group_id` | pipeline name | Consumer group ID |
| `auto_offset_reset` | `latest` | `latest` \| `earliest` |
| `enable_auto_commit` | `true` | Auto-commit offsets |
| `max_poll_records` | `500` | Max records per poll |
| `session_timeout_ms` | `30000` | Consumer session timeout |
| `security_protocol` | `PLAINTEXT` | `PLAINTEXT` \| `SASL_PLAINTEXT` \| `SASL_SSL` \| `SSL` |
| `sasl_mechanism` | — | `PLAIN` \| `SCRAM-SHA-256` \| `SCRAM-SHA-512` |
| `sasl_username` | — | SASL username |
| `sasl_password` | — | SASL password |
| `ssl_cafile` | — | Path to CA certificate |
| `reconnect_delay_seconds` | `5.0` | Seconds between reconnect attempts |
| `max_reconnect_attempts` | `0` | Max reconnects; `0` = infinite |

```yaml
source:
  type: kafka
  brokers: [kafka-1:9092, kafka-2:9092]
  topic: raw-pm-events
  group_id: tram-pm-consumer
  auto_offset_reset: earliest
  security_protocol: SASL_SSL
  sasl_mechanism: SCRAM-SHA-256
  sasl_username: ${KAFKA_USER}
  sasl_password: ${KAFKA_PASS}
```

---

### ftp

Reads files from an FTP server. Batch mode.

| Parameter | Default | Description |
|---|---|---|
| `host` | required | FTP hostname |
| `port` | `21` | FTP port |
| `username` | required | FTP username |
| `password` | required | FTP password |
| `remote_path` | `/` | Remote directory |
| `file_pattern` | `"*"` | Glob filter |
| `passive` | `true` | Use passive mode |
| `move_after_read` | — | Move files after reading |
| `delete_after_read` | `false` | Delete after reading |
| `skip_processed` | `false` | Skip already-processed files |

```yaml
source:
  type: ftp
  host: ftp.legacy-oss.example.com
  username: ${FTP_USER}
  password: ${FTP_PASS}
  remote_path: /export/pm
  file_pattern: "*.csv"
  skip_processed: true
```

---

### s3

Reads objects from an S3 bucket. Batch mode. Requires `pip install tram[s3]`.

| Parameter | Default | Description |
|---|---|---|
| `bucket` | required | S3 bucket name |
| `prefix` | `""` | Object key prefix filter |
| `endpoint_url` | — | Override endpoint (e.g. MinIO, Ceph) |
| `region_name` | — | AWS region |
| `aws_access_key_id` | — | AWS access key |
| `aws_secret_access_key` | — | AWS secret key |
| `skip_processed` | `false` | Skip already-processed keys |

```yaml
source:
  type: s3
  bucket: my-pm-bucket
  prefix: counters/hourly/
  aws_access_key_id: ${AWS_ACCESS_KEY_ID}
  aws_secret_access_key: ${AWS_SECRET_ACCESS_KEY}
  region_name: eu-west-1
  skip_processed: true

# MinIO
source:
  type: s3
  bucket: tram-data
  endpoint_url: http://minio:9000
  aws_access_key_id: minioadmin
  aws_secret_access_key: minioadmin
```

---

### syslog

Receives syslog messages over UDP or TCP. Stream mode.
In manager mode, `syslog` is blocked in v1.3.0 because the UDP push-source architecture is not ready yet; use standalone mode or wait for v1.3.1 broadcast support.

| Parameter | Default | Description |
|---|---|---|
| `host` | `0.0.0.0` | Bind address |
| `port` | `514` | UDP/TCP port (use `1514`+ for non-root) |
| `protocol` | `udp` | `udp` \| `tcp` |

```yaml
source:
  type: syslog
  host: 0.0.0.0
  port: 1514
  protocol: udp
```

---

### snmp_trap

Receives SNMP v1/v2c/v3 traps. Stream mode. Requires `pip install tram[snmp]`.
In manager mode, `snmp_trap` is blocked in v1.3.0 because the UDP push-source architecture is not ready yet; use standalone mode or wait for v1.3.1 broadcast support.

| Parameter | Default | Description |
|---|---|---|
| `host` | `0.0.0.0` | Bind address |
| `port` | `162` | UDP port (use `1162`+ for non-root) |
| `community` | `public` | v1/v2c community string |
| `version` | `2c` | `1` \| `2c` \| `3` |
| `resolve_oids` | `true` | Resolve OIDs to symbolic names using loaded MIBs |
| `mib_dirs` | `[]` | Extra directories containing compiled MIB Python files |
| `mib_modules` | `[]` | MIB module names to pre-load, e.g. `["IF-MIB", "SNMPv2-MIB"]` |
| `security_name` | `""` | SNMPv3 USM username |
| `auth_protocol` | `SHA` | `MD5` \| `SHA` \| `SHA224` \| `SHA256` \| `SHA384` \| `SHA512` |
| `auth_key` | — | SNMPv3 auth passphrase (omit for noAuthNoPriv) |
| `priv_protocol` | `AES128` | `DES` \| `3DES` \| `AES` \| `AES128` \| `AES192` \| `AES256` |
| `priv_key` | — | SNMPv3 privacy passphrase (omit for authNoPriv) |
| `context_name` | `""` | SNMPv3 context name |

```yaml
# SNMPv2c trap receiver
source:
  type: snmp_trap
  host: 0.0.0.0
  port: 1162
  community: public
  resolve_oids: true
  mib_modules: [IF-MIB, SNMPv2-MIB, CISCO-ENTITY-FRU-CONTROL-MIB]
```

---

### snmp_poll

Polls SNMP agents via GET or WALK. Batch mode. Requires `pip install tram[snmp]`.

| Parameter | Default | Description |
|---|---|---|
| `host` | required | SNMP agent hostname or IP |
| `port` | `161` | SNMP agent UDP port |
| `community` | `public` | v1/v2c community string |
| `version` | `2c` | `1` \| `2c` \| `3` |
| `oids` | required | List of OIDs or symbolic names |
| `operation` | `get` | `get` — exact instance; `walk` — subtree traversal |
| `resolve_oids` | `true` | Resolve OIDs to symbolic names |
| `mib_dirs` | `[]` | Extra compiled MIB directories |
| `mib_modules` | `[]` | MIB module names to pre-load |
| `yield_rows` | `false` | `true` = one record per table row (use with walk) |
| `index_depth` | `0` | `0` = auto; `>0` = last N OID components form row index |

Every record includes `_polled_at` (UTC ISO 8601).

```yaml
# Walk IF-MIB, one record per interface row
source:
  type: snmp_poll
  host: 192.168.1.1
  community: ${SNMP_COMMUNITY}
  operation: walk
  oids: [IF-MIB::ifTable]
  mib_modules: [IF-MIB]
  resolve_oids: true
  yield_rows: true
```

---

### mqtt

Subscribes to an MQTT topic. Stream mode. Requires `pip install tram[mqtt]`.

| Parameter | Default | Description |
|---|---|---|
| `host` | required | MQTT broker hostname |
| `port` | `1883` | Broker port |
| `topic` | required | Topic to subscribe (supports wildcards: `+`, `#`) |
| `qos` | `0` | Quality of service `0` \| `1` \| `2` |
| `client_id` | — | MQTT client ID (auto-generated if omitted) |
| `username` | — | Broker username |
| `password` | — | Broker password |
| `tls` | `false` | Enable TLS |
| `tls_ca_certs` | — | Path to CA certificate file |

```yaml
source:
  type: mqtt
  host: mqtt.example.com
  topic: sensors/telemetry/#
  qos: 1
  username: ${MQTT_USER}
  password: ${MQTT_PASS}
```

---

### amqp

Consumes from an AMQP 0-9-1 queue (RabbitMQ). Stream mode. Requires `pip install tram[amqp]`.

| Parameter | Default | Description |
|---|---|---|
| `url` | `amqp://guest:guest@localhost:5672/` | AMQP connection URL (includes credentials and vhost) |
| `queue` | required | Queue name |
| `prefetch_count` | `10` | Prefetch limit |
| `durable` | `true` | Expect a durable queue |
| `auto_ack` | `false` | Auto-acknowledge messages |

```yaml
source:
  type: amqp
  url: amqp://${RABBIT_USER}:${RABBIT_PASS}@rabbitmq:5672/prod
  queue: pm-raw-events
  prefetch_count: 50
```

---

### nats

Subscribes to a NATS subject. Stream mode. Requires `pip install tram[nats]`.

| Parameter | Default | Description |
|---|---|---|
| `servers` | `["nats://localhost:4222"]` | NATS server URLs |
| `subject` | required | Subject to subscribe |
| `queue_group` | pipeline name | Load-balancing queue group; set `""` for broadcast |
| `username` | — | NATS credentials username |
| `password` | — | NATS credentials password |
| `token` | — | NATS auth token |

```yaml
source:
  type: nats
  servers: [nats://nats-1:4222, nats://nats-2:4222]
  subject: telemetry.pm.>
  queue_group: tram-pm-workers
```

---

### gnmi

Subscribes to gNMI telemetry streams. Stream mode. Requires `pip install tram[gnmi]`.

| Parameter | Default | Description |
|---|---|---|
| `host` | required | gNMI target hostname/IP |
| `port` | `57400` | gNMI port |
| `username` | — | gRPC auth username |
| `password` | — | gRPC auth password |
| `insecure` | `false` | Skip TLS verification |
| `subscriptions` | required | List of subscription dicts (see below) |

Each subscription dict: `path` (XPath), `mode` (`SAMPLE`/`ON_CHANGE`/`TARGET_DEFINED`), `sample_interval` (nanoseconds).

```yaml
source:
  type: gnmi
  host: router.example.com
  port: 57400
  username: ${GNMI_USER}
  password: ${GNMI_PASS}
  insecure: true
  subscriptions:
    - path: /interfaces/interface/state/counters
      mode: SAMPLE
      sample_interval: 10000000000   # 10s in nanoseconds
    - path: /network-instances/network-instance/protocols
      mode: ON_CHANGE
```

---

### sql

Queries a relational database. Batch mode. Requires `pip install tram[sql]` (already a core dependency for SQLAlchemy; add `tram[postgresql]` or `tram[mysql]` for specific drivers).

| Parameter | Default | Description |
|---|---|---|
| `connection_url` | required | SQLAlchemy connection URL |
| `query` | required | SQL SELECT query |
| `chunk_size` | `0` | Stream rows in chunks; `0` = fetch all at once |

```yaml
source:
  type: sql
  connection_url: postgresql+psycopg2://${DB_USER}:${DB_PASS}@postgres:5432/oss
  query: >
    SELECT ne_id, counter_name, value, collected_at
    FROM pm_counters
    WHERE collected_at > NOW() - INTERVAL '1 hour'
  chunk_size: 1000
```

---

### clickhouse

Queries ClickHouse. Batch mode. Requires `pip install tram[clickhouse]`.

| Parameter | Default | Description |
|---|---|---|
| `host` | `localhost` | ClickHouse server |
| `port` | `9000` | Native TCP port |
| `database` | `default` | Database name |
| `username` | `default` | Username |
| `password` | `""` | Password |
| `query` | required | SELECT query |
| `params` | `{}` | Query parameters |
| `chunk_size` | `0` | Rows per chunk; `0` = all |
| `secure` | `false` | TLS |
| `connect_timeout` | `10` | Connection timeout (s) |
| `send_receive_timeout` | `300` | Query timeout (s) |

```yaml
source:
  type: clickhouse
  host: clickhouse.example.com
  database: telecom
  username: ${CH_USER}
  password: ${CH_PASS}
  query: >
    SELECT ne_id, metric, value, ts
    FROM pm_hourly
    WHERE ts >= now() - INTERVAL 1 HOUR
```

---

### influxdb

Queries InfluxDB using Flux. Batch mode. Requires `pip install tram[influxdb]`.

| Parameter | Default | Description |
|---|---|---|
| `url` | required | InfluxDB URL |
| `token` | required | Auth token |
| `org` | required | Organization name |
| `query` | required | Flux query string |

```yaml
source:
  type: influxdb
  url: http://influxdb:8086
  token: ${INFLUX_TOKEN}
  org: my-org
  query: >
    from(bucket: "pm")
    |> range(start: -1h)
    |> filter(fn: (r) => r._measurement == "interface_stats")
```

---

### redis

Reads from a Redis list or stream. Batch (list LPOP) or stream (XREAD) mode. Requires `pip install tram[redis]`.

| Parameter | Default | Description |
|---|---|---|
| `host` | `localhost` | Redis hostname |
| `port` | `6379` | Redis port |
| `password` | — | Redis password |
| `db` | `0` | Database index |
| `key` | required | List key or stream name |
| `mode` | `list` | `list` (LPOP) \| `stream` (XREAD) |
| `block_ms` | `1000` | Block timeout for `XREAD` in milliseconds |
| `count` | `100` | Max items per read |
| `stream_id` | `0-0` | Starting stream ID for XREAD |

```yaml
source:
  type: redis
  host: redis
  password: ${REDIS_PASS}
  key: tram:events
  mode: stream
  block_ms: 2000
```

---

### gcs

Reads objects from Google Cloud Storage. Batch mode. Requires `pip install tram[gcs]`.

| Parameter | Default | Description |
|---|---|---|
| `bucket` | required | GCS bucket name |
| `prefix` | `""` | Object prefix filter |
| `service_account_json` | — | Path to service account JSON key file |
| `skip_processed` | `false` | Skip already-processed objects |

```yaml
source:
  type: gcs
  bucket: my-pm-bucket
  prefix: pm/hourly/
  service_account_json: /secrets/gcp-sa.json
  skip_processed: true
```

---

### azure_blob

Reads blobs from Azure Blob Storage. Batch mode. Requires `pip install tram[azure]`.

| Parameter | Default | Description |
|---|---|---|
| `container` | required | Container name |
| `connection_string` | — | Azure Storage connection string |
| `account_name` | — | Storage account name (alternative to connection_string) |
| `account_key` | — | Storage account key |
| `prefix` | `""` | Blob name prefix filter |
| `skip_processed` | `false` | Skip already-processed blobs |

```yaml
source:
  type: azure_blob
  container: pm-data
  connection_string: ${AZURE_STORAGE_CONNECTION_STRING}
  prefix: counters/
  skip_processed: true
```

---

### webhook

Receives HTTP POSTs on the daemon's built-in HTTP port. Stream mode. Each pipeline owns a unique path.

| Parameter | Default | Description |
|---|---|---|
| `path` | required | URL path segment, e.g. `pm-events` → `POST /webhooks/pm-events` |
| `secret` | — | Required Bearer token; returns `401` if missing/wrong |
| `max_queue_size` | `1000` | Max queued payloads before backpressure |

Multiple pipelines can listen on different paths simultaneously.

Optional dedicated Kubernetes Service exposure:

```yaml
kubernetes:
  enabled: true
  service_type: NodePort
  node_port: 30042   # optional; omit to let Kubernetes assign one
  service_name: ""   # optional custom Service name
```

This is control-plane owned:
- standalone mode creates a Service targeting the local daemon `POST /webhooks/{path}`
- manager mode creates a Service targeting worker ingress on `:8767`
- the Service exists only while the pipeline is active and is removed on stop/delete

```yaml
# Pipeline A — POST /webhooks/pm-raw
source:
  type: webhook
  path: pm-raw
  secret: ${WEBHOOK_SECRET}

# Pipeline B — POST /webhooks/alarms/cisco  (nested paths work too)
source:
  type: webhook
  path: alarms/cisco
```

---

### websocket

Connects to a WebSocket server and streams messages. Stream mode. Requires `pip install tram[websocket]`.

| Parameter | Default | Description |
|---|---|---|
| `url` | required | `ws://` or `wss://` URL |
| `reconnect` | `true` | Auto-reconnect on disconnect |
| `reconnect_delay` | `5` | Seconds between reconnect attempts |
| `extra_headers` | `{}` | Additional WebSocket handshake headers |

```yaml
source:
  type: websocket
  url: wss://stream.example.com/telemetry
  reconnect: true
  extra_headers:
    Authorization: Bearer ${WS_TOKEN}
```

---

### elasticsearch

Scrolls documents from an Elasticsearch index. Batch mode. Requires `pip install tram[elasticsearch]`.

| Parameter | Default | Description |
|---|---|---|
| `hosts` | required | List of Elasticsearch hosts |
| `index` | required | Index name or pattern |
| `query` | `{"match_all": {}}` | Elasticsearch query DSL |
| `scroll` | `2m` | Scroll context TTL |
| `batch_size` | `500` | Documents per scroll page |
| `username` | — | HTTP basic auth username |
| `password` | — | HTTP basic auth password |

```yaml
source:
  type: elasticsearch
  hosts: [https://es-1:9200, https://es-2:9200]
  index: logs-*
  query:
    range:
      "@timestamp":
        gte: now-1h
  batch_size: 1000
  username: ${ES_USER}
  password: ${ES_PASS}
```

---

### prometheus_rw

Accepts Prometheus `remote_write` payloads (Snappy-compressed Protobuf). Stream mode. Requires `pip install tram[prometheus_rw]`.

| Parameter | Default | Description |
|---|---|---|
| `path` | `prom-rw` | URL path → `POST /webhooks/{path}` |
| `secret` | — | Bearer token |

```yaml
source:
  type: prometheus_rw
  path: prom-rw
  secret: ${PROM_SECRET}
kubernetes:
  enabled: true
  service_type: LoadBalancer   # or NodePort
```

Prometheus scrape config:
```yaml
remote_write:
  - url: http://tram:8765/webhooks/prom-rw
    bearer_token: your-secret
```

---

### corba

Invokes a remote CORBA operation via DII (no compiled stubs needed). Covers 3GPP Itf-N, TMN X.700, Ericsson ENM, Nokia NetAct, Huawei iManager. Batch mode. Requires `pip install tram[corba]`.

| Parameter | Default | Description |
|---|---|---|
| `ior` | — | Direct IOR string (mutually exclusive with `naming_service`) |
| `naming_service` | — | `corbaloc:` URI, e.g. `corbaloc:iiop:192.168.1.1:2809/NameService` |
| `object_name` | — | NamingService path, e.g. `PM/PMCollect` |
| `operation` | required | CORBA operation name |
| `args` | `[]` | Positional arguments |
| `timeout_seconds` | `30` | ORB request timeout |
| `skip_processed` | `false` | Skip if this `(operation, args)` has already run for this pipeline |

```yaml
source:
  type: corba
  naming_service: corbaloc:iiop:ems.example.com:2809/NameService
  object_name: PM/PMDataService
  operation: getPMData
  args: ["ne-01", "2026-03-13T00:00:00Z"]
```

---

## Sinks

All sinks accept these optional reliability fields:

```yaml
retry_count: 3               # retry on write failure (0 = no retry)
retry_delay_seconds: 1.0     # base delay; exponential back-off per attempt
circuit_breaker_threshold: 5 # open circuit after N consecutive failures for 60s
condition: "severity == 'CRITICAL'"   # only write records matching this expression
transforms: []               # per-sink transform chain (applied before serialization)
serializer_out:              # per-sink serializer override
  type: avro
  schema_file: /schemas/event.avsc
```

---

### sftp

Writes a file to an SFTP server.

| Parameter | Default | Description |
|---|---|---|
| `host` | required | SFTP hostname |
| `port` | `22` | SSH port |
| `username` | required | SSH username |
| `password` / `private_key_path` | — | Auth |
| `remote_path` | required | Target directory |
| `filename_template` | `"{pipeline}_{timestamp}.bin"` | Output filename; tokens: `{pipeline}`, `{timestamp}`, `{epoch}`, `{epoch_m}`, `{part}` / `{index}`, `{run_id}`, `{source_filename}`, `{source_stem}`, `{source_suffix}`, `{source_path}`, `{field.nf_name}` |
| `file_mode` | `append` | `append` keeps writing to the current file part; `single` writes one fresh file per sink call |
| `max_records` | — | Roll to a new file part when the next write would exceed this record count |
| `max_time` | — | Roll to a new file part when the current file has been open this many seconds |
| `max_bytes` | — | Roll to a new file part when the next write would exceed this byte count |
| `max_index` | `99999` | Highest allowed rolling part number; also defines zero-padding width |

Notes:
- `append` is the default for `sftp` file sinks.
- `timestamp`, `epoch`, and `epoch_m` use the current file-open time, not pipeline start time.
- `source_stem` / `source_suffix` are derived from `source_filename`; if source metadata is absent, they fall back to `data` / empty suffix.
- Field tokens such as `{field.nf_name}` trigger executor-side partitioning before serialization. Each distinct field value gets its own active file/object path; missing values use `unknown`.
- `csv` and `ndjson` support append/rolling naturally.
- `json` file sinks are forced to `file_mode=single`; rolling with `max_records`, `max_time`, or `max_bytes` is rejected because plain JSON arrays are not append-safe.
- When rolling is enabled and the template lacks a strong uniqueness token (`{part}`, `{index}`, or `{epoch_m}`), TRAM auto-appends `_{part}` and logs a warning to avoid filename collisions.
- Risky field choices like `{field.timestamp}` or `{field.value}` are allowed but produce a lint warning because they may create runaway file counts.

```yaml
sinks:
  - type: sftp
    host: archive.example.com
    username: ${SFTP_USER}
    password: ${SFTP_PASS}
    remote_path: /archive/pm
    filename_template: "pm_{pipeline}_{timestamp}_{part}.ndjson"
    file_mode: append
    max_records: 1000
    max_time: 60
    max_bytes: 134217728
    serializer_out:
      type: ndjson
```

---

### local

Writes a file to the local filesystem.

| Parameter | Default | Description |
|---|---|---|
| `path` | required | Target directory |
| `filename_template` | `"{pipeline}_{timestamp}.bin"` | Output filename; same tokens as `sftp` |
| `file_mode` | `append` | `append` keeps writing to the current file part; `single` writes one fresh file per sink call |
| `overwrite` | `true` | In `single` mode, allow replacing an existing file |
| `max_records` | — | Roll to a new file part when the next write would exceed this record count |
| `max_time` | — | Roll to a new file part when the current file has been open this many seconds |
| `max_bytes` | — | Roll to a new file part when the next write would exceed this byte count |
| `max_index` | `99999` | Highest allowed rolling part number; also defines zero-padding width |

Notes:
- `append` is the default for `local` file sinks.
- `csv` append strips repeated headers after the first file write.
- `ndjson` append preserves newline-delimited framing automatically.
- `json` file sinks are forced to `file_mode=single`; rolling with `max_records`, `max_time`, or `max_bytes` is rejected.

```yaml
sinks:
  - type: local
    path: /data/output
    filename_template: "out_{pipeline}_{timestamp}_{part}.csv"
    file_mode: append
    max_records: 50000
    serializer_out:
      type: csv
```

---

### rest

POSTs serialized data to an HTTP endpoint.

| Parameter | Default | Description |
|---|---|---|
| `url` | required | Endpoint URL |
| `method` | `POST` | `POST` \| `PUT` \| `PATCH` |
| `headers` | `{}` | Additional headers |
| `content_type` | `application/json` | `Content-Type` header |
| `auth_type` | `none` | `none` \| `basic` \| `bearer` \| `apikey` |
| `username` | — | Basic auth username |
| `password` | — | Basic auth password |
| `token` | — | Bearer token |
| `api_key` | — | API key value |
| `api_key_header` | `X-API-Key` | Header name for API key |
| `timeout` | `30` | Request timeout (s) |
| `verify_ssl` | `true` | Verify TLS certificates |
| `expected_status` | `[200,201,202,204]` | Accepted HTTP status codes |

```yaml
sinks:
  - type: rest
    url: https://collector.example.com/ingest
    auth_type: apikey
    api_key: ${COLLECTOR_KEY}
    content_type: application/json
    expected_status: [200, 201]
    retry_count: 3
```

---

### kafka

Produces messages to a Kafka topic.

| Parameter | Default | Description |
|---|---|---|
| `brokers` | required | Bootstrap servers |
| `topic` | required | Topic name |
| `key_field` | — | Record field to use as message key |
| `security_protocol` | `PLAINTEXT` | Same options as Kafka source |
| `sasl_mechanism` | — | SASL mechanism |
| `sasl_username` | — | SASL username |
| `sasl_password` | — | SASL password |

```yaml
sinks:
  - type: kafka
    brokers: [kafka:9092]
    topic: pm-normalized
    key_field: ne_id
    serializer_out:
      type: avro
      schema_file: /schemas/pm.avsc
```

---

### opensearch

Bulk-indexes documents to OpenSearch.

| Parameter | Default | Description |
|---|---|---|
| `hosts` | required | List of OpenSearch hosts |
| `index` | required | Index name (supports `{pipeline}`, `{timestamp}`, `{YYYY}`, `{MM}`, `{DD}`) |
| `id_field` | — | Record field to use as document `_id` |
| `chunk_size` | `500` | Documents per bulk request |
| `username` | — | Basic auth username |
| `password` | — | Basic auth password |
| `verify_ssl` | `true` | Verify TLS |

```yaml
sinks:
  - type: opensearch
    hosts: [https://opensearch:9200]
    index: "alarms-{YYYY}.{MM}"
    id_field: alarm_id
    username: ${OS_USER}
    password: ${OS_PASS}
    chunk_size: 200
```

---

### ftp

Writes a file to an FTP server.

| Parameter | Default | Description |
|---|---|---|
| `host` | required | FTP hostname |
| `username` / `password` | required | FTP credentials |
| `remote_path` | `/` | Target directory |
| `filename_template` | `"{pipeline}_{timestamp}"` | Output filename; same tokens as `sftp` |
| `passive` | `true` | Passive mode |

```yaml
sinks:
  - type: ftp
    host: ftp.legacy-oss.example.com
    username: ${FTP_USER}
    password: ${FTP_PASS}
    remote_path: /processed
    filename_template: "pm_{timestamp}.xml"
```

---

### ves

Sends events to the ONAP VES (Virtual Event Streaming) collector.

| Parameter | Default | Description |
|---|---|---|
| `url` | required | VES collector URL |
| `domain` | required | VES domain, e.g. `fault`, `measurement`, `other` |
| `source_name` | required | `reportingEntityName` in the event |
| `auth_type` | `none` | `none` \| `basic` \| `bearer` |
| `username` / `password` / `token` | — | Auth credentials |
| `version` | `7.1` | VES API version |

```yaml
sinks:
  - type: ves
    url: https://ves-collector:8443/eventListener/v7
    domain: fault
    source_name: tram-adapter
    auth_type: basic
    username: ${VES_USER}
    password: ${VES_PASS}
```

---

### s3

Writes an object to an S3 bucket. Requires `pip install tram[s3]`.

| Parameter | Default | Description |
|---|---|---|
| `bucket` | required | S3 bucket name |
| `key_template` | `"{pipeline}/{timestamp}"` | Object key; tokens: `{pipeline}`, `{timestamp}`, `{epoch}`, `{epoch_m}`, `{part}` / `{index}`, `{run_id}`, `{source_filename}`, `{source_stem}`, `{source_suffix}`, `{source_path}`, `{field.nf_name}` |
| `endpoint_url` | — | Override endpoint (MinIO, Ceph) |
| `aws_access_key_id` / `aws_secret_access_key` | — | AWS credentials |
| `region_name` | — | AWS region |
| `content_type` | `application/octet-stream` | S3 object content-type |

```yaml
sinks:
  - type: s3
    bucket: pm-archive
    key_template: "pm/{pipeline}/{timestamp}.json.gz"
    region_name: eu-west-1
```

---

### snmp_trap

Sends SNMP v1/v2c/v3 traps. Requires `pip install tram[snmp]`.

| Parameter | Default | Description |
|---|---|---|
| `host` | required | Trap destination |
| `port` | `162` | Destination UDP port |
| `community` | `public` | v1/v2c community |
| `version` | `2c` | `1` \| `2c` \| `3` |
| `trap_oid` | required | SNMP notification OID placed in `snmpTrapOID.0` |
| `varbinds` | `[]` | Explicit varbind list (see below); empty = auto-typed |
| `mib_modules` | `[]` | MIBs to load for symbolic OID resolution |
| SNMPv3 fields | — | Same as snmp_trap source: `security_name`, `auth_key`, `priv_key`, etc. |

```yaml
sinks:
  - type: snmp_trap
    host: nms.example.com
    community: public
    trap_oid: "1.3.6.1.4.1.99999"
    varbinds:
      - oid: "IF-MIB::ifOperStatus"
        value_field: status
        type: Integer32
      - oid: "IF-MIB::ifDescr"
        value_field: interface_name
        type: OctetString
```

---

### mqtt

Publishes to an MQTT topic. Requires `pip install tram[mqtt]`.

| Parameter | Default | Description |
|---|---|---|
| `host` | required | MQTT broker |
| `port` | `1883` | Broker port |
| `topic` | required | Publish topic |
| `qos` | `0` | QoS level |
| `username` / `password` | — | Auth |
| `tls` | `false` | Enable TLS |
| `retain` | `false` | MQTT retain flag |

```yaml
sinks:
  - type: mqtt
    host: mqtt.example.com
    topic: tram/pm/normalized
    qos: 1
```

---

### amqp

Publishes to an AMQP exchange or queue. Requires `pip install tram[amqp]`.

| Parameter | Default | Description |
|---|---|---|
| `url` | `amqp://guest:guest@localhost:5672/` | AMQP connection URL |
| `exchange` | `""` | Exchange name (empty = default) |
| `routing_key` | required | Routing key |
| `delivery_mode` | `2` | `1` = transient; `2` = persistent |

```yaml
sinks:
  - type: amqp
    url: amqp://${RABBIT_USER}:${RABBIT_PASS}@rabbitmq:5672/prod
    exchange: pm-events
    routing_key: pm.normalized
```

---

### nats

Publishes to a NATS subject. Requires `pip install tram[nats]`.

| Parameter | Default | Description |
|---|---|---|
| `servers` | `["nats://localhost:4222"]` | NATS server URLs |
| `subject` | required | Publish subject |
| `username` / `password` / `token` | — | Auth |

```yaml
sinks:
  - type: nats
    servers: [nats://nats:4222]
    subject: pm.normalized
```

---

### sql

Inserts or upserts records into a relational table.

| Parameter | Default | Description |
|---|---|---|
| `connection_url` | required | SQLAlchemy URL |
| `table` | required | Target table name |
| `mode` | `insert` | `insert` \| `upsert` |
| `upsert_keys` | `[]` | Primary key fields for upsert conflict resolution |
| `chunk_size` | `500` | Rows per batch insert |

```yaml
sinks:
  - type: sql
    connection_url: postgresql+psycopg2://${DB_USER}:${DB_PASS}@postgres:5432/oss
    table: pm_counters_normalized
    mode: upsert
    upsert_keys: [ne_id, counter_name, collected_at]
```

---

### clickhouse

Inserts records into a ClickHouse table. Requires `pip install tram[clickhouse]`.

| Parameter | Default | Description |
|---|---|---|
| `host` | `localhost` | ClickHouse server |
| `port` | `9000` | Native TCP port |
| `database` | `default` | Database name |
| `username` | `default` | Username |
| `password` | `""` | Password |
| `table` | required | Target table |
| `secure` | `false` | TLS |
| `connect_timeout` | `10` | Connection timeout (s) |
| `send_receive_timeout` | `300` | Query timeout (s) |

```yaml
sinks:
  - type: clickhouse
    host: clickhouse.example.com
    database: telecom
    username: ${CH_USER}
    password: ${CH_PASS}
    table: pm_counters
```

---

### influxdb

Writes line-protocol measurements to InfluxDB. Requires `pip install tram[influxdb]`.

| Parameter | Default | Description |
|---|---|---|
| `url` | required | InfluxDB URL |
| `token` | required | Auth token |
| `org` | required | Organization |
| `bucket` | required | Bucket name |
| `measurement` | required | Measurement name |
| `tag_fields` | `[]` | Record fields to write as tags |
| `timestamp_field` | — | Field to use as point timestamp |
| `precision` | `s` | Timestamp precision: `s` \| `ms` \| `us` \| `ns` |

```yaml
sinks:
  - type: influxdb
    url: http://influxdb:8086
    token: ${INFLUX_TOKEN}
    org: my-org
    bucket: pm
    measurement: interface_stats
    tag_fields: [ne_id, interface_name]
    timestamp_field: _polled_at
    precision: s
```

---

### redis

Pushes records to a Redis list or stream. Requires `pip install tram[redis]`.

| Parameter | Default | Description |
|---|---|---|
| `host` | `localhost` | Redis hostname |
| `port` | `6379` | Redis port |
| `password` | — | Redis password |
| `db` | `0` | Database index |
| `key` | required | List key or stream name |
| `mode` | `list` | `list` (RPUSH) \| `stream` (XADD) |
| `maxlen` | `0` | Stream max length (`XADD MAXLEN`); `0` = unlimited |

```yaml
sinks:
  - type: redis
    host: redis
    key: tram:pm:out
    mode: stream
    maxlen: 100000
```

---

### gcs

Writes an object to Google Cloud Storage. Requires `pip install tram[gcs]`.

| Parameter | Default | Description |
|---|---|---|
| `bucket` | required | GCS bucket name |
| `blob_template` | `"{pipeline}/{timestamp}"` | Object path; same tokens as `s3` |
| `service_account_json` | — | Path to service account JSON key |
| `content_type` | `application/octet-stream` | Object content-type |

```yaml
sinks:
  - type: gcs
    bucket: my-pm-bucket
    blob_template: "archive/{pipeline}/{timestamp}.json"
    service_account_json: /secrets/gcp-sa.json
```

---

### azure_blob

Writes a blob to Azure Blob Storage. Requires `pip install tram[azure]`.

| Parameter | Default | Description |
|---|---|---|
| `container` | required | Container name |
| `connection_string` | — | Storage connection string |
| `account_name` / `account_key` | — | Alternative auth |
| `blob_template` | `"{pipeline}/{timestamp}"` | Blob name template; same tokens as `s3` |
| `content_type` | `application/octet-stream` | Blob content-type |

```yaml
sinks:
  - type: azure_blob
    container: pm-archive
    connection_string: ${AZURE_STORAGE_CONNECTION_STRING}
    blob_template: "pm/{pipeline}/{timestamp}.json"
```

---

### websocket

Sends serialized bytes to a WebSocket server (connects, writes, disconnects per batch). Requires `pip install tram[websocket]`.

| Parameter | Default | Description |
|---|---|---|
| `url` | required | `ws://` or `wss://` URL |
| `extra_headers` | `{}` | Additional handshake headers |

```yaml
sinks:
  - type: websocket
    url: wss://stream.example.com/ingest
    extra_headers:
      Authorization: Bearer ${WS_TOKEN}
```

---

### elasticsearch

Bulk-indexes documents to Elasticsearch. Requires `pip install tram[elasticsearch]`.

| Parameter | Default | Description |
|---|---|---|
| `hosts` | required | Elasticsearch hosts |
| `index_template` | required | Index name (supports `{pipeline}`, `{timestamp}`) |
| `id_field` | — | Document `_id` field |
| `chunk_size` | `500` | Documents per bulk request |
| `pipeline` | — | Elasticsearch ingest pipeline name |
| `username` / `password` | — | Basic auth |

```yaml
sinks:
  - type: elasticsearch
    hosts: [https://es:9200]
    index_template: "pm-{YYYY}.{MM}.{DD}"
    id_field: event_id
    username: ${ES_USER}
    password: ${ES_PASS}
```

---

## Serializers

Serializers handle the raw `bytes ↔ list[dict]` conversion at pipeline boundaries.

- `serializer_in` — how to **parse** bytes coming from the source
- `serializer_out` — how to **format** records going to sinks (defaults to `json` if omitted)
- Each sink can override `serializer_out` independently for multi-format fan-out

---

### json

Standard JSON. Parses arrays (`[{...}, {...}]`) and single objects (`{...}`).

| Parameter | Default | Description |
|---|---|---|
| `indent` | `null` | Pretty-print indent; `null` = compact |
| `ensure_ascii` | `true` | Escape non-ASCII characters |

```yaml
serializer_in:
  type: json

serializer_out:
  type: json
  indent: 2
```

---

### ndjson

Newline-Delimited JSON (JSON Lines). One JSON object per line. Suitable for Kafka, Filebeat, Fluentd, Vector, and streaming `jq` output.

| Parameter | Default | Description |
|---|---|---|
| `ensure_ascii` | `true` | Escape non-ASCII characters |
| `strict` | `false` | Raise on non-object lines; `false` = wrap scalars/lists |
| `newline` | `\n` | Line separator for serialization |

```yaml
serializer_in:
  type: ndjson

serializer_out:
  type: ndjson
```

---

### csv

Comma-separated values with optional header row.

| Parameter | Default | Description |
|---|---|---|
| `delimiter` | `,` | Field delimiter |
| `has_header` | `true` | First row is a header (parse) / write header row (serialize) |
| `quotechar` | `"` | Quote character |

```yaml
serializer_in:
  type: csv
  has_header: true
  delimiter: ";"

serializer_out:
  type: csv
```

---

### xml

XML with a two-level document structure (root → repeated record element). Parsed with `defusedxml` (XXE-safe).

| Parameter | Default | Description |
|---|---|---|
| `root_element` | `records` | Outer XML element name |
| `record_element` | `record` | Repeated child element name |
| `encoding` | `utf-8` | Character encoding |

```yaml
serializer_in:
  type: xml
  root_element: measCollecFile
  record_element: measValue

serializer_out:
  type: xml
  root_element: output
  record_element: item
```

---

### avro

Apache Avro binary encoding. Schema can come from an inline definition, a file, or a Confluent-compatible schema registry. Requires `pip install tram[avro]`.

| Parameter | Default | Description |
|---|---|---|
| `schema` | — | Inline Avro schema JSON string |
| `schema_file` | — | Path to `.avsc` schema file |
| `schema_registry_url` | — | Confluent-compatible registry URL (overrides `TRAM_SCHEMA_REGISTRY_URL`) |
| `schema_registry_subject` | — | Registry subject name |
| `schema_registry_id` | — | Registry schema ID (for deserialization) |
| `use_magic_bytes` | `true` | Expect/write Confluent magic bytes prefix |

One of `schema`, `schema_file`, or `schema_registry_url` is required.

```yaml
serializer_in:
  type: avro
  schema_registry_url: http://schema-registry:8081
  schema_registry_subject: pm-events-value

serializer_out:
  type: avro
  schema_file: /schemas/pm_event.avsc
```

---

### protobuf

Protocol Buffers encoding. Compiles `.proto` files on first use with `grpcio-tools`. Requires `pip install tram[protobuf_ser]`.

| Parameter | Default | Description |
|---|---|---|
| `schema_file` | required | Path to `.proto` file |
| `message_class` | required | Top-level message name |
| `framing` | `length_delimited` | `length_delimited` \| `none` |
| `schema_registry_url` | — | Registry URL (overrides `TRAM_SCHEMA_REGISTRY_URL`) |
| `schema_registry_subject` | — | Registry subject name |
| `schema_registry_id` | — | Registry schema ID |
| `use_magic_bytes` | `true` | Confluent magic bytes prefix |

```yaml
serializer_in:
  type: protobuf
  schema_file: /schemas/device_event.proto
  message_class: DeviceEvent
  framing: none

serializer_out:
  type: protobuf
  schema_file: /schemas/pm_counter.proto
  message_class: PmCounter
```

---

### asn1

ASN.1 binary decoding (BER/DER/PER/XER/JER). Compiles a standard `.asn` schema file at first use. Requires `pip install tram[asn1]`.

Deserialize only (`serializer_in`) — use `serializer_out: type: json` (or another serializer) to write the decoded records.

| Parameter | Default | Description |
|---|---|---|
| `schema_file` | required | Path to `.asn` file **or** directory of `.asn` files (compiled together) |
| `message_class` | required | Top-level ASN.1 type name to decode |
| `encoding` | `ber` | `ber` \| `der` \| `per` \| `uper` \| `xer` \| `jer` |

**Type mapping:**

| ASN.1 type | Python / JSON result |
|---|---|
| SEQUENCE, SET | `dict` |
| SEQUENCE OF, SET OF | `list` |
| CHOICE | `{"type": "<name>", "value": <value>}` |
| GeneralizedTime, UTCTime | ISO 8601 string |
| OCTET STRING | hex string |
| INTEGER, REAL, BOOLEAN, NULL | native JSON scalar |

**Multi-file schemas:** point `schema_file` at a directory and all `.asn` files in it are compiled together (imports resolved across files).

```yaml
serializer_in:
  type: asn1
  schema_file: /data/schemas/ericsson/3gpp_32401.asn
  message_class: FileContent
  encoding: ber

serializer_out:
  type: json
  indent: 2
```

Upload the schema via the UI or API:
```bash
curl -F "file=@3gpp_32401.asn" \
  "http://localhost:8765/api/schemas/upload?subdir=ericsson"
```

A reference schema for Ericsson 3GPP TS 32.401 PM statsfiles is shipped at `docs/schemas/3gpp_32401.asn`.

---

### pm_xml

3GPP PM XML (Nokia NCOM / 3GPP TS 32.432 measData) deserializer. Produces one flat record per `<measValue>` element. Auto-closes truncated files. Requires `pip install defusedxml`.

Deserialize only (`serializer_in`).

| Parameter | Default | Description |
|---|---|---|
| `encoding` | `utf-8` | File encoding |
| `add_managed_element` | `true` | Include `managed_element` field (localDn from `<managedElement>`) |
| `add_duration` | `false` | Include `duration` field (granPeriod duration attribute) |
| `numeric_values` | `true` | Cast counter values to `float` where possible; keep as string otherwise |

Each output record contains:
- `end_time` — `granPeriod endTime`
- `meas_info_id` — `measInfo measInfoId`
- `meas_obj_ldn` — `measValue measObjLdn`
- `managed_element` — `managedElement localDn` (if `add_managed_element: true`)
- one field per `<measType>` counter

```yaml
serializer_in:
  type: pm_xml
  add_managed_element: true
  numeric_values: true

serializer_out:
  type: csv
```

---

### parquet

Apache Parquet columnar format. Best for S3/GCS batch archival. Requires `pip install tram[parquet]`.

| Parameter | Default | Description |
|---|---|---|
| `compression` | `snappy` | `snappy` \| `gzip` \| `brotli` \| `none` |

```yaml
serializer_out:
  type: parquet
  compression: snappy
```

---

### msgpack

MessagePack compact binary format. Requires `pip install tram[msgpack_ser]`.

No configurable parameters.

```yaml
serializer_in:
  type: msgpack

serializer_out:
  type: msgpack
```

---

### bytes

Passthrough binary — wraps raw bytes in a dict for the record pipeline without parsing structure.

| Parameter | Default | Description |
|---|---|---|
| `encoding` | `base64` | How raw bytes are represented in the record: `base64` \| `hex` \| `none` |

The record contains `{"_raw": "<encoded>", "_size": <n>}`. Serialization reverses the encoding. Useful for binary-in-binary forwarding (e.g. passthrough MQTT → S3).

```yaml
serializer_in:
  type: bytes
  encoding: base64
```

---

### text

Line-by-line text. Each non-empty line becomes one record.

| Parameter | Default | Description |
|---|---|---|
| `encoding` | `utf-8` | Text encoding |
| `skip_empty` | `true` | Skip blank lines |
| `line_field` | `_line` | Record key for the line content |
| `include_line_num` | `true` | Add `_line_num` field |
| `newline` | `\n` | Line separator for serialization |

```yaml
serializer_in:
  type: text
  line_field: raw_log
  include_line_num: false

serializer_out:
  type: text
  line_field: raw_log
```

---

## Per-sink features

### Conditional routing

Attach `condition:` to any sink. Only records matching the expression are written to that sink. The expression runs in the same `simpleeval` sandbox as the `filter` transform.

```yaml
sinks:
  - type: kafka
    brokers: [kafka:9092]
    topic: all-alarms

  - type: opensearch
    hosts: [http://os:9200]
    index: critical-alarms
    condition: "severity == 'CRITICAL'"

  - type: local
    path: /data/dlq-debug
    condition: "retry_count > 0"
```

### Per-sink transforms

Apply an additional transform chain before writing to a specific sink.

```yaml
sinks:
  - type: kafka
    brokers: [kafka:9092]
    topic: pm-avro
    serializer_out:
      type: avro
      schema_file: /schemas/pm.avsc
    transforms:
      - type: drop
        fields: [debug_flag, internal_id]

  - type: local
    path: /data/output
    transforms:
      - type: add_field
        fields:
          written_at: "'local'"
```

### Per-sink serializer

Each sink can override the global `serializer_out`, enabling multi-format fan-out from one pipeline.

```yaml
serializer_out:          # global default
  type: json

sinks:
  - type: kafka
    brokers: [kafka:9092]
    topic: pm-avro
    serializer_out:      # override → Avro to Kafka
      type: avro
      schema_file: /schemas/pm.avsc

  - type: local
    path: /data/output   # inherits global → JSON to disk

  - type: s3
    bucket: pm-archive
    serializer_out:      # override → Parquet to S3
      type: parquet
```

---

## Adding a custom connector

Three steps, no core changes required.

**Step 1** — Create the connector:

```python
# tram/connectors/myproto/source.py
from tram.registry.registry import register_source
from tram.interfaces.base_source import BaseSource

@register_source("myproto")
class MyProtoSource(BaseSource):
    def __init__(self, config: dict):
        self.host = config["host"]

    def read(self):
        """Yield (bytes, meta) tuples."""
        yield b'{"key": "value"}', {"source": "myproto"}
```

**Step 2** — Register the import:

```python
# tram/connectors/__init__.py
from tram.connectors.myproto import source  # noqa: F401
```

**Step 3** — Add the Pydantic config model:

```python
# tram/models/pipeline.py
class MyProtoSourceConfig(BaseModel):
    type: Literal["myproto"]
    host: str
    port: int = 9000

# Add MyProtoSourceConfig to the SourceConfig discriminated union
```

The pipeline YAML immediately supports `source.type: myproto`.

---

## Optional dependencies

| Extra | Install command | Enables |
|---|---|---|
| `kafka` | `pip install tram[kafka]` | kafka source/sink |
| `opensearch` | `pip install tram[opensearch]` | opensearch sink |
| `s3` | `pip install tram[s3]` | s3 source/sink |
| `snmp` | `pip install tram[snmp]` | snmp_trap/snmp_poll source/sink |
| `avro` | `pip install tram[avro]` | avro serializer |
| `protobuf_ser` | `pip install tram[protobuf_ser]` | protobuf serializer |
| `asn1` | `pip install tram[asn1]` | asn1 serializer (BER/DER/PER/XER/JER) |
| `parquet` | `pip install tram[parquet]` | parquet serializer |
| `msgpack_ser` | `pip install tram[msgpack_ser]` | msgpack serializer |
| `mqtt` | `pip install tram[mqtt]` | mqtt source/sink |
| `amqp` | `pip install tram[amqp]` | amqp source/sink |
| `nats` | `pip install tram[nats]` | nats source/sink |
| `gnmi` | `pip install tram[gnmi]` | gnmi source |
| `sql` | `pip install tram[sql]` | sql source/sink (SQLAlchemy already a core dep) |
| `postgresql` | `pip install tram[postgresql]` | PostgreSQL driver (psycopg2) |
| `mysql` | `pip install tram[mysql]` | MySQL driver (PyMySQL) |
| `influxdb` | `pip install tram[influxdb]` | influxdb source/sink |
| `redis` | `pip install tram[redis]` | redis source/sink |
| `gcs` | `pip install tram[gcs]` | gcs source/sink |
| `azure` | `pip install tram[azure]` | azure_blob source/sink |
| `websocket` | `pip install tram[websocket]` | websocket source/sink |
| `elasticsearch` | `pip install tram[elasticsearch]` | elasticsearch source/sink |
| `prometheus_rw` | `pip install tram[prometheus_rw]` | prometheus_rw source |
| `corba` | `pip install tram[corba]` | corba source |
| `clickhouse` | `pip install tram[clickhouse]` | clickhouse source/sink |
| `mib` | `pip install tram[mib]` | `tram mib compile` (raw .mib → Python) |
| `otel` | `pip install tram[otel]` | OpenTelemetry tracing |
| `watch` | `pip install tram[watch]` | `TRAM_WATCH_PIPELINES` hot-reload |
| `metrics` | `pip install tram[metrics]` | Prometheus `/metrics` endpoint |
| `jmespath` | `pip install tram[jmespath]` | jmespath transform |
| `all` | `pip install tram[all]` | Everything above |
