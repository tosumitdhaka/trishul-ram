"""Local filesystem sink connector."""

from __future__ import annotations

import logging
from pathlib import Path

from tram.connectors.config_utils import cfg_bool, cfg_int
from tram.connectors.file_sink_common import (
    LocalRollingBackend,
    RollingWriter,
    source_unit_key,
)
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
                                             {timestamp}, {epoch_m}/{epoch_ms},
                                             {source_filename}.
                                             Default: "{pipeline}_{timestamp}.bin"
        overwrite          (bool, default True)  Overwrite existing files in single mode.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.path = Path(config["path"])
        self.overwrite: bool = cfg_bool(config, "overwrite", True)
        # Shared roll/stage/partition state machine (review E3) — the same
        # logic SFTPSink runs, with a local-filesystem backend.
        self._writer = RollingWriter(
            filename_template=config.get("filename_template", "{pipeline}_{timestamp}.bin"),
            file_mode=str(config.get("file_mode", "append")),
            max_records=config.get("max_records"),
            max_time=config.get("max_time"),
            max_bytes=config.get("max_bytes"),
            max_index=cfg_int(config, "max_index", 99999),
            sink_name="Local",
            logger=logger,
        )
        self._backend = LocalRollingBackend(self.path, overwrite=self.overwrite)
        # Legacy attribute the executor's partition logic reads; reflects the
        # writer's effective template (review E3).
        self.filename_template = self._writer.filename_template

    def write(self, data: bytes, meta: dict) -> None:
        try:
            dest = self._writer.write(data, meta, backend=self._backend, handle=None)
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Error writing to {self.path}: {exc}") from exc
        if dest is not None:
            logger.info(
                "Wrote file locally",
                extra={"filepath": dest, "bytes": len(data)},
            )

    def finalize_source(self, meta: dict, success: bool) -> None:
        try:
            self._writer.finalize_source(
                source_unit_key(meta),
                backend=self._backend,
                handle=None,
                success=success,
            )
        except SinkError:
            raise
        except Exception as exc:
            raise SinkError(f"Error finalizing local sink output: {exc}") from exc