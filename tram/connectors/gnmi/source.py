"""gNMI source connector — subscribes to gNMI telemetry stream."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

@register_source("gnmi")
class GnmiSource(BaseSource):
    """Subscribe to a gNMI telemetry stream (STREAM mode).

    Config keys:
        host            (str, required)
        port            (int, default 57400)
        username        (str, default "")
        password        (str, default "")
        tls             (bool, default True)
        tls_ca          (str, optional)  Path to CA certificate file
        subscriptions   (list[dict])     Each: {path, mode, sample_interval}
            path            (str, required)   XPath e.g. "/interfaces/interface[name=*]/state"
            mode            (str, default "SAMPLE")  SAMPLE|ON_CHANGE|TARGET_DEFINED
            sample_interval (int, default 10000000000)  nanoseconds
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.host: str = config["host"]
        self.port: int = int(config.get("port", 57400))
        self.username: str = config.get("username", "")
        self.password: str = config.get("password", "")
        self.tls: bool = bool(config.get("tls", True))
        self.tls_ca: str | None = config.get("tls_ca")
        self.subscriptions: list[dict] = config.get("subscriptions", [])

    def read(self) -> Iterator[tuple[bytes, dict]]:
        try:
            from pygnmi.client import gNMIclient
        except ImportError as exc:
            raise SourceError(
                "gNMI source requires pygnmi — install with: pip install tram[gnmi]"
            ) from exc

        gnmi_kwargs = {
            "target": (self.host, self.port),
            "username": self.username,
            "password": self.password,
            "insecure": not self.tls,
        }
        if self.tls_ca:
            gnmi_kwargs["path_cert"] = self.tls_ca

        subscribe_request = {
            "subscription": [
                {
                    "path": sub["path"],
                    "mode": sub.get("mode", "SAMPLE").upper(),
                    "sampleInterval": sub.get("sample_interval", 10_000_000_000),
                }
                for sub in self.subscriptions
            ],
            "mode": "STREAM",
            "encoding": "JSON_IETF",
        }

        try:
            with gNMIclient(**gnmi_kwargs) as client:
                logger.info(
                    "gNMI source streaming",
                    extra={"host": self.host, "port": self.port},
                )
                for response in client.subscribe_stream(subscribe=subscribe_request):
                    try:
                        updates = []
                        for update in response.get("update", {}).get("update", []):
                            updates.append({
                                "path": update.get("path", ""),
                                "val": update.get("val", {}),
                                "timestamp": response.get("update", {}).get("timestamp", 0),
                            })
                        if updates:
                            payload = json.dumps(updates).encode()
                            yield payload, {
                                "gnmi_host": self.host,
                                "gnmi_port": self.port,
                            }
                    except Exception as exc:
                        logger.warning("gNMI update decode error: %s", exc)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"gNMI subscription failed: {exc}") from exc
