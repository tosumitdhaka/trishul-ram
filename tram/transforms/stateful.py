"""StatefulTransform protocol — the contract for transforms that outlive a chunk.

Transforms implementing this protocol keep per-key state (previous counter
values, open windows) that must survive across batch runs (interval polls) and
stream redispatches. The executor (``tram/pipeline/executor.py``) is the only
place that hydrates and persists this state: it loads the pipeline's durable
blob at run start, calls ``set_state`` on every stateful transform, collects
``get_state`` from each, and saves the merged blob back through the configured
``TransformStateStore`` (design F.1 §3.2c).

``state_key`` is the transform's stable identity inside the blob: transform
type + position in the transforms list (assigned by the executor at build
time, e.g. ``"counter_delta:0"``).

``close(flush)`` is the run-end hook mirroring the sink ``close()`` contract.
``flush=False`` means "no partial output": batch runs never flush per-tick
(flushing would re-emit the same partial window after rehydration). The stream
``flush_on_close`` default-true semantics arrive with ``window_aggregate``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StatefulTransform(Protocol):
    """A transform that keeps durable state across chunks and runs."""

    state_key: str

    def get_state(self) -> dict:
        """Return the transform's current state blob (must be JSON-safe)."""
        ...

    def set_state(self, blob: dict) -> None:
        """Replace the transform's state from a hydrated blob."""
        ...

    def close(self, flush: bool) -> None:
        """Release run-scoped resources; emit partial output when *flush*."""
        ...