"""counter_delta transform — per-key counter deltas with wrap/reset correction.

Computes ``v_now - v_prev`` for cumulative SNMP/gNMI counters with Counter32
wrap correction and reboot/reset detection, plus an optional rate over the
actual elapsed time. This is the defining PM-mediation primitive the SNMP poll
source was missing (telecom domain review, design F.1 §4).

Semantics (design §4):

* **Delta math** — ``raw = (v_now - v_prev) mod W``. A *decrease* is a wrap
  candidate: the wrap-corrected delta ``v_now + W - v_prev``. **Wrap vs reset
  is distinguished by gap size**: corrected delta ``> reset_threshold × W``
  (default 0.5) means the device rebooted and the counter restarted near zero —
  classify as **reset** with ``delta = v_now`` (bytes since boot) and
  ``_counter_reset: true``; otherwise it is a **wrap** and the corrected delta
  stands. Counter64 never wraps in practice, so a 64-bit decrease is always a
  reset.
* **Rate** — ``delta / (t_now - t_prev)`` per-second, from the timestamps on
  the records (``timestamp_field``, default ``["_polled_at", "timestamp"]``,
  parsed with the reused ``_parse_timestamp``). A missing/unparseable timestamp
  follows ``on_error`` (raise | null | keep).
* **on_error on a bad record** — ``raise`` raises; ``null`` nulls the failing
  field's outputs *and any not-yet-processed fields* (fields already computed
  before the failure keep their valid delta/rate); ``keep`` returns the record
  as it arrived — a true snapshot, so no outputs are written at all.
* **Keys** — counter identity = (source identity from runtime meta's
  ``source_host``, ``key_fields`` values, field path), so two hosts polled by
  the same pipeline never cross-contaminate. A record missing a key field
  follows ``on_error``.
* **First sight** — no ``v_prev``: pass through with ``delta``/``rate`` set to
  ``None`` (default), or ``first_sample: "drop"`` the whole record. The value
  is always stored so the next sample computes a real delta.
* **Width** — ``_snmp_widths`` on the record (from the SNMP source's
  ``_classify_bindings``) is authoritative, then explicit ``width: 32|64``,
  then the ``auto`` heuristic (either sample ≥ 2³² → 64, else 32). Documented
  edge: a 64-bit reset where both values sit below 2³² can be misread as a
  32-bit wrap (spike instead of reset) — escape with explicit ``width: 64``.
* **Output** — per configured field ``f`` (dotted paths): ``f_delta`` and/or
  ``f_rate`` alongside the original; ``keep_raw: false`` drops the raw
  cumulative value after the delta is computed.
"""

from __future__ import annotations

import copy
import logging

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_utils import delete_path, get_path, set_path
from tram.transforms.stateful import StatefulTransform
from tram.transforms.timestamp_normalize import _parse_timestamp

logger = logging.getLogger(__name__)

_W32 = 1 << 32
_W64 = 1 << 64

# Separator for the string counter identity (JSON-safe dict key). The unit
# separator cannot appear in hostnames/field values and keeps the identity
# readable in the persisted state blob.
_ID_SEP = "\x1f"


@register_transform("counter_delta")
class CounterDeltaTransform(BaseTransform, StatefulTransform):
    """Compute wrap-corrected counter deltas and rates, keyed per counter series."""

    # Base state_key; the executor overrides it with "counter_delta:<position>"
    # (design §3.2a: transform type + position in the transforms list).
    state_key = "counter_delta"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.fields: list[str] = list(config.get("fields", []))
        if not self.fields:
            raise TransformError("counter_delta: 'fields' list is required")
        self.key_fields: list[str] = list(config.get("key_fields", ["_index"]))
        raw_ts = config.get("timestamp_field", ["_polled_at", "timestamp"])
        self.timestamp_fields: list[str] = (
            [raw_ts] if isinstance(raw_ts, str) else list(raw_ts or [])
        )
        if not self.timestamp_fields:
            raise TransformError("counter_delta: 'timestamp_field' must not be empty")
        self.width = config.get("width", "auto")
        if self.width not in ("auto", 32, 64):
            raise TransformError("counter_delta: 'width' must be 'auto', 32, or 64")
        self.output = config.get("output", "both")
        if self.output not in ("delta", "rate", "both"):
            raise TransformError("counter_delta: 'output' must be 'delta', 'rate', or 'both'")
        self.keep_raw: bool = bool(config.get("keep_raw", True))
        self.first_sample = config.get("first_sample", "pass")
        if self.first_sample not in ("pass", "drop"):
            raise TransformError("counter_delta: 'first_sample' must be 'pass' or 'drop'")
        self.reset_threshold = float(config.get("reset_threshold", 0.5))
        if not (0 < self.reset_threshold < 1):
            raise TransformError("counter_delta: 'reset_threshold' must be between 0 and 1")
        self.max_gap_seconds = config.get("max_gap_seconds")
        if self.max_gap_seconds is not None and float(self.max_gap_seconds) <= 0:
            raise TransformError("counter_delta: 'max_gap_seconds' must be > 0")
        self.on_error = config.get("on_error", "raise")
        if self.on_error not in ("raise", "null", "keep"):
            raise TransformError("counter_delta: 'on_error' must be 'raise', 'null', or 'keep'")

        self._meta: dict = {}
        # {identity: {"v": int, "t": float epoch-seconds}}
        self._state: dict[str, dict] = {}

    # ── Runtime metadata (source identity) ─────────────────────────────────

    def set_runtime_meta(self, meta: dict) -> None:
        """Capture per-chunk metadata; ``source_host`` feeds counter identity."""
        self._meta = dict(meta)

    # ── StatefulTransform protocol ─────────────────────────────────────────

    def get_state(self) -> dict:
        return dict(self._state)

    def set_state(self, blob: dict) -> None:
        self._state = dict(blob or {})

    def close(self, flush: bool) -> None:
        """No-op — counter_delta keeps no open resources or partial output."""
        return

    # ── Internal helpers ───────────────────────────────────────────────────

    def _resolve_timestamp(self, record: dict) -> float:
        """Return epoch seconds for a record, or raise ``TransformError``."""
        for field in self.timestamp_fields:
            found, val = get_path(record, field)
            if found and val is not None and val != "":
                return _parse_timestamp(val, None, None).timestamp()
        raise TransformError(
            f"counter_delta: no parseable timestamp in {self.timestamp_fields!r}"
        )

    def _identity(self, record: dict, field: str) -> str | None:
        """Build the counter-series identity string, or None on a missing key."""
        parts = [str(self._meta.get("source_host", ""))]
        for key_field in self.key_fields:
            found, val = get_path(record, key_field)
            if not found or val is None:
                return None
            parts.append(f"{key_field}={val}")
        parts.append(field)
        return _ID_SEP.join(parts)

    def _resolve_width(self, record: dict, field: str, prev_v: int, now_v: int) -> int:
        """Width resolution: ``_snmp_widths`` → explicit → auto heuristic."""
        snmp_widths = record.get("_snmp_widths") or {}
        base = field.rsplit(".", 1)[-1]
        snmp_width = snmp_widths.get(base)
        if snmp_width in (32, 64):
            return int(snmp_width)
        if self.width in (32, 64):
            return int(self.width)
        return 64 if (prev_v >= _W32 or now_v >= _W32) else 32

    def _delta_for(self, prev_v: int, now_v: int, width: int) -> tuple[int, bool]:
        """Return ``(delta, is_reset)`` for one counter pair (design §4.1)."""
        modulus = _W64 if width == 64 else _W32
        if now_v >= prev_v:
            # No decrease → plain difference (a Counter64 decrease can only be
            # a reset; a Counter32 one is classified below).
            return now_v - prev_v, False
        corrected = now_v + modulus - prev_v
        if corrected > self.reset_threshold * modulus:
            return now_v, True
        return corrected, False

    def _set_outputs(self, record: dict, field: str, delta, rate) -> None:
        if self.output in ("delta", "both"):
            set_path(record, f"{field}_delta", delta)
        if self.output in ("rate", "both"):
            set_path(record, f"{field}_rate", rate)

    def _handle_bad_record(
        self,
        record: dict,
        message: str,
        *,
        from_field_index: int = 0,
        snapshot: dict | None = None,
    ) -> dict:
        """Apply ``on_error`` for a record with no usable counter input.

        ``raise`` → raise; ``null`` → null the field outputs at index
        ``>= from_field_index`` — fields already computed before the failure
        keep their valid outputs, only the failing and not-yet-processed
        fields are nulled; ``keep`` → return the record *as it arrived*
        (``snapshot``), i.e. a true snapshot with no outputs written. Never
        updates state.
        """
        if self.on_error == "raise":
            raise TransformError(message)
        if self.on_error == "null":
            for field in self.fields[from_field_index:]:
                self._set_outputs(record, field, None, None)
            return record
        return snapshot if snapshot is not None else record

    # ── Apply ──────────────────────────────────────────────────────────────

    def apply(self, records: list[dict]) -> list[dict]:
        if not records:
            return []
        from tram.metrics.registry import (
            TRANSFORM_COUNTER_RESETS_TOTAL,
            TRANSFORM_COUNTER_WRAPS_TOTAL,
        )

        pipeline = (self.config.get("_pipeline") or {}).get("name", "")
        result: list[dict] = []
        for record in records:
            # Deep copy: outputs are written into dotted paths (e.g.
            # ``_metrics.ifInOctets_delta``), so a shallow copy would mutate the
            # caller's nested dicts.
            new_record = copy.deepcopy(record)

            try:
                t_now = self._resolve_timestamp(new_record)
            except Exception as exc:
                # Missing/unparseable timestamp → on_error policy; no state
                # update (we cannot anchor a prev time). Record-wide failure:
                # no field was processed, so null starts at index 0 and keep
                # returns the untouched snapshot.
                if isinstance(exc, TransformError):
                    result.append(
                        self._handle_bad_record(
                            new_record, str(exc), snapshot=record
                        )
                    )
                else:
                    raise
                continue

            first_sight = False
            bad_record = False
            for field_index, field in enumerate(self.fields):
                found, raw_v = get_path(new_record, field)
                if not found or raw_v is None or isinstance(raw_v, bool):
                    new_record = self._handle_bad_record(
                        new_record,
                        f"counter_delta: field {field!r} not found or null",
                        from_field_index=field_index,
                        snapshot=record,
                    )
                    bad_record = True
                    break
                try:
                    v_now = int(raw_v)
                except (TypeError, ValueError):
                    new_record = self._handle_bad_record(
                        new_record,
                        f"counter_delta: field {field!r} is not a number: {raw_v!r}",
                        from_field_index=field_index,
                        snapshot=record,
                    )
                    bad_record = True
                    break

                identity = self._identity(new_record, field)
                if identity is None:
                    new_record = self._handle_bad_record(
                        new_record,
                        f"counter_delta: key field missing for {field!r} "
                        f"(key_fields={self.key_fields})",
                        from_field_index=field_index,
                        snapshot=record,
                    )
                    bad_record = True
                    break

                prev = self._state.get(identity)
                if prev is None:
                    # First sight: pass through with null outputs (or drop the
                    # whole record) but always record the sample so the next
                    # interval computes a real delta.
                    first_sight = True
                    self._state[identity] = {"v": v_now, "t": t_now}
                    self._set_outputs(new_record, field, None, None)
                    continue

                prev_v = int(prev["v"])
                prev_t = float(prev["t"])
                elapsed = t_now - prev_t
                width = self._resolve_width(new_record, field, prev_v, v_now)

                if self.max_gap_seconds is not None and elapsed > self.max_gap_seconds:
                    # Outage guard: a device that was down and rebooted yields a
                    # plausible-but-wrong wrap-corrected delta; emit the
                    # v_now-style reset treatment instead.
                    delta = v_now
                    is_reset = True
                else:
                    delta, is_reset = self._delta_for(prev_v, v_now, width)

                if is_reset:
                    new_record["_counter_reset"] = True
                    TRANSFORM_COUNTER_RESETS_TOTAL.labels(
                        pipeline=pipeline, field=field
                    ).inc()
                elif v_now < prev_v:
                    TRANSFORM_COUNTER_WRAPS_TOTAL.labels(
                        pipeline=pipeline, field=field
                    ).inc()

                self._set_outputs(
                    new_record, field, delta, delta / elapsed if elapsed > 0 else None
                )
                if not self.keep_raw:
                    delete_path(new_record, field)
                self._state[identity] = {"v": v_now, "t": t_now}

            if bad_record:
                result.append(new_record)
                continue
            if self.first_sample == "drop" and first_sight:
                # KPI-only streams: drop the first sample entirely (the counter
                # is still tracked, so the next sample computes a delta).
                continue
            result.append(new_record)
        return result