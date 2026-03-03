"""Enrich transform — left-join records with a static lookup file."""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform

logger = logging.getLogger(__name__)


def _load_lookup(path: str, fmt: str, lookup_key: str) -> dict[str, dict]:
    """Load lookup file into a dict keyed by lookup_key value."""
    p = Path(path)
    if not p.exists():
        raise TransformError(f"enrich: lookup file not found: {path}")

    raw = p.read_text(encoding="utf-8")

    if fmt == "json":
        data = json.loads(raw)
        if not isinstance(data, list):
            raise TransformError("enrich: JSON lookup file must contain a list of objects")
        rows = data
    elif fmt == "csv":
        reader = csv.DictReader(io.StringIO(raw))
        rows = [dict(r) for r in reader]
    else:
        raise TransformError(f"enrich: unsupported lookup_format '{fmt}'. Use 'csv' or 'json'")

    table: dict[str, dict] = {}
    for row in rows:
        key_val = row.get(lookup_key)
        if key_val is not None:
            table[str(key_val)] = row

    logger.debug(
        "Loaded lookup file",
        extra={"filepath": path, "rows": len(table), "key": lookup_key},
    )
    return table


@register_transform("enrich")
class EnrichTransform(BaseTransform):
    """Left-join each record with a static lookup file loaded once at init.

    Lookup file is read from disk at pipeline startup (not on each apply call).

    Config keys:
        lookup_file    (str, required)        Path to lookup file.
        lookup_format  (str, default "csv")   "csv" or "json".
        join_key       (str, required)        Field in records to join on.
        lookup_key     (str, optional)        Field in lookup file (defaults to join_key).
        add_fields     (list[str], optional)  Fields to copy from lookup row.
                                              Copies all fields if omitted.
        prefix         (str, default "")      Prefix added to copied field names.
        on_miss        (str, default "keep")  "keep" | "null_fields" — behaviour when
                                              no lookup row matches.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.join_key: str = config["join_key"]
        self.lookup_key: str = config.get("lookup_key", self.join_key)
        self.add_fields: list[str] | None = config.get("add_fields")
        self.prefix: str = config.get("prefix", "")
        self.on_miss: str = config.get("on_miss", "keep")
        fmt: str = config.get("lookup_format", "csv")
        self._table: dict[str, dict] = _load_lookup(
            config["lookup_file"], fmt, self.lookup_key
        )

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        for record in records:
            new_record = dict(record)
            join_val = record.get(self.join_key)
            lookup_row = self._table.get(str(join_val)) if join_val is not None else None

            if lookup_row is None:
                if self.on_miss == "null_fields" and self.add_fields:
                    for f in self.add_fields:
                        new_record[f"{self.prefix}{f}"] = None
            else:
                fields_to_copy = self.add_fields or [
                    k for k in lookup_row if k != self.lookup_key
                ]
                for f in fields_to_copy:
                    if f in lookup_row:
                        new_record[f"{self.prefix}{f}"] = lookup_row[f]

            result.append(new_record)
        return result
