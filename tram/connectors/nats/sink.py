"""NATS sink connector — publishes data via asyncio.run."""
from __future__ import annotations

import asyncio
import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

@register_sink("nats")
class NatsSink(BaseSink):
    """Publish data to a NATS subject.

    Config keys:
        servers             (list[str], default ["nats://localhost:4222"])
        subject             (str, required)
        credentials_file    (str, optional)
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.servers: list[str] = config.get("servers", ["nats://localhost:4222"])
        self.subject: str = config["subject"]
        self.credentials_file: str | None = config.get("credentials_file")

    async def _async_publish(self, data: bytes) -> None:
        try:
            import nats
        except ImportError as exc:
            raise SinkError(
                "NATS sink requires nats-py — install with: pip install tram[nats]"
            ) from exc
        kwargs = {"servers": self.servers}
        if self.credentials_file:
            kwargs["credentials"] = self.credentials_file
        nc = await nats.connect(**kwargs)
        try:
            await nc.publish(self.subject, data)
            await nc.flush()
            logger.info("Published to NATS", extra={"subject": self.subject, "bytes": len(data)})
        finally:
            await nc.close()

    def write(self, data: bytes, meta: dict) -> None:
        try:
            asyncio.run(self._async_publish(data))
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"NATS publish failed: {exc}") from exc
