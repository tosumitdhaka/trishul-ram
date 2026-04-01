"""Redis sink connector — list, pubsub, or stream write."""
from __future__ import annotations

import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

@register_sink("redis")
class RedisSink(BaseSink):
    """Write data to Redis using list (RPUSH), pubsub (PUBLISH), or stream (XADD) mode.

    Config keys:
        host        (str, default "localhost")
        port        (int, default 6379)
        db          (int, default 0)
        password    (str, optional)
        mode        (str, default "list")    "list", "pubsub", or "stream"
        key         (str, required)
        max_len     (int, optional)          XADD MAXLEN (stream mode only)
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config.get("host", "localhost")
        self.port: int = int(config.get("port", 6379))
        self.db: int = int(config.get("db", 0))
        self.password: str | None = config.get("password")
        self.mode: str = config.get("mode", "list")
        self.key: str = config["key"]
        self.max_len: int | None = config.get("max_len")

    def _get_client(self):
        try:
            import redis
        except ImportError as exc:
            raise SinkError(
                "Redis sink requires redis — install with: pip install tram[redis]"
            ) from exc
        try:
            kwargs = {"host": self.host, "port": self.port, "db": self.db, "decode_responses": False}
            if self.password:
                kwargs["password"] = self.password
            return redis.Redis(**kwargs)
        except Exception as exc:
            raise SinkError(f"Redis client creation failed: {exc}") from exc

    def write(self, data: bytes, meta: dict) -> None:
        client = self._get_client()
        try:
            if self.mode == "list":
                client.rpush(self.key, data)
                logger.info("Redis RPUSH", extra={"key": self.key, "bytes": len(data)})
            elif self.mode == "pubsub":
                client.publish(self.key, data)
                logger.info("Redis PUBLISH", extra={"key": self.key, "bytes": len(data)})
            elif self.mode == "stream":
                xadd_kwargs = {"name": self.key, "fields": {"data": data}}
                if self.max_len is not None:
                    xadd_kwargs["maxlen"] = self.max_len
                    xadd_kwargs["approximate"] = True
                client.xadd(**xadd_kwargs)
                logger.info("Redis XADD", extra={"key": self.key, "bytes": len(data)})
            else:
                raise SinkError(f"Redis sink: unknown mode '{self.mode}'. Use 'list', 'pubsub', or 'stream'.")
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Redis write failed: {exc}") from exc
