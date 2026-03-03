"""Local filesystem sink connector."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from tram.core.exceptions import SinkError
from tram.interfaces.base_sink import BaseSink
from tram.registry.registry import register_sink

logger = logging.getLogger(__name__)


@register_sink("local")
class LocalSink(BaseSink):
    """Write data to a local directory.

    Config keys:
        path               (str, required)   Directory to write to (created if absent).
        filename_template  (str, optional)   Filename template. Tokens: {pipeline},
                                             {timestamp}, {source_filename}.
                                             Default: "{pipeline}_{timestamp}.bin"
        overwrite          (bool, default True)  Overwrite existing files.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.path = Path(config["path"])
        self.filename_template: str = config.get(
            "filename_template", "{pipeline}_{timestamp}.bin"
        )
        self.overwrite: bool = bool(config.get("overwrite", True))

    def _render_filename(self, meta: dict) -> str:
        ts = datetime.now(timezone.utc).isoformat().replace(":", "-")
        return self.filename_template.format(
            pipeline=meta.get("pipeline_name", "tram"),
            timestamp=ts,
            source_filename=meta.get("source_filename", "data"),
        )

    def write(self, data: bytes, meta: dict) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            filename = self._render_filename(meta)
            dest = self.path / filename
            if dest.exists() and not self.overwrite:
                raise SinkError(f"File already exists and overwrite=false: {dest}")
            dest.write_bytes(data)
            logger.info(
                "Wrote file locally",
                extra={"filepath": str(dest), "bytes": len(data)},
            )
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Error writing to {self.path}: {exc}") from exc
