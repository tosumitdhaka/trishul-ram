"""BaseSource ABC — yields (bytes, meta) tuples."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator


class BaseSource(ABC):
    """Abstract base class for all TRAM source connectors.

    A source yields ``(data, meta)`` pairs:
    - ``data``: raw bytes (file content, message payload, etc.)
    - ``meta``: dict of metadata (filename, offset, topic, etc.)

    Batch sources yield a finite number of items and return.
    Stream sources yield indefinitely (blocking between items) until the
    consuming ``stream_run`` loop sets its stop event.
    """

    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    def read(self) -> Iterator[tuple[bytes, dict]]:
        """Yield ``(bytes, metadata)`` tuples.

        For batch mode: finite iterator — yields all available items then returns.
        For stream mode: infinite generator — blocks waiting for next message.
        """
        ...
