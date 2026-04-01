"""NodeRegistry — registers this node in the shared DB and maintains a heartbeat."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tram.persistence.db import TramDB

logger = logging.getLogger(__name__)


class NodeRegistry:
    """Registers the local node and sends periodic heartbeats to the shared DB.

    Also expires stale peer nodes so the coordinator sees an up-to-date live set.
    """

    def __init__(
        self,
        db: TramDB,
        node_id: str,
        ordinal: int,
        heartbeat_seconds: int = 10,
        ttl_seconds: int = 30,
    ) -> None:
        self._db = db
        self._node_id = node_id
        self._ordinal = ordinal
        self._heartbeat_seconds = heartbeat_seconds
        self._ttl_seconds = ttl_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Register node in DB and start the heartbeat background thread."""
        self._db.register_node(self._node_id, self._ordinal)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="tram-heartbeat",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "NodeRegistry started",
            extra={"node_id": self._node_id, "ordinal": self._ordinal},
        )

    def stop(self) -> None:
        """Stop the heartbeat and mark this node as stopped in the DB."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._heartbeat_seconds + 2)
        try:
            self._db.deregister_node(self._node_id)
        except Exception as exc:
            logger.warning(
                "Could not deregister node",
                extra={"node_id": self._node_id, "error": str(exc)},
            )
        logger.info("NodeRegistry stopped", extra={"node_id": self._node_id})

    # ── Node discovery ─────────────────────────────────────────────────────

    def get_live_nodes(self) -> list[dict]:
        """Return all nodes with a recent heartbeat (within TTL)."""
        return self._db.get_live_nodes(self._ttl_seconds)

    # ── Internal ───────────────────────────────────────────────────────────

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self._heartbeat_seconds):
            try:
                self._db.heartbeat(self._node_id)
                self._db.expire_nodes(self._ttl_seconds)
            except Exception as exc:
                logger.error(
                    "Heartbeat failed",
                    extra={"node_id": self._node_id, "error": str(exc)},
                )
