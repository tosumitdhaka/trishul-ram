"""Tests for thread_workers functionality in PipelineExecutor (v0.9.0).

The multi-threaded tests use a real ThreadPoolExecutor and a real file
source: mocking _process_chunk cannot catch the mark-before-write window
(code review A2) or the unbounded submission (RCA #16) that this file's
tests were written to lock down.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.local.source import LocalSource
from tram.core.context import PipelineRunContext, RunStatus
from tram.core.exceptions import TramError
from tram.pipeline.executor import PipelineExecutor, _batch_inflight_cap

# ── PipelineRunContext thread-safety ──────────────────────────────────────


class TestPipelineRunContextThreadSafety:
    """Verify that counter helpers are lock-guarded and race-condition-free."""

    def test_inc_records_in_uses_lock(self):
        ctx = PipelineRunContext(pipeline_name="test")
        assert hasattr(ctx, "_lock")
        assert isinstance(ctx._lock, type(threading.Lock()))

    def test_inc_records_out_uses_lock(self):
        ctx = PipelineRunContext(pipeline_name="test")
        ctx.inc_records_out(5)
        assert ctx.records_out == 5

    def test_inc_records_skipped_uses_lock(self):
        ctx = PipelineRunContext(pipeline_name="test")
        ctx.inc_records_skipped(3)
        assert ctx.records_skipped == 3

    def test_inc_bytes_uses_lock(self):
        ctx = PipelineRunContext(pipeline_name="test")
        ctx.inc_bytes_in(10)
        ctx.inc_bytes_out(20)
        assert ctx.bytes_in == 10
        assert ctx.bytes_out == 20

    def test_record_error_uses_lock(self):
        ctx = PipelineRunContext(pipeline_name="test")
        ctx.record_error("something went wrong")
        assert len(ctx.errors) == 1
        assert ctx.records_skipped == 1

    def test_record_dlq_uses_lock(self):
        ctx = PipelineRunContext(pipeline_name="test")
        ctx.record_dlq()
        ctx.record_dlq()
        assert ctx.dlq_count == 2

    def test_concurrent_inc_records_in_no_lost_counts(self):
        """Race-condition test: N threads each add M counts; total must be N*M."""
        ctx = PipelineRunContext(pipeline_name="concurrent-test")
        n_threads = 20
        increments_per_thread = 50

        def worker():
            for _ in range(increments_per_thread):
                ctx.inc_records_in(1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert ctx.records_in == n_threads * increments_per_thread

    def test_concurrent_inc_records_out_no_lost_counts(self):
        """Concurrent writes to records_out must be consistent."""
        ctx = PipelineRunContext(pipeline_name="test")
        n_threads = 10
        count_each = 100

        def worker():
            for _ in range(count_each):
                ctx.inc_records_out(1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert ctx.records_out == n_threads * count_each


# ── _run_batch_chunks: single-threaded path ───────────────────────────────


class TestRunBatchChunksSingleThreaded:
    """Verify sequential processing when thread_workers=1."""

    def _make_config(self, chunks, thread_workers=1, batch_size=None, record_chunk_size=None):
        config = MagicMock()
        config.name = "test-pipe"
        config.thread_workers = thread_workers
        config.batch_size = batch_size
        config.record_chunk_size = record_chunk_size
        config.on_error = "continue"
        config.rate_limit_rps = None

        mock_source = MagicMock()
        mock_source.read.return_value = iter(chunks)
        return config, mock_source

    def test_single_threaded_processes_all_chunks(self):
        chunks = [
            (b'[{"x": 1}]', {"f": "a.json"}),
            (b'[{"x": 2}]', {"f": "b.json"}),
            (b'[{"x": 3}]', {"f": "c.json"}),
        ]
        config, mock_source = self._make_config(chunks, thread_workers=1)

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(mock_sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="test-pipe")

        executor._run_batch_chunks(
            config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
        )

        assert mock_sink.write.call_count == 3

    def test_single_threaded_respects_batch_size(self):
        """batch_size=2 with 5 chunks should stop after records_in >= 2."""
        chunks = [(b'[{"x": %d}]' % i, {}) for i in range(5)]
        config, mock_source = self._make_config(chunks, thread_workers=1, batch_size=2)

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(mock_sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="test-pipe")

        executor._run_batch_chunks(
            config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
        )

        # Should have stopped after 2 records
        assert ctx.records_in <= 2
        assert mock_sink.write.call_count <= 2

    def test_single_threaded_uses_serializer_parse_chunks_when_configured(self):
        chunks = [(b"payload", {"f": "a.ber"})]
        config, mock_source = self._make_config(
            chunks, thread_workers=1, record_chunk_size=2
        )

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse_chunks.return_value = iter([
            [{"x": 1}, {"x": 2}],
            [{"x": 3}],
        ])
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(mock_sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="test-pipe")

        executor._run_batch_chunks(
            config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
        )

        mock_ser_in.parse.assert_not_called()
        mock_ser_in.parse_chunks.assert_called_once_with(b"payload", 2)
        assert ctx.records_in == 3
        assert mock_sink.write.call_count == 2

    def test_single_threaded_record_chunk_size_respects_batch_size(self):
        chunks = [(b"payload", {"f": "a.ber"})]
        config, mock_source = self._make_config(
            chunks, thread_workers=1, batch_size=2, record_chunk_size=3
        )

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse_chunks.return_value = iter([
            [{"x": 1}, {"x": 2}, {"x": 3}],
        ])
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(mock_sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="test-pipe")

        executor._run_batch_chunks(
            config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
        )

        assert ctx.records_in == 2
        serialized_records = mock_ser_out.serialize.call_args.args[0]
        assert serialized_records == [{"x": 1}, {"x": 2}]

    def test_single_threaded_finalizes_each_source_file(self):
        chunks = [
            (b"payload-a", {"source_filename": "a.ber", "source_path": "/in/a.ber"}),
            (b"payload-b", {"source_filename": "b.ber", "source_path": "/in/b.ber"}),
        ]
        config, mock_source = self._make_config(
            chunks, thread_workers=1, record_chunk_size=2
        )

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse_chunks.side_effect = [
            iter([[{"x": 1}], [{"x": 2}]]),
            iter([[{"x": 3}]]),
        ]
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(mock_sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="test-pipe")

        executor._run_batch_chunks(
            config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
        )

        assert mock_sink.finalize_source.call_count == 2
        first_meta, first_success = mock_sink.finalize_source.call_args_list[0].args
        second_meta, second_success = mock_sink.finalize_source.call_args_list[1].args
        assert first_meta["source_filename"] == "a.ber"
        assert second_meta["source_filename"] == "b.ber"
        assert first_meta["run_id"] == ctx.run_id
        assert second_meta["run_id"] == ctx.run_id
        assert first_meta["pipeline_name"] == "test-pipe"
        assert second_meta["pipeline_name"] == "test-pipe"
        assert first_success is True
        assert second_success is True
        # The source-side finalize hook fires per file after its chunks drain.
        assert mock_source.finalize.call_count == 2
        assert mock_source.finalize.call_args_list[0].kwargs["success"] is True
        assert mock_source.finalize.call_args_list[1].kwargs["success"] is True

    def test_single_threaded_failure_finalizes_current_source_with_failure(self):
        chunks = [
            (b"payload-a", {"source_filename": "a.ber", "source_path": "/in/a.ber"}),
        ]
        config, mock_source = self._make_config(chunks, thread_workers=1)
        config.on_error = "abort"

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = ValueError("boom")
        mock_ser_out = MagicMock()
        sinks = [(mock_sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="test-pipe")

        with pytest.raises(TramError):
            executor._run_batch_chunks(
                config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
            )

        assert mock_sink.finalize_source.call_count == 1
        failed_meta, failed_success = mock_sink.finalize_source.call_args.args
        assert failed_meta["source_filename"] == "a.ber"
        assert failed_meta["run_id"] == ctx.run_id
        assert failed_success is False
        # The source-side hook is told the unit failed (a no-op in the file
        # sources) so the file is left unmarked for a retry.
        mock_source.finalize.assert_called_once()
        assert mock_source.finalize.call_args.kwargs["success"] is False

    def test_single_threaded_batch_size_stop_finalizes_completed_files_only(self):
        """batch_size stop abandons the read mid-file: files finalized before
        the stop are marked, the current (last-read) file is left unmarked."""
        chunks = [
            (b'[{"x":1}]', {"source_filename": "a.json", "source_path": "/in/a.json"}),
            (b'[{"x":2}]', {"source_filename": "b.json", "source_path": "/in/b.json"}),
            (b'[{"x":3}]', {"source_filename": "c.json", "source_path": "/in/c.json"}),
        ]
        config, _ = self._make_config(chunks, thread_workers=1, batch_size=2)
        mock_source = MagicMock()
        mock_source.read.return_value = iter(chunks)

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(mock_sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="test-pipe")

        executor._run_batch_chunks(
            config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
        )

        # a.json was fully processed before the b.json chunk tripped batch_size,
        # so only a.json is finalized; b.json (current) and c.json stay unmarked.
        assert mock_source.finalize.call_count == 1
        finalized_meta = mock_source.finalize.call_args.args[0]
        assert finalized_meta["source_filename"] == "a.json"
        assert mock_source.finalize.call_args.kwargs["success"] is True


# ── _run_batch_chunks: multi-threaded path ────────────────────────────────


class TestRunBatchChunksMultiThreaded:
    """Verify the bounded ThreadPoolExecutor path when thread_workers > 1.

    These tests use a REAL ThreadPoolExecutor (no _process_chunk mock) so the
    two defects this path used to have are observable: files marked before
    their chunks' writes complete (code review A2) and unbounded submission of
    the whole source into the pool queue (RCA #16).
    """

    @staticmethod
    def _make_threaded_config(thread_workers=2, on_error="continue"):
        config = MagicMock()
        config.name = "threaded-pipe"
        config.thread_workers = thread_workers
        config.batch_size = None
        config.on_error = on_error
        config.rate_limit_rps = None
        config.parallel_sinks = False
        return config

    def test_multi_threaded_uses_thread_pool_executor(self):
        """When thread_workers=3, _run_batch_chunks should use ThreadPoolExecutor."""
        config = self._make_threaded_config(thread_workers=3)

        chunks = [
            (b'[{"n": 1}]', {}),
            (b'[{"n": 2}]', {}),
        ]
        mock_source = MagicMock()
        mock_source.read.return_value = iter(chunks)

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(mock_sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="threaded-pipe")

        with patch("tram.pipeline.executor.ThreadPoolExecutor") as mock_pool_cls:
            # Set up context manager properly
            mock_pool = MagicMock()
            mock_pool_cls.return_value.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

            future1 = Future()
            future1.set_result(True)
            future2 = Future()
            future2.set_result(True)
            mock_pool.submit.side_effect = [future1, future2]

            executor._run_batch_chunks(
                config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
            )

            mock_pool_cls.assert_called_once_with(max_workers=3)
            assert mock_pool.submit.call_count == 2

    def test_multi_threaded_submit_calls_process_chunk(self):
        """Submitted futures should wrap _process_chunk calls."""
        config = self._make_threaded_config(thread_workers=2)

        chunks = [(b'[{"x": 1}]', {"meta": "a"})]
        mock_source = MagicMock()
        mock_source.read.return_value = iter(chunks)

        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = [{"x": 1}]
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(mock_sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="threaded-pipe")

        submitted_fns = []

        with patch("tram.pipeline.executor.ThreadPoolExecutor") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool_cls.return_value.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

            fut = Future()
            fut.set_result(True)

            def capture_submit(fn, *args, **kwargs):
                submitted_fns.append(fn)
                return fut

            mock_pool.submit.side_effect = capture_submit

            executor._run_batch_chunks(
                config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
            )

        # The submitted function should be executor._process_chunk
        # Note: bound methods are re-created on each attribute access so we compare
        # by __func__ and __self__ rather than identity.
        assert len(submitted_fns) == 1
        assert submitted_fns[0].__func__ is PipelineExecutor._process_chunk
        assert submitted_fns[0].__self__ is executor

    def test_threaded_finalizes_files_after_their_chunks_complete(self, tmp_path):
        """Success path: files are moved+marked only after their chunks drain."""
        src = tmp_path / "in"
        dst = tmp_path / "processed"
        src.mkdir()
        for name in ("a.json", "b.json"):
            (src / name).write_bytes(b'[{"x":1}]')

        config = self._make_threaded_config(thread_workers=2, on_error="continue")

        source = LocalSource({
            "path": str(src),
            "move_after_read": str(dst),
            "skip_processed": True,
            "_pipeline_name": "threaded-pipe",
        })
        tracker = _RecordingTracker()
        source._file_tracker = tracker

        sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="threaded-pipe")

        executor._run_batch_chunks(
            config, source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
        )

        assert sorted(p.name for p in dst.iterdir()) == ["a.json", "b.json"]
        assert sorted(p.name for p in src.iterdir()) == []
        assert sorted(tracker.marked) == sorted([
            str(src / "a.json"),
            str(src / "b.json"),
        ])

    def test_threaded_mid_run_failure_leaves_files_unmarked_and_unmoved(self, tmp_path):
        """A chunk failure after some chunks were submitted must not mark or
        move the files whose chunks did not all complete — the old generator
        marked them at *submit* time (code review A2: permanent data loss)."""
        src = tmp_path / "in"
        dst = tmp_path / "processed"
        src.mkdir()
        for name in ("a.json", "b.json", "c.json"):
            (src / name).write_bytes(b'[{"x":1}]')

        config = self._make_threaded_config(thread_workers=2, on_error="abort")

        source = LocalSource({
            "path": str(src),
            "move_after_read": str(dst),
            "skip_processed": True,
            "_pipeline_name": "threaded-pipe",
        })
        tracker = _RecordingTracker()
        source._file_tracker = tracker

        sink = MagicMock()

        def flaky_write(serialized, sink_meta):
            if sink_meta.get("source_filename") == "b.json":
                raise ValueError("boom")

        sink.write.side_effect = flaky_write
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="threaded-pipe")

        with pytest.raises(TramError):
            executor._run_batch_chunks(
                config, source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
            )

        # a.json was fully drained before b.json failed → moved + marked.
        # b.json (failed) and c.json (cancelled) stay in the source dir,
        # unmarked and unmoved, so a retry can reprocess them.
        assert sorted(p.name for p in dst.iterdir()) == ["a.json"]
        assert sorted(p.name for p in src.iterdir()) == ["b.json", "c.json"]
        assert tracker.marked == [str(src / "a.json")]

    def test_threaded_inflight_cap_limits_outstanding_futures(self):
        """The producer never holds more than _batch_inflight_cap futures
        pending — the old code submitted the entire source into the unbounded
        pool queue (RCA #16: thread_workers=2 doubled the peak heap)."""
        config = self._make_threaded_config(thread_workers=2, on_error="continue")
        cap = _batch_inflight_cap(config.thread_workers)
        assert cap == 4

        n = 8
        chunks = [
            (
                f'[{{"x":{i}}}]'.encode(),
                {"source_filename": f"f{i}.json", "source_path": f"/in/f{i}.json"},
            )
            for i in range(n)
        ]
        mock_source = MagicMock()
        mock_source.read.return_value = iter(chunks)

        sink = MagicMock()
        sink.write.side_effect = lambda *args, **kwargs: time.sleep(0.03)
        mock_ser_in = MagicMock()
        mock_ser_in.parse.side_effect = lambda raw: json.loads(raw)
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b"[]"
        sinks = [(sink, None, [])]

        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="threaded-pipe")

        pools = []

        class TrackingPool(ThreadPoolExecutor):
            """Real pool that records submitted-but-not-yet-completed futures."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._lock = threading.Lock()
                self.pending = 0
                self.max_pending = 0
                pools.append(self)

            def submit(self, fn, *args, **kwargs):
                with self._lock:
                    self.pending += 1
                    self.max_pending = max(self.max_pending, self.pending)
                fut = super().submit(fn, *args, **kwargs)
                fut.add_done_callback(self._on_done)
                return fut

            def _on_done(self, fut):
                with self._lock:
                    self.pending -= 1

        with patch("tram.pipeline.executor.ThreadPoolExecutor", TrackingPool):
            executor._run_batch_chunks(
                config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
            )

        assert pools, "threaded path did not create a ThreadPoolExecutor"
        assert pools[0].max_pending <= cap
        # More than the worker count was in flight, proving the window is a
        # bounded buffer, not accidental serialization.
        assert pools[0].max_pending >= 2


class _RecordingTracker:
    """Minimal ProcessedFileTracker stand-in that records mark_processed calls."""

    def __init__(self):
        self.marked = []

    def is_processed(self, pipeline_name, source_key, filepath):
        return False

    def mark_processed(self, pipeline_name, source_key, filepath):
        self.marked.append(filepath)


# ── batch_run with thread_workers=1 ──────────────────────────────────────


class TestBatchRunWithThreadWorkers:
    """Verify batch_run returns correct RunResult when thread_workers=1."""

    def test_batch_run_thread_workers_1_success(self):
        """batch_run with thread_workers=1 should process records and return SUCCESS."""
        import textwrap

        from tram.pipeline.loader import load_pipeline_from_yaml

        yaml_text = textwrap.dedent("""
            pipeline:
              name: tw-test
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
              thread_workers: 1
        """)
        config = load_pipeline_from_yaml(yaml_text)

        executor = PipelineExecutor()

        records = [{"id": "1", "val": "a"}, {"id": "2", "val": "b"}]
        mock_source = MagicMock()
        mock_source.read.return_value = iter([
            (json.dumps(records).encode(), {"source_filename": "test.json"}),
        ])
        mock_sink = MagicMock()
        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = json.dumps(records).encode()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=mock_ser_in),
            patch.object(executor, "_build_serializer_out", return_value=mock_ser_out),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_build_dlq_sink", return_value=None),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_in == 2
        assert result.records_out == 2
        assert result.records_skipped == 0
        mock_sink.write.assert_called_once()

    def test_batch_run_thread_workers_1_empty_source(self):
        """thread_workers=1 with empty source → SUCCESS, 0 records."""
        import textwrap

        from tram.pipeline.loader import load_pipeline_from_yaml

        yaml_text = textwrap.dedent("""
            pipeline:
              name: tw-empty
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
              thread_workers: 1
        """)
        config = load_pipeline_from_yaml(yaml_text)
        executor = PipelineExecutor()

        mock_source = MagicMock()
        mock_source.read.return_value = iter([])
        mock_sink = MagicMock()

        with (
            patch.object(executor, "_build_source", return_value=mock_source),
            patch.object(executor, "_build_sinks", return_value=[(mock_sink, None, [])]),
            patch.object(executor, "_build_serializer_in", return_value=MagicMock()),
            patch.object(executor, "_build_serializer_out", return_value=MagicMock()),
            patch.object(executor, "_build_transforms", return_value=[]),
            patch.object(executor, "_build_dlq_sink", return_value=None),
        ):
            result = executor.batch_run(config)

        assert result.status == RunStatus.SUCCESS
        assert result.records_in == 0
        mock_sink.write.assert_not_called()
