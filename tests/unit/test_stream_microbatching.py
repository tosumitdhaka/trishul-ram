"""Issue #78 — stream sink micro-batching.

Pins the micro-batch flush design:
* record-threshold and flush-interval triggers, both configurable (env
  defaults + per-pipeline overrides);
* sink-write reuse: one serialization + one sink write per flush (the batch
  executor's sink path), never per record;
* crash-window semantics: buffered-but-unflushed records are flushed at
  graceful stop and surfaced best-effort on crash (no silent loss); the kafka
  ``source_batch_end`` marker orders the flush before the offset commit;
* Wave-1 semantics preserved across flush boundaries: the local-sink part cap
  (GH #77) is consumed per flush and counters stay in − out (GH #84);
* webhook ingress stays decoupled from the flush cadence (202-path enqueues
  never wait on a sink write).
"""
from __future__ import annotations

import json
import queue
import sys
import textwrap
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tram.agent.metrics import PipelineStats
from tram.core.context import PipelineRunContext
from tram.core.exceptions import ConfigError
from tram.pipeline.executor import (
    PipelineExecutor,
    _StreamFlushBuffer,
)
from tram.pipeline.loader import load_pipeline_from_yaml
from tram.serializers.json_serializer import JsonSerializer

# ── Test doubles ──────────────────────────────────────────────────────────


class _RecordingSink:
    """Records every serialized payload written; optionally fails writes."""

    def __init__(self, position_source=None, fail: bool = False) -> None:
        self.payloads: list[bytes] = []
        self.positions: list[int] = []
        self._source = position_source
        self.fail = fail
        self.closed = False

    def write(self, data: bytes, meta: dict) -> None:
        if self.fail:
            raise RuntimeError("sink boom")
        self.payloads.append(data)
        if self._source is not None:
            self.positions.append(self._source.position)

    def close(self) -> None:
        self.closed = True


class _FiniteSource:
    """read() yields the given chunks once, then the generator ends."""

    def __init__(self, chunks: list[tuple[bytes, dict]]) -> None:
        self.chunks = chunks

    def read(self):
        yield from self.chunks


class _CrashSource:
    """read() yields the given chunks, then raises on the next iteration."""

    def __init__(self, chunks: list[tuple[bytes, dict]], error: str = "source exploded") -> None:
        self.chunks = chunks
        self.error = error

    def read(self):
        yield from self.chunks
        raise RuntimeError(self.error)


class _ControllableSource:
    """read() blocks on an internal queue until chunks are fed in by the test."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._sentinel = object()

    def read(self):
        while True:
            item = self._q.get()
            if item is self._sentinel:
                return
            yield item

    def send(self, raw: bytes, meta: dict | None = None) -> None:
        self._q.put((raw, meta or {}))

    def finish(self) -> None:
        self._q.put(self._sentinel)


class _PositionTrackingSource:
    """Tracks how far the executor has advanced the generator.

    ``position`` is the index of the message the generator is currently
    suspended at — the commit of a kafka poll batch fires only when the
    executor resumes the generator past its last message, so the position seen
    by the sink at flush time pins commit-after-flush ordering (GH #78).
    """

    def __init__(self, chunks: list[tuple[bytes, dict]]) -> None:
        self.chunks = chunks
        # The index of the message the generator is currently suspended at
        # (-1 before the first message is yielded).
        self.position = -1
        self.yield_positions: list[int] = []

    def read(self):
        for i, (raw, meta) in enumerate(self.chunks):
            self.position = i
            self.yield_positions.append(i)
            yield raw, meta


class _FakeClock:
    """Stand-in for time.monotonic that the test advances deterministically."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _NoOverlapSink(_RecordingSink):
    """Flags when two sink.write() calls overlap in time (finding 1).

    The *gate* blocks every write until the test releases it: the interval
    flusher drains the first batch and parks mid-write, then the loop thread's
    threshold flush drains a second batch and parks too — the two flushes are
    then deterministically concurrent, and any overlap is recorded.
    """

    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self.gate = gate
        self.entered = threading.Event()
        self._active = 0
        self._entries = 0
        self._state_lock = threading.Lock()
        self.overlap_seen = threading.Event()

    @property
    def entered_count(self) -> int:
        with self._state_lock:
            return self._entries

    def write(self, data: bytes, meta: dict) -> None:
        with self._state_lock:
            self._active += 1
            self._entries += 1
            if self._active > 1:
                self.overlap_seen.set()
        self.entered.set()
        self.gate.wait(5.0)
        try:
            super().write(data, meta)
        finally:
            with self._state_lock:
                self._active -= 1


class _PreemptedCounterDict(dict):
    """Simulates OS preemption at the exact instruction boundary that matters.

    ``RollingWriter._next_path`` is an unlocked read-modify-write on
    ``_part_counters``: it reads the current index, then writes it back. The
    first reader here parks (with a test-visible signal) right after its read,
    so a concurrent flush reads the SAME pre-store index and both flushes pick
    the same ``{part}`` path — silently overwriting each other — unless the
    executor's flush lock serializes them (finding 1).
    """

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.preempted = threading.Event()

    def get(self, key, default=None):
        value = super().get(key, default)
        self.reads += 1
        if self.reads == 1:
            # Park the first reader mid-read-modify-write (reviewer's "forced
            # preemption"); the test feeds more records while we are parked.
            self.preempted.set()
            time.sleep(0.4)
        return value


class _SlowFlushTrackingSink(_RecordingSink):
    """Sink whose write is slower than the old 2s flusher-join cap; flags if
    close() ever runs while a write is still in flight (finding 4)."""

    def __init__(self, write_delay: float = 2.5) -> None:
        super().__init__()
        self.write_delay = write_delay
        self._inflight = 0
        self._state_lock = threading.Lock()
        self.closed_while_active = threading.Event()

    def write(self, data: bytes, meta: dict) -> None:
        with self._state_lock:
            self._inflight += 1
        try:
            super().write(data, meta)
            time.sleep(self.write_delay)
        finally:
            with self._state_lock:
                self._inflight -= 1

    @property
    def inflight(self) -> int:
        with self._state_lock:
            return self._inflight

    def close(self) -> None:
        if self.inflight > 0:
            self.closed_while_active.set()
        super().close()


class _FinalizeOrderSource:
    """Records every finalize() call in a shared log (finding 3)."""

    def __init__(self, chunks: list[tuple[bytes, dict]], log: list) -> None:
        self.chunks = chunks
        self.log = log

    def read(self):
        yield from self.chunks

    def finalize(self, meta: dict, success: bool = True) -> None:
        self.log.append(("finalize", meta.get("source_path", "")))


class _WriteOrderSink(_RecordingSink):
    """Records every write() call in a shared log (finding 3)."""

    def __init__(self, log: list) -> None:
        super().__init__()
        self.log = log

    def write(self, data: bytes, meta: dict) -> None:
        self.log.append(("write", meta.get("source_path", "")))
        super().write(data, meta)


# ── Helpers ───────────────────────────────────────────────────────────────


def _stream_config(extra_yaml: str = "") -> object:
    yaml_text = textwrap.dedent(f"""
        pipeline:
          name: micro-stream
          schedule:
            type: stream
          source:
            type: webhook
            path: /ingest
          serializer_in:
            type: json
          serializer_out:
            type: json
          sinks:
            - type: local
              path: /tmp/out
          {extra_yaml}
    """)
    return load_pipeline_from_yaml(yaml_text)


def _chunk(records: list[dict]) -> bytes:
    return json.dumps(records).encode()


def _run_finite_stream(config, chunks: list[tuple[bytes, dict]], *, source=None,
                       sink=None, stats=None) -> tuple[PipelineStats, _RecordingSink]:
    """Run a single-threaded stream over a finite source to completion.

    Only the source/sink builders are patched — the serializers and transforms
    are built from the config by the executor itself.
    """
    executor = PipelineExecutor()
    source = source or _FiniteSource(chunks)
    sink = sink or _RecordingSink()
    stats = stats or PipelineStats(
        run_id="r1", pipeline_name=config.name, schedule_type="stream"
    )
    with (
        patch.object(executor, "_build_source", return_value=source),
        patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
    ):
        executor.stream_run(config, threading.Event(), stats=stats)
    return stats, sink


def _wait_until(cond, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def _webhook_queue(path: str):
    from tram.connectors.webhook.source import _REGISTRY_LOCK, _WEBHOOK_REGISTRY

    with _REGISTRY_LOCK:
        return _WEBHOOK_REGISTRY.get(path)


# ── Flush triggers ────────────────────────────────────────────────────────


class TestFlushTriggers:
    def test_record_threshold_flush_serializes_once_per_flush(self):
        """4 records, threshold 2 → exactly 2 flushes, one serialization +
        sink write each, 2 records per payload. The batch sink path is reused
        (never a per-record write)."""
        config = _stream_config("stream_flush_records: 2")
        chunks = [(_chunk([{"seq": i}]), {}) for i in range(4)]
        stats, sink = _run_finite_stream(config, chunks)

        assert len(sink.payloads) == 2
        assert [json.loads(p) for p in sink.payloads] == [[{"seq": 0}, {"seq": 1}],
                                                          [{"seq": 2}, {"seq": 3}]]
        assert stats.snapshot()["records_in"] == 4
        assert stats.snapshot()["records_out"] == 4
        assert stats.snapshot()["records_skipped"] == 0

    def test_record_threshold_defaults_from_env(self, monkeypatch):
        """The env default (TRAM_STREAM_FLUSH_RECORDS) is used when the
        pipeline does not override it."""
        monkeypatch.setenv("TRAM_STREAM_FLUSH_RECORDS", "2")
        config = _stream_config()
        chunks = [(_chunk([{"seq": i}]), {}) for i in range(4)]
        _stats, sink = _run_finite_stream(config, chunks)
        assert [len(json.loads(p)) for p in sink.payloads] == [2, 2]

    def test_pipeline_override_beats_env_default(self, monkeypatch):
        """Per-pipeline stream_flush_records wins over the env default."""
        monkeypatch.setenv("TRAM_STREAM_FLUSH_RECORDS", "1000")
        config = _stream_config("stream_flush_records: 2")
        chunks = [(_chunk([{"seq": i}]), {}) for i in range(4)]
        _stats, sink = _run_finite_stream(config, chunks)
        assert [len(json.loads(p)) for p in sink.payloads] == [2, 2]

    def test_interval_trigger_with_partial_buffer(self):
        """A chunk arriving after the flush interval has elapsed flushes the
        partial buffer (bounded latency), even below the record threshold."""
        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="micro-stream")
        ser_in = JsonSerializer({})
        ser_out = JsonSerializer({})
        sink = _RecordingSink()
        buffer = _StreamFlushBuffer(record_threshold=100, interval_s=1.0)
        clock = _FakeClock(1000.0)

        with patch("tram.pipeline.executor.time.monotonic", clock):
            assert executor._process_chunk(
                _chunk([{"seq": 0}]), {}, ser_in, [], ser_out,
                [(sink, None, [])], ctx, "continue", flush_buffer=buffer,
            ) is False  # below threshold, interval not yet due
            clock.advance(1.5)
            assert executor._process_chunk(
                _chunk([{"seq": 1}]), {}, ser_in, [], ser_out,
                [(sink, None, [])], ctx, "continue", flush_buffer=buffer,
            ) is True  # interval due → flush now
            executor._flush_stream_buffer(
                buffer, ser_out, [(sink, None, [])], ctx, "continue",
                None, None, False, None, None,
            )

        assert len(sink.payloads) == 1
        assert json.loads(sink.payloads[0]) == [{"seq": 0}, {"seq": 1}]

    def test_interval_timer_flushes_quiet_partial_buffer(self):
        """The per-run interval timer flushes a partial buffer even when the
        source goes quiet below the record threshold (true bounded latency)."""
        config = _stream_config(
            "stream_flush_records: 100\n"
            "          stream_flush_interval_s: 0.2"
        )
        executor = PipelineExecutor()
        source = _ControllableSource()
        sink = _RecordingSink()
        stats = PipelineStats(run_id="r2", pipeline_name=config.name, schedule_type="stream")
        stop_event = threading.Event()

        with (
            patch.object(executor, "_build_source", return_value=source),
            patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
        ):
            t = threading.Thread(target=executor.stream_run, args=(config, stop_event, stats))
            t.start()
            try:
                source.send(_chunk([{"seq": 0}]))
                source.send(_chunk([{"seq": 1}]))
                # No further chunks: the flusher must fire on its own.
                assert _wait_until(lambda: len(sink.payloads) == 1, timeout=3.0)
                assert json.loads(sink.payloads[0]) == [{"seq": 0}, {"seq": 1}]
            finally:
                stop_event.set()
                source.finish()
                t.join(timeout=5.0)

        assert stats.snapshot()["records_in"] == 2
        assert stats.snapshot()["records_out"] == 2

    def test_batch_end_flush_precedes_generator_advance(self):
        """At-least-once ordering: the flush triggered by ``source_batch_end``
        happens while the generator is still suspended at the batch-end message
        — a replayable source's offset commit (which fires only on the next
        generator advance) can never precede the flush of its records."""
        config = _stream_config(
            "stream_flush_records: 100\n"
            "          stream_flush_interval_s: 60"
        )
        source = _PositionTrackingSource([
            (_chunk([{"seq": 0}]), {}),
            (_chunk([{"seq": 1}]), {"source_batch_end": True}),
            (_chunk([{"seq": 2}]), {}),
        ])
        sink = _RecordingSink(position_source=source)
        _stats, sink = _run_finite_stream(config, chunks=[], source=source, sink=sink)

        # The batch-end flush wrote both batch messages in one write, while the
        # generator had yielded exactly through the batch-end message (position
        # 1, not 2+) — i.e. the commit on the next advance comes after the flush.
        assert len(sink.payloads) == 2
        assert json.loads(sink.payloads[0]) == [{"seq": 0}, {"seq": 1}]
        assert sink.positions[0] == 1
        # The trailing record was flushed by the graceful-stop drain.
        assert json.loads(sink.payloads[1]) == [{"seq": 2}]
        assert sink.positions[1] == 2

    def test_zero_interval_disables_interval_trigger(self):
        """Finding 5: ``stream_flush_interval_s: 0`` disables the interval
        trigger (the documented semantic) — it must NOT mean "flush every
        chunk". A huge interval elapse still does not flush; only the record
        threshold or batch-end triggers do."""
        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="micro-stream")
        ser_in = JsonSerializer({})
        ser_out = JsonSerializer({})
        sink = _RecordingSink()
        buffer = _StreamFlushBuffer(record_threshold=100, interval_s=0.0)
        clock = _FakeClock(1000.0)

        with patch("tram.pipeline.executor.time.monotonic", clock):
            assert executor._process_chunk(
                _chunk([{"seq": 0}]), {}, ser_in, [], ser_out,
                [(sink, None, [])], ctx, "continue", flush_buffer=buffer,
            ) is False
            clock.advance(60.0)
            assert executor._process_chunk(
                _chunk([{"seq": 1}]), {}, ser_in, [], ser_out,
                [(sink, None, [])], ctx, "continue", flush_buffer=buffer,
            ) is False
            assert buffer.due() is False

        assert sink.payloads == []

    def test_zero_interval_threshold_and_batch_end_still_flush(self):
        """Finding 5: with the interval disabled, the record threshold and the
        ``source_batch_end`` marker still trigger flushes."""
        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="micro-stream")
        ser_in = JsonSerializer({})
        ser_out = JsonSerializer({})
        sink = _RecordingSink()
        buffer = _StreamFlushBuffer(record_threshold=2, interval_s=0.0)

        with patch("tram.pipeline.executor.time.monotonic", _FakeClock(1000.0)):
            assert executor._process_chunk(
                _chunk([{"seq": 0}]), {}, ser_in, [], ser_out,
                [(sink, None, [])], ctx, "continue", flush_buffer=buffer,
            ) is False
            # Second record crosses the threshold → flush due.
            assert executor._process_chunk(
                _chunk([{"seq": 1}]), {}, ser_in, [], ser_out,
                [(sink, None, [])], ctx, "continue", flush_buffer=buffer,
            ) is True
            # batch-end marker also flushes with a zero interval.
            batch_buffer = _StreamFlushBuffer(record_threshold=100, interval_s=0.0)
            assert batch_buffer.append([{"seq": 9}], {}, batch_end=True) is True

        executor._flush_stream_buffer(
            buffer, ser_out, [(sink, None, [])], ctx, "continue",
            None, None, False, None, None,
        )
        assert json.loads(sink.payloads[0]) == [{"seq": 0}, {"seq": 1}]

    def test_zero_interval_no_time_based_flush_end_to_end(self):
        """Finding 5: a quiet source below the record threshold with a zero
        interval never flushes on its own (no per-message flushes, no flusher
        firing); the records are delivered by the graceful-stop drain."""
        config = _stream_config(
            "stream_flush_records: 100\n"
            "          stream_flush_interval_s: 0"
        )
        executor = PipelineExecutor()
        source = _ControllableSource()
        sink = _RecordingSink()
        stats = PipelineStats(run_id="r-iv0", pipeline_name=config.name, schedule_type="stream")
        stop_event = threading.Event()

        with (
            patch.object(executor, "_build_source", return_value=source),
            patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
        ):
            t = threading.Thread(target=executor.stream_run, args=(config, stop_event, stats))
            t.start()
            try:
                source.send(_chunk([{"seq": 0}]))
                source.send(_chunk([{"seq": 1}]))
                # Longer than any interval we would have flushed on if 0 meant
                # "immediate": nothing may be written yet.
                time.sleep(0.4)
                assert sink.payloads == []
            finally:
                stop_event.set()
                source.finish()
                t.join(timeout=5.0)

        assert json.loads(sink.payloads[0]) == [{"seq": 0}, {"seq": 1}]
        assert stats.snapshot()["records_out"] == 2


# ── Config plumbing ───────────────────────────────────────────────────────


class TestConfigPlumbing:
    def test_zero_record_threshold_rejected(self):
        with pytest.raises(ConfigError, match="stream_flush_records"):
            _stream_config("stream_flush_records: 0")

    def test_invalid_env_values_fall_back_to_defaults(self, monkeypatch):
        from tram.core.config import (
            stream_flush_interval_seconds,
            stream_flush_records,
        )

        monkeypatch.setenv("TRAM_STREAM_FLUSH_RECORDS", "banana")
        monkeypatch.setenv("TRAM_STREAM_FLUSH_INTERVAL_SECONDS", "also-banana")
        assert stream_flush_records() == 500
        assert stream_flush_interval_seconds() == 1.0

    def test_zero_env_record_threshold_falls_back_to_default(self, monkeypatch):
        """Finding 12b: TRAM_STREAM_FLUSH_RECORDS=0 is rejected at load the way
        the model rejects 0 (ge=1) — warning + fall back to the default, never
        a silent clamp to 1 (the surrounding env readers' invalid-value
        convention)."""
        from tram.core.config import stream_flush_records

        monkeypatch.setenv("TRAM_STREAM_FLUSH_RECORDS", "0")
        assert stream_flush_records() == 500

    def test_zero_env_interval_disables_interval_trigger(self, monkeypatch):
        """Finding 5: TRAM_STREAM_FLUSH_INTERVAL_SECONDS=0 stays valid and
        means the interval trigger is disabled."""
        from tram.core.config import stream_flush_interval_seconds

        monkeypatch.setenv("TRAM_STREAM_FLUSH_INTERVAL_SECONDS", "0")
        assert stream_flush_interval_seconds() == 0.0

    def test_model_defaults_and_override_fields(self):
        config = _stream_config()
        assert config.stream_flush_records is None
        assert config.stream_flush_interval_s is None
        config2 = _stream_config(
            "stream_flush_records: 3\n"
            "          stream_flush_interval_s: 0.5"
        )
        assert config2.stream_flush_records == 3
        assert config2.stream_flush_interval_s == 0.5


# ── Wave-1 semantics across flush boundaries (GH #77 / #84) ───────────────


class TestWave1SemanticsAcrossFlushes:
    def test_part_cap_consumed_per_flush_not_per_record(self, tmp_path):
        """GH #77: the local-sink part cap advances one part per flush; a
        past-cap flush raises exactly like a past-cap batch chunk (records
        skipped, error surfaced — the run never reports clean success)."""
        from tram.connectors.local.sink import LocalSink

        config = _stream_config("stream_flush_records: 3")
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "out_{part}.json",
            "file_mode": "single",
            "max_index": 1,
        })
        chunks = [(_chunk([{"seq": i}]), {}) for i in range(6)]
        stats, _ = _run_finite_stream(config, chunks, sink=sink)

        snap = stats.snapshot()
        assert snap["records_in"] == 6
        assert snap["records_out"] == 3  # first flush wrote part 1; second raised past cap
        assert snap["records_skipped"] == 3  # in − out, dropped records counted
        # One part file: the cap was consumed per flush, not per record.
        assert sorted(p.name for p in tmp_path.glob("out_*.json")) == ["out_1.json"]
        assert sink._writer.dropped_past_cap_total == 1
        assert any("max_index=1" in e for e in snap["errors_last_window"])

    def test_counters_stay_in_minus_out_across_flush_boundaries(self):
        """GH #84: with a failing sink the flush batches report
        skipped = in − out (not 2×), and the per-sink failure is surfaced."""
        config = _stream_config("stream_flush_records: 2")
        sink = _RecordingSink(fail=True)
        chunks = [(_chunk([{"seq": i}]), {}) for i in range(4)]
        stats, _ = _run_finite_stream(config, chunks, sink=sink)

        snap = stats.snapshot()
        assert snap["records_in"] == 4
        assert snap["records_out"] == 0
        assert snap["records_skipped"] == 4
        assert any("sink boom" in e for e in snap["errors_last_window"])

    def test_graceful_stop_drains_buffered_records(self):
        """Records below the threshold at natural end are flushed by the
        stop drain — never silently stranded (no silent loss)."""
        config = _stream_config(
            "stream_flush_records: 100\n"
            "          stream_flush_interval_s: 60"
        )
        chunks = [(_chunk([{"seq": i}]), {}) for i in range(3)]
        stats, sink = _run_finite_stream(config, chunks)

        assert len(sink.payloads) == 1
        assert json.loads(sink.payloads[0]) == [{"seq": 0}, {"seq": 1}, {"seq": 2}]
        assert stats.snapshot()["records_in"] == 3
        assert stats.snapshot()["records_out"] == 3

    def test_crash_drain_surfaces_buffered_records(self):
        """A source crash with a partial buffer still writes the buffered
        records (best-effort) — they are surfaced, never silently vanished."""
        config = _stream_config(
            "stream_flush_records: 100\n"
            "          stream_flush_interval_s: 60"
        )
        executor = PipelineExecutor()
        source = _CrashSource([(_chunk([{"seq": 0}]), {}), (_chunk([{"seq": 1}]), {})])
        sink = _RecordingSink()
        stats = PipelineStats(run_id="r3", pipeline_name=config.name, schedule_type="stream")

        with (
            patch.object(executor, "_build_source", return_value=source),
            patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
        ):
            with pytest.raises(RuntimeError, match="source exploded"):
                executor.stream_run(config, threading.Event(), stats=stats)

        assert len(sink.payloads) == 1
        assert json.loads(sink.payloads[0]) == [{"seq": 0}, {"seq": 1}]
        assert stats.snapshot()["records_out"] == 2


# ── Threaded stream path ──────────────────────────────────────────────────


class TestThreadedStreamPath:
    def test_threaded_stream_microbatches(self):
        """The threaded path (thread_workers > 1) also routes through the
        micro-batch buffer: 6 records at threshold 3 → 2 batched flushes."""
        config = _stream_config(
            "stream_flush_records: 3\n"
            "          stream_flush_interval_s: 60\n"
            "          thread_workers: 2"
        )
        chunks = [(_chunk([{"seq": i}]), {}) for i in range(6)]
        stats, sink = _run_finite_stream(config, chunks)

        snap = stats.snapshot()
        assert snap["records_in"] == 6
        assert snap["records_out"] == 6
        assert len(sink.payloads) == 2
        assert sum(len(json.loads(p)) for p in sink.payloads) == 6

    def test_threaded_finalize_happens_after_records_are_flushed(self):
        """Finding 3: the threaded path drains before ``source.finalize()`` at
        source-unit boundaries and natural end — a file is never marked
        processed while its records still sit in the chunk queue or the
        micro-batch buffer (a crash would lose them with the source marked
        done)."""
        config = _stream_config(
            "stream_flush_records: 1000\n"
            "          stream_flush_interval_s: 60\n"
            "          thread_workers: 2"
        )
        log: list[tuple[str, str]] = []
        source = _FinalizeOrderSource([
            (_chunk([{"seq": 0}]), {"source_path": "a.json"}),
            (_chunk([{"seq": 1}]), {"source_path": "a.json"}),
            (_chunk([{"seq": 2}]), {"source_path": "b.json"}),
            (_chunk([{"seq": 3}]), {"source_path": "b.json"}),
        ], log)
        sink = _WriteOrderSink(log)
        stats, _ = _run_finite_stream(config, chunks=[], source=source, sink=sink)

        # Neither file may be finalized while any of its records is still
        # unwritten (log order: all ("write", path) before ("finalize", path)).
        fin_a = next(i for i, e in enumerate(log) if e == ("finalize", "a.json"))
        assert all(e != ("write", "a.json") for e in log[fin_a:])
        fin_b = next(i for i, e in enumerate(log) if e == ("finalize", "b.json"))
        assert all(e != ("write", "b.json") for e in log[fin_b:])
        snap = stats.snapshot()
        assert snap["records_in"] == 4
        assert snap["records_out"] == 4


class TestFlushSerialization:
    """Finding 1 — concurrent flushes must not interleave sink writes."""

    def test_concurrent_flushers_never_interleave_sink_writes(self):
        """The interval flusher and the chunk-loop flush race for the same
        sink; the per-run flush lock serializes them so two sink.write() calls
        never overlap (an interleave would corrupt RollingWriter part paths
        and lose ordering — the reviewer's 20/20 silent-loss repro)."""
        config = _stream_config(
            "stream_flush_records: 3\n"
            "          stream_flush_interval_s: 0.05"
        )
        executor = PipelineExecutor()
        source = _ControllableSource()
        gate = threading.Event()
        sink = _NoOverlapSink(gate)
        stats = PipelineStats(run_id="r-serial", pipeline_name=config.name, schedule_type="stream")
        stop_event = threading.Event()

        with (
            patch.object(executor, "_build_source", return_value=source),
            patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
        ):
            t = threading.Thread(target=executor.stream_run, args=(config, stop_event, stats))
            t.start()
            try:
                # Two records: the interval flusher drains them and parks
                # mid-write on the gate.
                source.send(_chunk([{"seq": 0}]))
                source.send(_chunk([{"seq": 1}]))
                assert _wait_until(lambda: sink.entered.is_set(), timeout=3.0)
                # Three more records: the loop-thread threshold flush now
                # drains a second batch. Buggy code: it enters write() while
                # the flusher is parked → overlap flagged. Fixed code: it
                # blocks on the flush lock until the gate is released.
                source.send(_chunk([{"seq": 2}]))
                source.send(_chunk([{"seq": 3}]))
                source.send(_chunk([{"seq": 4}]))
                time.sleep(0.3)  # window in which the buggy overlap appears
            finally:
                gate.set()
                stop_event.set()
                source.finish()
                t.join(timeout=5.0)

        assert not sink.overlap_seen.is_set()
        seqs = [r["seq"] for p in sink.payloads for r in json.loads(p)]
        assert sorted(seqs) == list(range(5))

    def test_rolling_writer_part_indices_written_exactly_once(self, tmp_path):
        """Serialized flushes keep the RollingWriter's ``{part}`` counter a
        single-threaded read-modify-write: every part index is written exactly
        once. With the flush lock removed, two concurrent flushes read the same
        pre-store index and both write the same ``out_{part}`` path — the
        later write silently overwrites the earlier flush's records."""
        from tram.connectors.local.sink import LocalSink

        config = _stream_config(
            "stream_flush_records: 3\n"
            "          stream_flush_interval_s: 0.05"
        )
        sink = LocalSink({
            "path": str(tmp_path),
            "filename_template": "out_{part}.json",
            "file_mode": "single",
        })
        # Force the preemption the reviewer used, exactly where the unlocked
        # read-modify-write lives (see _PreemptedCounterDict).
        counter = _PreemptedCounterDict()
        sink._writer._part_counters = counter
        executor = PipelineExecutor()
        source = _ControllableSource()
        stats = PipelineStats(run_id="r-parts", pipeline_name=config.name, schedule_type="stream")
        stop_event = threading.Event()

        with (
            patch.object(executor, "_build_source", return_value=source),
            patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
        ):
            t = threading.Thread(target=executor.stream_run, args=(config, stop_event, stats))
            t.start()
            try:
                # Two records: the flusher reads the part counter (0) and is
                # parked mid-read-modify-write.
                source.send(_chunk([{"seq": 0}]))
                source.send(_chunk([{"seq": 1}]))
                assert counter.preempted.wait(3.0)
                # Three more records: the loop-thread threshold flush reads the
                # SAME pre-store counter while the flusher is parked.
                source.send(_chunk([{"seq": 2}]))
                source.send(_chunk([{"seq": 3}]))
                source.send(_chunk([{"seq": 4}]))
                time.sleep(0.5)  # let the flusher wake and finish its write
            finally:
                stop_event.set()
                source.finish()
                t.join(timeout=5.0)

        parts = sorted(p.name for p in tmp_path.glob("out_*.json"))
        assert len(parts) == len(set(parts)), f"duplicate part index: {parts}"
        seqs = [
            r["seq"]
            for p in tmp_path.glob("out_*.json")
            for r in json.loads(p.read_bytes())
        ]
        assert sorted(seqs) == list(range(5))


class TestFlusherShutdown:
    """Finding 4 — the flusher join must outlive an in-flight flush."""

    def test_close_never_runs_while_a_flush_is_still_writing(self):
        """A flush slower than the old 2s join cap must still finish before
        ``sink.close()``: the finally joins the daemon flusher unboundedly
        (bounded by sink timeouts), so sink teardown never races an in-flight
        write."""
        config = _stream_config(
            "stream_flush_records: 100\n"
            "          stream_flush_interval_s: 0.1"
        )
        executor = PipelineExecutor()
        source = _ControllableSource()
        sink = _SlowFlushTrackingSink(write_delay=2.5)
        stats = PipelineStats(run_id="r-shut", pipeline_name=config.name, schedule_type="stream")
        stop_event = threading.Event()

        with (
            patch.object(executor, "_build_source", return_value=source),
            patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
        ):
            t = threading.Thread(target=executor.stream_run, args=(config, stop_event, stats))
            t.start()
            try:
                source.send(_chunk([{"seq": 0}]))
                source.send(_chunk([{"seq": 1}]))
                # Wait until the interval flusher is mid-write, then stop.
                assert _wait_until(lambda: sink.inflight > 0, timeout=3.0)
                time.sleep(0.2)
                stop_event.set()
                source.finish()
                t.join(timeout=10.0)
            finally:
                stop_event.set()
                source.finish()
                t.join(timeout=10.0)

        assert not sink.closed_while_active.is_set()
        assert sink.closed


class TestRateLimitSemantics:
    """Finding 6 — stream flush rate limit is per record, not per flush."""

    def test_stream_flush_rate_limit_consumes_one_token_per_record(self):
        """A 500-record flush consumes one token per record that reaches the
        sink (v1.5.1 semantics) — not one token for the whole flush."""
        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="micro-stream")
        ser_out = JsonSerializer({})
        sink = _RecordingSink()
        buffer = _StreamFlushBuffer(record_threshold=500, interval_s=60.0)
        for i in range(500):
            buffer.append([{"seq": i}], {"serializer_type": "json", "serializer_config": {}})

        with patch.object(executor, "_rate_limit") as rate:
            executor._flush_stream_buffer(
                buffer, ser_out, [(sink, None, [])], ctx, "continue",
                100, None, False, None, None,
            )

        assert rate.call_count == 500
        assert len(sink.payloads) == 1
        assert len(json.loads(sink.payloads[0])) == 500

    def test_stream_flush_rate_limit_throttles_to_rps_not_flush_rate(self):
        """500 records flushed at once with rate_limit_rps=100 take ~5s (100
        rec/s steady state), not ~0s — a per-flush token would allow ~50,000
        rec/s at the same setting."""
        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="micro-stream")
        ser_out = JsonSerializer({})
        sink = _RecordingSink()
        buffer = _StreamFlushBuffer(record_threshold=500, interval_s=60.0)
        for i in range(500):
            buffer.append([{"seq": i}], {"serializer_type": "json", "serializer_config": {}})

        start = time.monotonic()
        executor._flush_stream_buffer(
            buffer, ser_out, [(sink, None, [])], ctx, "continue",
            100, None, False, None, None,
        )
        elapsed = time.monotonic() - start

        assert len(sink.payloads) == 1
        # 500 tokens @100/s steady state ≈ 2s (the bucket refills during its
        # own sleeps); a per-flush token would take ~0s — 50,000 rec/s.
        assert elapsed >= 1.5

    def test_batch_path_rate_limit_stays_per_chunk(self):
        """The batch path keeps its per-chunk token consumption: a 500-record
        chunk consumes exactly one token (changing batch would alter existing
        deployments)."""
        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="micro-stream")
        ser_out = JsonSerializer({})
        sink = _RecordingSink()
        records = [{"seq": i} for i in range(500)]

        with patch.object(executor, "_rate_limit") as rate:
            executor._process_records(
                records, {"serializer_type": "json", "serializer_config": {}},
                [], ser_out, [(sink, None, [])], ctx, "continue",
                100, None, False, None, None,
            )

        assert rate.call_count == 1
        assert len(sink.payloads) == 1
        assert len(json.loads(sink.payloads[0])) == 500


# ── Webhook ingress decoupling ────────────────────────────────────────────


class TestWebhookIngressDecoupling:
    def test_webhook_ingress_decoupled_from_flush_cadence(self):
        """The webhook 202-path enqueue (queue.put_nowait, what the router
        does) is never gated on a sink write: messages drain off the source
        queue into the micro-batch buffer while the flush is deferred below
        the record threshold, and the stop drain delivers them all."""
        from tram.connectors.webhook.source import WebhookSource

        config = _stream_config(
            "stream_flush_records: 10\n"
            "          stream_flush_interval_s: 60"
        )
        executor = PipelineExecutor()
        sink = _RecordingSink()
        source = WebhookSource({"path": "/ingest"})
        stop_event = threading.Event()

        with (
            patch.object(executor, "_build_source", return_value=source),
            patch.object(executor, "_build_sinks", return_value=[(sink, None, [])]),
        ):
            t = threading.Thread(target=executor.stream_run, args=(config, stop_event))
            t.start()
            try:
                assert _wait_until(lambda: _webhook_queue("ingest") is not None, timeout=3.0)
                q = _webhook_queue("ingest")
                # Simulate the router's ingress enqueue (the 202 path): these
                # puts must never wait on the flush cadence.
                for i in range(3):
                    q.put_nowait(
                        (_chunk([{"seq": i}]), {"content_type": "application/json"})
                    )
                assert _wait_until(lambda: q.qsize() == 0, timeout=3.0)
                # The sink write is deferred: buffer below threshold.
                time.sleep(0.2)
                assert sink.payloads == []
                # Ingress keeps being accepted while the flush is deferred.
                q.put_nowait((_chunk([{"seq": 99}]), {}))
                assert _wait_until(lambda: q.qsize() == 0, timeout=3.0)
            finally:
                stop_event.set()
                source.stop()
                t.join(timeout=5.0)
                from tram.connectors.webhook.source import _REGISTRY_LOCK, _WEBHOOK_REGISTRY

                with _REGISTRY_LOCK:
                    _WEBHOOK_REGISTRY.pop("ingest", None)

        # All four records landed (the final stop-drain flushed the buffer).
        total = sum(len(json.loads(p)) for p in sink.payloads)
        assert total == 4


# ── Kafka source batch-end marker ─────────────────────────────────────────


class TestKafkaSourceBatchEndMarker:
    def test_kafka_source_marks_last_live_message_of_each_poll_batch(self):
        """The last live (non-tombstone) message of each poll batch carries
        ``source_batch_end`` so the executor can flush the buffer before the
        batch offsets are committed (commit-after-flush, GH #78)."""
        from tram.connectors.kafka.source import KafkaSource

        class _TP:
            def __init__(self, topic: str, partition: int) -> None:
                self.topic = topic
                self.partition = partition

        def _msg(value, offset: int):
            m = MagicMock()
            m.value = value
            m.topic = "events"
            m.partition = 0
            m.offset = offset
            m.key = None
            return m

        consumer = MagicMock()
        consumer.assignment.return_value = []
        consumer.end_offsets.return_value = {}
        consumer.poll.side_effect = [
            {_TP("events", 0): [_msg(b'{"a":1}', 0), _msg(b'{"a":2}', 1), _msg(None, 2)]},
            {_TP("events", 0): [_msg(b"STOP", 3)]},
        ]
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = KafkaSource({"brokers": ["b:9092"], "topic": "t"})
            it = src.read()
            metas = [next(it)[1] for _ in range(2)]
            next(it)  # resume past batch 1 → commit fires
            it.close()

        assert metas[0]["source_batch_end"] is False
        assert metas[1]["source_batch_end"] is True  # last live message of batch 1
        consumer.commit.assert_called_once()