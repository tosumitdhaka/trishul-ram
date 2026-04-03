"""Tests for per-sink transform chains in PipelineExecutor."""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, patch

from tram.core.context import PipelineRunContext
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


def _make_pipeline_with_sink_transforms(sink_transforms_yaml: str = "") -> object:
    yaml_text = textwrap.dedent(f"""
        pipeline:
          name: sink-t-test
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
            {sink_transforms_yaml}
    """)
    return load_pipeline_from_yaml(yaml_text)


class TestBuildSinksReturnsTuple:
    def test_build_sinks_returns_three_tuple(self):
        config = _make_pipeline_with_sink_transforms()
        executor = PipelineExecutor()

        mock_cls = MagicMock()
        mock_cls.return_value = MagicMock()

        with patch("tram.pipeline.executor.get_sink", return_value=mock_cls):
            sinks = executor._build_sinks(config)

        assert len(sinks) == 1
        # _build_sinks returns 5-tuple: (instance, condition, transforms, cfg, per_sink_ser)
        sink_instance, condition, sink_transforms, sink_cfg, per_sink_ser = sinks[0]
        assert sink_instance is mock_cls.return_value
        assert condition is None
        assert sink_transforms == []

    def test_build_sinks_with_transforms_creates_transform_instances(self):
        yaml_text = textwrap.dedent("""
            pipeline:
              name: sink-t2
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
                transforms:
                  - type: drop
                    fields: [secret]
        """)
        config = load_pipeline_from_yaml(yaml_text)
        executor = PipelineExecutor()

        mock_sink_cls = MagicMock()
        mock_transform_cls = MagicMock()
        mock_transform_instance = MagicMock()
        mock_transform_cls.return_value = mock_transform_instance

        with patch("tram.pipeline.executor.get_sink", return_value=mock_sink_cls), \
             patch("tram.pipeline.executor.get_transform", return_value=mock_transform_cls):
            sinks = executor._build_sinks(config)

        _, _, sink_transforms, _, _ = sinks[0]
        assert len(sink_transforms) == 1
        assert sink_transforms[0] is mock_transform_instance


class TestPerSinkTransformApplication:
    def _run_with_sink_transforms(self, records, sink_transforms_mocks,
                                   dlq_sink=None, condition=None):
        executor = PipelineExecutor()

        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b'[]'
        mock_sink = MagicMock()

        sinks = [(mock_sink, condition, sink_transforms_mocks)]
        ctx = PipelineRunContext(pipeline_name="sink-t-test")

        executor._process_chunk(
            b'[]', {}, mock_ser_in, [], mock_ser_out,
            sinks, ctx, "continue", dlq_sink=dlq_sink,
        )
        return ctx, mock_sink, mock_ser_out

    def test_sink_with_no_transforms_passes_records_unchanged(self):
        records = [{"a": 1}, {"b": 2}]
        ctx, mock_sink, mock_ser_out = self._run_with_sink_transforms(records, [])

        mock_ser_out.serialize.assert_called_once_with(records)
        mock_sink.write.assert_called_once()

    def test_sink_transforms_applied_in_order(self):
        records = [{"a": 1}]
        step1_out = [{"a": 1, "step": 1}]
        step2_out = [{"a": 1, "step": 2}]

        t1 = MagicMock()
        t1.apply.return_value = step1_out
        t2 = MagicMock()
        t2.apply.return_value = step2_out

        ctx, mock_sink, mock_ser_out = self._run_with_sink_transforms(records, [t1, t2])

        t1.apply.assert_called_once_with(records)
        t2.apply.assert_called_once_with(step1_out)
        mock_ser_out.serialize.assert_called_once_with(step2_out)

    def test_sink_transform_failure_routes_to_dlq(self):
        records = [{"id": 1}]
        dlq_sink = MagicMock()

        bad_t = MagicMock()
        bad_t.apply.side_effect = RuntimeError("sink transform error")

        ctx, mock_sink, _ = self._run_with_sink_transforms(
            records, [bad_t], dlq_sink=dlq_sink
        )

        # Sink should NOT be written to because transform failed
        mock_sink.write.assert_not_called()
        # DLQ should be written
        dlq_sink.write.assert_called_once()
        assert ctx.dlq_count == 1

    def test_two_sinks_have_independent_transforms(self):
        records = [{"x": 10}]
        out_a = [{"x": 10, "tag": "a"}]
        out_b = [{"x": 10, "tag": "b"}]

        ta = MagicMock()
        ta.apply.return_value = out_a
        tb = MagicMock()
        tb.apply.return_value = out_b

        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b'[]'
        sink_a = MagicMock()
        sink_b = MagicMock()

        sinks = [(sink_a, None, [ta]), (sink_b, None, [tb])]
        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="two-sinks")

        executor._process_chunk(
            b'[]', {}, mock_ser_in, [], mock_ser_out,
            sinks, ctx, "continue",
        )

        ta.apply.assert_called_once_with(records)
        tb.apply.assert_called_once_with(records)
        # serialize called twice: once for each sink
        assert mock_ser_out.serialize.call_count == 2

    def test_condition_filter_then_sink_transforms(self):
        records = [{"type": "A", "val": 1}, {"type": "B", "val": 2}]
        filtered = [{"type": "A", "val": 1}]
        transformed = [{"type": "A", "val": 99}]

        t = MagicMock()
        t.apply.return_value = transformed

        mock_ser_in = MagicMock()
        mock_ser_in.parse.return_value = records
        mock_ser_out = MagicMock()
        mock_ser_out.serialize.return_value = b'[]'
        mock_sink = MagicMock()

        sinks = [(mock_sink, 'type == "A"', [t])]
        executor = PipelineExecutor()
        ctx = PipelineRunContext(pipeline_name="cond-t")

        executor._process_chunk(
            b'[]', {}, mock_ser_in, [], mock_ser_out,
            sinks, ctx, "continue",
        )

        # transform should receive only filtered records
        t.apply.assert_called_once_with(filtered)
        mock_ser_out.serialize.assert_called_once_with(transformed)
