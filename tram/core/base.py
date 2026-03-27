"""Base mixins for TRAM connector extensions."""

from __future__ import annotations


class ConnectorTestMixin:
    """Mixin for source/sink connectors that support connectivity testing.

    Implement ``test_connection()`` on a source or sink class and it will
    be invoked automatically by ``POST /api/connectors/test``.
    """

    def test_connection(self) -> dict:
        """Probe connectivity to the target system.

        Returns:
            {"ok": bool, "latency_ms": int | None, "detail": str}

        Raise any exception to signal failure — the endpoint catches it
        and returns {"ok": false, "error": str(exc)}.
        """
        raise NotImplementedError
