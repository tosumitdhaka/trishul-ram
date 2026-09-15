"""Tests for the window_aggregate transform (design F.1 §5, §10 window subset).

Covers: epoch-aligned UTC windows, watermark-driven finalization,
late-within/after-lateness handling, state-carried open windows across ticks,
stream-close flush vs batch-close no-flush, the flush run, group_by isolation,
and accumulator-not-samples state.

Flush semantics note: batch ticks pass ``close(flush=False)`` (open windows
stay in state), a manual flush run passes ``True`` (authoritative over the
field), and a stream's graceful stop honors each transform's
``flush_on_close`` field (``true`` → partials emitted and state cleared;
``false``/unset → open windows stay in state). A stream crash never flushes —
the open windows stay in the saved state for a redispatch to continue.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from tram.core.exceptions import TransformError
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml
from tram.pipeline.state_store import DbTransformStateStore
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


# ── Window alignment (epoch-aligned UTC) ────────────────────────────────────


class TestAlignment:
    def test_window_alignment_epoch_utc(self):
        """A 09:47:12 sample lands in the 09:45–10:00 window."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:47:12+00:00")])
        blob = t.get_state()
        windows = blob["windows"]
        assert len(windows) == 1
        group_windows = next(iter(windows.values()))
        # Absolute epoch-aligned boundaries: 09:45:00Z start, 10:00:00Z end.
        assert list(group_windows) == [_epoch("2026-09-16T10:00:00+00:00")]
        entry = group_windows[_epoch("2026-09-16T10:00:00+00:00")]
        assert entry["start"] == _epoch("2026-09-16T09:45:00+00:00")
        assert entry["end"] == _epoch("2026-09-16T10:00:00+00:00")

    def test_window_boundary_record_belongs_to_later_window(self):
        """A record exactly at 09:15:00 belongs to 09:15–09:30, not 09:00–09:15."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:15:00+00:00")])
        group_windows = next(iter(t.get_state()["windows"].values()))
        assert list(group_windows) == [_epoch("2026-09-16T09:30:00+00:00")]


# ── Watermark / finalization ────────────────────────────────────────────────


class TestWatermarkFinalize:
    def test_finalize_on_watermark(self):
        """A record at window_end + lateness + ε closes the prior window."""
        t = _make_transform()
        out = t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        assert out == []  # accumulated, not emitted
        out = t.apply([_record(120.0, t="2026-09-16T09:16:01+00:00")])
        assert len(out) == 1
        rec = out[0]
        assert rec["window_start"] == "2026-09-16T09:00:00Z"
        assert rec["window_end"] == "2026-09-16T09:15:00Z"
        assert rec["window_complete"] is True
        assert rec["mean_rate"] == pytest.approx(100.0)
        assert rec["peak_rate"] == pytest.approx(100.0)
        assert rec["sample_count"] == 1
        # The trigger record itself is accumulating into the NEXT window.
        remaining = t.get_state()["windows"]
        assert list(next(iter(remaining.values()))) == [
            _epoch("2026-09-16T09:30:00+00:00")
        ]

    def test_late_within_lateness_included(self):
        """An out-of-order record still inside an open window is included."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        t.apply([_record(150.0, t="2026-09-16T09:14:50+00:00")])
        # Out-of-order arrival within the open window's lateness bound.
        t.apply([_record(110.0, t="2026-09-16T09:14:40+00:00")])
        out = t.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])
        assert len(out) == 1
        assert out[0]["sample_count"] == 3
        assert out[0]["mean_rate"] == pytest.approx(120.0)
        assert out[0]["window_complete"] is True

    def test_late_after_finalize_dropped_and_counted(self):
        """A record for a finalized window is dropped and counted."""
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        t.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])  # finalizes
        with patch("tram.metrics.registry.TRANSFORM_WINDOW_LATE_DROPPED_TOTAL") as late:
            out = t.apply([_record(50.0, t="2026-09-16T09:14:50+00:00")])
            assert out == []  # dropped
            late.labels.assert_called_once_with(pipeline="wa-pipe")
            late.labels.return_value.inc.assert_called_once()
        # The late record does not advance the watermark.
        blob = t.get_state()
        assert blob["max_ts"] == pytest.approx(
            datetime.fromisoformat("2026-09-16T09:16:01+00:00").timestamp()
        )

    def test_missing_timestamp_raises(self):
        t = _make_transform()
        with pytest.raises(TransformError, match="timestamp"):
            t.apply([{"rate": 1.0, "_index": "1"}])

    def test_gnmi_timestamp_candidate(self):
        """gNMI-style nanosecond epoch timestamps parse via the 'timestamp' candidate."""
        t = _make_transform(timestamp_field=["_polled_at", "timestamp"])
        ts = int(datetime(2026, 9, 16, 9, 10, 0, tzinfo=UTC).timestamp() * 1e9)
        t.apply([{"rate": 100.0, "_index": "1", "timestamp": ts}])
        out = t.apply([
            {"rate": 200.0, "_index": "1",
             "timestamp": ts + int(366 * 1e9)}  # 09:16:06 → watermark 09:15:06
        ])
        assert len(out) == 1
        assert out[0]["window_start"] == "2026-09-16T09:00:00Z"


# ── Group isolation ─────────────────────────────────────────────────────────


class TestGroupBy:
    def test_group_by_isolation(self):
        """Two groups accumulate and finalize independently.

        Multi-character values (``ne-01``/``ne-10``): single-character values
        could mask character-level corruption of the emitted group field.
        """
        t = _make_transform()
        t.apply([
            _record(100.0, t="2026-09-16T09:10:00+00:00", index="ne-01"),
            _record(300.0, t="2026-09-16T09:10:00+00:00", index="ne-10"),
        ])
        out = t.apply([
            _record(110.0, t="2026-09-16T09:16:01+00:00", index="ne-01"),
            _record(310.0, t="2026-09-16T09:16:02+00:00", index="ne-10"),
        ])
        by_index = {r["_index"]: r for r in out}
        assert set(by_index) == {"ne-01", "ne-10"}
        assert by_index["ne-01"]["mean_rate"] == pytest.approx(100.0)
        assert by_index["ne-10"]["mean_rate"] == pytest.approx(300.0)
        assert by_index["ne-01"]["sample_count"] == 1
        assert by_index["ne-10"]["sample_count"] == 1

    def test_multi_field_emission_exact_values(self):
        """Two group fields emit their EXACT values — the key's join separator
        or character-splitting must never leak into the output fields."""
        t = _make_transform(group_by=["_index", "_labels.ifDescr"])
        t.apply([
            {
                "rate": 100.0, "_index": "10",
                "_labels": {"ifDescr": "eth0/1"},
                "_polled_at": "2026-09-16T09:10:00+00:00",
            }
        ])
        out = t.apply([
            {
                "rate": 200.0, "_index": "10",
                "_labels": {"ifDescr": "eth0/1"},
                "_polled_at": "2026-09-16T09:16:01+00:00",
            }
        ])
        assert len(out) == 1
        assert out[0]["_index"] == "10"
        assert out[0]["_labels.ifDescr"] == "eth0/1"
        assert out[0]["mean_rate"] == pytest.approx(100.0)

    def test_close_flush_emits_exact_group_values(self):
        """The close(flush=True) partial path emits exact multi-field values
        too (it shares the emit-from-entry group_values fix)."""
        t = _make_transform(group_by=["_index", "_labels.ifDescr"])
        t.apply([
            {
                "rate": 100.0, "_index": "10",
                "_labels": {"ifDescr": "eth0/1"},
                "_polled_at": "2026-09-16T09:10:00+00:00",
            }
        ])
        partials = t.close(flush=True)
        assert len(partials) == 1
        assert partials[0]["_index"] == "10"
        assert partials[0]["_labels.ifDescr"] == "eth0/1"
        assert partials[0]["sample_count"] == 1

    def test_group_by_empty_is_single_global_group(self):
        t = _make_transform(group_by=[])
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        out = t.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])
        assert len(out) == 1
        assert out[0]["mean_rate"] == pytest.approx(100.0)


# ── Accumulators, not samples ───────────────────────────────────────────────


class TestAccumulatorState:
    def test_accumulator_not_samples(self):
        """State keeps op-relevant running values only — never the raw samples."""
        t = _make_transform()
        for i, ts in enumerate(
            ["2026-09-16T09:10:00+00:00", "2026-09-16T09:11:00+00:00",
             "2026-09-16T09:12:00+00:00"]
        ):
            t.apply([_record(100.0 + i, t=ts)])
        blob = t.get_state()
        assert json.dumps(blob)  # JSON-safe
        group_windows = next(iter(blob["windows"].values()))
        entry = next(iter(group_windows.values()))
        acc = entry["acc"]
        assert entry["sample_count"] == 3
        # avg stores {sum, count}, max stores a scalar, last stores a scalar.
        assert acc["mean_rate"] == {"sum": pytest.approx(303.0), "count": 3}
        assert acc["peak_rate"] == {"value": pytest.approx(102.0), "seen": True}
        assert acc["last_rate"] == {"value": pytest.approx(102.0)}
        # No raw-sample list anywhere in the blob.
        raw = json.dumps(blob)
        assert "sample_count" in raw
        # Accumulating more samples keeps the same shape (no growth per sample).
        size_3 = len(raw)
        for i in range(10):
            t.apply([_record(200.0 + i, t="2026-09-16T09:13:00+00:00")])
        size_13 = len(json.dumps(t.get_state()))
        assert size_13 < size_3 + 200  # O(groups × ops), not O(samples)

    def test_first_op_keeps_first_value(self):
        t = _make_transform(operations={"first_rate": "first:rate"})
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        t.apply([_record(200.0, t="2026-09-16T09:11:00+00:00")])
        out = t.apply([_record(300.0, t="2026-09-16T09:16:01+00:00")])
        assert out[0]["first_rate"] == pytest.approx(100.0)

    def test_last_skips_missing_field_value(self):
        """A missing field between two present records must not clobber 'last'."""
        t = _make_transform(operations={"last_rate": "last:rate"})
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        t.apply([{"rate": None, "_index": "1",
                  "_polled_at": "2026-09-16T09:11:00+00:00"}])
        t.apply([_record(300.0, t="2026-09-16T09:12:00+00:00")])
        out = t.apply([_record(400.0, t="2026-09-16T09:16:01+00:00")])
        assert out[0]["last_rate"] == pytest.approx(300.0)

    def test_first_skips_leading_missing_field_value(self):
        """A leading missing field must not poison 'first' with None."""
        t = _make_transform(operations={"first_rate": "first:rate"})
        t.apply([{"rate": None, "_index": "1",
                  "_polled_at": "2026-09-16T09:10:00+00:00"}])
        t.apply([_record(200.0, t="2026-09-16T09:11:00+00:00")])
        out = t.apply([_record(300.0, t="2026-09-16T09:16:01+00:00")])
        assert out[0]["first_rate"] == pytest.approx(200.0)

    def test_count_op_counts_non_null(self):
        t = _make_transform(operations={"samples": "count:rate"})
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        t.apply([{"rate": None, "_index": "1", "_polled_at": "2026-09-16T09:11:00+00:00"}])
        out = t.apply([_record(300.0, t="2026-09-16T09:16:01+00:00")])
        assert out[0]["samples"] == 1
        assert out[0]["sample_count"] == 2


# ── Stateful contract / state round-trip ────────────────────────────────────


class TestStatefulContract:
    def test_state_roundtrip(self):
        t1 = _make_transform()
        t1.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        blob = t1.get_state()
        t2 = _make_transform()
        t2.set_state(blob)
        out = t2.apply([_record(200.0, t="2026-09-16T09:16:01+00:00")])
        assert out[0]["mean_rate"] == pytest.approx(100.0)
        assert out[0]["sample_count"] == 1

    def test_set_state_missing_keys_is_empty(self):
        t = _make_transform()
        t.set_state(None)
        assert t.get_state() == {"max_ts": None, "windows": {}}

    def test_close_flush_false_is_noop(self):
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        assert t.close(flush=False) == []
        assert t.get_state()["windows"]  # window still open

    def test_close_flush_true_emits_partials_and_clears(self):
        t = _make_transform()
        t.apply([_record(100.0, t="2026-09-16T09:10:00+00:00")])
        with patch("tram.metrics.registry.TRANSFORM_WINDOWS_EMITTED_TOTAL") as emitted:
            partials = t.close(flush=True)
            assert len(partials) == 1
            assert partials[0]["window_complete"] is False
            assert partials[0]["mean_rate"] == pytest.approx(100.0)
            emitted.labels.assert_called_once_with(pipeline="wa-pipe", complete="partial")
        # State cleared — a rehydration never re-emits the partials.
        assert t.get_state()["windows"] == {}

    def test_config_validation(self):
        with pytest.raises(TransformError, match="window_seconds"):
            _make_transform(window_seconds=0)
        with pytest.raises(TransformError, match="allowed_lateness_seconds"):
            _make_transform(allowed_lateness_seconds=-1)
        with pytest.raises(TransformError, match="operations"):
            _make_transform(operations={})
        with pytest.raises(TransformError, match="timestamp_field"):
            _make_transform(timestamp_field=[])
        with pytest.raises(TransformError, match="unsupported operation"):
            _make_transform(operations={"bad": "median:rate"})


# ── Executor integration: state across ticks, close semantics, flush run ───


_PIPELINE_YAML = """\
pipeline:
  name: wa-test
  source:
    type: local
    path: /tmp/in
  serializer_in:
    type: json
  sinks:
    - type: local
      path: /tmp/out
  transforms:
    - type: window_aggregate
      window_seconds: 900
      allowed_lateness_seconds: 60
      timestamp_field: [_polled_at, timestamp]
      group_by: [_index]
      operations:
        mean_rate: "avg:rate"
        peak_rate: "max:rate"
{extra}
"""


def _pipeline_yaml(extra: str = "") -> str:
    # extra is injected INSIDE the window_aggregate transform block, so it must
    # be indented at the transform-field level (6 spaces).
    return textwrap.dedent(_PIPELINE_YAML).format(extra=extra)


def _sha(yaml_text: str) -> str:
    return hashlib.sha256(yaml_text.encode()).hexdigest()[:16]


def _record_for_wa(rate: float, t: str) -> dict:
    return {"rate": rate, "_index": "1", "_polled_at": t}


class _Harness:
    """Batch/stream runner with mocked I/O and a REAL transform build."""

    def __init__(self, store, yaml_text: str):
        self.yaml_text = yaml_text
        self.config = load_pipeline_from_yaml(yaml_text)
        self.config_sha = _sha(yaml_text)
        self.executor = PipelineExecutor(state_store=store)

    def _mocks(self):
        sink = MagicMock()
        ser_in = MagicMock()
        ser_out = MagicMock()
        ser_out.serialize.side_effect = lambda recs: json.dumps(recs).encode()
        return sink, ser_in, ser_out

    def batch(self, records, run_id="r1", config_sha=None, flush=False):
        sink, ser_in, ser_out = self._mocks()
        ser_in.parse.return_value = records
        mock_source = MagicMock()
        mock_source.read.return_value = iter(
            [(json.dumps(records).encode(), {"source_host": "h1"})]
        )
        with patch.object(self.executor, "_build_source", return_value=mock_source), \
             patch.object(self.executor, "_build_sinks", return_value=[(sink, None, [])]), \
             patch.object(self.executor, "_build_serializer_in", return_value=ser_in), \
             patch.object(self.executor, "_build_serializer_out", return_value=ser_out):
            result = self.executor.batch_run(
                self.config, run_id=run_id,
                config_sha256=config_sha if config_sha is not None else self.config_sha,
                flush=flush,
            )
        return result, ser_out

    def stream(self, records, run_id="r-s1", config_sha=None):
        import threading
        sink, ser_in, ser_out = self._mocks()
        ser_in.parse.side_effect = lambda raw: json.loads(raw)

        def _gen():
            for rec in records:
                yield json.dumps([rec]).encode(), {"source_host": "h1"}

        mock_source = MagicMock()
        mock_source.read.return_value = iter(_gen())
        with patch.object(self.executor, "_build_source", return_value=mock_source), \
             patch.object(self.executor, "_build_sinks", return_value=[(sink, None, [])]), \
             patch.object(self.executor, "_build_serializer_in", return_value=ser_in), \
             patch.object(self.executor, "_build_serializer_out", return_value=ser_out):
            self.executor.stream_run(
                self.config, threading.Event(),
                config_sha256=config_sha if config_sha is not None else self.config_sha,
            )
        return ser_out


def _sink_records(mock_ser_out) -> list[dict]:
    if mock_ser_out.serialize.call_args is None:
        return []
    return mock_ser_out.serialize.call_args[0][0]


class TestExecutorIntegration:
    def test_state_carries_open_windows_across_ticks(self, tmp_path):
        """Two batch runs: samples from both ticks land in one emitted window."""
        from tram.persistence.db import TramDB
        from tram.pipeline.state_store import DbTransformStateStore

        db = TramDB(url=f"sqlite:///{tmp_path}/wa-ticks.db")
        store = DbTransformStateStore(db)
        yaml_text = _pipeline_yaml()

        h1 = _Harness(store, yaml_text)
        result1, ser1 = h1.batch([_record_for_wa(100.0, "2026-09-16T09:10:00+00:00")])
        assert result1.status.value == "success"
        assert _sink_records(ser1) == []  # accumulated, nothing emitted

        # Tick 2: fresh executor + transforms, hydrated from tick 1's state.
        h2 = _Harness(store, yaml_text)
        result2, ser2 = h2.batch([
            _record_for_wa(150.0, "2026-09-16T09:11:00+00:00"),
            _record_for_wa(200.0, "2026-09-16T09:16:01+00:00"),  # finalizes
        ])
        assert result2.status.value == "success"
        emitted = _sink_records(ser2)
        assert len(emitted) == 1
        assert emitted[0]["window_complete"] is True
        # The 09:16:01 record finalizes the window but belongs to the next one.
        assert emitted[0]["sample_count"] == 2
        assert emitted[0]["mean_rate"] == pytest.approx(125.0)

    def test_batch_close_does_not_flush(self, tmp_path):
        """Default batch runs never emit partials — no double emission."""
        from tram.persistence.db import TramDB
        from tram.pipeline.state_store import DbTransformStateStore

        db = TramDB(url=f"sqlite:///{tmp_path}/wa-nflush.db")
        store = DbTransformStateStore(db)
        yaml_text = _pipeline_yaml()  # flush_on_close default false

        h1 = _Harness(store, yaml_text)
        result1, ser1 = h1.batch([_record_for_wa(100.0, "2026-09-16T09:10:00+00:00")])
        assert _sink_records(ser1) == []  # no partial emitted per tick

        # Rehydrated tick: the same window is still open and emits exactly once.
        h2 = _Harness(store, yaml_text)
        result2, ser2 = h2.batch([
            _record_for_wa(150.0, "2026-09-16T09:11:00+00:00"),
            _record_for_wa(200.0, "2026-09-16T09:16:01+00:00"),
        ])
        emitted = _sink_records(ser2)
        assert len(emitted) == 1
        assert emitted[0]["window_complete"] is True
        assert emitted[0]["sample_count"] == 2

    def test_flush_run_emits_and_clears(self, tmp_path):
        """A ?flush=true manual run emits partials and clears the saved state."""
        from tram.persistence.db import TramDB
        from tram.pipeline.state_store import DbTransformStateStore

        db = TramDB(url=f"sqlite:///{tmp_path}/wa-flush.db")
        store = DbTransformStateStore(db)
        yaml_text = _pipeline_yaml()

        # Tick 1: leave a window open in state.
        h1 = _Harness(store, yaml_text)
        h1.batch([_record_for_wa(100.0, "2026-09-16T09:10:00+00:00")])
        assert db.load_transform_state("wa-test")["state"]["window_aggregate:0"]["windows"]

        # Flush run: normal run + close(flush=True) → partial emitted, state cleared.
        h2 = _Harness(store, yaml_text)
        result, ser = h2.batch(
            [_record_for_wa(120.0, "2026-09-16T09:11:00+00:00")], run_id="r-flush",
            flush=True,
        )
        assert result.status.value == "success"
        emitted = _sink_records(ser)
        assert len(emitted) == 1
        assert emitted[0]["window_complete"] is False
        assert emitted[0]["sample_count"] == 2

        # Saved state has the windows cleared — no double emission later.
        saved = db.load_transform_state("wa-test")["state"]["window_aggregate:0"]
        assert saved["windows"] == {}

    def test_flush_run_no_double_emit(self, tmp_path):
        """After a flush run, a later run cannot re-emit the cleared windows."""
        from tram.persistence.db import TramDB
        from tram.pipeline.state_store import DbTransformStateStore

        db = TramDB(url=f"sqlite:///{tmp_path}/wa-nodouble.db")
        store = DbTransformStateStore(db)
        yaml_text = _pipeline_yaml()

        h1 = _Harness(store, yaml_text)
        h1.batch([_record_for_wa(100.0, "2026-09-16T09:10:00+00:00")])
        h2 = _Harness(store, yaml_text)
        _, ser_flush = h2.batch([], run_id="r-flush", flush=True)
        assert len(_sink_records(ser_flush)) == 1  # the partial

        # A fresh run hydrates cleared state; the old window emits nothing.
        h3 = _Harness(store, yaml_text)
        _, ser3 = h3.batch([_record_for_wa(300.0, "2026-09-16T09:20:00+00:00")])
        assert _sink_records(ser3) == []

    def test_stream_close_flushes_partial(self, tmp_path):
        """Stream flush_on_close: true emits the open window on graceful stop."""
        from tram.persistence.db import TramDB

        db = TramDB(url=f"sqlite:///{tmp_path}/wa-stream.db")
        store = DbTransformStateStore(db)
        yaml_text = _pipeline_yaml("      flush_on_close: true\n")

        h = _Harness(store, yaml_text)
        ser = h.stream([
            _record_for_wa(100.0, "2026-09-16T09:10:00+00:00"),
            _record_for_wa(120.0, "2026-09-16T09:11:00+00:00"),
        ])
        records = _sink_records(ser)
        assert len(records) == 1
        assert records[0]["window_complete"] is False
        assert records[0]["sample_count"] == 2
        # Final state PUT reflects the cleared windows.
        saved = db.load_transform_state("wa-test")["state"]["window_aggregate:0"]
        assert saved["windows"] == {}

    def test_stream_flush_on_close_false_keeps_windows(self, tmp_path):
        """A stream with flush_on_close: false does NOT emit partials on a
        graceful stop — the open window stays in the saved state."""
        from tram.persistence.db import TramDB

        db = TramDB(url=f"sqlite:///{tmp_path}/wa-nofoc.db")
        store = DbTransformStateStore(db)
        yaml_text = _pipeline_yaml("      flush_on_close: false\n")

        h = _Harness(store, yaml_text)
        ser = h.stream([
            _record_for_wa(100.0, "2026-09-16T09:10:00+00:00"),
            _record_for_wa(120.0, "2026-09-16T09:11:00+00:00"),
        ])
        assert _sink_records(ser) == []  # no partial emission on graceful stop
        saved = db.load_transform_state("wa-test")["state"]["window_aggregate:0"]
        assert saved["windows"]  # the open window is retained in state

    def test_stream_crash_keeps_windows_no_partials(self, tmp_path):
        """A stream that crashes mid-window never emits partials; the open
        window stays in the saved state so a redispatch continues it (D.2
        snapshot recovery)."""
        from tram.persistence.db import TramDB

        db = TramDB(url=f"sqlite:///{tmp_path}/wa-crash.db")
        store = DbTransformStateStore(db)
        h = _Harness(store, _pipeline_yaml("      flush_on_close: true\n"))

        def _gen():
            yield json.dumps([_record_for_wa(100.0, "2026-09-16T09:10:00+00:00")]).encode(), \
                {"source_host": "h1"}
            yield json.dumps([_record_for_wa(120.0, "2026-09-16T09:11:00+00:00")]).encode(), \
                {"source_host": "h1"}
            raise RuntimeError("source exploded mid-window")

        sink, ser_in, ser_out = h._mocks()
        ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_source = MagicMock()
        mock_source.read.return_value = iter(_gen())
        with patch.object(h.executor, "_build_source", return_value=mock_source), \
             patch.object(h.executor, "_build_sinks", return_value=[(sink, None, [])]), \
             patch.object(h.executor, "_build_serializer_in", return_value=ser_in), \
             patch.object(h.executor, "_build_serializer_out", return_value=ser_out):
            with pytest.raises(RuntimeError, match="source exploded"):
                h.executor.stream_run(
                    h.config, threading.Event(),
                    config_sha256=h.config_sha,
                )
        # No partial emission — the sink never received the open window.
        assert _sink_records(ser_out) == []
        # The open window stays in the saved state for a redispatch.
        saved = db.load_transform_state("wa-test")["state"]["window_aggregate:0"]
        assert saved["windows"]

    def test_state_key_set_by_executor(self, tmp_path):
        """The executor assigns 'window_aggregate:0' as the blob key."""
        from tram.persistence.db import TramDB

        db = TramDB(url=f"sqlite:///{tmp_path}/wa-key.db")
        store = DbTransformStateStore(db)
        h = _Harness(store, _pipeline_yaml())
        h.batch([_record_for_wa(100.0, "2026-09-16T09:10:00+00:00")])
        row = db.load_transform_state("wa-test")
        assert "window_aggregate:0" in row["state"]