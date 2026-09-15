"""Tests for the counter_delta transform (design F.1 §4 semantics)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.counter_delta import CounterDeltaTransform

_W32 = 1 << 32


def _make_transform(**overrides) -> CounterDeltaTransform:
    config = {
        "fields": ["_metrics.ifInOctets"],
        "key_fields": ["_index"],
        "timestamp_field": ["_polled_at", "timestamp"],
        "width": "auto",
        "output": "both",
        "keep_raw": True,
        "first_sample": "pass",
        "reset_threshold": 0.5,
        "max_gap_seconds": None,
        "on_error": "raise",
        "_pipeline": {"name": "cd-pipe", "source": {"type": "snmp_poll"}},
    }
    config.update(overrides)
    return CounterDeltaTransform(config)


def _record(v, index="1", t="2026-09-16T09:15:00+00:00", host="h1", extra=None):
    rec = {
        "_metrics": {"ifInOctets": v},
        "_index": index,
        "_polled_at": t,
    }
    if host is not None:
        rec["_host"] = host
    if extra:
        rec.update(extra)
    return rec


class TestWrapAndReset:
    def test_wrap32_correction(self):
        """prev=4294967190, now=100, width 32 → delta=206; wrap metric +1."""
        t = _make_transform(width=32)
        with patch("tram.metrics.registry.TRANSFORM_COUNTER_WRAPS_TOTAL") as wrap:
            out = t.apply([_record(4294967190, t="2026-09-16T09:00:00+00:00")])  # first sight
            assert out[0]["_metrics"]["ifInOctets_delta"] is None
            out = t.apply([_record(100, t="2026-09-16T09:05:00+00:00")])
            assert out[0]["_metrics"]["ifInOctets_delta"] == 206
            assert out[0]["_metrics"]["ifInOctets_rate"] == pytest.approx(206 / 300)
            assert "_counter_reset" not in out[0]
            wrap.labels.assert_called_once_with(pipeline="cd-pipe", field="_metrics.ifInOctets")
            wrap.labels.return_value.inc.assert_called_once()

    def test_wrap_vs_reset_by_gap_size(self):
        """Decrease with corrected delta < W/2 → wrap; > W/2 → reset."""
        # Wrap: prev near the top of the 32-bit space → the wrap-corrected delta
        # is small (≈ rate × interval) → classified as a wrap.
        t = _make_transform(width=32)
        t.apply([_record(4294966000, t="2026-09-16T09:00:00+00:00")])
        with patch("tram.metrics.registry.TRANSFORM_COUNTER_WRAPS_TOTAL") as wrap:
            out = t.apply([_record(150, t="2026-09-16T09:05:00+00:00")])
            # corrected = 150 + W32 - 4294966000 = 150 + 1296 = 1446 (< W/2)
            assert out[0]["_metrics"]["ifInOctets_delta"] == 1446
            assert "_counter_reset" not in out[0]
            wrap.labels.return_value.inc.assert_called_once()

        # Reset: a small decrease deep in the counter space → corrected delta
        # ≈ full width > W/2 → classified as a reset (delta = v_now).
        t2 = _make_transform(width=32)
        t2.apply([_record(1000, t="2026-09-16T09:00:00+00:00")])
        with patch("tram.metrics.registry.TRANSFORM_COUNTER_RESETS_TOTAL") as reset:
            out = t2.apply([_record(900, t="2026-09-16T09:05:00+00:00")])
            assert out[0]["_metrics"]["ifInOctets_delta"] == 900  # delta = v_now
            assert out[0]["_counter_reset"] is True
            reset.labels.return_value.inc.assert_called_once()

    def test_counter64_reset(self):
        """A Counter64 decrease is always a reset (no wrap possible in practice)."""
        t = _make_transform(width=64)
        t.apply([_record(5000, t="2026-09-16T09:00:00+00:00")])
        with patch("tram.metrics.registry.TRANSFORM_COUNTER_RESETS_TOTAL") as reset:
            out = t.apply([_record(1000, t="2026-09-16T09:05:00+00:00")])
            assert out[0]["_metrics"]["ifInOctets_delta"] == 1000
            assert out[0]["_counter_reset"] is True
            reset.labels.return_value.inc.assert_called_once()

    def test_rate_uses_actual_elapsed(self):
        """Jittered intervals (295s, 305s) → exact per-second rates."""
        t = _make_transform()
        t.apply([_record(1000, t="2026-09-16T09:00:00+00:00")])          # first sight
        out = t.apply([_record(1590, t="2026-09-16T09:04:55+00:00")])    # +295s, Δ=590
        assert out[0]["_metrics"]["ifInOctets_delta"] == 590
        assert out[0]["_metrics"]["ifInOctets_rate"] == pytest.approx(2.0)
        out = t.apply([_record(2200, t="2026-09-16T09:10:00+00:00")])    # +305s, Δ=610
        assert out[0]["_metrics"]["ifInOctets_delta"] == 610
        assert out[0]["_metrics"]["ifInOctets_rate"] == pytest.approx(2.0)

    def test_max_gap_seconds_forced_reset(self):
        """A huge gap with a *rise* is treated as a reboot reset (not a long delta)."""
        t = _make_transform(max_gap_seconds=600)
        t.apply([_record(1000, t="2026-09-16T09:00:00+00:00")])
        with patch("tram.metrics.registry.TRANSFORM_COUNTER_RESETS_TOTAL") as reset:
            out = t.apply([_record(2000, t="2026-09-16T10:00:00+00:00")])  # +3600s
            assert out[0]["_metrics"]["ifInOctets_delta"] == 2000
            assert out[0]["_counter_reset"] is True
            reset.labels.return_value.inc.assert_called_once()

    def test_keep_raw_false_drops_cumulative(self):
        """keep_raw: false removes the raw cumulative value after the delta."""
        t = _make_transform(keep_raw=False)
        t.apply([_record(1000, t="2026-09-16T09:00:00+00:00")])
        out = t.apply([_record(1100, t="2026-09-16T09:05:00+00:00")])
        assert "_metrics" in out[0]
        assert "ifInOctets" not in out[0]["_metrics"]
        assert out[0]["_metrics"]["ifInOctets_delta"] == 100

    def test_output_modes(self):
        """output: delta|rate|both controls which fields are emitted."""
        t = _make_transform(output="delta")
        t.apply([_record(1000, t="2026-09-16T09:00:00+00:00")])
        out = t.apply([_record(1100, t="2026-09-16T09:05:00+00:00")])
        assert "ifInOctets_delta" in out[0]["_metrics"]
        assert "ifInOctets_rate" not in out[0]["_metrics"]

        t = _make_transform(output="rate")
        t.apply([_record(1000, t="2026-09-16T09:00:00+00:00")])
        out = t.apply([_record(1100, t="2026-09-16T09:05:00+00:00")])
        assert "ifInOctets_rate" in out[0]["_metrics"]
        assert "ifInOctets_delta" not in out[0]["_metrics"]


class TestFirstSight:
    def test_first_sample_pass_and_drop(self):
        """Both policies; null delta/rate on pass, whole record dropped on drop."""
        t = _make_transform(first_sample="pass")
        out = t.apply([_record(1000)])
        assert out[0]["_metrics"]["ifInOctets_delta"] is None
        assert out[0]["_metrics"]["ifInOctets_rate"] is None
        # The counter is tracked: the next sample computes a real delta.
        out = t.apply([_record(1100, t="2026-09-16T09:05:00+00:00")])
        assert out[0]["_metrics"]["ifInOctets_delta"] == 100

        t = _make_transform(first_sample="drop")
        out = t.apply([_record(1000)])
        assert out == []  # first sample dropped
        out = t.apply([_record(1100, t="2026-09-16T09:05:00+00:00")])
        assert len(out) == 1
        assert out[0]["_metrics"]["ifInOctets_delta"] == 100


class TestKeyIsolation:
    def test_key_isolation(self):
        """Two _index values and a second source_host never cross-contaminate."""
        t = _make_transform()
        # Chunk 1: both indexes first sight.
        t.set_runtime_meta({"source_host": "h1"})
        t.apply([
            _record(1000, index="1", t="2026-09-16T09:00:00+00:00"),
            _record(2000, index="2", t="2026-09-16T09:00:00+00:00"),
        ])
        # Chunk 2: index 1 advances on h1; index 2 still first sight.
        t.set_runtime_meta({"source_host": "h1"})
        out = t.apply([
            _record(1100, index="1", t="2026-09-16T09:05:00+00:00"),
            _record(2100, index="2", t="2026-09-16T09:05:00+00:00"),
        ])
        deltas = {r["_index"]: r["_metrics"]["ifInOctets_delta"] for r in out}
        assert deltas["1"] == 100
        assert deltas["2"] == 100  # each series advances independently
        # Chunk 3: the SAME index on a DIFFERENT host is a fresh series.
        t.set_runtime_meta({"source_host": "h2"})
        out = t.apply([_record(9999, index="1", t="2026-09-16T09:10:00+00:00")])
        assert out[0]["_metrics"]["ifInOctets_delta"] is None

    def test_missing_key_field_on_error(self):
        """A record missing a key field follows on_error (raise/null/keep)."""
        t = _make_transform()
        with pytest.raises(TransformError, match="key field"):
            t.apply([{"_metrics": {"ifInOctets": 100}, "_polled_at": "2026-09-16T09:00:00+00:00"}])

        t = _make_transform(on_error="null")
        out = t.apply([{"_metrics": {"ifInOctets": 100}, "_polled_at": "2026-09-16T09:00:00+00:00"}])
        assert out[0]["_metrics"]["ifInOctets_delta"] is None

        t = _make_transform(on_error="keep")
        out = t.apply([{"_metrics": {"ifInOctets": 100}, "_polled_at": "2026-09-16T09:00:00+00:00"}])
        assert "ifInOctets_delta" not in out[0]["_metrics"]


def _rec2(a, b, t="2026-09-16T09:00:00+00:00") -> dict:
    """Two-field counter record (ifInOctets, ifOutOctets)."""
    return {
        "_metrics": {"ifInOctets": a, "ifOutOctets": b},
        "_index": "1",
        "_polled_at": t,
    }


class TestOnErrorMidRecord:
    """on_error semantics when only the SECOND configured field fails.

    ``null`` must null only the failing (and not-yet-processed) fields — the
    earlier field's valid delta/rate must survive. ``keep`` must return the
    record as it arrived — a true snapshot, not partial outputs.
    """

    def test_null_only_nulls_not_yet_processed_fields(self):
        t = _make_transform(
            fields=["_metrics.ifInOctets", "_metrics.ifOutOctets"],
            on_error="null",
        )
        t.apply([_rec2(1000, 2000)])  # first sight for both series
        out = t.apply([_rec2(1100, None, t="2026-09-16T09:05:00+00:00")])
        # Field 1 was computed before the failure → its outputs stand.
        assert out[0]["_metrics"]["ifInOctets_delta"] == 100
        assert out[0]["_metrics"]["ifInOctets_rate"] == pytest.approx(100 / 300)
        # Field 2 failed → nulled, not silently dropped.
        assert out[0]["_metrics"]["ifOutOctets_delta"] is None
        assert out[0]["_metrics"]["ifOutOctets_rate"] is None

    def test_keep_returns_snapshot_on_mid_record_failure(self):
        t = _make_transform(
            fields=["_metrics.ifInOctets", "_metrics.ifOutOctets"],
            on_error="keep",
        )
        t.apply([_rec2(1000, 2000)])  # first sight for both series
        rec = _rec2(1100, None, t="2026-09-16T09:05:00+00:00")
        out = t.apply([rec])
        # A true snapshot: NO outputs are written, not even for the field that
        # was processed successfully before the failure.
        assert out[0] == rec
        assert "ifInOctets_delta" not in out[0]["_metrics"]
        assert "ifOutOctets_delta" not in out[0]["_metrics"]


class TestWidth:
    def test_width_snmp_authoritative(self):
        """_snmp_widths on the record wins over the explicit config value."""
        # Explicit config says 32, but the record says Counter64 → the decrease
        # is a reset, not a 32-bit wrap.
        t = _make_transform(width=32)
        t.apply([_record(5000, t="2026-09-16T09:00:00+00:00")])
        out = t.apply([
            _record(1000, t="2026-09-16T09:05:00+00:00",
                    extra={"_snmp_widths": {"ifInOctets": 64}})
        ])
        assert out[0]["_metrics"]["ifInOctets_delta"] == 1000
        assert out[0]["_counter_reset"] is True

    def test_width_auto_heuristic(self):
        """auto: a sample ≥ 2³² → 64-bit, otherwise 32-bit."""
        t = _make_transform()  # auto
        t.apply([_record(4294967000, t="2026-09-16T09:00:00+00:00")])
        # both below 2^32 → 32-bit width; a decrease from near the top is a wrap
        out = t.apply([_record(100, t="2026-09-16T09:05:00+00:00")])
        assert out[0]["_metrics"]["ifInOctets_delta"] == 100 + 296  # 100 + W32 - (W32 - 296)
        assert "_counter_reset" not in out[0]

    def test_width_64_as_32_misread_and_explicit_escape(self):
        """Documented edge: a 64-bit reset with both samples below 2³² can be
        misread as a 32-bit wrap; explicit width: 64 is the escape."""
        # Misread: prev near the 32-bit ceiling, now near zero, both < 2^32.
        t = _make_transform(width="auto")
        t.apply([_record(_W32 - 1000, t="2026-09-16T09:00:00+00:00")])
        with patch("tram.metrics.registry.TRANSFORM_COUNTER_WRAPS_TOTAL") as wrap:
            out = t.apply([_record(100, t="2026-09-16T09:05:00+00:00")])
            # corrected = 100 + W32 - (_W32 - 1000) = 1100 < W/2 → misread as wrap
            assert out[0]["_metrics"]["ifInOctets_delta"] == 1100
            wrap.labels.return_value.inc.assert_called_once()

        # Escape: explicit width: 64 → same samples classify as a reset.
        t = _make_transform(width=64)
        t.apply([_record(_W32 - 1000, t="2026-09-16T09:00:00+00:00")])
        with patch("tram.metrics.registry.TRANSFORM_COUNTER_RESETS_TOTAL") as reset:
            out = t.apply([_record(100, t="2026-09-16T09:05:00+00:00")])
            assert out[0]["_metrics"]["ifInOctets_delta"] == 100
            assert out[0]["_counter_reset"] is True
            reset.labels.return_value.inc.assert_called_once()


class TestTimestamps:
    def test_missing_timestamp_on_error_policies(self):
        """raise/null/keep parity with timestamp_normalize for missing timestamps."""
        rec_no_ts = {"_metrics": {"ifInOctets": 100}, "_index": "1"}

        t = _make_transform()
        with pytest.raises(TransformError, match="timestamp"):
            t.apply([rec_no_ts])

        t = _make_transform(on_error="null")
        out = t.apply([rec_no_ts])
        assert out[0]["_metrics"]["ifInOctets_delta"] is None
        assert out[0]["_metrics"]["ifInOctets_rate"] is None
        # No state update: a later well-formed record is still first sight.
        out = t.apply([_record(100, t="2026-09-16T09:00:00+00:00")])
        assert out[0]["_metrics"]["ifInOctets_delta"] is None

        t = _make_transform(on_error="keep")
        out = t.apply([rec_no_ts])
        assert out[0]["_metrics"] == {"ifInOctets": 100}  # untouched

    def test_unparseable_timestamp_follows_on_error(self):
        """An unparseable timestamp is the same as a missing one."""
        t = _make_transform()
        with pytest.raises(TransformError):
            t.apply([_record(100, t="not-a-timestamp")])

        t = _make_transform(on_error="null")
        out = t.apply([_record(100, t="not-a-timestamp")])
        assert out[0]["_metrics"]["ifInOctets_delta"] is None

    def test_gnmi_timestamp_candidate(self):
        """gNMI-style records use the 'timestamp' candidate (ns epoch int)."""
        t = _make_transform(timestamp_field=["_polled_at", "timestamp"])
        t.apply([{"_metrics": {"ifInOctets": 1000}, "_index": "1",
                  "timestamp": 1760613302000000000}])
        out = t.apply([{"_metrics": {"ifInOctets": 1100}, "_index": "1",
                        "timestamp": 1760613303000000000}])  # +1s (1e9 ns)
        assert out[0]["_metrics"]["ifInOctets_delta"] == 100
        assert out[0]["_metrics"]["ifInOctets_rate"] == pytest.approx(100.0)


class TestStatefulContract:
    def test_state_key_and_state_roundtrip(self):
        """The protocol contract: get_state/set_state/close and a JSON-safe blob."""
        t = _make_transform()
        t.set_runtime_meta({"source_host": "h1"})
        t.apply([_record(1000, t="2026-09-16T09:00:00+00:00")])
        blob = t.get_state()
        assert len(blob) == 1
        identity = next(iter(blob))
        assert blob[identity]["v"] == 1000

        t2 = _make_transform()
        t2.set_state(blob)
        t2.set_runtime_meta({"source_host": "h1"})
        out = t2.apply([_record(1100, t="2026-09-16T09:05:00+00:00")])
        assert out[0]["_metrics"]["ifInOctets_delta"] == 100

        t.close(flush=False)  # no-op, must not raise
        assert t.state_key == "counter_delta"

    def test_identity_uses_unit_separator(self):
        """The identity string is JSON-safe (round-trips through json.dumps)."""
        import json
        t = _make_transform()
        t.set_runtime_meta({"source_host": "h1"})
        t.apply([_record(1000)])
        blob = t.get_state()
        assert json.dumps(blob)  # serializable
        assert json.loads(json.dumps(blob)) == blob