"""ClickHouse sink connector — inserts records via clickhouse-driver with batch buffering."""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import (
    BaseSink,
    DeliveryTier,
    SinkCapability,
    SinkCommitReceipt,
)
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


@register_sink("clickhouse")
class ClickHouseSink(BaseSink):
    """Insert records into a ClickHouse table using the native protocol.

    Buffers records in memory and flushes as a single bulk INSERT when either
    ``batch_size`` rows accumulate or ``batch_timeout_seconds`` elapses —
    preventing the ClickHouse "too many parts" error on MergeTree tables under
    high-throughput stream workloads (e.g. Kafka source).

    Requires clickhouse-driver: ``pip install tram[clickhouse]``

    Config keys:
        host                  (str,   default "localhost")  ClickHouse host
        port                  (int,   default 9000)         Native protocol port
        database              (str,   default "default")    Database name
        username              (str,   default "default")    Username
        password              (str,   default "")           Password
        table                 (str,   required)             Target table name
        secure                (bool,  default False)        Use TLS
        verify                (bool,  default True)         Verify TLS certificate
        connect_timeout       (int,   default 10)           Connection timeout (s)
        send_receive_timeout  (int,   default 300)          Send/receive timeout (s)
        batch_size            (int,   default 5000)         Flush when buffer reaches N rows
        batch_timeout_seconds (float, default 2.0)          Flush every N seconds regardless
        batch_flush_on_stop   (bool,  default True)         Flush remaining rows on close

    Delivery (R1 fix, V18-01 section 6): rows are retained until the insert is
    confirmed — the buffer clears only after a successful bulk insert, never
    before. Timer / foreground (``write``) / close flushes are serialized so no
    two inserts interleave. Background (timer) failures are latched and surface
    via ``latched_error()`` and ``commit()``, which is the delivery barrier the
    executor calls before source acknowledgement. Cancellation/stop never
    erases the pending buffer: a failed or skipped final flush leaves the rows
    buffered for replay/retry. The synchronous insert return is the
    confirmation the clickhouse-driver client provides — that is the frozen
    tier boundary; no stronger durability is invented.
    """

    # V18-01 frozen tier table, section 6: remote_durable (confirmed insert,
    # retained buffer, serialized flushes — R1 fix). Not replay-safe: a replay
    # re-inserts the same rows (duplicates).
    delivery_capability = SinkCapability(
        tier=DeliveryTier.REMOTE_DURABLE,
        replay_safe=False,
    )

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config.get("host", "localhost")
        self.port: int = int(config.get("port", 9000))
        self.database: str = config.get("database", "default")
        self.username: str = config.get("username", "default")
        self.password: str = config.get("password", "")
        self.table: str = config["table"]
        if not _IDENTIFIER_RE.fullmatch(self.table):
            raise SinkError(
                f"ClickHouse table name {self.table!r} is not a valid identifier — "
                "use letters, digits, underscores, and at most one dot (db.table)"
            )
        self.secure: bool = bool(config.get("secure", False))
        self.verify: bool = bool(config.get("verify", True))
        self.connect_timeout: int = int(config.get("connect_timeout", 10))
        self.send_receive_timeout: int = int(config.get("send_receive_timeout", 300))
        self.batch_size: int = int(config.get("batch_size", 5000))
        self.batch_timeout_seconds: float = float(config.get("batch_timeout_seconds", 2.0))
        self.batch_flush_on_stop: bool = bool(config.get("batch_flush_on_stop", True))

        self._buffer: list[dict] = []
        self._buffer_lock = threading.Lock()
        # Serializes timer / foreground (write) / close flushes: one bulk
        # insert at a time, never interleaved (plan C, R1 fix).
        self._flush_lock = threading.Lock()
        self._latched_error: Exception | None = None
        self._closed = False
        self._flush_timer: threading.Timer | None = None
        self._schedule_flush()

    # ── Timer ──────────────────────────────────────────────────────────────

    def _schedule_flush(self) -> None:
        if self._closed or self.batch_timeout_seconds <= 0:
            return
        self._flush_timer = threading.Timer(self.batch_timeout_seconds, self._timer_flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _timer_flush(self) -> None:
        try:
            self._flush()
        except Exception as exc:
            # _flush() already latched the failure; log it here so the
            # background path is observable and commit() will refuse to
            # report a clean success.
            logger.error("ClickHouse timer flush failed", extra={"table": self.table, "error": str(exc)})
        if not self._closed:
            self._schedule_flush()

    # ── Buffer management ──────────────────────────────────────────────────

    def _flush(self) -> None:
        """Deliver buffered rows as one bulk INSERT.

        Serialized by ``_flush_lock`` so concurrent timer/foreground/close
        flushes never interleave. The buffer is cleared ONLY after the insert
        is confirmed — a failed insert keeps the rows buffered for
        retry/replay (R1 fix).
        """
        with self._flush_lock:
            with self._buffer_lock:
                if not self._buffer:
                    return
                rows = self._buffer[:]
            try:
                self._insert_rows(rows)
            except Exception as exc:
                self._latched_error = exc
                raise
            with self._buffer_lock:
                # Drop only the rows that were just confirmed; records appended
                # by write() while the insert was in flight stay buffered.
                del self._buffer[: len(rows)]

    def close(self) -> None:
        """Flush remaining buffer and stop the timer. Idempotent — safe to call twice.

        Cancellation/stop never erases the pending buffer: a failed final
        flush (or ``batch_flush_on_stop=False``) leaves the rows buffered so
        replay or retry can re-drive them — no false clean success.
        """
        if self._closed:
            return
        self._closed = True
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None
        if self.batch_flush_on_stop:
            try:
                self._flush()
            except Exception as exc:
                # _flush() already latched the failure; log it here.
                logger.error("ClickHouse close flush failed", extra={"table": self.table, "error": str(exc)})

    # ── Delivery barrier ───────────────────────────────────────────────────

    def commit(self, *, deadline: float | None = None) -> SinkCommitReceipt:
        """Delivery flush/commit barrier (V18-01 section 6).

        Flushes buffered rows and confirms the insert. Raises ``SinkError``
        when a background flush has latched a failure or the final flush fails
        — the executor must not acknowledge the source in that case. The
        synchronous insert return is the confirmation the current client
        provides (frozen tier boundary); it is reported at ``remote_durable``.
        """
        latched = self._latched_error
        if latched is not None:
            self._latched_error = None
            raise SinkError(f"ClickHouse sink has a latched delivery failure: {latched}") from latched
        if deadline is not None and time.monotonic() >= deadline:
            raise SinkError("ClickHouse commit deadline exceeded before flush")
        self._flush()
        return SinkCommitReceipt(
            sink_key=self.__class__.__name__,
            tier=DeliveryTier.REMOTE_DURABLE,
            confirmed=True,
            notes="synchronous insert confirmed; clickhouse-driver exposes no stronger receipt",
        )

    def latched_error(self) -> Exception | None:
        """Return the first unobserved flush failure, clearing it on read."""
        exc = self._latched_error
        self._latched_error = None
        return exc

    # ── Transport ──────────────────────────────────────────────────────────

    def _get_client(self):
        try:
            from clickhouse_driver import Client
        except ImportError as exc:
            raise SinkError(
                "ClickHouse sink requires clickhouse-driver — "
                "install with: pip install tram[clickhouse]"
            ) from exc
        return Client(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.username,
            password=self.password,
            secure=self.secure,
            verify=self.verify,
            connect_timeout=self.connect_timeout,
            send_receive_timeout=self.send_receive_timeout,
        )

    def _insert_rows(self, rows: list[dict]) -> None:
        try:
            client = self._get_client()
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"ClickHouse connection failed: {exc}") from exc
        try:
            client.execute(f"INSERT INTO {self.table} VALUES", rows)
            logger.info(
                "ClickHouse sink flushed batch",
                extra={"table": self.table, "rows": len(rows)},
            )
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"ClickHouse insert failed: {exc}") from exc
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    # ── BaseSink interface ─────────────────────────────────────────────────

    def write(self, data: bytes, meta: dict) -> None:
        try:
            records = json.loads(data.decode())
        except Exception as exc:
            raise SinkError(f"ClickHouse sink: failed to parse input JSON: {exc}") from exc

        if not records:
            return

        with self._buffer_lock:
            self._buffer.extend(records)
            should_flush = len(self._buffer) >= self.batch_size

        if should_flush:
            self._flush()
