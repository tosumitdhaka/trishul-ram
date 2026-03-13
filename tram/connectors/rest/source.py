"""REST source connector — polls an HTTP endpoint and yields the response body."""

from __future__ import annotations

import logging
from typing import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


def _extract_nested(data, path: str):
    """Extract a nested value using dot-notation path (e.g. 'data.items')."""
    for key in path.split("."):
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list) and key.isdigit():
            data = data[int(key)]
        else:
            return None
    return data


@register_source("rest")
class RestSource(BaseSource):
    """Poll an HTTP endpoint and yield the response as raw bytes.

    Batch mode: makes one request (or paginated requests) per run, returns when done.

    Config keys:
        url              (str, required)         Endpoint URL.
        method           (str, default "GET")    HTTP method.
        headers          (dict, optional)        Request headers.
        params           (dict, optional)        URL query parameters.
        body             (str/dict, optional)    Request body (for POST/PUT).
        auth_type        (str, optional)         "basic" | "bearer" | "none"
        username         (str, optional)         Basic auth username.
        password         (str, optional)         Basic auth password.
        token            (str, optional)         Bearer token.
        timeout          (int, default 30)       Request timeout in seconds.
        response_path    (str, optional)         Dot-path to extract from JSON response
                                                 before handing to serializer_in.
                                                 E.g. "data.items" or "result"
        paginate         (bool, default False)   Enable offset-based pagination.
        page_param       (str, default "offset") Pagination query param name.
        page_size        (int, default 100)      Records per page.
        total_path       (str, optional)         Dot-path to total count in response.
        verify_ssl       (bool, default True)    Verify SSL certificates.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url: str = config["url"]
        self.method: str = config.get("method", "GET").upper()
        self.headers: dict = config.get("headers", {})
        self.params: dict = config.get("params", {})
        self.body = config.get("body")
        self.auth_type: str = config.get("auth_type", "none")
        self.username: str | None = config.get("username")
        self.password: str | None = config.get("password")
        self.token: str | None = config.get("token")
        self.timeout: int = int(config.get("timeout", 30))
        self.response_path: str | None = config.get("response_path")
        self.paginate: bool = bool(config.get("paginate", False))
        self.page_param: str = config.get("page_param", "offset")
        self.page_size: int = int(config.get("page_size", 100))
        self.total_path: str | None = config.get("total_path")
        self.verify_ssl: bool = bool(config.get("verify_ssl", True))

    def _build_auth(self):
        if self.auth_type == "basic" and self.username:
            return (self.username, self.password or "")
        return None

    def _build_headers(self) -> dict:
        headers = dict(self.headers)
        if self.auth_type == "bearer" and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _make_request(self, client, params: dict) -> bytes:
        import json as _json
        kwargs = dict(
            headers=self._build_headers(),
            params=params,
            timeout=self.timeout,
        )
        auth = self._build_auth()
        if auth:
            kwargs["auth"] = auth
        if self.body:
            if isinstance(self.body, dict):
                kwargs["json"] = self.body
            else:
                kwargs["content"] = self.body.encode() if isinstance(self.body, str) else self.body

        try:
            resp = client.request(self.method, self.url, **kwargs)
            resp.raise_for_status()
        except Exception as exc:
            raise SourceError(f"REST request failed: {exc}") from exc

        raw = resp.content

        # Extract sub-path if configured
        if self.response_path:
            try:
                data = resp.json()
                extracted = _extract_nested(data, self.response_path)
                if extracted is None:
                    raise SourceError(
                        f"response_path '{self.response_path}' not found in response"
                    )
                raw = _json.dumps(extracted).encode("utf-8")
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError(f"Failed to extract response_path: {exc}") from exc

        return raw

    def read(self) -> Iterator[tuple[bytes, dict]]:
        try:
            import httpx
        except ImportError as exc:
            raise SourceError("REST source requires httpx (already a TRAM dependency)") from exc

        with httpx.Client(verify=self.verify_ssl) as client:
            if not self.paginate:
                raw = self._make_request(client, dict(self.params))
                yield raw, {"source_url": self.url, "source_path": self.url}
                return

            # Paginated mode
            offset = 0
            while True:
                params = {**self.params, self.page_param: offset, "limit": self.page_size}
                raw = self._make_request(client, params)

                import json as _json
                try:
                    page_data = _json.loads(raw)
                except Exception:
                    yield raw, {"source_url": self.url, "page": offset}
                    break

                items = page_data if isinstance(page_data, list) else page_data
                count = len(items) if isinstance(items, list) else 0

                yield raw, {"source_url": self.url, "page": offset}

                if count < self.page_size:
                    break

                offset += self.page_size
                logger.debug(
                    "REST pagination",
                    extra={"url": self.url, "offset": offset},
                )
