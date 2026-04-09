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

    def __init__(self, registry: NodeRegistry, node_id: str) -> None:
        self._registry = registry
        self._node_id = node_id
        self._lock = threading.Lock()
        self._live_nodes: list[dict] = []
        self._my_position: int = 0
        self._owned: frozenset[str] | None = None  # pre-computed ownership set

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

    def rebalance_ownership(self, all_names: list[str]) -> None:
        """Pre-compute which pipelines this node owns using rank-based assignment.

        Pipelines are sorted by stable hash, then assigned round-robin across
        nodes (rank % count == position).  This guarantees at most 1 pipeline
        difference between any two nodes regardless of name hashes.
        """
        with self._lock:
            count = len(self._live_nodes)
            pos = self._my_position

        if count == 0:
            owned: frozenset[str] = frozenset(all_names)
        else:
            ranked = sorted(all_names, key=_stable_hash)
            owned = frozenset(n for i, n in enumerate(ranked) if i % count == pos)

        with self._lock:
            self._owned = owned

        logger.debug(
            "Ownership recomputed",
            extra={"owned": len(owned), "total": len(all_names), "position": pos, "nodes": count},
        )

    def owns(self, pipeline_name: str) -> bool:
        """Return True if this node is responsible for running the pipeline."""
        with self._lock:
            owned = self._owned
            count = len(self._live_nodes)
            pos = self._my_position

        if owned is not None:
            return pipeline_name in owned

        # Fallback: modulo (before first rebalance_ownership call)
        if count == 0:
            return True  # no peers yet — own everything as a safe fallback
        return _stable_hash(pipeline_name) % count == pos

    def live_node_ids(self) -> list[str]:
        """Return the current list of live node IDs (sorted)."""
        with self._lock:
            return [n["node_id"] for n in self._live_nodes]

    def is_node_alive(self, node_id: str) -> bool:
        """Return True if node_id is currently in the live nodes set."""
        with self._lock:
            return any(n["node_id"] == node_id for n in self._live_nodes)

    def least_loaded_node(self, pipeline_counts: dict[str, int], exclude: str = "") -> str:
        """Return the live node with the fewest owned pipelines.

        Uses node_id as a deterministic tiebreak so all pods independently
        arrive at the same assignment without coordination.

        Args:
            pipeline_counts: {node_id: count} from DB (may omit nodes with 0 pipelines)
            exclude:         node_id to skip (e.g. the dead node being replaced)
        """
        with self._lock:
            live = [n["node_id"] for n in self._live_nodes if n["node_id"] != exclude]

        if not live:
            return self._node_id  # fallback: self

        # Fill zeros for nodes not yet in the counts map
        counts = {node: pipeline_counts.get(node, 0) for node in live}
        # Primary sort: count ASC; secondary: node_id ASC (deterministic tiebreak)
        return min(live, key=lambda n: (counts[n], n))

    def get_state(self, pipeline_names: list[str] | None = None) -> dict:
        """Return cluster state dict for the API endpoint.

        pipeline_names: list of all registered pipeline names — annotates each
        node with its pipelines using the same rank-based formula as owns().
        """
        with self._lock:
            nodes = list(self._live_nodes)
            count = len(nodes)

        # Rank-based assignment — consistent with rebalance_ownership()
        if pipeline_names and count > 0:
            ranked = sorted(pipeline_names, key=_stable_hash)
        else:
            ranked = []

        # Annotate each node with its owned pipelines (computed, not stored in DB)
        enriched = []
        for i, node in enumerate(nodes):
            if ranked and count > 0:
                owned = [n for j, n in enumerate(ranked) if j % count == i]
            else:
                owned = []
            enriched.append({
                **node,
                "pipelines": owned,
                "pipeline_count": len(owned),
            })

        return {
            "node_id": self._node_id,
            "my_position": self._my_position,
            "live_node_count": count,
            "nodes": enriched,
        }
