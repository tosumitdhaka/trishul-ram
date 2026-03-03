"""Parquet serializer."""
from __future__ import annotations
import io
from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer

@register_serializer("parquet")
class ParquetSerializer(BaseSerializer):
    """Serialize/deserialize Parquet data. Requires pyarrow>=16."""
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.compression: str = config.get("compression", "snappy")

    def parse(self, data: bytes) -> list[dict]:
        try:
            from pyarrow import parquet as pq
        except ImportError as exc:
            raise SerializerError("Parquet serializer requires pyarrow — install with: pip install tram[parquet]") from exc
        try:
            buf = io.BytesIO(data)
            return pq.read_table(buf).to_pylist()
        except Exception as exc:
            raise SerializerError(f"Parquet parse error: {exc}") from exc

    def serialize(self, records: list[dict]) -> bytes:
        try:
            import pyarrow as pa
            from pyarrow import parquet as pq
        except ImportError as exc:
            raise SerializerError("Parquet serializer requires pyarrow — install with: pip install tram[parquet]") from exc
        try:
            table = pa.Table.from_pylist(records)
            buf = io.BytesIO()
            pq.write_table(table, buf, compression=self.compression if self.compression != "none" else None)
            return buf.getvalue()
        except Exception as exc:
            raise SerializerError(f"Parquet serialize error: {exc}") from exc
