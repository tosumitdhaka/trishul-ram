"""Helpers for broadcast placement and active stream API views."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _rate(total: int, uptime_seconds: float, stale: bool) -> float:
    if stale or uptime_seconds <= 0:
        return 0.0
    return float(total) / uptime_seconds


def _empty_stats_view(*, stale: bool = True) -> dict[str, Any]:
    return {
        "schedule_type": None,
        "timestamp": None,
        "uptime_seconds": 0.0,
        "records_in": 0,
        "records_out": 0,
        "records_skipped": 0,
        "dlq_count": 0,
        "error_count": 0,
        "bytes_in": 0,
        "bytes_out": 0,
        "errors_last_window": [],
        "stale": stale,
        "records_in_per_sec": 0.0,
        "records_out_per_sec": 0.0,
        "bytes_in_per_sec": 0.0,
        "bytes_out_per_sec": 0.0,
    }


def _stats_view_from_entry(entry, stale: bool) -> dict[str, Any]:
    uptime_seconds = float(getattr(entry, "uptime_seconds", 0.0) or 0.0)
    records_in = int(getattr(entry, "records_in", 0) or 0)
    records_out = int(getattr(entry, "records_out", 0) or 0)
    records_skipped = int(getattr(entry, "records_skipped", 0) or 0)
    dlq_count = int(getattr(entry, "dlq_count", 0) or 0)
    error_count = int(getattr(entry, "error_count", 0) or 0)
    bytes_in = int(getattr(entry, "bytes_in", 0) or 0)
    bytes_out = int(getattr(entry, "bytes_out", 0) or 0)
    return {
        "schedule_type": getattr(entry, "schedule_type", None),
        "timestamp": getattr(entry, "timestamp", None),
        "uptime_seconds": uptime_seconds,
        "records_in": records_in,
        "records_out": records_out,
        "records_skipped": records_skipped,
        "dlq_count": dlq_count,
        "error_count": error_count,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "errors_last_window": list(getattr(entry, "errors_last_window", []) or []),
        "stale": stale,
        "records_in_per_sec": _rate(records_in, uptime_seconds, stale),
        "records_out_per_sec": _rate(records_out, uptime_seconds, stale),
        "bytes_in_per_sec": _rate(bytes_in, uptime_seconds, stale),
        "bytes_out_per_sec": _rate(bytes_out, uptime_seconds, stale),
    }


def build_stats_view(stats_store, run_id: str | None) -> dict[str, Any]:
    if stats_store is None or not run_id:
        return _empty_stats_view(stale=True)

    entry = stats_store.get_by_run_id(run_id)
    if entry is None:
        return _empty_stats_view(stale=True)
    return _stats_view_from_entry(entry, stale=stats_store.is_stale(entry))


def _stats_view_from_live_item(item: dict[str, Any]) -> dict[str, Any]:
    stats = item.get("stats", {}) or {}
    uptime_seconds = float(item.get("uptime_seconds", stats.get("uptime_seconds", 0.0)) or 0.0)
    records_in = int(stats.get("records_in", item.get("records_in", 0)) or 0)
    records_out = int(stats.get("records_out", item.get("records_out", 0)) or 0)
    records_skipped = int(stats.get("records_skipped", item.get("records_skipped", 0)) or 0)
    dlq_count = int(stats.get("dlq_count", item.get("dlq_count", 0)) or 0)
    error_count = int(stats.get("error_count", item.get("error_count", 0)) or 0)
    bytes_in = int(stats.get("bytes_in", item.get("bytes_in", 0)) or 0)
    bytes_out = int(stats.get("bytes_out", item.get("bytes_out", 0)) or 0)
    return {
        "schedule_type": item.get("schedule_type", "stream"),
        "timestamp": item.get("timestamp"),
        "uptime_seconds": uptime_seconds,
        "records_in": records_in,
        "records_out": records_out,
        "records_skipped": records_skipped,
        "dlq_count": dlq_count,
        "error_count": error_count,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "errors_last_window": list(stats.get("errors_last_window", item.get("errors_last_window", [])) or []),
        "stale": False,
        "records_in_per_sec": _rate(records_in, uptime_seconds, False),
        "records_out_per_sec": _rate(records_out, uptime_seconds, False),
        "bytes_in_per_sec": _rate(bytes_in, uptime_seconds, False),
        "bytes_out_per_sec": _rate(bytes_out, uptime_seconds, False),
    }


def build_slot_view(slot: dict[str, Any], stats_store) -> dict[str, Any]:
    return {
        "worker_index": slot.get("worker_index"),
        "worker_id": slot.get("worker_id"),
        "worker_url": slot.get("worker_url"),
        "run_id_prefix": slot.get("run_id_prefix"),
        "current_run_id": slot.get("current_run_id"),
        "status": slot.get("status"),
        "restart_count": int(slot.get("restart_count", 0) or 0),
        "stats": build_stats_view(stats_store, slot.get("current_run_id")),
    }


def _build_stream_summary(
    *,
    pipeline_name: str,
    placement_group_id: str | None,
    status: str,
    target_count: Any,
    started_at: Any,
    slots: list[dict[str, Any]],
) -> dict[str, Any]:
    active_slots = [slot for slot in slots if not slot["stats"]["stale"]]
    return {
        "pipeline_name": pipeline_name,
        "placement_group_id": placement_group_id,
        "status": status,
        "target_count": target_count,
        "started_at": started_at,
        "slot_count": len(slots),
        "active_slots": len(active_slots),
        "records_in": sum(slot["stats"]["records_in"] for slot in active_slots),
        "records_out": sum(slot["stats"]["records_out"] for slot in active_slots),
        "records_skipped": sum(slot["stats"]["records_skipped"] for slot in active_slots),
        "dlq_count": sum(slot["stats"]["dlq_count"] for slot in active_slots),
        "error_count": sum(slot["stats"]["error_count"] for slot in active_slots),
        "bytes_in": sum(slot["stats"]["bytes_in"] for slot in active_slots),
        "bytes_out": sum(slot["stats"]["bytes_out"] for slot in active_slots),
        "records_in_per_sec": sum(slot["stats"]["records_in_per_sec"] for slot in active_slots),
        "records_out_per_sec": sum(slot["stats"]["records_out_per_sec"] for slot in active_slots),
        "bytes_in_per_sec": sum(slot["stats"]["bytes_in_per_sec"] for slot in active_slots),
        "bytes_out_per_sec": sum(slot["stats"]["bytes_out_per_sec"] for slot in active_slots),
        "slots": slots,
    }


def _live_slot_view(index: int, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "worker_index": item.get("worker_index", index),
        "worker_id": item.get("worker_id"),
        "worker_url": item.get("worker_url"),
        "run_id_prefix": item.get("run_id"),
        "current_run_id": item.get("run_id"),
        "status": item.get("status", "running"),
        "restart_count": int(item.get("restart_count", 0) or 0),
        "stats": _stats_view_from_live_item(item),
    }


def _apply_live_slot(slot: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    updated = dict(slot)
    updated["worker_id"] = item.get("worker_id") or updated.get("worker_id")
    updated["worker_url"] = item.get("worker_url") or updated.get("worker_url")
    updated["run_id_prefix"] = updated.get("run_id_prefix") or item.get("run_id")
    updated["current_run_id"] = item.get("run_id") or updated.get("current_run_id")
    updated["status"] = item.get("status", updated.get("status", "running"))
    updated["stats"] = _stats_view_from_live_item(item)
    return updated


def build_placement_view(
    placement: dict[str, Any],
    stats_store,
    live_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    slots = [build_slot_view(slot, stats_store) for slot in placement.get("slots", [])]
    live_items = list(live_items or [])
    used_run_ids: set[str] = set()
    by_run_id = {
        str(item.get("run_id")): item
        for item in live_items
        if item.get("run_id")
    }
    by_worker_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in live_items:
        worker_key = item.get("worker_id") or item.get("worker_url")
        if worker_key:
            by_worker_key[(placement["pipeline_name"], str(worker_key))] = item

    for index, slot in enumerate(slots):
        item = None
        current_run_id = slot.get("current_run_id")
        if current_run_id:
            item = by_run_id.get(str(current_run_id))
        if item is None:
            worker_key = slot.get("worker_id") or slot.get("worker_url")
            if worker_key:
                item = by_worker_key.get((placement["pipeline_name"], str(worker_key)))
        if item is not None:
            slots[index] = _apply_live_slot(slot, item)
            if item.get("run_id"):
                used_run_ids.add(str(item["run_id"]))

    for item in live_items:
        run_id = str(item.get("run_id", "") or "")
        if run_id and run_id in used_run_ids:
            continue
        slots.append(_live_slot_view(len(slots), item))
        if run_id:
            used_run_ids.add(run_id)

    return _build_stream_summary(
        pipeline_name=placement["pipeline_name"],
        placement_group_id=placement["placement_group_id"],
        status=placement["status"],
        target_count=placement.get("target_count"),
        started_at=placement.get("started_at"),
        slots=slots,
    )


def build_cluster_streams(
    placements: list[dict[str, Any]],
    stats_store,
    live_streams: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    live_streams = list(live_streams or [])
    live_by_pipeline: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in live_streams:
        pipeline_name = str(item.get("pipeline_name", "") or "")
        if pipeline_name:
            live_by_pipeline[pipeline_name].append(item)

    streams = [
        build_placement_view(placement, stats_store, live_by_pipeline.pop(placement["pipeline_name"], []))
        for placement in placements
    ]
    placement_pipelines = {placement["pipeline_name"] for placement in placements}

    if stats_store is not None:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for entry in stats_store.all_active():
            if entry.pipeline_name not in placement_pipelines:
                grouped[entry.pipeline_name].append(entry)
    else:
        grouped = defaultdict(list)

    for pipeline_name, live_items in live_by_pipeline.items():
        for item in live_items:
            grouped[pipeline_name].append(item)

    for pipeline_name, entries in grouped.items():
        slots = []
        seen_run_ids: set[str] = set()
        live_entries = [entry for entry in entries if isinstance(entry, dict)]
        stats_entries = [entry for entry in entries if not isinstance(entry, dict)]
        for entry in live_entries:
            if isinstance(entry, dict):
                run_id = str(entry.get("run_id", "") or "")
                if run_id and run_id in seen_run_ids:
                    continue
                slots.append(_live_slot_view(len(slots), entry))
            else:
                run_id = ""
            if run_id:
                seen_run_ids.add(run_id)
        for entry in stats_entries:
            run_id = str(getattr(entry, "run_id", "") or "")
            if run_id and run_id in seen_run_ids:
                continue
            slots.append({
                "worker_index": len(slots),
                "worker_id": getattr(entry, "worker_id", None),
                "worker_url": None,
                "run_id_prefix": getattr(entry, "run_id", None),
                "current_run_id": getattr(entry, "run_id", None),
                "status": "running",
                "restart_count": 0,
                "stats": _stats_view_from_entry(entry, stale=False),
            })
            if run_id:
                seen_run_ids.add(run_id)

        # When both stats_store and live worker status are present for the same
        # non-placement stream, prefer the live slot snapshot.
        if stats_store is not None:
            for entry in stats_store.all_active():
                if entry.pipeline_name != pipeline_name or entry.pipeline_name in placement_pipelines:
                    continue
                run_id = str(getattr(entry, "run_id", "") or "")
                if run_id and run_id in seen_run_ids:
                    continue
                slots.append({
                    "worker_index": len(slots),
                    "worker_id": getattr(entry, "worker_id", None),
                    "worker_url": None,
                    "run_id_prefix": getattr(entry, "run_id", None),
                    "current_run_id": getattr(entry, "run_id", None),
                    "status": "running",
                    "restart_count": 0,
                    "stats": _stats_view_from_entry(entry, stale=False),
                })
                if run_id:
                    seen_run_ids.add(run_id)

        streams.append(_build_stream_summary(
            pipeline_name=pipeline_name,
            placement_group_id=None,
            status="running",
            target_count=len(slots),
            started_at=None,
            slots=slots,
        ))

    streams.sort(key=lambda item: item["pipeline_name"])
    return streams
