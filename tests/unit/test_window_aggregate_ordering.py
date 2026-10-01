"""window_aggregate characterization tests for the O(log g) finalize refactor.

Issue #80 cost center 1 replaces ``_finalize_due_windows``'s O(groups x open
windows) scan with a global heap of open windows keyed by window-end. These
tests characterize the due-window semantics the refactor MUST preserve:

* which windows a record belongs to (in-order, out-of-order-within-lateness,
  late-after-finalize, exact-boundary records);
* when windows close (watermark vs record timestamps, not window-boundary
  records);
* multi-group isolation, including several groups sharing one window-end
  (the heap's collision case);
* eviction of stale groups (a group whose windows all finalized leaves no
  empty entry in the state blob);
* heap hygiene across the stateful lifecycle (set_state rehydration and
  close(flush=True)) — the heap is derived, not persisted;
* the structural O(log g) property: the per-record path must never scan all
  open groups when nothing is due.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tram.transforms.window_aggregate import WindowAggregateTransform


def _make_transform(**overrides) -> WindowAggregateTransform:
    config = {
        "window_seconds": 900,
        "allowed_lateness_seconds": 60,
        "timestamp_field": ["_polled_at", "timestamp"],
        "group_by": ["_index"],
        "operations": {
            "mean_rate": "avg:rate",
            "peak_rate": "max:rate",
            "last_rate": "last:rate",
        },
        "flush_on_close": False,
        "_pipeline": {"name": "wa-pipe", "source": {"type": "snmp_poll"}},
    }
    config.update(overrides)
    return WindowAggregateTransform(config)


def _record(rate, t="2026-09-16T09:47:12+00:00", index="1") -> dict:
    return {"rate": rate, "_index": index, "_polled_at": t}


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


class _CountingDict(dict):
    """dict subclass that counts full-container iteration (structural pin)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()

    def items(self):
        self.iterations += 1
        return super().items()

    def values(self):
        self.iterations += 1
        return super().values()

    def keys(self):
        self.iterations += 1
        return super().keys()


# ── Characterization: which windows a record belongs to ────────────────────


class TestWindowMembership:
    def test_in_order_across_multiple_windows(self):
        """In-order arrival: each window finalizes exactly once, on the
        watermark, and the triggering record belongs to the next window."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])   # w1 [09:00, 09:15)
        t.apply([_record(110.0, t="2026-09-16T09:14:50+00:00")])   # w1
        out = t.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])  # closes w1, opens w2
        assert len(out) == 1
        assert out[0]["window_start"] == "2026-09-16T09:00:00Z"
        assert out[0]["window_end"] == "2026-09-16T09:15:00Z"
        assert out[0]["window_complete"] is True
        assert out[0]["sample_count"] == 2
        assert out[0]["mean_rate"] == pytest.approx(105.0)
        # The trigger record is accumulating in w2, not emitted.
        remaining = t.get_state()["windows"]
        assert list(next(iter(remaining.values()))) == [_epoch("2026-09-16T09:30:00+00:00")]

        out = t.apply([_record(300.0, t="2026-09-16T09:31:01+00:00")])  # closes w2
        assert len(out) == 1
        assert out[0]["window_start"] == "2026-09-16T09:15:00Z"
        assert out[0]["sample_count"] == 1
        assert out[0]["mean_rate"] == pytest.approx(200.0)

    def test_out_of_order_within_lateness_included(self):
        """Records that arrive out of order but whose window is still open
        (within the lateness bound) fold into that window's aggregates."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        t.apply([_record(120.0, t="2026-09-16T09:12:00+00:00")])
        # Out-of-order arrival for w1 — watermark (09:11:00) has not passed w1's end.
        t.apply([_record(110.0, t="2026-09-16T09:10:30+00:00")])
        out = t.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])
        assert len(out) == 1
        assert out[0]["sample_count"] == 3
        assert out[0]["mean_rate"] == pytest.approx(110.0)
        assert out[0]["window_complete"] is True

    def test_late_record_for_finalized_window_dropped(self):
        """A record whose window the watermark has already covered is dropped
        and does not contribute to any window's aggregates."""
        from unittest.mock import patch

        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])   # w1
        t.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])   # closes w1
        with patch("tram.metrics.registry.TRANSFORM_WINDOW_LATE_DROPPED_TOTAL") as late:
            out = t.apply([_record(50.0, t="2026-09-16T09:14:50+00:00")])
            assert out == []
            late.labels.assert_called_once_with(pipeline="wa-pipe")
        # The late record did not reopen w1 nor advance the watermark.
        assert t.get_state()["max_ts"] == pytest.approx(
            datetime.fromisoformat("2026-09-16T09:16:01+00:00").timestamp()
        )

    def test_exact_boundary_record_belongs_to_new_window(self):
        """A record exactly at 09:15:00 belongs to [09:15, 09:30), and does
        NOT close the [09:00, 09:15) window — only the watermark closes it."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        out = t.apply([_record(200.0, t="2026-09-16T09:15:00+00:00")])
        assert out == []  # nothing due: watermark 09:14:00 < 09:15:00
        # The boundary record sits in the later window; w1 is still open.
        group_windows = next(iter(t.get_state()["windows"].values()))
        assert set(group_windows) == {
            _epoch("2026-09-16T09:15:00+00:00"),
            _epoch("2026-09-16T09:30:00+00:00"),
        }
        out = t.apply([_record(300.0, t="2026-09-16T09:16:01+00:00")])
        assert len(out) == 1
        assert out[0]["window_end"] == "2026-09-16T09:15:00Z"
        assert out[0]["window_complete"] is True
        assert out[0]["sample_count"] == 1
        assert out[0]["mean_rate"] == pytest.approx(100.0)


# ── Characterization: multi-group / eviction ───────────────────────────────


class TestMultiGroupAndEviction:
    def test_multiple_groups_share_one_window_end(self):
        """Two groups with records in the SAME window: one watermark advance
        finalizes both, each emitting its own aggregates."""
        t = _make_transform()
        t.apply([
            _record(100.0, t="2026-09-16T09:10:00+00:00", index="ne-01"),
            _record(300.0, t="2026-09-16T09:10:00+00:00", index="ne-02"),
        ])
        out = t.apply([_record(200.0, t="2026-09-16T09:16:01+00:00", index="ne-01")])
        by_index = {r["_index"]: r for r in out}
        assert set(by_index) == {"ne-01", "ne-02"}
        assert by_index["ne-01"]["mean_rate"] == pytest.approx(100.0)
        assert by_index["ne-02"]["mean_rate"] == pytest.approx(300.0)
        assert by_index["ne-01"]["sample_count"] == 1
        assert by_index["ne-02"]["sample_count"] == 1
        assert all(r["window_complete"] is True for r in out)

    def test_stale_group_evicted_after_finalize(self):
        """A group whose last window finalizes leaves no entry in the state
        blob (no unbounded group-key growth at high cardinality). The record
        that advances the watermark is a DIFFERENT group's record — the
        finalizing group is evicted, the advancing group's own new window
        keeps ITS group alive."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00", index="ne-01")])
        out = t.apply([_record(200.0, t="2026-09-16T09:16:01+00:00", index="ne-02")])
        assert len(out) == 1
        assert out[0]["_index"] == "ne-01"  # finalized by ne-02's watermark advance
        remaining = t.get_state()["windows"]
        assert "ne-01" not in remaining
        assert set(remaining) == {"ne-02"}  # ne-02's fresh window survives

    def test_group_partially_evicted(self):
        """Two groups whose windows finalize together are evicted together,
        while a third group with a later window survives."""
        t = _make_transform()
        t.apply([
            _record(100.0, t="2026-09-16T09:10:00+00:00", index="ne-01"),
            _record(300.0, t="2026-09-16T09:10:00+00:00", index="ne-02"),
        ])
        out = t.apply([_record(500.0, t="2026-09-16T09:20:00+00:00", index="ne-03")])
        by_index = {r["_index"]: r for r in out}
        assert set(by_index) == {"ne-01", "ne-02"}  # both w1 windows due
        remaining = t.get_state()["windows"]
        assert set(remaining) == {"ne-03"}  # ne-03's window (end 09:30) open
        assert next(iter(remaining.values()))[datetime.fromisoformat(
            "2026-09-16T09:30:00+00:00").timestamp()]["sample_count"] == 1


# ── Heap lifecycle: rehydration, flush, single-push ────────────────────────


class TestHeapLifecycle:
    def test_state_roundtrip_rebuilds_due_detection(self):
        """The heap is derived state: after set_state rehydration, an advance
        of the watermark still finalizes the hydrated open windows."""
        t1 = _make_transform()
        t1.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        blob = t1.get_state()

        t2 = _make_transform()
        t2.set_state(blob)
        out = t2.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])
        assert len(out) == 1
        assert out[0]["mean_rate"] == pytest.approx(100.0)
        assert out[0]["sample_count"] == 1
        assert out[0]["window_complete"] is True

    def test_hybrid_hydrated_and_fresh_windows(self):
        """Hydrated windows and freshly-created windows coexist: advancing the
        watermark finalizes only the due (hydrated) window — the fresh window
        (whose record stays below the finalizing watermark) survives."""
        t1 = _make_transform()
        t1.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        t2 = _make_transform()
        t2.set_state(t1.get_state())
        # A record at 09:15:30 opens a NEW window (end 09:30) without advancing
        # the watermark past the hydrated window's end (09:15:00).
        t2.apply([_record(900.0, t="2026-09-16T09:15:30+00:00")])
        out = t2.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])
        assert len(out) == 1
        assert out[0]["window_end"] == "2026-09-16T09:15:00Z"
        assert out[0]["sample_count"] == 1
        remaining = t2.get_state()["windows"]
        group_windows = next(iter(remaining.values()))
        assert set(group_windows) == {
            datetime.fromisoformat("2026-09-16T09:30:00+00:00").timestamp()
        }

    def test_close_flush_resets_due_tracking(self):
        """close(flush=True) clears windows AND the due index — a later run
        can never re-emit the flushed partials."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        partials = t.close(flush=True)
        assert len(partials) == 1
        assert partials[0]["window_complete"] is False
        assert t.get_state()["windows"] == {}

        # A new window after the flush finalizes exactly once, sample_count 1.
        t.apply([_record(200.0, t="2026-09-16T09:20:00+00:00")])
        out = t.apply([_record(300.0, t="2026-09-16T09:31:01+00:00")])
        assert len(out) == 1
        assert out[0]["sample_count"] == 1
        assert out[0]["mean_rate"] == pytest.approx(200.0)

    def test_same_window_pushed_once_emitted_once(self):
        """Multiple records for the same (group, window) across several apply
        calls push one due-index entry and emit the window exactly once."""
        t = _make_transform()
        for rate, ts in (
            (100.0, "2026-09-16T09:10:00+00:00"),
            (110.0, "2026-09-16T09:11:00+00:00"),
            (120.0, "2026-09-16T09:12:00+00:00"),
        ):
            t.apply([_record(rate, t=ts)])
        out = t.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])
        assert len(out) == 1
        assert out[0]["sample_count"] == 3
        assert out[0]["mean_rate"] == pytest.approx(110.0)

    def test_set_state_missing_keys_is_empty(self):
        t = _make_transform()
        t.set_state(None)
        assert t.get_state() == {"max_ts": None, "windows": {}}
        assert t._due_heap == []


# ── Structural pin: O(log g), no full-group scan on the hot path ───────────


class TestNoGroupScan:
    def test_per_record_path_never_scans_open_groups(self):
        """The issue #80 hot path: when nothing is due, apply() must not
        iterate the open-group dict at all (the old code did, once per
        record — O(groups) per record)."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        counting = _CountingDict(t._windows)
        t._windows = counting
        # Same window: watermark advances but covers no window-end.
        t.apply([_record(110.0, t="2026-09-16T09:11:00+00:00")])
        assert counting.iterations == 0

    def test_due_path_visits_only_due_groups(self):
        """When windows are due, finalization pops heap entries and touches only
        the specific (group, window) keys that are due — it never iterates the
        full open-group dict (the O(groups) scan the refactor removed)."""
        t = _make_transform()
        # 100 groups with a window ending 09:15:00.
        t.apply([
            _record(100.0 + i, t="2026-09-16T09:10:00+00:00", index=f"g-{i:03d}")
            for i in range(100)
        ])
        # 100 MORE groups with a window ending 09:30:00; this apply advances
        # the watermark past 09:15:00 and thus finalizes the first 100 windows
        # (pre-count, so the counting dict only sees the open w2 windows).
        t.apply([
            _record(1.0, t="2026-09-16T09:20:00+00:00", index=f"f-{i:03d}")
            for i in range(100)
        ])
        assert len(t.get_state()["windows"]) == 100  # only w2 groups remain
        counting = _CountingDict(t._windows)
        t._windows = counting
        out = t.apply([_record(999.0, t="2026-09-16T09:31:01+00:00", index="z-last")])
        assert len(out) == 100  # exactly the due w2 windows emit
        # The finalize pass pops heap entries and touches only the specific
        # (group, window) keys that are due — it never iterates the full
        # open-group dict (the O(groups) scan the refactor removed).
        assert counting.iterations == 0