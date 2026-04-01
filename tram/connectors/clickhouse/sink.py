"""ClickHouse sink connector — inserts records via clickhouse-driver with batch buffering."""
from __future__ import annotations

import json
import logging
import threading

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


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
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config.get("host", "localhost")
        self.port: int = int(config.get("port", 9000))
        self.database: str = config.get("database", "default")
        self.username: str = config.get("username", "default")
        self.password: str = config.get("password", "")
        self.table: str = config["table"]
        self.secure: bool = bool(config.get("secure", False))
        self.verify: bool = bool(config.get("verify", True))
        self.connect_timeout: int = int(config.get("connect_timeout", 10))
        self.send_receive_timeout: int = int(config.get("send_receive_timeout", 300))
        self.batch_size: int = int(config.get("batch_size", 5000))
        self.batch_timeout_seconds: float = float(config.get("batch_timeout_seconds", 2.0))
        self.batch_flush_on_stop: bool = bool(config.get("batch_flush_on_stop", True))

        self._buffer: list[dict] = []
        self._buffer_lock = threading.Lock()
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
            logger.error("ClickHouse timer flush failed", extra={"table": self.table, "error": str(exc)})
        if not self._closed:
            self._schedule_flush()

    # ── Buffer management ──────────────────────────────────────────────────

    def _flush(self) -> None:
        with self._buffer_lock:
            if not self._buffer:
                return
            rows = self._buffer[:]
            self._buffer.clear()
        self._insert_rows(rows)

    def close(self) -> None:
        """Flush remaining buffer and stop the timer. Called by executor on stream stop."""
        self._closed = True
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        if self.batch_flush_on_stop:
            try:
                self._flush()
            except Exception as exc:
                logger.error("ClickHouse close flush failed", extra={"table": self.table, "error": str(exc)})

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
