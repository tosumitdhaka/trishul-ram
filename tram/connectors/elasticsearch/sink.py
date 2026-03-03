"""Elasticsearch sink — writes records via bulk API."""

from __future__ import annotations

import json
import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("elasticsearch")
class ElasticsearchSink(BaseSink):
    """Write documents to Elasticsearch using helpers.bulk().

    Config:
        hosts (list[str]): Elasticsearch cluster hosts.
        index_template (str): Index name (may contain {timestamp}, {pipeline}).
        id_field (str, optional): Document field to use as _id.
        chunk_size (int): Docs per bulk request. Default 500.
        refresh (str): refresh param ("false", "true", "wait_for"). Default "false".
        username / password / api_key / ca_certs: auth/TLS.
        pipeline (str, optional): Ingest pipeline name.
        condition (str, optional): Routing condition expression.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.hosts: list[str] = config["hosts"]
        self.index_template: str = config["index_template"]
        self.id_field: str | None = config.get("id_field")
        self.chunk_size: int = config.get("chunk_size", 500)
        self.refresh: str = config.get("refresh", "false")
        self.username: str | None = config.get("username")
        self.password: str | None = config.get("password")
        self.api_key: str | None = config.get("api_key")
        self.ca_certs: str | None = config.get("ca_certs")
        self.ingest_pipeline: str | None = config.get("pipeline")

    def _build_client(self):
        try:
            from elasticsearch import Elasticsearch
        except ImportError as exc:
            raise SinkError(
                "Elasticsearch sink requires elasticsearch — "
                "install with: pip install tram[elasticsearch]"
            ) from exc

        kwargs: dict = {
            "hosts": self.hosts,
        }
        if self.ca_certs:
            kwargs["ca_certs"] = self.ca_certs
        if self.api_key:
            kwargs["api_key"] = self.api_key
        elif self.username:
            kwargs["basic_auth"] = (self.username, self.password or "")

        return Elasticsearch(**kwargs)

    def write(self, data: bytes, meta: dict) -> None:
        try:
            from elasticsearch import helpers
        except ImportError as exc:
            raise SinkError(
                "Elasticsearch sink requires elasticsearch — "
                "install with: pip install tram[elasticsearch]"
            ) from exc

        from datetime import datetime, timezone

        try:
            records = json.loads(data)
        except Exception as exc:
            raise SinkError(f"Elasticsearch sink: failed to decode JSON: {exc}") from exc

        if not isinstance(records, list):
            records = [records]

        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        pipeline_name = meta.get("pipeline_name", "tram")
        index = self.index_template.format(timestamp=ts, pipeline=pipeline_name)

        actions = []
        for rec in records:
            action: dict = {"_index": index, "_source": rec}
            if self.id_field and self.id_field in rec:
                action["_id"] = rec[self.id_field]
            if self.ingest_pipeline:
                action["pipeline"] = self.ingest_pipeline
            actions.append(action)

        client = self._build_client()
        try:
            helpers.bulk(
                client,
                actions,
                chunk_size=self.chunk_size,
                refresh=self.refresh,
            )
        except Exception as exc:
            raise SinkError(f"Elasticsearch bulk write error: {exc}") from exc
