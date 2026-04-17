"""Helpers for broadcast placement and active stream API views."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _rate(total: int, uptime_seconds: float, stale: bool) -> float:
    if stale or uptime_seconds <= 0:
        return 0.0
    return float(total) / uptime_seconds


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
            "stale": True,
            "records_in_per_sec": 0.0,
            "records_out_per_sec": 0.0,
            "bytes_in_per_sec": 0.0,
            "bytes_out_per_sec": 0.0,
        }

    entry = stats_store.get_by_run_id(run_id)
    if entry is None:
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
            "stale": True,
            "records_in_per_sec": 0.0,
            "records_out_per_sec": 0.0,
            "bytes_in_per_sec": 0.0,
            "bytes_out_per_sec": 0.0,
        }
    return _stats_view_from_entry(entry, stale=stats_store.is_stale(entry))


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


def build_placement_view(placement: dict[str, Any], stats_store) -> dict[str, Any]:
    slots = [build_slot_view(slot, stats_store) for slot in placement.get("slots", [])]
    active_slots = [slot for slot in slots if not slot["stats"]["stale"]]
    return {
        "pipeline_name": placement["pipeline_name"],
        "placement_group_id": placement["placement_group_id"],
        "status": placement["status"],
        "target_count": placement.get("target_count"),
        "started_at": placement.get("started_at"),
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


def build_cluster_streams(placements: list[dict[str, Any]], stats_store) -> list[dict[str, Any]]:
    streams = [build_placement_view(placement, stats_store) for placement in placements]
    placement_pipelines = {placement["pipeline_name"] for placement in placements}

    if stats_store is not None:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for entry in stats_store.all_active():
            if entry.pipeline_name not in placement_pipelines:
                grouped[entry.pipeline_name].append(entry)

        for pipeline_name, entries in grouped.items():
            slots = []
            for index, entry in enumerate(entries):
                stats = _stats_view_from_entry(entry, stale=False)
                slots.append({
                    "worker_index": index,
                    "worker_id": getattr(entry, "worker_id", None),
                    "worker_url": None,
                    "run_id_prefix": getattr(entry, "run_id", None),
                    "current_run_id": getattr(entry, "run_id", None),
                    "status": "running",
                    "restart_count": 0,
                    "stats": stats,
                })
            streams.append({
                "pipeline_name": pipeline_name,
                "placement_group_id": None,
                "status": "running",
                "target_count": len(slots),
                "started_at": None,
                "slot_count": len(slots),
                "active_slots": len(slots),
                "records_in": sum(slot["stats"]["records_in"] for slot in slots),
                "records_out": sum(slot["stats"]["records_out"] for slot in slots),
                "records_skipped": sum(slot["stats"]["records_skipped"] for slot in slots),
                "dlq_count": sum(slot["stats"]["dlq_count"] for slot in slots),
                "error_count": sum(slot["stats"]["error_count"] for slot in slots),
                "bytes_in": sum(slot["stats"]["bytes_in"] for slot in slots),
                "bytes_out": sum(slot["stats"]["bytes_out"] for slot in slots),
                "records_in_per_sec": sum(slot["stats"]["records_in_per_sec"] for slot in slots),
                "records_out_per_sec": sum(slot["stats"]["records_out_per_sec"] for slot in slots),
                "bytes_in_per_sec": sum(slot["stats"]["bytes_in_per_sec"] for slot in slots),
                "bytes_out_per_sec": sum(slot["stats"]["bytes_out_per_sec"] for slot in slots),
                "slots": slots,
            })

    streams.sort(key=lambda item: item["pipeline_name"])
    return streams
