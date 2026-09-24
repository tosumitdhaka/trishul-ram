"""Error-hygiene helpers for API routers (GH #44).

Client-facing 5xx details must be generic and carry a short correlation id;
the full exception is logged server-side with the same id.
"""

from __future__ import annotations

import logging
import uuid


def new_correlation_id() -> str:
    """Short random id correlating a client-visible error to server logs."""
    return uuid.uuid4().hex[:8]


def internal_error_detail(
    logger: logging.Logger,
    exc: BaseException,
    *,
    message: str = "Internal server error",
) -> str:
    """Log the full exception server-side and return a generic client detail.

    The returned detail embeds the correlation id so an operator can match the
    client-visible error to the server log line.  The exception itself is never
    sent to the client.
    """
    cid = new_correlation_id()
    logger.error("%s (correlation_id=%s): %s", message, cid, exc, exc_info=True)
    return f"{message} (correlation_id={cid})"
