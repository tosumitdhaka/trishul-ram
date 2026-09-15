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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# Dispatch outcome labels — ``dispatch()`` historically collapsed every failure
# into ``None``, conflating "no healthy workers" (a capacity condition) with
# "a healthy worker was selected but the dispatch attempt failed" (an error).
# ``dispatch_with_result()`` labels the real cause so it can reach run history.
DISPATCH_ACCEPTED = "accepted"
DISPATCH_NO_CAPACITY = "no_capacity"
DISPATCH_FAILED = "dispatch_failed"


@dataclass
class DispatchOutcome:
    """Labeled result of a single-run dispatch attempt.

    ``worker_url`` is the accepting worker URL when ``outcome`` is
    ``DISPATCH_ACCEPTED``, otherwise ``None``. ``error`` carries the failure
    detail for ``DISPATCH_FAILED`` outcomes.
    """

    worker_url: str | None
    outcome: str
    error: str | None = None


class WorkerPool:
    """Manager-side registry for tram-worker agents."""

    def __init__(
        self,
        workers: list[str],
        manager_url: str = "",
        poll_interval: int = 10,
        stats_store=None,
        stats_interval: int = 30,
        health_failures_to_down: int = 2,
        on_health_restored: Callable[[], None] | None = None,
    ) -> None:
        self._workers = list(workers)
        self._manager_url = manager_url
        self._poll_interval = poll_interval
        self._stats_store = stats_store
        self._stats_interval = stats_interval
        # Number of consecutive failed health probes before a worker is marked
        # down (health debounce / hysteresis).
        self._health_failures_to_down = max(1, health_failures_to_down)
        # E.2 (§6.5): optional hook fired when a worker transitions down→up in
        # the poll loop. The app wires it to the BatchReconciler's drain nudge
        # event so a restored worker wakes the drain immediately. Public
        # attribute so the app can wire it after construction; absent wiring
        # degrades to pure interval polling (correct, just slower).
        self.on_health_restored = on_health_restored

        # {url: {"ok": bool, "active_runs": int, "running_pipelines": list[str],
        #        "failures": int}}  # "failures" = consecutive failed probes
        self._health: dict[str, dict] = {
            url: {"ok": True, "active_runs": 0, "running_pipelines": [], "failures": 0}
            for url in workers
        }
        # {run_id: worker_url}
        self._assignments: dict[str, str] = {}
        # {run_id: pipeline_name} — lets on_run_complete prune _pipeline_workers
        # once a pipeline's last active run completes (D8: bounded map growth).
        self._run_pipelines: dict[str, str] = {}
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

        # Shared machine key for manager→worker agent calls. Sent as
        # X-API-Key when configured; no header when unset — mirrors the agent
        # server's callback pattern (`server.py`).
        self._api_key = os.environ.get("TRAM_API_KEY", "")

        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    # ── Manager→worker HTTP ────────────────────────────────────────────────

    def _agent_client(self, timeout: float) -> httpx.Client:
        """Return an httpx client preconfigured for agent endpoints.

        Every manager→worker HTTP call (health probes, dispatch, stop, status)
        goes through this helper so the shared machine key is attached as
        ``X-API-Key`` whenever ``TRAM_API_KEY`` is set. Without a key the
        client carries no header, matching the agent server's behavior.
        """
        headers = {"X-API-Key": self._api_key} if self._api_key else None
        return httpx.Client(timeout=timeout, headers=headers)

    # ── Discovery ──────────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        manager_url: str = "",
        stats_store=None,
        stats_interval: int = 30,
        on_health_restored: Callable[[], None] | None = None,
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
                    on_health_restored=on_health_restored,
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
                on_health_restored=on_health_restored,
            )

        return None

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Probe all workers once, then launch background health-poll thread."""
        self._poll_stop.clear()
        self._poll_all(initial_scan=True)  # boot scan: first-probe failures mark workers down
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

    def _probe_health(self, url: str) -> dict:
        """Probe one worker's /agent/health endpoint.

        Runs inside a probe thread during :meth:`_poll_all`. Returns the parsed
        probe payload and raises on transport/HTTP errors so the caller records
        the failure under its lock.
        """
        with self._agent_client(5) as client:
            resp = client.get(f"{url}/agent/health")
            data = resp.json() if resp.status_code == 200 else {}
            return {
                "ok": resp.status_code == 200 and bool(data.get("ok")),
                "active_runs": int(data.get("active_runs", 0)),
                "running_pipelines": list(data.get("running_pipelines", [])),
                "worker_id": str(data.get("worker_id", "")).strip(),
            }

    def _poll_all(self, initial_scan: bool = False) -> None:
        """Probe /agent/health on every configured worker.

        Probes run concurrently (one thread per worker), so a slow or
        unreachable worker no longer stalls the probes of every other worker:
        with the per-probe 5s timeout the serial loop cost N×5s per poll cycle,
        which regularly exceeded the poll interval and delayed health updates.

        A single failed probe does not mark a worker down: the worker is only
        marked unhealthy after ``health_failures_to_down`` consecutive failed
        probes, so a transient blip that recovers on the next poll is ignored.

        During the boot scan (``initial_scan=True``) the debounce is skipped:
        a worker that is unreachable at manager boot is marked down on the
        first probe, otherwise boot-time dispatches would target it for one
        poll interval while it is still reported healthy (startup hysteresis
        window, plan B.6).
        """
        probes: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=len(self._workers) or 1) as executor:
            futures = {executor.submit(self._probe_health, url): url for url in self._workers}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    probes[url] = future.result()
                except Exception as exc:
                    probes[url] = {"ok": False, "worker_id": "", "error": str(exc)}

        for url in self._workers:
            probe = probes[url]
            probe_error = probe.get("error")
            ok = bool(probe["ok"])
            active = int(probe.get("active_runs", 0))
            pipelines = list(probe.get("running_pipelines", []))
            worker_id = str(probe.get("worker_id", "")).strip()

            with self._lock:
                health = self._health[url]
                prev_ok = health["ok"]
                if ok:
                    health.update({
                        "ok": True,
                        "failures": 0,
                        "active_runs": active,
                        "running_pipelines": pipelines,
                    })
                else:
                    if initial_scan:
                        # No grace period at boot: an unreachable worker is
                        # down from the first probe so dispatch decisions
                        # never rely on it.
                        health["failures"] = self._health_failures_to_down
                    else:
                        health["failures"] = health.get("failures", 0) + 1
                    if health["failures"] >= self._health_failures_to_down:
                        health["ok"] = False
                    health["running_pipelines"] = []
                if prev_ok and not health["ok"]:
                    # Healthy → down transition (hysteresis threshold crossed):
                    # the worker's runs will never complete, so their
                    # _assignments/_run_pipelines entries would otherwise leak
                    # forever and keep _pipeline_workers un-pruned (D8 bound).
                    # Reap the bookkeeping only — the placement reconciler owns
                    # actual run recovery.
                    self._reap_assignments_for_down_worker(url)
                consecutive_failures = health["failures"]
                if worker_id:
                    prev_worker_id = self._url_to_worker_id.get(url)
                    if prev_worker_id and prev_worker_id != worker_id:
                        self._worker_ids.pop(prev_worker_id, None)
                    self._worker_ids[worker_id] = url
                    self._url_to_worker_id[url] = worker_id

            if ok and not prev_ok:
                logger.info("Worker came back up", extra={"worker": url})
                if self.on_health_restored is not None:
                    try:
                        self.on_health_restored()
                    except Exception as exc:  # noqa: BLE001 — hook must never break polling
                        logger.warning(
                            "on_health_restored hook failed",
                            extra={"worker": url, "error": str(exc)},
                        )
            elif not ok and prev_ok:
                logger.warning(
                    "Worker health probe failed",
                    extra={
                        "worker": url,
                        "error": probe_error,
                        "consecutive_failures": consecutive_failures,
                    },
                )

        with self._lock:
            healthy = sum(1 for h in self._health.values() if h["ok"])
        total = len(self._workers)
        from tram.metrics.registry import MGR_WORKER_HEALTHY, MGR_WORKER_TOTAL
        MGR_WORKER_HEALTHY.set(healthy)
        MGR_WORKER_TOTAL.set(total)
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
            health = {
                url: {
                    "ok": h["ok"],
                    "active_runs": h.get("active_runs", 0),
                    "running_pipelines": list(h.get("running_pipelines", [])),
                }
                for url, h in self._health.items()
            }
            worker_ids = dict(self._url_to_worker_id)

        rows: list[dict] = []
        probe_urls = [url for url, h in health.items() if h["ok"]]
        live_statuses = self._worker_statuses(probe_urls) if probe_urls else {}
        for url, h in health.items():
            live_status = live_statuses.get(url)
            running_items = []
            stream_items = []
            worker_id = worker_ids.get(url)
            active_runs = int(h.get("active_runs", 0) or 0)
            running_pipelines = sorted(set(h.get("running_pipelines", [])))
            if live_status is not None:
                running_items = list(live_status.get("running", []))
                stream_items = list(live_status.get("streams", []))
                if live_status.get("worker_id"):
                    worker_id = str(live_status["worker_id"])
                active_runs = int(
                    live_status.get("active_runs", len(running_items) + len(stream_items)) or 0
                )
                running_pipelines = sorted({
                    str(item.get("pipeline", ""))
                    for item in running_items + stream_items
                    if item.get("pipeline")
                })
            rows.append({
                "url": url,
                "worker_id": worker_id,
                "ok": h["ok"],
                "active_runs": active_runs,
                "active_streams": len(stream_items),
                "running_pipelines": running_pipelines,
                "running": running_items,
                "streams": stream_items,
                "assigned_pipelines": sorted(worker_pipelines.get(url, [])),
            })
        return rows

    def assignment_for_run(self, run_id: str) -> str | None:
        with self._lock:
            return self._assignments.get(run_id)

    def worker_status(self, worker_url: str) -> dict | None:
        try:
            with self._agent_client(5) as client:
                resp = client.get(f"{worker_url}/agent/status")
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning(
                "worker_status probe failed",
                extra={"worker": worker_url, "error": str(exc)},
            )
            return None

        return {
            "worker_id": data.get("worker_id"),
            "active_runs": int(
                data.get("active_runs", len(data.get("running", [])) + len(data.get("streams", []))) or 0
            ),
            "running_pipelines": list(data.get("running_pipelines", [])),
            "running": list(data.get("running", [])),
            "streams": list(data.get("streams", [])),
        }

    def _worker_statuses(self, worker_urls: list[str]) -> dict[str, dict | None]:
        """Probe /agent/status on several workers concurrently.

        Returns ``{worker_url: status-dict-or-None}`` with the same semantics
        as :meth:`worker_status` (None on probe failure). Used by
        :meth:`status` and :meth:`live_streams` so a single slow worker no
        longer serializes the whole fan-out (previously N workers × 5s timeout
        in the worst case, inside async API handlers).
        """
        results: dict[str, dict | None] = {}
        with ThreadPoolExecutor(max_workers=len(worker_urls) or 1) as executor:
            futures = {executor.submit(self.worker_status, url): url for url in worker_urls}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def live_streams(self) -> list[dict]:
        with self._lock:
            worker_urls = list(self._workers)

        live: list[dict] = []
        statuses = self._worker_statuses(worker_urls)
        for worker_url in worker_urls:
            status = statuses.get(worker_url)
            if status is None:
                continue
            worker_id = status.get("worker_id") or self.worker_id_for_url(worker_url)
            for item in status.get("streams", []):
                entry = {
                    "worker_url": worker_url,
                    "worker_id": worker_id,
                    "pipeline_name": item.get("pipeline"),
                    "run_id": item.get("run_id"),
                    "started_at": item.get("started_at"),
                    "schedule_type": item.get("schedule_type", "stream"),
                    "uptime_seconds": item.get("uptime_seconds", 0.0),
                    "stats": dict(item.get("stats", {})),
                }
                # D.2 §6.1: older agents omit the key entirely; consumers treat
                # a missing key as "unknown" and never act on it.
                if "config_sha256" in item:
                    entry["config_sha256"] = item["config_sha256"]
                live.append(entry)
        return live

    def is_run_active(self, run_id: str, worker_url: str | None = None) -> bool:
        target_worker = worker_url or self.assignment_for_run(run_id)
        if not target_worker:
            return False
        status = self.worker_status(target_worker)
        if status is None:
            return False
        active = list(status.get("running", [])) + list(status.get("streams", []))
        return any(item.get("run_id") == run_id for item in active)

    def find_pipeline_runs(self, pipeline_name: str, *, schedule_type: str = "batch") -> list[dict]:
        matches: list[dict] = []
        with self._lock:
            worker_urls = list(self._workers)

        key = "streams" if schedule_type == "stream" else "running"
        for worker_url in worker_urls:
            status = self.worker_status(worker_url)
            if status is None:
                continue
            for item in status.get(key, []):
                if item.get("pipeline") != pipeline_name or not item.get("run_id"):
                    continue
                entry = {
                    "worker_url": worker_url,
                    "run_id": str(item["run_id"]),
                    "pipeline_name": str(item.get("pipeline", pipeline_name)),
                    "started_at": item.get("started_at"),
                    "schedule_type": schedule_type,
                }
                # D.2 §6.1: same "missing key ⇒ unknown" contract as live_streams().
                if "config_sha256" in item:
                    entry["config_sha256"] = item["config_sha256"]
                matches.append(entry)
        return matches

    def adopt_stream_assignment(self, pipeline_name: str, run_id: str, worker_url: str) -> None:
        """Record a worker-reported live stream run as manager-owned (B.6).

        Used by the controller's boot adopt-or-skip guard: after a manager
        restart a count=1 stream may still be running on a worker. This
        re-registers the run assignment and pipeline mapping so stop_run(),
        on_run_complete() and the status/placement views behave as if the
        manager had dispatched the run — without any dispatch HTTP call.
        Mirrors the assignment bookkeeping in _dispatch_to_worker().
        """
        with self._lock:
            self._assignments[run_id] = worker_url
            self._run_pipelines[run_id] = pipeline_name
            self._pipeline_workers.setdefault(pipeline_name, [])
            if worker_url not in self._pipeline_workers[pipeline_name]:
                self._pipeline_workers[pipeline_name].append(worker_url)
            if worker_url in self._health:
                self._health[worker_url]["active_runs"] += 1

    # ── Dispatch ───────────────────────────────────────────────────────────

    def _dispatch_to_worker(
        self,
        worker_url: str,
        run_id: str,
        pipeline_name: str,
        yaml_text: str,
        schedule_type: str,
        callback_url: str = "",
    ) -> str | None:
        """POST a run to a specific worker.

        Returns ``None`` on success, or the failure detail string when the
        HTTP dispatch attempt raised or returned a non-2xx status.
        """
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
            with self._agent_client(10) as client:
                resp = client.post(f"{worker_url}/agent/run", json=payload)
                resp.raise_for_status()
        except Exception as exc:
            logger.error(
                "Worker dispatch failed",
                extra={"worker": worker_url, "pipeline": pipeline_name, "error": str(exc)},
            )
            return str(exc)

        with self._lock:
            self._assignments[run_id] = worker_url
            self._run_pipelines[run_id] = pipeline_name
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
        return None

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
            dispatch_error: str | None = None
            if worker_url is not None:
                dispatch_error = self._dispatch_to_worker(
                    worker_url=worker_url,
                    run_id=slot_run_id,
                    pipeline_name=pipeline_name,
                    yaml_text=yaml_text,
                    schedule_type=schedule_type,
                    callback_url=callback_url,
                )
                if dispatch_error is None:
                    accepted.append(worker_url)
                    run_ids.append(slot_run_id)
                    current_run_id = slot_run_id
                    slot_status = "running"
                else:
                    rejected.append(worker_url)

            slot = {
                "worker_index": index,
                "worker_url": worker_url,
                "worker_id": pinned_worker_id or (self.worker_id_for_url(worker_url) if worker_url else None),
                "pinned_worker_id": pinned_worker_id,
                "run_id_prefix": slot_run_id,
                "current_run_id": current_run_id,
                "status": slot_status,
                "restart_count": 0,
            }
            if dispatch_error is not None:
                slot["error"] = dispatch_error
            slots.append(slot)

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
        """POST a run to the least-loaded healthy worker.

        Backward-compatible single-dispatch helper: returns the accepting
        worker URL, or ``None`` when the dispatch did not succeed. Callers
        that need the real cause (capacity vs. dispatch failure) should use
        :meth:`dispatch_with_result` instead.
        """
        return self.dispatch_with_result(
            run_id=run_id,
            pipeline_name=pipeline_name,
            yaml_text=yaml_text,
            schedule_type=schedule_type,
            callback_url=callback_url,
        ).worker_url

    def dispatch_with_result(
        self,
        run_id: str,
        pipeline_name: str,
        yaml_text: str,
        schedule_type: str,
        callback_url: str = "",
    ) -> DispatchOutcome:
        """POST a run to the least-loaded healthy worker, labeling the outcome.

        The returned :class:`DispatchOutcome` distinguishes "no healthy
        workers" (``DISPATCH_NO_CAPACITY``, a capacity condition) from "a
        healthy worker was selected but the dispatch attempt failed"
        (``DISPATCH_FAILED``, an error) so the real cause can reach run history.
        """
        from tram.models.pipeline import WorkersConfig

        result = self.multi_dispatch(
            placement_group_id=run_id,
            pipeline_name=pipeline_name,
            yaml_text=yaml_text,
            workers_cfg=WorkersConfig(count=1),
            schedule_type=schedule_type,
            callback_url=callback_url,
        )
        if result.accepted:
            return DispatchOutcome(worker_url=result.accepted[0], outcome=DISPATCH_ACCEPTED)
        if result.rejected:
            return DispatchOutcome(
                worker_url=None,
                outcome=DISPATCH_FAILED,
                error=result.slots[0].get("error") if result.slots else None,
            )
        return DispatchOutcome(
            worker_url=None,
            outcome=DISPATCH_NO_CAPACITY,
            error="No healthy workers available for dispatch",
        )

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
        ) is None

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
            with self._agent_client(5) as client:
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

        with self._agent_client(5) as client:
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
            pipeline_name = self._run_pipelines.pop(run_id, None)
            if worker_url and worker_url in self._health:
                self._health[worker_url]["active_runs"] = max(
                    0, self._health[worker_url]["active_runs"] - 1
                )
            if pipeline_name is not None and pipeline_name not in self._run_pipelines.values():
                # D8: the pipeline's last active run completed, so no placement
                # slot (or batch run) references it anymore — drop the worker
                # list to keep _pipeline_workers bounded. Referenced entries
                # (other active runs for the same pipeline) are left untouched.
                self._pipeline_workers.pop(pipeline_name, None)

    def _reap_assignments_for_down_worker(self, worker_url: str) -> None:
        """Drop run bookkeeping for a worker that just transitioned to down.

        A dead worker's runs never complete, so their ``_assignments`` and
        ``_run_pipelines`` entries would otherwise leak forever and keep
        ``_pipeline_workers`` un-pruned (D8 bound) — the stale entry prevents
        on_run_complete-style pruning from ever firing for that run. Reaps the
        down worker's entries and removes it from shared pipeline worker lists,
        then prunes pipelines no longer referenced by any active run. Bookkeeping
        only: no dispatch, stop, or placement writes — the placement reconciler
        owns actual run recovery. Caller holds the lock.
        """
        reaped: list[str] = [
            run_id
            for run_id, url in self._assignments.items()
            if url == worker_url
        ]
        for run_id in reaped:
            self._assignments.pop(run_id, None)
            pipeline_name = self._run_pipelines.pop(run_id, None)
            if pipeline_name is None:
                continue
            workers = self._pipeline_workers.get(pipeline_name)
            if workers is not None and worker_url in workers:
                workers.remove(worker_url)
            if pipeline_name not in self._run_pipelines.values():
                # Mirrors on_run_complete's D8 prune: no active run references
                # the pipeline anymore.
                self._pipeline_workers.pop(pipeline_name, None)
        if reaped:
            health = self._health.get(worker_url)
            if health is not None:
                health["active_runs"] = max(0, health["active_runs"] - len(reaped))
            logger.warning(
                "Reaped run bookkeeping for down worker",
                extra={"worker": worker_url, "run_ids": reaped},
            )
