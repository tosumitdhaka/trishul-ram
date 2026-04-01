"""Redis source connector — list batch or stream reading."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

@register_source("redis")
class RedisSource(BaseSource):
    """Read from Redis using list (batch) or stream (XREAD) mode.

    Config keys:
        host                (str, default "localhost")
        port                (int, default 6379)
        db                  (int, default 0)
        password            (str, optional)
        mode                (str, default "list")    "list" or "stream"
        key                 (str, required)
        count               (int, default 100)       Items per read
        block_ms            (int, default 1000)      XREAD block timeout ms (stream mode)
        start_id            (str, default "$")       XREAD start ID (stream mode)
        delete_after_read   (bool, default False)    Delete list after reading (list mode)
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config.get("host", "localhost")
        self.port: int = int(config.get("port", 6379))
        self.db: int = int(config.get("db", 0))
        self.password: str | None = config.get("password")
        self.mode: str = config.get("mode", "list")
        self.key: str = config["key"]
        self.count: int = int(config.get("count", 100))
        self.block_ms: int = int(config.get("block_ms", 1000))
        self.start_id: str = config.get("start_id", "$")
        self.delete_after_read: bool = bool(config.get("delete_after_read", False))

    def _get_client(self):
        try:
            import redis
        except ImportError as exc:
            raise SourceError(
                "Redis source requires redis — install with: pip install tram[redis]"
            ) from exc
        try:
            kwargs = {"host": self.host, "port": self.port, "db": self.db, "decode_responses": False}
            if self.password:
                kwargs["password"] = self.password
            return redis.Redis(**kwargs)
        except Exception as exc:
            raise SourceError(f"Redis client creation failed: {exc}") from exc

    def read(self) -> Iterator[tuple[bytes, dict]]:
        client = self._get_client()
        if self.mode == "list":
            try:
                items = client.lrange(self.key, 0, self.count - 1)
                if self.delete_after_read:
                    client.delete(self.key)
                logger.info("Redis list read", extra={"key": self.key, "count": len(items)})
                yield json.dumps([item.decode() if isinstance(item, bytes) else item for item in items]).encode(), {
                    "redis_key": self.key,
                    "redis_mode": "list",
                    "item_count": len(items),
                }
            except Exception as exc:
                raise SourceError(f"Redis LRANGE failed: {exc}") from exc
        elif self.mode == "stream":
            last_id = self.start_id
            while True:
                try:
                    results = client.xread({self.key: last_id}, count=self.count, block=self.block_ms)
                    if results:
                        for stream_key, messages in results:
                            for msg_id, fields in messages:
                                last_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                                payload = {
                                    k.decode() if isinstance(k, bytes) else k:
                                    v.decode() if isinstance(v, bytes) else v
                                    for k, v in fields.items()
                                }
                                yield json.dumps(payload).encode(), {
                                    "redis_key": self.key,
                                    "redis_stream_id": last_id,
                                }
                except Exception as exc:
                    raise SourceError(f"Redis XREAD failed: {exc}") from exc
        else:
            raise SourceError(f"Redis source: unknown mode '{self.mode}'. Use 'list' or 'stream'.")
