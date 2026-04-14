"""WorkerPool — manager-side tracker for tram-worker agents.

Responsibilities:
  - Maintain a health map for all configured worker URLs
  - Poll /agent/health every poll_interval seconds
  - Dispatch runs to the least-loaded healthy worker
  - Route /agent/stop calls to the worker that owns a run
  - Decrement active-run counters when the manager receives a run-complete callback

Worker discovery modes (evaluated in order):
  1. Explicit list:   TRAM_WORKERS=http://w0:8766,http://w1:8766
  2. K8s headless DNS: TRAM_WORKER_REPLICAS=3
                       TRAM_WORKER_SERVICE=tram-worker   (default)
                       TRAM_WORKER_NAMESPACE=default     (default)
                       TRAM_WORKER_PORT=8766             (default)
"""

from __future__ import annotations

import logging
import os
import threading

import httpx

logger = logging.getLogger(__name__)


class WorkerPool:
    """Manager-side registry for tram-worker agents."""

    def __init__(
        self,
        workers: list[str],
        manager_url: str = "",
        poll_interval: int = 10,
    ) -> None:
        self._workers = list(workers)
        self._manager_url = manager_url
        self._poll_interval = poll_interval

        # {url: {"ok": bool, "active_runs": int}}
        self._health: dict[str, dict] = {
            url: {"ok": True, "active_runs": 0} for url in workers
        }
        # {run_id: worker_url}
        self._assignments: dict[str, str] = {}
        self._lock = threading.Lock()

        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    # ── Discovery ──────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, manager_url: str = "") -> WorkerPool | None:
        """Build a WorkerPool from environment variables.

        Returns None when no workers are configured (standalone / worker mode).
        """
        explicit = os.environ.get("TRAM_WORKER_URLS", "").strip()
        if explicit:
            urls = [u.strip() for u in explicit.split(",") if u.strip()]
            if urls:
                logger.info("WorkerPool: explicit worker list", extra={"workers": urls})
                return cls(workers=urls, manager_url=manager_url)

        replicas = int(os.environ.get("TRAM_WORKER_REPLICAS", "0"))
        if replicas > 0:
            service = os.environ.get("TRAM_WORKER_SERVICE", "tram-worker")
            namespace = os.environ.get("TRAM_WORKER_NAMESPACE", "default")
            port = int(os.environ.get("TRAM_WORKER_PORT", "8766"))
            urls = [
                f"http://{service}-{i}.{service}.{namespace}.svc.cluster.local:{port}"
                for i in range(replicas)
            ]
            logger.info(
                "WorkerPool: K8s headless DNS workers",
                extra={"service": service, "replicas": replicas, "namespace": namespace},
            )
            return cls(workers=urls, manager_url=manager_url)

        return None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Probe all workers once, then launch background health-poll thread."""
        self._poll_stop.clear()
        self._poll_all()  # initial probe so manager starts with accurate state
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="tram-worker-health",
            daemon=True,
        )
        self._poll_thread.start()
        logger.info(
            "WorkerPool started",
            extra={"workers": len(self._workers), "poll_interval": self._poll_interval},
        )

    def stop(self) -> None:
        """Stop the background health-poll thread."""
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=self._poll_interval + 2)
        logger.info("WorkerPool stopped")

    # ── Health polling ─────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._poll_stop.wait(self._poll_interval):
            self._poll_all()

    def _poll_all(self) -> None:
        """Probe /agent/health on every configured worker."""
        with httpx.Client(timeout=5) as client:
            for url in self._workers:
                try:
                    resp = client.get(f"{url}/agent/health")
                    data = resp.json() if resp.status_code == 200 else {}
                    ok = resp.status_code == 200 and bool(data.get("ok"))
                    active = int(data.get("active_runs", 0))
                    with self._lock:
                        prev_ok = self._health[url]["ok"]
                        self._health[url] = {"ok": ok, "active_runs": active}
                    if prev_ok and not ok:
                        logger.warning("Worker went down", extra={"worker": url})
                    elif not prev_ok and ok:
                        logger.info("Worker came back up", extra={"worker": url})
                except Exception as exc:
                    with self._lock:
                        was_ok = self._health[url]["ok"]
                        self._health[url]["ok"] = False
                    if was_ok:
                        logger.warning(
                            "Worker health probe failed",
                            extra={"worker": url, "error": str(exc)},
                        )

    # ── Queries ────────────────────────────────────────────────────────────

    def healthy_workers(self) -> list[str]:
        """Return URLs of currently-healthy workers."""
        with self._lock:
            return [url for url, h in self._health.items() if h["ok"]]

    def least_loaded(self) -> str | None:
        """Return the healthy worker URL with the fewest active runs."""
        with self._lock:
            candidates = [
                (url, h["active_runs"])
                for url, h in self._health.items()
                if h["ok"]
            ]
        if not candidates:
            return None
        return min(candidates, key=lambda x: x[1])[0]

    def status(self) -> list[dict]:
        """Snapshot of all worker health states (for /api/cluster/nodes)."""
        with self._lock:
            return [
                {"url": url, "ok": h["ok"], "active_runs": h["active_runs"]}
                for url, h in self._health.items()
            ]

    # ── Dispatch ───────────────────────────────────────────────────────────

    def dispatch(
        self,
        run_id: str,
        pipeline_name: str,
        yaml_text: str,
        schedule_type: str,
        callback_url: str = "",
    ) -> str | None:
        """POST a run to the least-loaded healthy worker.

        Returns the worker URL on success, None if no healthy workers are available
        or the HTTP call fails.
        """
        worker_url = self.least_loaded()
        if worker_url is None:
            logger.error(
                "No healthy workers available for dispatch",
                extra={"pipeline": pipeline_name, "run_id": run_id},
            )
            return None

        if not callback_url and self._manager_url:
            callback_url = f"{self._manager_url}/api/internal/run-complete"

        payload = {
            "pipeline_name": pipeline_name,
            "yaml_text": yaml_text,
            "run_id": run_id,
            "schedule_type": schedule_type,
            "callback_url": callback_url,
        }
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(f"{worker_url}/agent/run", json=payload)
                resp.raise_for_status()
        except Exception as exc:
            logger.error(
                "Worker dispatch failed",
                extra={"worker": worker_url, "pipeline": pipeline_name, "error": str(exc)},
            )
            return None

        with self._lock:
            self._assignments[run_id] = worker_url
            if worker_url in self._health:
                self._health[worker_url]["active_runs"] += 1

        logger.info(
            "Dispatched run to worker",
            extra={
                "pipeline": pipeline_name,
                "run_id": run_id,
                "worker": worker_url,
                "schedule_type": schedule_type,
            },
        )
        return worker_url

    def stop_run(self, run_id: str, pipeline_name: str) -> bool:
        """Send a stop signal to whichever worker owns run_id.

        Returns True if the HTTP call succeeded, False otherwise.
        """
        with self._lock:
            worker_url = self._assignments.get(run_id)
        if not worker_url:
            logger.debug(
                "stop_run: no worker assignment found",
                extra={"run_id": run_id, "pipeline": pipeline_name},
            )
            return False

        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{worker_url}/agent/stop",
                    json={"pipeline_name": pipeline_name, "run_id": run_id},
                )
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning(
                "stop_run failed",
                extra={"worker": worker_url, "run_id": run_id, "error": str(exc)},
            )
            return False

    def on_run_complete(self, run_id: str) -> None:
        """Called when the manager receives a run-complete callback.

        Removes the run assignment and decrements the worker's active-run counter.
        """
        with self._lock:
            worker_url = self._assignments.pop(run_id, None)
            if worker_url and worker_url in self._health:
                self._health[worker_url]["active_runs"] = max(
                    0, self._health[worker_url]["active_runs"] - 1
                )
