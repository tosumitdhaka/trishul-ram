"""BaseSource ABC — yields (bytes, meta) tuples."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from enum import StrEnum


class AckDisposition(StrEnum):
    """Outcome of a decided source unit; drives destructive source ``ack()``.

    ``delivered``/``filtered``/``dlq``/``dropped`` — every value the executor
    may pass to ``ack()`` for a unit whose disposition is decided.
    """

    DELIVERED = "delivered"
    FILTERED = "filtered"
    DLQ = "dlq"
    DROPPED = "dropped"


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

    def finalize(self, meta: dict, *, success: bool) -> None:
        """Deferred post-read finalize hook (no-op by default).

        The executor calls this after every chunk yielded for a source unit
        (file) has been drained from the worker pool. Batch sources that move,
        delete, or mark files as processed after reading should override this
        instead of performing those actions inside ``read()`` — a generator
        resumes as soon as the last chunk is *submitted*, not when its writes
        complete, so post-read actions run inside ``read()`` can destroy input
        while sink writes are still pending (code review A2).

        ``success`` is False when the run is aborting mid-unit; the default
        implementations of the file sources then leave the file untouched so a
        retry can reprocess it.
        """

    def close(self) -> None:
        """Release run-scoped resources (connections, handles).

        Called by the executor when a batch/stream run finishes. Defaults to a
        no-op; sources that keep a connection open across ``read()`` and
        ``finalize()`` must close it here.
        """

    def stop(self) -> None:
        """Unblock an idle read() (reconnect waits, broker polls).

        Formalizes the duck-typed call in the executor's stop watcher
        (executor.py:1988). The default is a no-op for sources whose read()
        unblocks on close() or run teardown.
        """

    def source_unit_id(self, meta: dict) -> str | None:
        """Stable replay identity for the unit; None = no durable identity.

        See the replay identity matrix in the V18-01 frozen contracts. Returned
        identities must be stable across retries within a run.
        """

    def ack(self, meta: dict, disposition: AckDisposition) -> None:
        """Called only for decided units (delivered/filtered/dlq/dropped).

        The destructive mark/move/delete currently inside finalize() moves
        behind this gate. Default adapter: no-op, preserving current
        finalize() behavior for unmigrated connectors.
        """
