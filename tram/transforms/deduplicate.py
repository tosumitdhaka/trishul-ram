"""Deduplicate transform — removes duplicate records based on key fields."""

from __future__ import annotations

from collections import OrderedDict

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("deduplicate")
class DeduplicateTransform(BaseTransform):
    """Remove duplicate records, keeping either the first or last occurrence.

    The seen-key cache is bounded by ``max_cache_size`` (default 100 000) using
    an LRU eviction policy backed by ``collections.OrderedDict``.  This prevents
    unbounded memory growth on long-running stream pipelines where the transform
    instance is reused across many batch runs.

    Config keys:
        fields         (list[str], required)   Fields that form the dedup key.
        keep           (str, default "first")  "first" | "last" — which duplicate
                                               to keep within a single batch.
        max_cache_size (int, default 100_000)  Maximum number of keys held in the
                                               cross-batch seen cache.  Oldest keys
                                               are evicted when the limit is reached.
                                               Set to 0 to disable cross-batch dedup
                                               (per-batch only).
    """

    _DEFAULT_MAX_CACHE = 100_000

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        fields = config.get("fields")
        if not fields:
            raise TransformError("deduplicate: 'fields' config key is required and must not be empty")
        self.fields: list[str] = fields
        self.keep: str = config.get("keep", "first")
        if self.keep not in ("first", "last"):
            raise TransformError(
                f"deduplicate: 'keep' must be 'first' or 'last', got '{self.keep}'"
            )
        self.max_cache_size: int = int(config.get("max_cache_size", self._DEFAULT_MAX_CACHE))
        # OrderedDict used as a bounded LRU set: keys are dedup tuples, values are None.
        # move_to_end(key) marks recent access; popitem(last=False) evicts the oldest.
        self._seen: OrderedDict[tuple, None] = OrderedDict()

    def _cache_contains(self, key: tuple) -> bool:
        """Return True if *key* is in the cross-batch cache; refresh recency."""
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        return False

    def _cache_add(self, key: tuple) -> None:
        """Add *key* to the cache, evicting the oldest entry if at capacity."""
        if self.max_cache_size <= 0:
            return
        self._seen[key] = None
        self._seen.move_to_end(key)
        if len(self._seen) > self.max_cache_size:
            self._seen.popitem(last=False)

    def apply(self, records: list[dict]) -> list[dict]:
        # Within-batch dedup: track per-apply index winners first.
        batch_winner: dict[tuple, int] = {}
        for i, record in enumerate(records):
            key = tuple(record.get(f) for f in self.fields)
            if self.keep == "first" and key not in batch_winner:
                batch_winner[key] = i
            elif self.keep == "last":
                batch_winner[key] = i

        result = []
        for i, record in enumerate(records):
            key = tuple(record.get(f) for f in self.fields)
            if batch_winner.get(key) != i:
                # Lost within-batch dedup — skip without touching cross-batch cache.
                continue
            if self._cache_contains(key):
                # Already seen in a previous batch run — skip.
                continue
            self._cache_add(key)
            result.append(record)

        return result
