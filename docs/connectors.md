# TRAM Connector Guide

## How to Add a New Connector

Adding a new protocol (Kafka, OpenSearch, REST, SNMP, VES, etc.) requires exactly **3 steps** and zero changes to the core engine.

### Step 1 — Create connector file(s)

```python
# tram/connectors/kafka/source.py
from tram.registry.registry import register_source
from tram.interfaces.base_source import BaseSource

@register_source("kafka")
class KafkaSource(BaseSource):
    def __init__(self, config: dict):
        self.brokers = config["brokers"]
        self.topic = config["topic"]
        self.group_id = config.get("group_id", "tram")

    def read(self):
        """Infinite generator for stream mode."""
        from kafka import KafkaConsumer
        consumer = KafkaConsumer(
            self.topic,
            bootstrap_servers=self.brokers,
            group_id=self.group_id,
        )
        for msg in consumer:
            yield msg.value, {
                "offset": msg.offset,
                "topic": msg.topic,
                "partition": msg.partition,
            }
```

### Step 2 — Add import to `connectors/__init__.py`

```python
# tram/connectors/__init__.py  (add one line)
from tram.connectors.kafka import source  # noqa: F401
```

### Step 3 — Add Pydantic config model (recommended)

```python
# tram/models/pipeline.py — add to the discriminated union
from typing import Literal

class KafkaSourceConfig(BaseModel):
    type: Literal["kafka"]
    brokers: list[str]
    topic: str
    group_id: str = "tram"
    auto_offset_reset: str = "latest"

# Then add KafkaSourceConfig to the SourceConfig union:
SourceConfig = Annotated[
    Union[SFTPSourceConfig, KafkaSourceConfig],
    Field(discriminator="type")
]
```

**That's it.** The pipeline YAML immediately supports:

```yaml
source:
  type: kafka
  brokers: [kafka:9092]
  topic: pm-raw
  group_id: tram-pm
schedule:
  type: stream
```

## Built-in Connectors

### SFTP Source

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | str | required | SFTP server hostname |
| `port` | int | 22 | SSH port |
| `username` | str | required | SSH username |
| `password` | str | — | SSH password (or use private_key_path) |
| `private_key_path` | str | — | Path to private key file |
| `remote_path` | str | required | Directory to read files from |
| `file_pattern` | str | `"*"` | Glob pattern for file matching |
| `move_after_read` | str | — | Move files here after reading |
| `delete_after_read` | bool | false | Delete files after reading |

### SFTP Sink

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `host` | str | required | SFTP server hostname |
| `port` | int | 22 | SSH port |
| `username` | str | required | SSH username |
| `password` | str | — | SSH password (or use private_key_path) |
| `private_key_path` | str | — | Path to private key file |
| `remote_path` | str | required | Remote directory to write to |
| `filename_template` | str | `"{pipeline}_{timestamp}.bin"` | Filename template |

**Filename template tokens:**
- `{pipeline}` — pipeline name
- `{timestamp}` — UTC timestamp (ISO format, colons replaced with dashes)
- `{source_filename}` — original source filename (if available in meta)

## Sink Interface

```python
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

@register_sink("opensearch")
class OpenSearchSink(BaseSink):
    def __init__(self, config: dict):
        self.index = config["index"]
        # ...

    def write(self, data: bytes, meta: dict) -> None:
        import json
        records = json.loads(data)
        # bulk index to OpenSearch
```

## Transform Interface

```python
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

@register_transform("deduplicate")
class DeduplicateTransform(BaseTransform):
    def __init__(self, config: dict):
        self.key_field = config["key_field"]

    def apply(self, records: list[dict]) -> list[dict]:
        seen = set()
        result = []
        for r in records:
            k = r.get(self.key_field)
            if k not in seen:
                seen.add(k)
                result.append(r)
        return result
```

## Serializer Interface

```python
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer

@register_serializer("parquet")
class ParquetSerializer(BaseSerializer):
    def parse(self, data: bytes) -> list[dict]:
        import pyarrow.parquet as pq
        import io
        table = pq.read_table(io.BytesIO(data))
        return table.to_pylist()

    def serialize(self, records: list[dict]) -> bytes:
        import pyarrow as pa
        import pyarrow.parquet as pq
        import io
        table = pa.Table.from_pylist(records)
        buf = io.BytesIO()
        pq.write_table(table, buf)
        return buf.getvalue()
```
