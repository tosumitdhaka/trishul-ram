"""BaseSerializer ABC — converts between bytes and list[dict]."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator


class BaseSerializer(ABC):
    """Abstract base class for all TRAM serializers."""

    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    def parse(self, data: bytes) -> list[dict]:
        """Parse raw bytes into a list of records.

        Args:
            data: Raw bytes from a source (file content, message payload).

        Returns:
            List of dicts, one per logical record.
        """
        ...

    def parse_chunks(self, data: bytes, record_chunk_size: int) -> Iterator[list[dict]]:
        """Parse raw bytes into bounded batches of records.

        Serializers may override this to avoid materializing the full decoded
        record set in memory. The default implementation preserves current
        behavior by calling ``parse()`` once and slicing the result.
        """
        records = self.parse(data)
        if record_chunk_size <= 0:
            yield records
            return
        for offset in range(0, len(records), record_chunk_size):
            yield records[offset:offset + record_chunk_size]

    @abstractmethod
    def serialize(self, records: list[dict]) -> bytes:
        """Serialize a list of records to bytes.

        Args:
            records: List of dicts to serialize.

        Returns:
            Serialized bytes suitable for writing to a sink.
        """
        ...
