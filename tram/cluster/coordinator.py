"""ClusterCoordinator — pipeline ownership via deterministic consistent hashing."""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tram.cluster.registry import NodeRegistry

logger = logging.getLogger(__name__)


def _stable_hash(name: str) -> int:
    """Deterministic hash for a pipeline name (consistent across processes and restarts)."""
    return int(hashlib.sha1(name.encode()).hexdigest(), 16)


def detect_ordinal(node_id: str) -> int:
    """Extract StatefulSet ordinal from hostname (e.g. ``tram-2`` → ``2``).

    Falls back to 0 for non-StatefulSet deployments.
    """
    parts = node_id.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return 0


class ClusterCoordinator:
    """Decides which pipelines the local node owns.

    Ownership formula::

        sorted_live_nodes = sort(live_nodes by node_id)
        my_position       = index of my node_id in sorted_live_nodes
        owns(pipeline)    = sha1(pipeline_name) % len(live_nodes) == my_position

    Using sorted position (not a static ordinal) means the hash space is always
    fully covered even when a node fails and the cluster has gaps in ordinals.
    """

    def __init__(self, registry: "NodeRegistry", node_id: str) -> None:
        self._registry = registry
        self._node_id = node_id
        self._lock = threading.Lock()
        self._live_nodes: list[dict] = []
        self._my_position: int = 0

    # ── Topology ───────────────────────────────────────────────────────────

    def refresh(self) -> bool:
        """Query live nodes from DB and update the cached topology.

        Returns True if the live node set changed (triggers rebalance).
        """
        nodes = sorted(self._registry.get_live_nodes(), key=lambda n: n["node_id"])
        node_ids = [n["node_id"] for n in nodes]

        with self._lock:
            old_ids = [n["node_id"] for n in self._live_nodes]
            changed = node_ids != old_ids
            self._live_nodes = nodes
            self._my_position = next(
                (i for i, n in enumerate(nodes) if n["node_id"] == self._node_id),
                0,
            )

        if changed:
            logger.info(
                "Cluster topology changed",
                extra={
                    "live_nodes": len(nodes),
                    "my_position": self._my_position,
                    "node_ids": node_ids,
                },
            )
        return changed

    def owns(self, pipeline_name: str) -> bool:
        """Return True if this node is responsible for running the pipeline."""
        with self._lock:
            count = len(self._live_nodes)
            pos = self._my_position
        if count == 0:
            return True  # no peers yet — own everything as a safe fallback
        return _stable_hash(pipeline_name) % count == pos

    def get_state(self) -> dict:
        """Return cluster state dict for the API endpoint."""
        with self._lock:
            return {
                "node_id": self._node_id,
                "my_position": self._my_position,
                "live_node_count": len(self._live_nodes),
                "nodes": list(self._live_nodes),
            }
