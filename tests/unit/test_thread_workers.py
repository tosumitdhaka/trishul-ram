"""Tests for thread_workers functionality in PipelineExecutor (v0.9.0)."""

from __future__ import annotations

import json
import threading
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from tram.core.context import PipelineRunContext, RunStatus
from tram.pipeline.executor import PipelineExecutor

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

    def _make_config(self, chunks, thread_workers=1, batch_size=None):
        config = MagicMock()
        config.name = "test-pipe"
        config.thread_workers = thread_workers
        config.batch_size = batch_size
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


# ── _run_batch_chunks: multi-threaded path ────────────────────────────────


class TestRunBatchChunksMultiThreaded:
    """Verify ThreadPoolExecutor is used when thread_workers > 1."""

    def test_multi_threaded_uses_thread_pool_executor(self):
        """When thread_workers=3, _run_batch_chunks should use ThreadPoolExecutor."""
        config = MagicMock()
        config.name = "threaded-pipe"
        config.thread_workers = 3
        config.batch_size = None
        config.on_error = "continue"
        config.rate_limit_rps = None

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

            with patch("tram.pipeline.executor.as_completed", return_value=iter([future1, future2])):
                executor._run_batch_chunks(
                    config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
                )

            mock_pool_cls.assert_called_once_with(max_workers=3)
            assert mock_pool.submit.call_count == 2

    def test_multi_threaded_submit_calls_process_chunk(self):
        """Submitted futures should wrap _process_chunk calls."""
        config = MagicMock()
        config.name = "threaded-pipe"
        config.thread_workers = 2
        config.batch_size = None
        config.on_error = "continue"
        config.rate_limit_rps = None

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

            with patch("tram.pipeline.executor.as_completed", return_value=iter([fut])):
                executor._run_batch_chunks(
                    config, mock_source, sinks, mock_ser_in, mock_ser_out, [], None, ctx
                )

        # The submitted function should be executor._process_chunk
        # Note: bound methods are re-created on each attribute access so we compare
        # by __func__ and __self__ rather than identity.
        assert len(submitted_fns) == 1
        assert submitted_fns[0].__func__ is PipelineExecutor._process_chunk
        assert submitted_fns[0].__self__ is executor


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
