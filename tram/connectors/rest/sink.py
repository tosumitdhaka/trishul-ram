"""REST sink connector — POSTs serialized data to an HTTP endpoint."""

from __future__ import annotations

import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("rest")
class RestSink(BaseSink):
    """POST/PUT serialized data to an HTTP endpoint.

    Config keys:
        url          (str, required)           Endpoint URL.
        method       (str, default "POST")     HTTP method (POST or PUT).
        headers      (dict, optional)          Request headers.
        content_type (str, default "application/json")  Content-Type header.
        auth_type    (str, optional)           "basic" | "bearer" | "none"
        username     (str, optional)           Basic auth username.
        password     (str, optional)           Basic auth password.
        token        (str, optional)           Bearer token.
        timeout      (int, default 30)         Request timeout in seconds.
        verify_ssl   (bool, default True)      Verify SSL certificates.
        expected_status (list[int], default [200,201,202,204])  Acceptable status codes.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config["url"]
        self.method: str = config.get("method", "POST").upper()
        self.headers: dict = config.get("headers", {})
        self.content_type: str = config.get("content_type", "application/json")
        self.auth_type: str = config.get("auth_type", "none")
        self.username: str | None = config.get("username")
        self.password: str | None = config.get("password")
        self.token: str | None = config.get("token")
        self.api_key: str | None = config.get("api_key")
        self.api_key_header: str = config.get("api_key_header", "X-API-Key")
        self.timeout: int = int(config.get("timeout", 30))
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))
        self.expected_status: list[int] = config.get("expected_status", [200, 201, 202, 204])

    def _build_headers(self) -> dict:
        headers = {**self.headers, "Content-Type": self.content_type}
        if self.auth_type == "bearer" and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.auth_type == "apikey" and self.api_key:
            headers[self.api_key_header] = self.api_key
        return headers

    def _build_auth(self):
        if self.auth_type == "basic" and self.username:
            return (self.username, self.password or "")
        return None

    def write(self, data: bytes, meta: dict) -> None:
        try:
            import httpx
        except ImportError as exc:
            raise SinkError("REST sink requires httpx (already a TRAM dependency)") from exc

        kwargs = dict(
            content=data,
            headers=self._build_headers(),
            timeout=self.timeout,
        )
        auth = self._build_auth()
        if auth:
            kwargs["auth"] = auth

        try:
            with httpx.Client(verify=self.verify_ssl) as client:
                resp = client.request(self.method, self.url, **kwargs)
        except Exception as exc:
            raise SinkError(f"REST sink request failed: {exc}") from exc

        if resp.status_code not in self.expected_status:
            raise SinkError(
                f"REST sink unexpected status {resp.status_code} from {self.url}: {resp.text[:200]}"
            )

        logger.info(
            "REST sink wrote data",
            extra={"url": self.url, "status": resp.status_code, "bytes": len(data)},
        )
