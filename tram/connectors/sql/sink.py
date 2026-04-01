"""SQL sink connector — inserts/upserts records via SQLAlchemy."""
from __future__ import annotations

import json
import logging

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)

@register_sink("sql")
class SqlSink(BaseSink):
    """Insert or upsert records into a relational database table.

    Config keys:
        connection_url  (str, required)    SQLAlchemy connection URL
        table           (str, required)    Target table name
        mode            (str, default "insert")  "insert" or "upsert"
        upsert_keys     (list[str], default [])   Conflict keys for upsert
    """
    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.connection_url: str = config["connection_url"]
        self.table_name: str = config["table"]
        self.mode: str = config.get("mode", "insert")
        self.upsert_keys: list[str] = config.get("upsert_keys", [])

    def write(self, data: bytes, meta: dict) -> None:
        try:
            from sqlalchemy import MetaData, Table, create_engine
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        except ImportError as exc:
            raise SinkError(
                "SQL sink requires sqlalchemy — install with: pip install tram[sql]"
            ) from exc
        try:
            records = json.loads(data.decode())
        except Exception as exc:
            raise SinkError(f"SQL sink: failed to parse input JSON: {exc}") from exc
        if not records:
            return
        try:
            engine = create_engine(self.connection_url)
        except Exception as exc:
            raise SinkError(f"SQL engine creation failed: {exc}") from exc
        try:
            with engine.connect() as conn:
                meta_obj = MetaData()
                table = Table(self.table_name, meta_obj, autoload_with=engine)
                if self.mode == "upsert" and self.upsert_keys:
                    dialect = engine.dialect.name
                    if dialect == "postgresql":
                        stmt = pg_insert(table).values(records)
                        update_cols = {
                            c.name: c
                            for c in stmt.excluded
                            if c.name not in self.upsert_keys
                        }
                        stmt = stmt.on_conflict_do_update(
                            index_elements=self.upsert_keys,
                            set_=update_cols,
                        )
                    elif dialect == "sqlite":
                        stmt = sqlite_insert(table).values(records)
                        update_cols = {
                            c.name: c
                            for c in stmt.excluded
                            if c.name not in self.upsert_keys
                        }
                        stmt = stmt.on_conflict_do_update(
                            index_elements=self.upsert_keys,
                            set_=update_cols,
                        )
                    else:
                        from sqlalchemy.dialects.mysql import insert as mysql_insert
                        stmt = mysql_insert(table).values(records)
                        stmt = stmt.prefix_with("REPLACE INTO")
                        stmt = table.insert().prefix_with("REPLACE INTO").values(records)
                else:
                    stmt = table.insert().values(records)
                conn.execute(stmt)
                conn.commit()
                logger.info("SQL sink inserted rows", extra={"table": self.table_name, "rows": len(records)})
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"SQL insert failed: {exc}") from exc
        finally:
            engine.dispose()
