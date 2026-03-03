"""Logging setup — JSON-structured logs for container environments."""

from __future__ import annotations

import logging
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime, timezone

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Merge extra fields attached via logger.info("msg", extra={...})
        for key, val in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            }:
                payload[key] = val
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure root logging. Call once at startup."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )

    root.handlers.clear()
    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
