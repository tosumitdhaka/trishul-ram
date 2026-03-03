"""SQL source connector — queries a relational database via SQLAlchemy."""
from __future__ import annotations
import json
import logging
from typing import Iterator
from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)

@register_source("sql")
class SqlSource(BaseSource):
    """Query a relational database and yield results as JSON bytes.

    Operates in batch mode. If chunk_size > 0, yields multiple items.

    Config keys:
        connection_url  (str, required)   SQLAlchemy connection URL
        query           (str, required)   SQL SELECT query
        params          (dict, default {}) Query bind parameters
        chunk_size      (int, default 0)  Rows per chunk; 0 = all in one item
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.connection_url: str = config["connection_url"]
        self.query: str = config["query"]
        self.params: dict = config.get("params", {})
        self.chunk_size: int = int(config.get("chunk_size", 0))

    def read(self) -> Iterator[tuple[bytes, dict]]:
        try:
            from sqlalchemy import create_engine, text
        except ImportError as exc:
            raise SourceError(
                "SQL source requires sqlalchemy — install with: pip install tram[sql]"
            ) from exc
        try:
            engine = create_engine(self.connection_url)
        except Exception as exc:
            raise SourceError(f"SQL engine creation failed: {exc}") from exc
        try:
            with engine.connect() as conn:
                result = conn.execute(text(self.query), self.params)
                keys = list(result.keys())
                if self.chunk_size > 0:
                    while True:
                        rows = result.fetchmany(self.chunk_size)
                        if not rows:
                            break
                        records = [dict(zip(keys, row)) for row in rows]
                        yield json.dumps(records).encode(), {
                            "source_query": self.query,
                            "row_count": len(records),
                        }
                        logger.debug("SQL source yielded chunk", extra={"rows": len(records)})
                else:
                    rows = result.fetchall()
                    records = [dict(zip(keys, row)) for row in rows]
                    logger.info("SQL source fetched rows", extra={"row_count": len(records)})
                    yield json.dumps(records).encode(), {
                        "source_query": self.query,
                        "row_count": len(records),
                    }
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError(f"SQL query failed: {exc}") from exc
        finally:
            engine.dispose()
