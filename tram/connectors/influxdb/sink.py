"""InfluxDB sink connector — writes Points."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

@register_sink("influxdb")
class InfluxDbSink(BaseSink):
    """Write records as InfluxDB Points.

    Config keys:
        url             (str, required)
        token           (str, required)
        org             (str, required)
        bucket          (str, required)
        measurement     (str, required)
        tag_fields      (list[str], default [])   Fields to use as tags
        timestamp_field (str, optional)            Field to use as timestamp
        precision       (str, default "ns")        ns/us/ms/s
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config["url"]
        self.token: str = config["token"]
        self.org: str = config["org"]
        self.bucket: str = config["bucket"]
        self.measurement: str = config["measurement"]
        self.tag_fields: list[str] = config.get("tag_fields", [])
        self.timestamp_field: str | None = config.get("timestamp_field")
        self.precision: str = config.get("precision", "ns")

    def write(self, data: bytes, meta: dict) -> None:
        try:
            records = json.loads(data.decode())
        except Exception as exc:
            raise SinkError(f"InfluxDB sink: failed to parse input JSON: {exc}") from exc
        if not records:
            return
        try:
            from influxdb_client import InfluxDBClient, Point, WritePrecision
            from influxdb_client.client.write_api import SYNCHRONOUS
        except ImportError as exc:
            raise SinkError(
                "InfluxDB sink requires influxdb-client — install with: pip install tram[influxdb]"
            ) from exc
        precision_map = {
            "ns": WritePrecision.NANOSECONDS,
            "us": WritePrecision.MICROSECONDS,
            "ms": WritePrecision.MILLISECONDS,
            "s": WritePrecision.SECONDS,
        }
        write_precision = precision_map.get(self.precision, WritePrecision.NANOSECONDS)
        try:
            client = InfluxDBClient(url=self.url, token=self.token, org=self.org)
            write_api = client.write_api(write_options=SYNCHRONOUS)
            points = []
            for rec in records:
                p = Point(self.measurement)
                for k, v in rec.items():
                    if k == self.timestamp_field:
                        p = p.time(v, write_precision)
                    elif k in self.tag_fields:
                        p = p.tag(k, v)
                    else:
                        p = p.field(k, v)
                points.append(p)
            write_api.write(bucket=self.bucket, org=self.org, record=points, precision=write_precision)
            client.close()
            logger.info("InfluxDB sink wrote points", extra={"bucket": self.bucket, "count": len(points)})
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"InfluxDB write failed: {exc}") from exc
