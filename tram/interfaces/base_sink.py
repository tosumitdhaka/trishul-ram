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

    def finalize_source(self, meta: dict, success: bool) -> None:
        """Finalize writes for one logical source unit.

        Batch file sinks may override this to publish staged output only after a
        source file completes successfully. The default implementation is a
        no-op for sinks without source-finalization semantics.
        """
        return None
