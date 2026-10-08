"""BaseSink ABC — accepts (bytes, meta) and writes to a destination."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum


class DeliveryTier(StrEnum):
    """Delivery confirmation tiers for sink commits (V18-01 frozen contracts).

    Ordered by durability; ``none`` means memory/undefined. Declared by a sink
    via ``delivery_capability`` and reported per commit via the receipt.
    """

    FSYNCED_LOCAL = "fsynced_local"
    REMOTE_DURABLE = "remote_durable"
    REMOTE_ACCEPTED = "remote_accepted"
    NONE = "none"


@dataclass
class SinkCapability:
    """Declared delivery capability of a sink.

    ``replay_safe`` asserts the sink tolerates replaying a source unit without
    duplicating observable output.
    """

    tier: DeliveryTier
    replay_safe: bool


@dataclass
class SinkCommitReceipt:
    """Result of a sink ``commit()`` barrier call."""

    sink_key: str
    tier: DeliveryTier
    confirmed: bool
    notes: str


class BaseSink(ABC):
    """Abstract base class for all TRAM sink connectors."""

    # Declared delivery capability; None = undeclared (legacy semantics).
    delivery_capability: SinkCapability | None = None

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

    def commit(self, *, deadline: float | None = None) -> SinkCommitReceipt:
        """Delivery flush/commit barrier. Distinct from close() (resource release).

        Called by the executor before source ack, transform-state advancement,
        or run success. The default adapter is a single no-op pass at tier
        ``none`` — acceptable only for non-strict (legacy) pipelines.
        """
        return SinkCommitReceipt(
            sink_key=self.__class__.__name__,
            tier=DeliveryTier.NONE,
            confirmed=True,
            notes="",
        )

    def latched_error(self) -> Exception | None:
        """Buffered/background writers (ClickHouse timer) surface latched failures.

        Returns the first unobserved failure recorded since the last check, or
        None when nothing has failed.
        """
        return None

    def close(self) -> None:
        """Release run-scoped resources (timers, buffers, connections).

        Called by the executor after a run finishes. The default is a no-op for
        sinks without persistent resources; sinks that hold resources override
        it. Implementations must be idempotent — close() may be called more
        than once.
        """
        return None
