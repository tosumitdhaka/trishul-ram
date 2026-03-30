"""VES sink connector — posts events to an ONAP VES Collector."""

from __future__ import annotations

import json
import logging
import time
import uuid

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("ves")
class VESSink(BaseSink):
    """POST each record to an ONAP VES Collector as an eventList JSON body.

    Each dict in the serialized payload is wrapped in the standard VES envelope:
    ``{commonEventHeader: {...}, <domain>Body: record}``

    Uses httpx (already a core TRAM dependency) — no extra deps required.

    Config keys:
        url                  (str, required)       VES Collector endpoint URL.
        domain               (str, default "other") Event domain.
        source_name          (str, default "tram")  VES sourceId / sourceName.
        reporting_entity_name (str, default "tram") reportingEntityName.
        priority             (str, default "Normal") Event priority.
        version              (str, default "4.1")   VES commonEventHeader version.
        auth_type            (str, default "none")  "none" | "basic" | "bearer".
        username             (str, optional)        Basic auth username.
        password             (str, optional)        Basic auth password.
        token                (str, optional)        Bearer token.
        expected_status      (list[int], default [202]) Acceptable HTTP status codes.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config["url"]
        self.domain: str = config.get("domain", "other")
        self.source_name: str = config.get("source_name", "tram")
        self.reporting_entity_name: str = config.get("reporting_entity_name", "tram")
        self.priority: str = config.get("priority", "Normal")
        self.version: str = config.get("version", "4.1")
        self.auth_type: str = config.get("auth_type", "none")
        self.username: str = config.get("username", "")
        self.password: str = config.get("password", "")
        self.token: str = config.get("token", "")
        self.expected_status: list[int] = config.get("expected_status", [202])

    def _build_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth_type == "bearer" and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _build_auth(self):
        if self.auth_type == "basic" and self.username:
            return (self.username, self.password)
        return None

    def _wrap_event(self, record: dict, sequence: int) -> dict:
        """Wrap a single record dict in the VES commonEventHeader envelope."""
        domain_body_key = f"{self.domain}Fields"
        return {
            "event": {
                "commonEventHeader": {
                    "domain": self.domain,
                    "eventId": str(uuid.uuid4()),
                    "eventName": f"{self.domain}_{self.source_name}",
                    "lastEpochMicrosec": int(time.time() * 1_000_000),
                    "priority": self.priority,
                    "reportingEntityName": self.reporting_entity_name,
                    "sequence": sequence,
                    "sourceName": self.source_name,
                    "startEpochMicrosec": int(time.time() * 1_000_000),
                    "version": self.version,
                    "vesEventListenerVersion": "7.2.1",
                },
                domain_body_key: record,
            }
        }

    def test_connection(self) -> dict:
        import time
        import urllib.request
        t0 = time.monotonic()
        url = self.config.get("url", "")
        if not url:
            raise RuntimeError("No 'url' in config")
        req = urllib.request.Request(url, method="HEAD")
        auth_type = self.config.get("auth_type", "none")
        if auth_type == "bearer" and self.config.get("token"):
            req.add_header("Authorization", f"Bearer {self.config['token']}")
        elif auth_type == "basic" and self.config.get("username"):
            import base64
            creds = base64.b64encode(
                f"{self.config['username']}:{self.config.get('password', '')}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {creds}")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                latency = int((time.monotonic() - t0) * 1000)
                return {"ok": True, "latency_ms": latency, "detail": f"HTTP {resp.status} {url}"}
        except urllib.error.HTTPError as e:
            if e.code < 500:
                latency = int((time.monotonic() - t0) * 1000)
                return {"ok": True, "latency_ms": latency, "detail": f"HTTP {e.code} {url}"}
            raise RuntimeError(f"HTTP {e.code}: {e.reason}")

    def write(self, data: bytes, meta: dict) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise SinkError("VES sink requires httpx (already a TRAM dependency)") from exc

        # Parse data as JSON — expect list of dicts or a single dict
        try:
            payload = json.loads(data)
        except Exception as exc:
            raise SinkError(f"VES sink: failed to parse data as JSON: {exc}") from exc

        records = payload if isinstance(payload, list) else [payload]
        event_list = [self._wrap_event(rec, i) for i, rec in enumerate(records)]
        body = json.dumps({"eventList": event_list}).encode("utf-8")

        kwargs: dict = {
            "content": body,
            "headers": self._build_headers(),
        }
        auth = self._build_auth()
        if auth:
            kwargs["auth"] = auth

        try:
            with httpx.Client() as client:
                resp = client.post(self.url, **kwargs)
        except Exception as exc:
            raise SinkError(f"VES sink request failed: {exc}") from exc

        if resp.status_code not in self.expected_status:
            raise SinkError(
                f"VES sink unexpected status {resp.status_code} from {self.url}: {resp.text[:200]}"
            )

        logger.info(
            "VES sink posted events",
            extra={
                "url": self.url,
                "status": resp.status_code,
                "events": len(records),
            },
        )
