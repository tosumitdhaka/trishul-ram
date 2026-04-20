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
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from tram.models.pipeline import WorkersConfig


@dataclass
class BroadcastResult:
    placement_group_id: str
    accepted: list[str]
    run_ids: list[str]
    rejected: list[str]
    status: str
    slots: list[dict]


class WorkerPool:
    """Manager-side registry for tram-worker agents."""

    def __init__(
        self,
        workers: list[str],
        manager_url: str = "",
        poll_interval: int = 10,
        stats_store=None,
        stats_interval: int = 30,
    ) -> None:
        self._workers = list(workers)
        self._manager_url = manager_url
        self._poll_interval = poll_interval
        self._stats_store = stats_store
        self._stats_interval = stats_interval

        # {url: {"ok": bool, "active_runs": int, "running_pipelines": list[str]}}
        self._health: dict[str, dict] = {
            url: {"ok": True, "active_runs": 0, "running_pipelines": []} for url in workers
        }
        # {run_id: worker_url}
        self._assignments: dict[str, str] = {}
        # {pipeline_name: [worker_url, ...]} — most recent dispatch per pipeline
        self._pipeline_workers: dict[str, list[str]] = {}
        # {worker_id: worker_url}
        self._worker_ids: dict[str, str] = {}
        # {worker_url: worker_id}
        self._url_to_worker_id: dict[str, str] = {}
        # Round-robin counter for tie-breaking equally-loaded workers
        self._rr_counter: int = 0
        self._last_healthy_count: int = -1
        self._lock = threading.Lock()

        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    # ── Discovery ──────────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        manager_url: str = "",
        stats_store=None,
        stats_interval: int = 30,
    ) -> WorkerPool | None:
        """Build a WorkerPool from environment variables.

        Returns None when no workers are configured (standalone / worker mode).
        """
        explicit = os.environ.get("TRAM_WORKER_URLS", "").strip()
        if explicit:
            urls = [u.strip() for u in explicit.split(",") if u.strip()]
            if urls:
                logger.info("WorkerPool: explicit worker list", extra={"workers": urls})
                return cls(
                    workers=urls,
                    manager_url=manager_url,
                    stats_store=stats_store,
                    stats_interval=stats_interval,
                )

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
            return cls(
                workers=urls,
                manager_url=manager_url,
                stats_store=stats_store,
                stats_interval=stats_interval,
            )

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
                    pipelines = list(data.get("running_pipelines", []))
                    worker_id = str(data.get("worker_id", "")).strip()
                    with self._lock:
                        prev_ok = self._health[url]["ok"]
                        self._health[url] = {"ok": ok, "active_runs": active, "running_pipelines": pipelines}
                        if worker_id:
                            prev_worker_id = self._url_to_worker_id.get(url)
                            if prev_worker_id and prev_worker_id != worker_id:
                                self._worker_ids.pop(prev_worker_id, None)
                            self._worker_ids[worker_id] = url
                            self._url_to_worker_id[url] = worker_id
                    if prev_ok and not ok:
                        logger.warning("Worker went down", extra={"worker": url})
                    elif not prev_ok and ok:
                        logger.info("Worker came back up", extra={"worker": url})
                except Exception as exc:
                    with self._lock:
                        was_ok = self._health[url]["ok"]
                        self._health[url]["ok"] = False
                        self._health[url]["running_pipelines"] = []
                    if was_ok:
                        logger.warning(
                            "Worker health probe failed",
                            extra={"worker": url, "error": str(exc)},
                        )

        with self._lock:
            healthy = sum(1 for h in self._health.values() if h["ok"])
        total = len(self._workers)
        if healthy != self._last_healthy_count:
            self._last_healthy_count = healthy
            level = logging.INFO if healthy == total else logging.WARNING
            logger.log(
                level,
                "Worker pool: %d/%d healthy",
                healthy,
                total,
                extra={"healthy": healthy, "total": total},
            )

    # ── Queries ────────────────────────────────────────────────────────────

    def healthy_workers(self) -> list[str]:
        """Return URLs of currently-healthy workers."""
        with self._lock:
            return [url for url, h in self._health.items() if h["ok"]]

    def least_loaded(self) -> str | None:
        """Return a healthy worker URL, using least-loaded + round-robin tiebreaker."""
        with self._lock:
            healthy_urls = [url for url, h in self._health.items() if h["ok"]]
        candidates = [(url, self.load_score(url)) for url in healthy_urls]
        with self._lock:
            if not candidates:
                return None
            min_score = min(score for _, score in candidates)
            min_workers = [url for url, score in candidates if score == min_score]
            # Round-robin among equally loaded workers to spread pipelines evenly
            idx = self._rr_counter % len(min_workers)
            self._rr_counter += 1
        return min_workers[idx]

    def load_score(self, worker_url: str) -> float:
        """Return a sortable load score for a worker."""
        if self._stats_store is not None:
            with self._lock:
                worker_id = self._url_to_worker_id.get(worker_url)
            if worker_id is not None:
                try:
                    stats = self._stats_store.for_worker(worker_id)
                except Exception:
                    stats = []
                if stats:
                    return float(sum(
                        (getattr(s, "bytes_in", 0) + getattr(s, "bytes_out", 0))
                        / max(getattr(s, "uptime_seconds", 0) or self._stats_interval, self._stats_interval)
                        for s in stats
                    ))
        with self._lock:
            return float(self._health.get(worker_url, {}).get("active_runs", 0)) * 1_000_000.0

    def resolve(self, workers_cfg: WorkersConfig) -> list[str]:
        """Return worker URLs selected by the workers config."""
        if workers_cfg.worker_ids is not None:
            resolved: list[str] = []
            with self._lock:
                worker_urls = {wid: self._worker_ids.get(wid) for wid in workers_cfg.worker_ids}
                health = dict(self._health)
            for worker_id in workers_cfg.worker_ids:
                worker_url = worker_urls.get(worker_id)
                if worker_url is not None and health.get(worker_url, {}).get("ok"):
                    resolved.append(worker_url)
            return resolved
        with self._lock:
            healthy_urls = [url for url, h in self._health.items() if h["ok"]]
        candidates = [(url, self.load_score(url)) for url in healthy_urls]
        candidates.sort(key=lambda item: item[1])
        if isinstance(workers_cfg.count, int) and workers_cfg.count > 1:
            return [url for url, _ in candidates[:workers_cfg.count]]
        if workers_cfg.count == "all":
            return [url for url, _ in candidates]
        return [candidates[0][0]] if candidates else []

    def workers_for_pipeline(self, pipeline_name: str) -> list[str]:
        with self._lock:
            return list(self._pipeline_workers.get(pipeline_name, []))

    def worker_id_for_url(self, worker_url: str) -> str | None:
        with self._lock:
            return self._url_to_worker_id.get(worker_url)

    def url_for_worker_id(self, worker_id: str) -> str | None:
        with self._lock:
            return self._worker_ids.get(worker_id)

    def is_worker_healthy(self, worker_url: str) -> bool:
        with self._lock:
            return bool(self._health.get(worker_url, {}).get("ok"))

    def status(self) -> list[dict]:
        """Snapshot of all worker health states (for /api/cluster/nodes)."""
        with self._lock:
            # Build per-worker pipeline assignment list
            worker_pipelines: dict[str, list[str]] = {url: [] for url in self._health}
            for pipeline_name, worker_urls in self._pipeline_workers.items():
                for worker_url in worker_urls:
                    if worker_url in worker_pipelines:
                        worker_pipelines[worker_url].append(pipeline_name)
            return [
                {
                    "url": url,
                    "ok": h["ok"],
                    "active_runs": h["active_runs"],
                    "running_pipelines": h.get("running_pipelines", []),
                    "assigned_pipelines": sorted(worker_pipelines.get(url, [])),
                }
                for url, h in self._health.items()
            ]

    # ── Dispatch ───────────────────────────────────────────────────────────

    def _dispatch_to_worker(
        self,
        worker_url: str,
        run_id: str,
        pipeline_name: str,
        yaml_text: str,
        schedule_type: str,
        callback_url: str = "",
    ) -> bool:
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
            return False

        with self._lock:
            self._assignments[run_id] = worker_url
            self._pipeline_workers.setdefault(pipeline_name, [])
            if worker_url not in self._pipeline_workers[pipeline_name]:
                self._pipeline_workers[pipeline_name].append(worker_url)
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
        return True

    def multi_dispatch(
        self,
        placement_group_id: str,
        pipeline_name: str,
        yaml_text: str,
        workers_cfg: WorkersConfig,
        schedule_type: str,
        callback_url: str = "",
    ) -> BroadcastResult:
        """POST a run to one or more selected workers."""
        worker_urls = self.resolve(workers_cfg)
        target_slots = (
            len(workers_cfg.worker_ids)
            if workers_cfg.worker_ids is not None
            else (len(worker_urls) if workers_cfg.count == "all" else int(workers_cfg.count or 1))
        )
        if not worker_urls and workers_cfg.worker_ids is None:
            logger.error(
                "No healthy workers available for dispatch",
                extra={"pipeline": pipeline_name, "placement_group_id": placement_group_id},
            )
            return BroadcastResult(
                placement_group_id=placement_group_id,
                accepted=[],
                run_ids=[],
                rejected=[],
                status="error",
                slots=[],
            )

        if not callback_url and self._manager_url:
            callback_url = f"{self._manager_url}/api/internal/run-complete"

        accepted: list[str] = []
        run_ids: list[str] = []
        rejected: list[str] = []
        slots: list[dict] = []
        for index in range(target_slots):
            pinned_worker_id = None
            if workers_cfg.worker_ids is not None:
                pinned_worker_id = workers_cfg.worker_ids[index]
                worker_url = self.url_for_worker_id(pinned_worker_id)
                if worker_url is not None and not self.is_worker_healthy(worker_url):
                    worker_url = None
            else:
                worker_url = worker_urls[index] if index < len(worker_urls) else None
            slot_run_id = placement_group_id if target_slots == 1 else f"{placement_group_id}-w{index}"
            current_run_id = None
            slot_status = "stale"
            if worker_url is not None and self._dispatch_to_worker(
                worker_url=worker_url,
                run_id=slot_run_id,
                pipeline_name=pipeline_name,
                yaml_text=yaml_text,
                schedule_type=schedule_type,
                callback_url=callback_url,
            ):
                accepted.append(worker_url)
                run_ids.append(slot_run_id)
                current_run_id = slot_run_id
                slot_status = "running"
            elif worker_url is not None:
                rejected.append(worker_url)

            slots.append({
                "worker_index": index,
                "worker_url": worker_url,
                "worker_id": pinned_worker_id or (self.worker_id_for_url(worker_url) if worker_url else None),
                "pinned_worker_id": pinned_worker_id,
                "run_id_prefix": slot_run_id,
                "current_run_id": current_run_id,
                "status": slot_status,
                "restart_count": 0,
            })

        status = "error"
        if accepted:
            status = "running" if len(accepted) == target_slots and not rejected else "degraded"
        return BroadcastResult(
            placement_group_id=placement_group_id,
            accepted=accepted,
            run_ids=run_ids,
            rejected=rejected,
            status=status,
            slots=slots,
        )

    def dispatch(
        self,
        run_id: str,
        pipeline_name: str,
        yaml_text: str,
        schedule_type: str,
        callback_url: str = "",
    ) -> str | None:
        """POST a run to the least-loaded healthy worker."""
        from tram.models.pipeline import WorkersConfig

        result = self.multi_dispatch(
            placement_group_id=run_id,
            pipeline_name=pipeline_name,
            yaml_text=yaml_text,
            workers_cfg=WorkersConfig(count=1),
            schedule_type=schedule_type,
            callback_url=callback_url,
        )
        return result.accepted[0] if result.accepted else None

    def dispatch_to_worker(
        self,
        worker_url: str,
        run_id: str,
        pipeline_name: str,
        yaml_text: str,
        schedule_type: str,
        callback_url: str = "",
    ) -> bool:
        if not self.is_worker_healthy(worker_url):
            return False
        return self._dispatch_to_worker(
            worker_url=worker_url,
            run_id=run_id,
            pipeline_name=pipeline_name,
            yaml_text=yaml_text,
            schedule_type=schedule_type,
            callback_url=callback_url,
        )

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

        return self._stop_run_on_worker(worker_url, run_id, pipeline_name)

    def _stop_run_on_worker(self, worker_url: str, run_id: str, pipeline_name: str) -> bool:
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

    def stop_pipeline_runs(self, pipeline_name: str) -> list[str]:
        """Stop every active run for a pipeline across all known workers."""
        stopped: list[str] = []
        with self._lock:
            worker_urls = list(self._workers)

        with httpx.Client(timeout=5) as client:
            for worker_url in worker_urls:
                try:
                    resp = client.get(f"{worker_url}/agent/status")
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.warning(
                        "stop_pipeline_runs: status probe failed",
                        extra={"worker": worker_url, "pipeline": pipeline_name, "error": str(exc)},
                    )
                    continue

                active = list(data.get("running", [])) + list(data.get("streams", []))
                run_ids = [
                    str(item.get("run_id", ""))
                    for item in active
                    if item.get("pipeline") == pipeline_name and item.get("run_id")
                ]
                for run_id in run_ids:
                    try:
                        resp = client.post(
                            f"{worker_url}/agent/stop",
                            json={"pipeline_name": pipeline_name, "run_id": run_id},
                        )
                        resp.raise_for_status()
                        stopped.append(run_id)
                    except Exception as exc:
                        logger.warning(
                            "stop_pipeline_runs: stop failed",
                            extra={
                                "worker": worker_url,
                                "pipeline": pipeline_name,
                                "run_id": run_id,
                                "error": str(exc),
                            },
                        )
        return stopped

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
