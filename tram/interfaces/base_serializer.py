"""BaseSerializer ABC — converts between bytes and list[dict]."""

from __future__ import annotations

from abc import ABC, abstractmethod


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

    @abstractmethod
    def serialize(self, records: list[dict]) -> bytes:
        """Serialize a list of records to bytes.

        Args:
            records: List of dicts to serialize.

        Returns:
            Serialized bytes suitable for writing to a sink.
        """
        ...
