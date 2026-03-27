"""InfluxDB source connector — queries via Flux."""
from __future__ import annotations
import json
import logging
from typing import Iterator
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

@register_source("influxdb")
class InfluxDbSource(BaseSource):
    """Query InfluxDB using Flux and yield results as JSON bytes.

    Config keys:
        url     (str, required)
        token   (str, required)
        org     (str, required)
        query   (str, required)   Flux query
        timeout (int, default 30)
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config["url"]
        self.token: str = config["token"]
        self.org: str = config["org"]
        self.query: str = config["query"]
        self.timeout: int = int(config.get("timeout", 30))

    def test_connection(self) -> dict:
        import time
        import urllib.request
        t0 = time.monotonic()
        url = (self.config.get("url") or "http://localhost:8086").rstrip("/")
        req = urllib.request.Request(url + "/ping")
        token = self.config.get("token", "")
        if token:
            req.add_header("Authorization", f"Token {token}")
        with urllib.request.urlopen(req, timeout=8) as resp:
            latency = int((time.monotonic() - t0) * 1000)
            return {"ok": True, "latency_ms": latency, "detail": f"InfluxDB /ping {resp.status}"}

    def read(self) -> Iterator[tuple[bytes, dict]]:
        try:
            from influxdb_client import InfluxDBClient
        except ImportError as exc:
            raise SourceError(
                "InfluxDB source requires influxdb-client — install with: pip install tram[influxdb]"
            ) from exc
        try:
            client = InfluxDBClient(url=self.url, token=self.token, org=self.org, timeout=self.timeout * 1000)
            query_api = client.query_api()
            tables = query_api.query(self.query)
            records = []
            for table in tables:
                for record in table.records:
                    records.append(record.values)
            client.close()
            logger.info("InfluxDB source fetched records", extra={"count": len(records)})
            yield json.dumps(records).encode(), {"source_query": self.query, "row_count": len(records)}
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"InfluxDB query failed: {exc}") from exc
