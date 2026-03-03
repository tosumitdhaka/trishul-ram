"""BaseSink ABC — accepts (bytes, meta) and writes to a destination."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseSink(ABC):
    """Abstract base class for all TRAM sink connectors."""

    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    def write(self, data: bytes, meta: dict) -> None:
        """Write serialized data to the destination.

        Args:
            data: Serialized bytes (output of serializer_out).
            meta: Metadata from the originating source read (filename, etc.).
        """
        ...
