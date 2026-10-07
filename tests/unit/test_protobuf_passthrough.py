"""Validated same-schema Protobuf passthrough (v1.7.0 pilot B) tests.

Covers: the registration eligibility matrix (every disqualifier surfaces as a
specific registration error), malformed-frame error parity with the dictionary
path, byte preservation of the original frame bytes, run-history counts,
flag-off behavior (old path untouched), and the runtime fallback to the
dictionary path with one WARNING.

These tests drive the REAL protobuf runtime with dynamic descriptors built via
descriptor_pb2/message_factory (no protoc needed), mirroring
test_protobuf_e2e_amplification.py: the .proto fixtures are real files on disk
(the registration gate hashes their content), while execution monkeypatches
``_get_message_class`` on the built serializer instances.
"""
from __future__ import annotations

import logging
import struct
from unittest.mock import MagicMock, patch

import pytest
import yaml

pytest.importorskip("google.protobuf")

import tram.connectors  # noqa: F401  (registers source/sink plugins)
import tram.serializers  # noqa: F401  (registers serializer plugins)
from tram.agent.metrics import PipelineStats
from tram.core.context import PipelineRunContext
from tram.core.exceptions import ConfigError, TramError
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml

# ── Fixture schema files ────────────────────────────────────────────────────
#
# Two distinct .proto contents in SEPARATE directories: the eligibility gate
# hashes every .proto in the schema file's directory (compile semantics), so
# the schema-content-mismatch case needs the contents in different dirs.

_PROTO_A = (
    'syntax = "proto3";\n'
    "message SampleRecord {\n"
    "  string id = 1;\n"
    "  int64 seq = 2;\n"
    "  bool active = 3;\n"
    "}\n"
)

_PROTO_B = (
    'syntax = "proto3";\n'
    "message OtherRecord {\n"
    "  int32 n = 1;\n"
    "}\n"
)


def _build_sample_msg_class():
    """Dynamically build SampleRecord (matches fixture A) without protoc."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    pool = descriptor_pool.DescriptorPool()
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "sample.proto"
    file_proto.package = "pt"
    file_proto.syntax = "proto3"
    msg = file_proto.message_type.add()
    msg.name = "SampleRecord"
    for name, number, ftype in (
        ("id", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING),
        ("seq", 2, descriptor_pb2.FieldDescriptorProto.TYPE_INT64),
        ("active", 3, descriptor_pb2.FieldDescriptorProto.TYPE_BOOL),
    ):
        field = msg.field.add()
        field.name = name
        field.number = number
        field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
        field.type = ftype
    pool.Add(file_proto)
    return message_factory.GetMessageClass(
        pool.FindMessageTypeByName("pt.SampleRecord")
    )


@pytest.fixture
def schema_a(tmp_path):
    path = tmp_path / "schemas-a" / "sample.proto"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PROTO_A)
    return path


@pytest.fixture
def schema_b(tmp_path):
    path = tmp_path / "schemas-b" / "other.proto"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PROTO_B)
    return path


def _frame(msg_bytes: bytes) -> bytes:
    return struct.pack(">I", len(msg_bytes)) + msg_bytes


def _make_stream(Sample, count: int = 3) -> bytes:
    frames = []
    for i in range(count):
        msg = Sample()
        msg.id = f"rec-{i}"
        msg.seq = i * 1000
        msg.active = i % 2 == 0
        frames.append(_frame(msg.SerializeToString()))
    return b"".join(frames)


def _build_config(
    schema_in,
    schema_out,
    *,
    flag: bool = True,
    ser_in_type: str = "protobuf",
    ser_out_type: str = "protobuf",
    message_class: str = "SampleRecord",
    out_message_class: str | None = None,
    framing: str = "length_delimited",
    out_framing: str | None = None,
    registry_url: str | None = None,
    transforms: list | None = None,
    sink_type: str = "local",
    sink_transforms: list | None = None,
    sink_condition: str | None = None,
    filename_template: str | None = None,
    sink_overrides: dict | None = None,
    dlq: dict | None = None,
):
    """Build a pipeline config YAML (protobuf in/out by default) and load it."""
    out_message_class = message_class if out_message_class is None else out_message_class
    out_framing = framing if out_framing is None else out_framing

    def _serializer(schema, stype, msg_class, fr):
        ser: dict = {"type": stype}
        if stype == "protobuf":
            ser["schema_file"] = str(schema)
            ser["message_class"] = msg_class
            ser["framing"] = fr
        if registry_url is not None:
            ser["schema_registry_url"] = registry_url
        return ser

    sink: dict = {"type": sink_type, "path": "/tmp/out"}
    if sink_type == "kafka":
        sink = {"type": "kafka", "brokers": ["localhost:9092"], "topic": "pt-topic"}
    elif sink_type == "sftp":
        sink = {
            "type": "sftp",
            "host": "localhost",
            "username": "tram",
            "password": "pw",
            "remote_path": "/tmp/out",
        }
    if sink_transforms is not None:
        sink["transforms"] = sink_transforms
    if sink_condition is not None:
        sink["condition"] = sink_condition
    if filename_template is not None:
        sink["filename_template"] = filename_template
    if sink_overrides:
        sink.update(sink_overrides)

    data: dict = {
        "name": "pt-pipe",
        "source": {"type": "local", "path": "/tmp/in"},
        "serializer_in": _serializer(schema_in, ser_in_type, message_class, framing),
        "serializer_out": _serializer(
            schema_out, ser_out_type, out_message_class, out_framing
        ),
        "sinks": [sink],
        "protobuf_passthrough": flag,
    }
    if transforms is not None:
        data["transforms"] = transforms
    if dlq is not None:
        data["dlq"] = dlq
    return load_pipeline_from_yaml(yaml.safe_dump({"pipeline": data}))


def _build_executor_pieces(schema_a, tmp_path):
    """Eligible config + real executor/serializers/sinks ready for execution."""
    config = _build_config(
        schema_a, schema_a, sink_overrides={"path": str(tmp_path / "out")}
    )
    executor = PipelineExecutor()
    ser_in = executor._build_serializer_in(config)
    ser_out = executor._build_serializer_out(config)
    Sample = _build_sample_msg_class()
    ser_in._get_message_class = lambda: Sample
    ser_out._get_message_class = lambda: Sample
    sinks = executor._build_sinks(config)
    return executor, config, ser_in, ser_out, sinks, Sample


# ── Registration eligibility matrix ─────────────────────────────────────────


class TestEligibilityRegistration:
    def test_eligible_pipeline_registers(self, schema_a):
        config = _build_config(schema_a, schema_a)
        assert config.protobuf_passthrough is True

    def test_flag_defaults_to_false_when_absent(self, schema_a):
        config = load_pipeline_from_yaml(
            yaml.safe_dump(
                {
                    "pipeline": {
                        "name": "pt-pipe",
                        "source": {"type": "local", "path": "/tmp/in"},
                        "serializer_in": {
                            "type": "protobuf",
                            "schema_file": str(schema_a),
                            "message_class": "SampleRecord",
                        },
                        "serializer_out": {
                            "type": "protobuf",
                            "schema_file": str(schema_a),
                            "message_class": "SampleRecord",
                        },
                        "sinks": [{"type": "local", "path": "/tmp/out"}],
                    }
                }
            )
        )
        assert config.protobuf_passthrough is False

    def test_flag_off_skips_eligibility(self, schema_a):
        config = _build_config(schema_a, schema_a, flag=False)
        assert config.protobuf_passthrough is False

    def test_schema_content_mismatch_disqualifies(self, schema_a, schema_b):
        with pytest.raises(ConfigError, match="schema content differ"):
            _build_config(schema_a, schema_b)

    def test_message_class_mismatch_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="message_class 'SampleRecord' != "):
            _build_config(schema_a, schema_a, out_message_class="OtherRecord")

    def test_framing_mismatch_disqualifies(self, schema_a):
        with pytest.raises(
            ConfigError,
            match="framing 'length_delimited' != serializer_out framing 'none'",
        ):
            _build_config(schema_a, schema_a, out_framing="none")

    def test_framing_none_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="framing must be 'length_delimited'"):
            _build_config(schema_a, schema_a, framing="none")

    def test_global_transform_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="global transforms are configured"):
            _build_config(
                schema_a, schema_a, transforms=[{"type": "rename", "fields": {"a": "b"}}]
            )

    def test_sink_transform_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="per-sink transforms are configured"):
            _build_config(
                schema_a,
                schema_a,
                sink_transforms=[{"type": "rename", "fields": {"a": "b"}}],
            )

    def test_sink_condition_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="a sink condition is configured"):
            _build_config(schema_a, schema_a, sink_condition="id > 1")

    def test_record_field_filename_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="record-field token"):
            _build_config(
                schema_a, schema_a, filename_template="{pipeline}_{field.id}.bin"
            )

    def test_kafka_sink_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="kafka sinks are not eligible"):
            _build_config(schema_a, schema_a, sink_type="kafka")

    def test_other_sink_type_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="sink type 'sftp' is not eligible"):
            _build_config(schema_a, schema_a, sink_type="sftp")

    def test_dlq_config_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="a DLQ sink \\(local\\) is configured"):
            _build_config(schema_a, schema_a, dlq={"type": "local", "path": "/tmp/dlq"})

    def test_per_sink_serializer_override_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="per-sink serializer_out override"):
            _build_config(
                schema_a, schema_a, sink_overrides={"serializer_out": {"type": "json"}}
            )

    def test_schema_registry_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="schema_registry configuration is not"):
            _build_config(schema_a, schema_a, registry_url="http://registry:8081")

    def test_serializer_in_not_protobuf_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="serializer_in must be type=protobuf"):
            _build_config(schema_a, schema_a, ser_in_type="json")

    def test_serializer_out_not_protobuf_disqualifies(self, schema_a):
        with pytest.raises(ConfigError, match="serializer_out must be type=protobuf"):
            _build_config(schema_a, schema_a, ser_out_type="json")

    def test_all_conditions_listed_at_once(self, schema_a, schema_b):
        """A pipeline violating several conditions reports ALL of them."""
        with pytest.raises(ConfigError) as excinfo:
            _build_config(
                schema_a,
                schema_b,
                transforms=[{"type": "rename", "fields": {"a": "b"}}],
                sink_condition="id > 1",
            )
        error = str(excinfo.value)
        assert "schema content differ" in error
        assert "global transforms are configured" in error
        assert "a sink condition is configured" in error


# ── Passthrough execution ──────────────────────────────────────────────────


class TestPassthroughExecution:
    def test_byte_preservation(self, schema_a, tmp_path):
        """Output frame bytes are exactly the original input frame bytes."""
        executor, _config, ser_in, ser_out, sinks, Sample = _build_executor_pieces(
            schema_a, tmp_path
        )
        raw = _make_stream(Sample, count=3)
        ctx = PipelineRunContext(pipeline_name="pt-pipe")

        result = executor._process_chunk(
            raw, {}, ser_in, [], ser_out, sinks, ctx, "continue", passthrough=True
        )

        assert result is True
        files = list((tmp_path / "out").iterdir())
        assert len(files) == 1
        assert files[0].read_bytes() == raw

    def test_run_history_counts(self, schema_a, tmp_path):
        executor, _config, ser_in, ser_out, sinks, Sample = _build_executor_pieces(
            schema_a, tmp_path
        )
        raw = _make_stream(Sample, count=5)
        ctx = PipelineRunContext(pipeline_name="pt-pipe")
        stats = PipelineStats(run_id="r1", pipeline_name="pt-pipe", schedule_type="batch")

        executor._process_chunk(
            raw, {}, ser_in, [], ser_out, sinks, ctx, "continue",
            passthrough=True, stats=stats,
        )

        assert ctx.records_in == 5
        assert ctx.records_out == 5
        assert ctx.records_skipped == 0
        assert ctx.bytes_in == len(raw)
        assert ctx.bytes_out == len(raw)
        assert stats.records_in == 5
        assert stats.records_out == 5
        assert stats.bytes_in == len(raw)
        assert stats.bytes_out == len(raw)

    def test_sink_meta_carries_output_record_count(self, schema_a, tmp_path):
        executor, _config, ser_in, ser_out, sinks, Sample = _build_executor_pieces(
            schema_a, tmp_path
        )
        mock_sink = MagicMock()
        sink_cfg = sinks[0][3]
        sinks = [(mock_sink, None, [], sink_cfg, None)]
        raw = _make_stream(Sample, count=4)
        ctx = PipelineRunContext(pipeline_name="pt-pipe")

        executor._process_chunk(
            raw, {}, ser_in, [], ser_out, sinks, ctx, "continue", passthrough=True
        )

        _, sink_meta = mock_sink.write.call_args[0]
        assert sink_meta["output_record_count"] == 4
        assert sink_meta["serializer_type"] == "protobuf"

    def test_sink_write_retries_flow_through(self, schema_a, tmp_path):
        executor, _config, ser_in, ser_out, sinks, Sample = _build_executor_pieces(
            schema_a, tmp_path
        )
        mock_sink = MagicMock()
        from tram.core.exceptions import SinkError

        mock_sink.write.side_effect = [SinkError("boom"), None]
        sink_cfg = sinks[0][3]
        sink_cfg.retry_count = 2
        sink_cfg.retry_delay_seconds = 0.0
        sinks = [(mock_sink, None, [], sink_cfg, None)]
        raw = _make_stream(Sample, count=3)
        ctx = PipelineRunContext(pipeline_name="pt-pipe")

        with patch("tram.pipeline.protobuf_passthrough.time.sleep"):
            result = executor._process_chunk(
                raw, {}, ser_in, [], ser_out, sinks, ctx, "continue", passthrough=True
            )

        assert result is True
        assert mock_sink.write.call_count == 2  # one failure, one success
        assert ctx.records_in == 3
        assert ctx.records_out == 3

    def test_batch_dispatch_runs_passthrough(self, schema_a, tmp_path):
        """The full _run_batch_chunks dispatch (re-check + flag threading)."""
        executor, config, ser_in, ser_out, sinks, Sample = _build_executor_pieces(
            schema_a, tmp_path
        )
        raw = _make_stream(Sample, count=4)
        mock_source = MagicMock()
        mock_source.read.return_value = iter([(raw, {"source_filename": "in.bin"})])
        ctx = PipelineRunContext(pipeline_name="pt-pipe")

        executor._run_batch_chunks(
            config, mock_source, sinks, ser_in, ser_out, [], None, ctx
        )

        assert ctx.records_in == 4
        assert ctx.records_out == 4
        files = list((tmp_path / "out").iterdir())
        assert len(files) == 1
        assert files[0].read_bytes() == raw


# ── Malformed-frame parity with the dictionary path ─────────────────────────


class TestMalformedFrameParity:
    @pytest.mark.parametrize(
        "bad_raw",
        [
            b"\x00\x00",  # truncated length prefix
            _frame(b"\x08"),  # frame content: field-1 varint with no value bytes
        ],
    )
    def test_parse_error_semantics_match_dictionary_path(self, schema_a, tmp_path, bad_raw):
        Sample = _build_sample_msg_class()

        # Passthrough path.
        executor, _config, ser_in, ser_out, sinks, _ = _build_executor_pieces(
            schema_a, tmp_path
        )
        ser_in._get_message_class = lambda: Sample
        ctx_pt = PipelineRunContext(pipeline_name="pt-pipe")
        with pytest.raises(TramError, match="Parse error") as pt_exc:
            executor._process_chunk(
                bad_raw, {}, ser_in, [], ser_out, sinks, ctx_pt, "abort",
                passthrough=True,
            )

        # Dictionary path (flag off) — same input, same abort semantics.
        executor2, _config2, ser_in2, ser_out2, sinks2, _ = _build_executor_pieces(
            schema_a, tmp_path
        )
        ser_in2._get_message_class = lambda: Sample
        ctx_dict = PipelineRunContext(pipeline_name="pt-pipe")
        with pytest.raises(TramError, match="Parse error") as dict_exc:
            executor2._process_chunk(
                bad_raw, {}, ser_in2, [], ser_out2, sinks2, ctx_dict, "abort"
            )

        assert str(pt_exc.value) == str(dict_exc.value)
        assert ctx_pt.records_in == 0
        assert ctx_dict.records_in == 0
        assert ctx_pt.records_out == 0

    def test_malformed_frame_continue_records_error(self, schema_a, tmp_path):
        executor, _config, ser_in, ser_out, sinks, _ = _build_executor_pieces(
            schema_a, tmp_path
        )
        ctx = PipelineRunContext(pipeline_name="pt-pipe")

        result = executor._process_chunk(
            _frame(b"\x08"), {}, ser_in, [], ser_out, sinks, ctx, "continue",
            passthrough=True,
        )

        assert result is False
        assert ctx.records_in == 0
        assert ctx.records_out == 0
        assert ctx.records_skipped == 1
        assert len(ctx.errors) == 1


# ── Flag off and runtime fallback ───────────────────────────────────────────


class TestFlagOffAndFallback:
    def test_flag_off_uses_dictionary_path(self, schema_a, tmp_path):
        executor, config, ser_in, ser_out, sinks, Sample = _build_executor_pieces(
            schema_a, tmp_path
        )
        config.protobuf_passthrough = False
        raw = _make_stream(Sample, count=3)
        mock_source = MagicMock()
        mock_source.read.return_value = iter([(raw, {})])
        ctx = PipelineRunContext(pipeline_name="pt-pipe")

        with (
            patch("tram.pipeline.protobuf_passthrough.process_chunk") as mock_pp,
            patch.object(ser_in, "parse", wraps=ser_in.parse) as parse_spy,
        ):
            executor._run_batch_chunks(
                config, mock_source, sinks, ser_in, ser_out, [], None, ctx
            )

        mock_pp.assert_not_called()
        parse_spy.assert_called_once()
        # Byte-identical output through the dictionary round trip.
        assert ctx.records_in == 3
        assert ctx.records_out == 3
        files = list((tmp_path / "out").iterdir())
        assert len(files) == 1
        assert files[0].read_bytes() == raw

    def test_runtime_fallback_warns_and_uses_dictionary_path(
        self, schema_a, tmp_path, caplog
    ):
        executor, config, ser_in, ser_out, sinks, Sample = _build_executor_pieces(
            schema_a, tmp_path
        )
        # Eligible at registration; break the runtime re-check only.
        config.record_chunk_size = 2
        raw = _make_stream(Sample, count=3)
        mock_source = MagicMock()
        mock_source.read.return_value = iter([(raw, {})])
        ctx = PipelineRunContext(pipeline_name="pt-pipe")

        with caplog.at_level(logging.WARNING, logger="tram.pipeline.executor"):
            executor._run_batch_chunks(
                config, mock_source, sinks, ser_in, ser_out, [], None, ctx
            )

        fallback = [
            r for r in caplog.records if "falling back to the dictionary path" in r.getMessage()
        ]
        assert fallback
        assert "record_chunk_size" in fallback[0].reasons
        # The dictionary path actually ran (incremental parse_chunks entry).
        assert ctx.records_in == 3
        assert ctx.records_out == 3