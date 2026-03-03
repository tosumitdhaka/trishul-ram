"""Elasticsearch source — reads documents via search + scroll API."""

from __future__ import annotations

import json
import logging
from typing import Generator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("elasticsearch")
class ElasticsearchSource(BaseSource):
    """Read documents from Elasticsearch using search + scroll.

    Config:
        hosts (list[str]): Elasticsearch cluster hosts.
        index (str): Index name or pattern.
        query (dict): Elasticsearch query body. Default: match_all.
        scroll (str): Scroll context timeout. Default "2m".
        batch_size (int): Documents per scroll page. Default 500.
        username / password / api_key / ca_certs / verify_certs: auth/TLS.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.hosts: list[str] = config["hosts"]
        self.index: str = config["index"]
        self.query: dict = config.get("query", {"match_all": {}})
        self.scroll: str = config.get("scroll", "2m")
        self.batch_size: int = config.get("batch_size", 500)
        self.username: str | None = config.get("username")
        self.password: str | None = config.get("password")
        self.api_key: str | None = config.get("api_key")
        self.ca_certs: str | None = config.get("ca_certs")
        self.verify_certs: bool = config.get("verify_certs", True)

    def _build_client(self):
        try:
            from elasticsearch import Elasticsearch
        except ImportError as exc:
            raise SourceError(
                "Elasticsearch source requires elasticsearch — "
                "install with: pip install tram[elasticsearch]"
            ) from exc

        kwargs: dict = {
            "hosts": self.hosts,
            "verify_certs": self.verify_certs,
        }
        if self.ca_certs:
            kwargs["ca_certs"] = self.ca_certs
        if self.api_key:
            kwargs["api_key"] = self.api_key
        elif self.username:
            kwargs["basic_auth"] = (self.username, self.password or "")

        return Elasticsearch(**kwargs)

    def read(self) -> Generator[tuple[bytes, dict], None, None]:
        client = self._build_client()
        try:
            resp = client.search(
                index=self.index,
                body={"query": self.query, "size": self.batch_size},
                scroll=self.scroll,
            )
            scroll_id = resp["_scroll_id"]
            hits = resp["hits"]["hits"]

            while hits:
                records = [hit["_source"] for hit in hits]
                yield json.dumps(records).encode("utf-8"), {"index": self.index}

                resp = client.scroll(scroll_id=scroll_id, scroll=self.scroll)
                scroll_id = resp["_scroll_id"]
                hits = resp["hits"]["hits"]

            # Clear scroll context
            try:
                client.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass
        except Exception as exc:
            raise SourceError(f"Elasticsearch read error: {exc}") from exc
