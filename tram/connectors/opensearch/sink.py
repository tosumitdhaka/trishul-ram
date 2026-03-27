"""OpenSearch sink connector — bulk-indexes records into OpenSearch/Elasticsearch."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("opensearch")
class OpenSearchSink(BaseSink):
    """Bulk-index a batch of records into OpenSearch (or Elasticsearch).

    Requires ``opensearch-py`` (``pip install opensearch-py``).
    Also compatible with Elasticsearch 7/8 via ``elasticsearch`` client.

    Config keys:
        hosts            (list[str], required)   OpenSearch host URLs.
        index            (str, required)          Target index name. Supports
                                                  strftime tokens: e.g. "pm-%Y.%m.%d"
        id_field         (str, optional)          Record field to use as document _id.
                                                  Auto-generated if omitted.
        pipeline         (str, optional)          Ingest pipeline name.
        username         (str, optional)          HTTP basic auth username.
        password         (str, optional)          HTTP basic auth password.
        verify_ssl       (bool, default True)      Verify SSL certificates.
        use_ssl          (bool, default False)     Use HTTPS.
        timeout          (int, default 30)         Request timeout.
        chunk_size       (int, default 500)        Records per bulk request.
        refresh          (str, default "false")    "true" | "false" | "wait_for"
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        hosts = config["hosts"]
        self.hosts: list[str] = hosts if isinstance(hosts, list) else [hosts]
        self.index_template: str = config["index"]
        self.id_field: str | None = config.get("id_field")
        self.pipeline: str | None = config.get("pipeline")
        self.username: str | None = config.get("username")
        self.password: str | None = config.get("password")
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))
        self.use_ssl: bool = bool(config.get("use_ssl", False))
        self.timeout: int = int(config.get("timeout", 30))
        self.chunk_size: int = int(config.get("chunk_size", 500))
        self.refresh: str = config.get("refresh", "false")

    def _get_client(self):
        try:
            from opensearchpy import OpenSearch
        except ImportError as exc:
            raise SinkError(
                "OpenSearch sink requires opensearch-py: pip install opensearch-py"
            ) from exc

        kwargs: dict = dict(
            hosts=self.hosts,
            use_ssl=self.use_ssl,
            verify_certs=self.verify_ssl,
            timeout=self.timeout,
        )
        if self.username:
            kwargs["http_auth"] = (self.username, self.password or "")

        return OpenSearch(**kwargs)

    def test_connection(self) -> dict:
        import time
        import urllib.request
        t0 = time.monotonic()
        hosts = self.config.get("hosts") or ["http://localhost:9200"]
        host = (hosts[0] if isinstance(hosts, list) else hosts).rstrip("/")
        req = urllib.request.Request(host + "/")
        username = self.config.get("username", "")
        password = self.config.get("password", "")
        if username:
            import base64
            creds = base64.b64encode(f"{username}:{password}".encode()).decode()
            req.add_header("Authorization", f"Basic {creds}")
        with urllib.request.urlopen(req, timeout=8) as resp:
            import json as _json
            info = _json.loads(resp.read())
            latency = int((time.monotonic() - t0) * 1000)
            name = info.get("name", "?")
            version = info.get("version", {}).get("number", "?")
            return {"ok": True, "latency_ms": latency,
                    "detail": f"OpenSearch node={name} version={version}"}

    def _current_index(self) -> str:
        return datetime.now(timezone.utc).strftime(self.index_template)

    def _build_bulk_body(self, records: list[dict], index: str) -> bytes:
        lines = []
        for record in records:
            action: dict = {"index": {"_index": index}}
            if self.id_field and self.id_field in record:
                action["index"]["_id"] = str(record[self.id_field])
            if self.pipeline:
                action["index"]["pipeline"] = self.pipeline
            lines.append(json.dumps(action))
            lines.append(json.dumps(record, default=str))
        return "\n".join(lines).encode("utf-8") + b"\n"

    def write(self, data: bytes, meta: dict) -> None:
        try:
            records: list[dict] = json.loads(data)
        except Exception as exc:
            raise SinkError(f"OpenSearch sink: failed to parse input as JSON: {exc}") from exc

        if not records:
            return

        client = self._get_client()
        index = self._current_index()
        total_ok = 0
        total_err = 0

        # Process in chunks
        for i in range(0, len(records), self.chunk_size):
            chunk = records[i : i + self.chunk_size]
            bulk_body = self._build_bulk_body(chunk, index)
            try:
                resp = client.bulk(body=bulk_body, refresh=self.refresh)
            except Exception as exc:
                raise SinkError(f"OpenSearch bulk request failed: {exc}") from exc

            if resp.get("errors"):
                for item in resp.get("items", []):
                    action_result = item.get("index", {})
                    if action_result.get("error"):
                        total_err += 1
                        logger.warning(
                            "OpenSearch index error",
                            extra={
                                "index": index,
                                "error": action_result["error"],
                            },
                        )
                    else:
                        total_ok += 1
            else:
                total_ok += len(chunk)

        logger.info(
            "OpenSearch bulk write complete",
            extra={
                "index": index,
                "ok": total_ok,
                "errors": total_err,
                "total": len(records),
            },
        )

        if total_err > 0 and total_ok == 0:
            raise SinkError(f"OpenSearch: all {total_err} documents failed to index")
