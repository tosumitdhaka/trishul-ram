"""BaseTransform ABC — maps list[dict] → list[dict]."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseTransform(ABC):
    """Abstract base class for all TRAM transforms."""

    def __init__(self, config: dict) -> None:
        self.config = config

    @abstractmethod
    def apply(self, records: list[dict]) -> list[dict]:
        """Transform a list of records.

        Args:
            records: Input records (may be modified in place or replaced).

        Returns:
            Transformed records (may be fewer if rows are filtered).
        """
        ...
