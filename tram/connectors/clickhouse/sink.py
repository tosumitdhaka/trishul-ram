"""ClickHouse sink connector — inserts records via clickhouse-driver."""
from __future__ import annotations

import json
import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("clickhouse")
class ClickHouseSink(BaseSink):
    """Insert records into a ClickHouse table using the native protocol.

    Requires clickhouse-driver: ``pip install tram[clickhouse]``

    Config keys:
        host            (str, default "localhost")  ClickHouse server host
        port            (int, default 9000)         Native protocol port
        database        (str, default "default")    Database name
        username        (str, default "default")    Username
        password        (str, default "")           Password
        table           (str, required)             Target table name
        secure          (bool, default False)       Use TLS
        verify          (bool, default True)        Verify TLS certificate
        connect_timeout (int, default 10)           Connection timeout seconds
        send_receive_timeout (int, default 300)     Send/receive timeout seconds
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

    def write(self, data: bytes, meta: dict) -> None:
        try:
            records = json.loads(data.decode())
        except Exception as exc:
            raise SinkError(f"ClickHouse sink: failed to parse input JSON: {exc}") from exc

        if not records:
            return

        try:
            client = self._get_client()
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"ClickHouse connection failed: {exc}") from exc

        try:
            client.execute(
                f"INSERT INTO {self.table} VALUES",
                records,
            )
            logger.info(
                "ClickHouse sink inserted rows",
                extra={"table": self.table, "rows": len(records)},
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
