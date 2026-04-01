"""ClickHouse source connector — queries a ClickHouse table via clickhouse-driver."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("clickhouse")
class ClickHouseSource(BaseSource):
    """Query a ClickHouse table and yield results as JSON bytes.

    Operates in batch mode. If chunk_size > 0, yields multiple chunks.

    Config keys:
        host            (str, default "localhost")  ClickHouse server host
        port            (int, default 9000)         Native protocol port
        database        (str, default "default")    Database name
        username        (str, default "default")    Username
        password        (str, default "")           Password
        query           (str, required)             SELECT query
        params          (dict, default {})          Named query parameters
        chunk_size      (int, default 0)            Rows per chunk; 0 = all in one item
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
        self.query: str = config["query"]
        self.params: dict = config.get("params", {})
        self.chunk_size: int = int(config.get("chunk_size", 0))
        self.secure: bool = bool(config.get("secure", False))
        self.verify: bool = bool(config.get("verify", True))
        self.connect_timeout: int = int(config.get("connect_timeout", 10))
        self.send_receive_timeout: int = int(config.get("send_receive_timeout", 300))

    def _get_client(self):
        try:
            from clickhouse_driver import Client
        except ImportError as exc:
            raise SourceError(
                "ClickHouse source requires clickhouse-driver — "
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

    def read(self) -> Iterator[tuple[bytes, dict]]:
        try:
            client = self._get_client()
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"ClickHouse connection failed: {exc}") from exc

        try:
            if self.chunk_size > 0:
                settings = {"max_block_size": self.chunk_size}
                rows_iter = client.execute_iter(
                    self.query,
                    self.params or {},
                    with_column_types=True,
                    settings=settings,
                )
                columns = None
                batch: list[dict] = []
                for item in rows_iter:
                    if columns is None:
                        # First item from execute_iter is column definitions
                        columns = [col[0] for col in item]
                        continue
                    batch.append(dict(zip(columns, item)))
                    if len(batch) >= self.chunk_size:
                        yield json.dumps(batch).encode(), {
                            "source_query": self.query,
                            "row_count": len(batch),
                        }
                        logger.debug("ClickHouse source yielded chunk", extra={"rows": len(batch)})
                        batch = []
                if batch:
                    yield json.dumps(batch).encode(), {
                        "source_query": self.query,
                        "row_count": len(batch),
                    }
            else:
                rows, columns_info = client.execute(
                    self.query,
                    self.params or {},
                    with_column_types=True,
                )
                columns = [col[0] for col in columns_info]
                records = [dict(zip(columns, row)) for row in rows]
                logger.info(
                    "ClickHouse source fetched rows",
                    extra={"row_count": len(records)},
                )
                yield json.dumps(records).encode(), {
                    "source_query": self.query,
                    "row_count": len(records),
                }
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"ClickHouse query failed: {exc}") from exc
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
