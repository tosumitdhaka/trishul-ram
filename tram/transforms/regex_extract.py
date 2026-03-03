"""Regex extract transform — extracts named groups from a field into the record."""

from __future__ import annotations

import re

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform


@register_transform("regex_extract")
class RegexExtractTransform(BaseTransform):
    """Extract named regex groups from a field and merge them into the record."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        if not config.get("field"):
            raise TransformError("regex_extract: 'field' config key is required")
        if not config.get("pattern"):
            raise TransformError("regex_extract: 'pattern' config key is required")
        self.field: str = config["field"]
        self.pattern: str = config["pattern"]
        self.destination: str | None = config.get("destination", None)
        self.on_no_match: str = config.get("on_no_match", "keep")
        if self.on_no_match not in ("keep", "null", "drop"):
            raise TransformError(
                f"regex_extract: 'on_no_match' must be 'keep', 'null', or 'drop', "
                f"got '{self.on_no_match}'"
            )
        try:
            self._re = re.compile(self.pattern)
        except re.error as exc:
            raise TransformError(
                f"regex_extract: invalid pattern {self.pattern!r}: {exc}"
            ) from exc

    def apply(self, records: list[dict]) -> list[dict]:
        result = []
        group_names = list(self._re.groupindex.keys())
        for record in records:
            val = str(record.get(self.field, ""))
            m = self._re.search(val)
            if m:
                new_record = dict(record)
                groups = m.groupdict()
                if self.destination:
                    new_record[self.destination] = groups
                else:
                    new_record.update(groups)
                result.append(new_record)
            else:
                if self.on_no_match == "keep":
                    result.append(record)
                elif self.on_no_match == "null":
                    new_record = dict(record)
                    if self.destination:
                        new_record[self.destination] = {}
                    else:
                        for name in group_names:
                            new_record[name] = None
                    result.append(new_record)
                # on_no_match == "drop": record is not appended
        return result
