"""window_aggregate transform — tumbling epoch-aligned UTC window aggregation.

Time-windowed counterpart to the batch-local ``aggregate`` transform: it
accumulates op-relevant running values per (group, window) in the pipeline's
durable state blob (design F.1 §5), so a 15-minute telecom PM period spanning
multiple polls (multiple chunks/runs) computes correctly.

Semantics (design §5):

* **Windows** — tumbling, **epoch-aligned UTC** (``window_seconds``, default
  900 — the 3GPP 15-min PM standard). A record at 23:47:12 belongs to
  23:45:00–00:00:00. Alignment is deliberate: telecom PM periods are
  wall-clock aligned; first-record alignment would drift with restarts.
* **Event time** — ``timestamp_field`` candidate list (same default as
  ``counter_delta``), parsed with the reused ``_parse_timestamp``. A record
  without a parseable timestamp raises ``TransformError`` (the executor's
  ``on_error`` path handles it like any other transform failure).
* **Watermark & lateness** — watermark = max event timestamp observed −
  ``allowed_lateness_seconds`` (default 60). A window finalizes — emits with
  ``window_complete: true`` — when the watermark passes its end. Records for
  an already-finalized window are dropped and counted in
  ``TRANSFORM_WINDOW_LATE_DROPPED_TOTAL`` (sinks are append-only; emitted
  windows are never updated).
* **State** — **accumulators, not samples**: per group+window only the
  op-relevant running values are kept (running sum/count for ``avg``, running
  max for ``max``, first/last values, …) plus the group-by values list (so the
  emitted group fields survive a state rehydration), so the blob stays
  O(groups × windows × ops), not O(samples). Open windows round-trip tick to
  tick in interval mode and are restored from a stream redispatch's snapshot.
* **Output record shape** — group-by fields verbatim, ``window_start`` /
  ``window_end`` as UTC ISO, one field per configured operation, plus
  ``sample_count`` and ``window_complete``.
* **Flush-on-close** — ``close(flush=True)`` (a ``?flush=true`` manual run,
  authoritative over the config field) emits every open window with
  ``window_complete: false`` **and clears them from the state blob**, so the
  partial windows are never re-emitted after rehydration (no double counting).
  A stream's graceful stop honors the transform's ``flush_on_close`` field
  (``true`` → flush; ``false`` → open windows stay in state and finalize
  naturally when the stream resumes). A stream that crashes never flushes —
  the open windows stay in the saved state so a redispatch continues them.
  ``close(flush=False)`` (a normal batch tick) is a no-op.

Operations reuse the ``aggregate`` transform's ``"name": "op:dotted.path"``
parser (``_SUPPORTED_OPS``: sum/avg/min/max/count/first/last).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.aggregate import AggregateTransform
from tram.transforms.path_utils import get_path
from tram.transforms.stateful import StatefulTransform
from tram.transforms.timestamp_normalize import _parse_timestamp

# Separator for the serialized group key (JSON-safe, like counter_delta's
# identity separator). Group values are coerced to str so any JSON-safe value
# produces a stable key.
_GROUP_SEP = "\x1f"

_NUMERIC = (int, float)


def _iso_utc(epoch_s: float) -> str:
    """UTC ISO-8601 (second precision, Z suffix) for a window boundary."""
    return datetime.fromtimestamp(epoch_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_numeric(value) -> bool:
    return isinstance(value, _NUMERIC) and not isinstance(value, bool)


@register_transform("window_aggregate")
class WindowAggregateTransform(BaseTransform, StatefulTransform):
    """Tumbling epoch-aligned windows with watermark-driven finalization."""

    # Base state_key; the executor overrides it with "window_aggregate:<position>".
    state_key = "window_aggregate"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.window_seconds = int(config.get("window_seconds", 900))
        if self.window_seconds <= 0:
            raise TransformError("window_aggregate: 'window_seconds' must be > 0")
        self.allowed_lateness_seconds = float(config.get("allowed_lateness_seconds", 60))
        if self.allowed_lateness_seconds < 0:
            raise TransformError(
                "window_aggregate: 'allowed_lateness_seconds' must be >= 0"
            )
        raw_ts = config.get("timestamp_field", ["_polled_at", "timestamp"])
        self.timestamp_fields: list[str] = (
            [raw_ts] if isinstance(raw_ts, str) else list(raw_ts or [])
        )
        if not self.timestamp_fields:
            raise TransformError("window_aggregate: 'timestamp_field' must not be empty")
        self.group_by: list[str] = list(config.get("group_by", []) or [])
        raw_ops = config.get("operations", {})
        if not raw_ops:
            raise TransformError("window_aggregate: 'operations' dict is required")
        # Reuse the aggregate transform's operation parser (design §5).
        self.operations: dict[str, tuple[str, str]] = AggregateTransform(
            {"group_by": [], "operations": raw_ops}
        ).operations
        self.flush_on_close: bool = bool(config.get("flush_on_close", False))

        # In-memory state (mirrors the persisted blob).
        self._max_ts: float | None = None
        # {group_key: {window_end_epoch: window_entry}}
        self._windows: dict[str, dict[int, dict]] = {}

    # ── StatefulTransform protocol ─────────────────────────────────────────

    def get_state(self) -> dict:
        return {
            "max_ts": self._max_ts,
            "windows": self._windows,
        }

    def set_state(self, blob: dict) -> None:
        blob = blob or {}
        self._max_ts = blob.get("max_ts")
        windows = blob.get("windows") or {}
        # JSON round-trips dict keys to strings — restore int window_end keys
        # (epoch seconds) so fresh and hydrated windows hash identically.
        self._windows = {
            group_key: {
                int(window_end): entry
                for window_end, entry in group_windows.items()
            }
            for group_key, group_windows in windows.items()
        }

    def close(self, flush: bool) -> list[dict]:
        """Emit open windows as partials when *flush*; clear them from state.

        Returns the partial-window records so the executor can route them to
        the sinks *before* the final state PUT (the saved blob then reflects
        the cleared windows — no double emission after rehydration).
        """
        if not flush or not self._windows:
            return []
        from tram.metrics.registry import TRANSFORM_WINDOWS_EMITTED_TOTAL

        pipeline = (self.config.get("_pipeline") or {}).get("name", "")
        emitted: list[dict] = []
        for _group_key, windows in self._windows.items():
            for window_end in sorted(windows):
                entry = windows[window_end]
                emitted.append(self._emit_window(entry["group_values"], entry, complete=False))
                TRANSFORM_WINDOWS_EMITTED_TOTAL.labels(
                    pipeline=pipeline, complete="partial"
                ).inc()
        self._windows = {}
        return emitted

    # ── Internal helpers ───────────────────────────────────────────────────

    def _resolve_timestamp(self, record: dict) -> float:
        """Return epoch seconds for a record, or raise ``TransformError``."""
        for field in self.timestamp_fields:
            found, val = get_path(record, field)
            if found and val is not None and val != "":
                return _parse_timestamp(val, None, None).timestamp()
        raise TransformError(
            f"window_aggregate: no parseable timestamp in {self.timestamp_fields!r}"
        )

    def _group_key(self, record: dict) -> tuple[str, list]:
        """Return ``(json_safe_key, group_values)`` for a record's group."""
        values = []
        for field in self.group_by:
            found, val = get_path(record, field)
            values.append(val if found else None)
        key = _GROUP_SEP.join("" if v is None else str(v) for v in values)
        return key, values

    def _new_accumulator(self, op: str) -> dict:
        if op == "avg":
            return {"sum": 0.0, "count": 0}
        if op == "count":
            return {"value": 0}
        if op == "first":
            return {"value": None, "seen": False}
        if op in ("sum", "min", "max"):
            return {"value": None, "seen": False}
        if op == "last":
            return {"value": None}
        raise TransformError(f"window_aggregate: unsupported operation '{op}'")

    def _update_accumulator(self, acc: dict, op: str, value) -> None:
        """Fold one record value into an accumulator (ops mirror aggregate)."""
        if op == "sum":
            if _is_numeric(value):
                acc["value"] = (acc["value"] or 0.0) + value
                acc["seen"] = True
        elif op == "avg":
            if _is_numeric(value):
                acc["sum"] += value
                acc["count"] += 1
        elif op == "min":
            if _is_numeric(value):
                acc["value"] = value if not acc["seen"] else min(acc["value"], value)
                acc["seen"] = True
        elif op == "max":
            if _is_numeric(value):
                acc["value"] = value if not acc["seen"] else max(acc["value"], value)
                acc["seen"] = True
        elif op == "count":
            if value is not None:
                acc["value"] += 1
        elif op == "first":
            # Skip missing values: a leading missing field must not occupy the
            # first slot — the first *observed* value stands.
            if not acc["seen"] and value is not None:
                acc["value"] = value
                acc["seen"] = True
        elif op == "last":
            # Skip missing values: a None must never clobber a real last.
            if value is not None:
                acc["value"] = value

    def _emit_value(self, acc: dict, op: str):
        """Project an accumulator to the output value (aggregate parity)."""
        if op == "sum":
            return acc["value"] if acc["seen"] else None
        if op == "avg":
            return (acc["sum"] / acc["count"]) if acc["count"] else None
        if op in ("min", "max", "first", "last"):
            return acc["value"]
        if op == "count":
            return acc["value"]
        return None

    def _emit_window(self, group_values: list, entry: dict, *, complete: bool) -> dict:
        """Build an output record for one window (design §5 output shape)."""
        out: dict = {}
        for field, value in zip(self.group_by, group_values):
            out[field] = value
        out["window_start"] = _iso_utc(entry["start"])
        out["window_end"] = _iso_utc(entry["end"])
        for out_field, (op, _src_field) in self.operations.items():
            out[out_field] = self._emit_value(entry["acc"][out_field], op)
        out["sample_count"] = entry["sample_count"]
        out["window_complete"] = complete
        return out

    def _finalize_due_windows(self, pipeline: str) -> list[dict]:
        """Emit + remove every open window whose end the watermark has passed."""
        from tram.metrics.registry import TRANSFORM_WINDOWS_EMITTED_TOTAL

        watermark = self._max_ts - self.allowed_lateness_seconds
        emitted: list[dict] = []
        for _group_key, windows in list(self._windows.items()):
            for window_end in list(windows):
                if window_end <= watermark:
                    entry = windows.pop(window_end)
                    emitted.append(
                        self._emit_window(entry["group_values"], entry, complete=True)
                    )
                    TRANSFORM_WINDOWS_EMITTED_TOTAL.labels(
                        pipeline=pipeline, complete="complete"
                    ).inc()
            if not windows:
                self._windows.pop(_group_key, None)
        return emitted

    # ── Apply ──────────────────────────────────────────────────────────────

    def apply(self, records: list[dict]) -> list[dict]:
        if not records:
            return []
        from tram.metrics.registry import TRANSFORM_WINDOW_LATE_DROPPED_TOTAL

        pipeline = (self.config.get("_pipeline") or {}).get("name", "")
        result: list[dict] = []
        for record in records:
            ts = self._resolve_timestamp(record)
            window_start = math.floor(ts / self.window_seconds) * self.window_seconds
            window_end = int(window_start + self.window_seconds)

            # Late check against the CURRENT watermark (pre-update): a record
            # whose window is already finalized is dropped and counted.
            if self._max_ts is not None and window_end <= (
                self._max_ts - self.allowed_lateness_seconds
            ):
                TRANSFORM_WINDOW_LATE_DROPPED_TOTAL.labels(pipeline=pipeline).inc()
                continue

            if self._max_ts is None or ts > self._max_ts:
                self._max_ts = ts
            # Finalize windows the (advanced) watermark now covers, then fold
            # the record into its (still open) window.
            result.extend(self._finalize_due_windows(pipeline))

            group_key, group_values = self._group_key(record)
            windows = self._windows.setdefault(group_key, {})
            entry = windows.get(window_end)
            if entry is None:
                entry = {
                    "start": window_start,
                    "end": window_end,
                    # The group-by VALUES list (not the \x1f-joined key string):
                    # emitted group fields must round-trip faithfully even after
                    # a state rehydration.
                    "group_values": group_values,
                    "sample_count": 0,
                    "acc": {
                        out_field: self._new_accumulator(op)
                        for out_field, (op, _src) in self.operations.items()
                    },
                }
                windows[window_end] = entry
            entry["sample_count"] += 1
            for out_field, (op, src_field) in self.operations.items():
                found, value = get_path(record, src_field)
                self._update_accumulator(
                    entry["acc"][out_field], op, value if found else None
                )
        return result