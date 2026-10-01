"""Wave 2 (GH #83): protobuf E2E amplification — batch decode/encode equivalence
and the `preserve_keys` wire convention.

These tests drive the REAL protobuf library (dynamic descriptors built via
descriptor_pb2/message_factory — no protoc needed) so the batch decode path and
the snake_case<->lowerCamelCase conversion behave exactly as in production.

Requires the `protobuf` runtime package (the `tram[protobuf]` extra); skipped
where it is not installed. Config-model/A.1 coverage lives in
test_protobuf_config_schema.py, which has no protobuf dependency.
"""
from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest

pytest.importorskip("google.protobuf")

from tram.serializers.protobuf_serializer import ProtobufSerializer  # noqa: E402


def _build_event_msg_class():
    """Dynamically build a proto3 message with nested/repeated fields without
    requiring grpcio-tools (protoc). Mirrors:
        message NestedInfo { string detail_msg = 1; int32 level = 2; }
        message DeviceEvent {
          string event_type = 1;
          uint64 timestamp_ms = 2;
          repeated string tags = 3;
          NestedInfo nested_info = 4;
          float severity = 5;
          bool active = 6;
        }
    """
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    pool = descriptor_pool.DescriptorPool()
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "test_events.proto"
    file_proto.package = "bench"
    file_proto.syntax = "proto3"

    nested = file_proto.message_type.add()
    nested.name = "NestedInfo"
    for name, number, ftype in (
        ("detail_msg", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING),
        ("level", 2, descriptor_pb2.FieldDescriptorProto.TYPE_INT32),
    ):
        f = nested.field.add()
        f.name = name
        f.number = number
        f.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
        f.type = ftype

    evt = file_proto.message_type.add()
    evt.name = "DeviceEvent"
    fields = [
        ("event_type", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING, None, False),
        ("timestamp_ms", 2, descriptor_pb2.FieldDescriptorProto.TYPE_UINT64, None, False),
        ("tags", 3, descriptor_pb2.FieldDescriptorProto.TYPE_STRING, None, True),
        ("nested_info", 4, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".bench.NestedInfo", False),
        ("severity", 5, descriptor_pb2.FieldDescriptorProto.TYPE_FLOAT, None, False),
        ("active", 6, descriptor_pb2.FieldDescriptorProto.TYPE_BOOL, None, False),
    ]
    for name, number, ftype, type_name, repeated in fields:
        f = evt.field.add()
        f.name = name
        f.number = number
        f.label = (
            descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
            if repeated
            else descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
        )
        f.type = ftype
        if type_name:
            f.type_name = type_name
    pool.Add(file_proto)
    return message_factory.GetMessageClass(
        pool.FindMessageTypeByName("bench.DeviceEvent")
    )


def _frame(proto_bytes: bytes) -> bytes:
    return struct.pack(">I", len(proto_bytes)) + proto_bytes


def _make_records(DeviceEvent) -> list:
    """Three distinct records covering scalars, nested, repeated, and unset fields."""
    recs = []
    m = DeviceEvent()
    m.event_type = "fault_alarm"
    m.timestamp_ms = 1720000000123
    m.tags.extend(["shelf-1", "port-3"])
    m.nested_info.detail_msg = "eth down"
    m.nested_info.level = 3
    m.severity = 0.5
    m.active = True
    recs.append(m)
    m = DeviceEvent()
    m.event_type = "state_change"
    m.timestamp_ms = 1720000000200
    m.tags.append("shelf-2")
    m.nested_info.level = 7
    recs.append(m)  # detail_msg / severity / active unset (proto3 defaults)
    m = DeviceEvent()
    m.event_type = "cleared"
    m.active = False
    recs.append(m)  # mostly empty — exercises omitted/default fields
    return recs


def _stream(records) -> bytes:
    return b"".join(_frame(r.SerializeToString()) for r in records)


def _per_record_decode(DeviceEvent, data: bytes, preserve_keys: bool = False):
    """Reference implementation of the OLD per-record decode loop (fresh
    message instance + plain MessageToDict per record)."""
    from google.protobuf.json_format import MessageToDict

    out = []
    buf = memoryview(data)
    offset = 0
    while offset < len(buf):
        (length,) = struct.unpack_from(">I", buf, offset)
        offset += 4
        msg = DeviceEvent()  # fresh instance per record (old behavior)
        msg.ParseFromString(buf[offset:offset + length])
        out.append(MessageToDict(msg, preserving_proto_field_name=preserve_keys))
        offset += length
    return out


def _per_record_encode(DeviceEvent, records: list[dict]) -> bytes:
    """Reference implementation of the OLD per-record encode loop (fresh
    message instance + BytesIO-style append per record)."""
    from google.protobuf.json_format import ParseDict

    out = b""
    for rec in records:
        msg = ParseDict(rec, DeviceEvent())  # fresh instance per record
        proto_bytes = msg.SerializeToString()
        out += _frame(proto_bytes)
    return out


def _make_serializer(proto_file, preserve_keys: bool = False) -> ProtobufSerializer:
    return ProtobufSerializer({
        "schema_file": str(proto_file),
        "message_class": "DeviceEvent",
        "preserve_keys": preserve_keys,
    })


@pytest.fixture
def proto_file(tmp_path):
    f = tmp_path / "test.proto"
    f.write_text("syntax = 'proto3';")
    return f


class TestBatchDecodeEquivalence:
    def test_batch_decode_matches_per_record_decode(self, proto_file, monkeypatch):
        DeviceEvent = _build_event_msg_class()
        records = _make_records(DeviceEvent)
        stream = _stream(records)

        s = _make_serializer(proto_file)
        monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
        decoded = s.parse(stream)

        expected = _per_record_decode(DeviceEvent, stream)
        assert decoded == expected  # field-by-field, nested values included
        assert len(decoded) == 3

    def test_batch_decode_reuses_one_message_instance(self, proto_file, monkeypatch):
        """The batch path must construct a single message object for the whole
        stream instead of one per record (the per-record construction overhead
        the perf issue names)."""
        DeviceEvent = _build_event_msg_class()
        stream = _stream(_make_records(DeviceEvent))

        # _get_message_class must return a CLASS the serializer instantiates;
        # message classes cannot be subclassed, so track constructions with a
        # MagicMock returning one shared real instance (the reuse semantics).
        tracking = MagicMock(return_value=DeviceEvent())

        s = _make_serializer(proto_file)
        monkeypatch.setattr(s, "_get_message_class", lambda: tracking)
        s.parse(stream)
        assert tracking.call_count == 1  # 3 records decoded with one instance

    def test_empty_stream_stays_empty(self, proto_file, monkeypatch):
        DeviceEvent = _build_event_msg_class()
        s = _make_serializer(proto_file)
        monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
        assert s.parse(b"") == []

    def test_batch_decode_keeps_records_isolated(self, proto_file, monkeypatch):
        """Instance reuse must not leak values between records: the second
        frame re-serialized must not carry fields the first record set."""
        DeviceEvent = _build_event_msg_class()
        m0 = DeviceEvent()
        m0.event_type = "a"
        m0.active = True
        m0.nested_info.detail_msg = "stale"
        m0.tags.append("t0")
        m1 = DeviceEvent()
        m1.event_type = "b"
        stream = _stream([m0, m1])

        s = _make_serializer(proto_file)
        monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
        s.parse(stream)  # must not error, and must not smear m0 into m1

        # m1's frame, re-decoded fresh, must hold only its own fields
        # (skip frame0, then m1's own 4-byte length prefix).
        fresh = DeviceEvent()
        fresh.ParseFromString(stream[len(_frame(m0.SerializeToString())) + 4:])
        assert fresh.event_type == "b"
        assert fresh.active is False
        assert fresh.nested_info.detail_msg == ""
        assert list(fresh.tags) == []


class TestPreserveKeys:
    def test_default_preserves_current_camel_case_wire_convention(self, proto_file, monkeypatch):
        DeviceEvent = _build_event_msg_class()
        records = _make_records(DeviceEvent)
        stream = _stream(records)

        s = _make_serializer(proto_file)  # preserve_keys defaults to False
        assert s.preserve_keys is False
        monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
        decoded = s.parse(stream)

        assert set(decoded[0]) == {
            "eventType", "timestampMs", "tags", "nestedInfo", "severity", "active",
        }
        assert decoded[0]["nestedInfo"]["detailMsg"] == "eth down"
        assert decoded[0]["eventType"] == "fault_alarm"
        # same output as the plain MessageToDict reference (old behavior)
        assert decoded == _per_record_decode(DeviceEvent, stream)

    def test_preserve_keys_true_keeps_snake_case(self, proto_file, monkeypatch):
        DeviceEvent = _build_event_msg_class()
        stream = _stream(_make_records(DeviceEvent))

        s = _make_serializer(proto_file, preserve_keys=True)
        monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
        decoded = s.parse(stream)

        assert set(decoded[0]) == {
            "event_type", "timestamp_ms", "tags", "nested_info", "severity", "active",
        }
        assert decoded[0]["nested_info"]["detail_msg"] == "eth down"
        assert decoded == _per_record_decode(DeviceEvent, stream, preserve_keys=True)

    def test_preserve_keys_values_identical_under_key_normalization(self, proto_file, monkeypatch):
        """Both settings must decode the same values — only key naming differs."""
        DeviceEvent = _build_event_msg_class()
        stream = _stream(_make_records(DeviceEvent))

        default_s = _make_serializer(proto_file)
        preserved_s = _make_serializer(proto_file, preserve_keys=True)
        monkeypatch.setattr(default_s, "_get_message_class", lambda: DeviceEvent)
        monkeypatch.setattr(preserved_s, "_get_message_class", lambda: DeviceEvent)
        camel = default_s.parse(stream)
        snake = preserved_s.parse(stream)

        def normalize(value):
            if isinstance(value, dict):
                # snake->stripped and camel->stripped differ in case — fold it.
                return {k.replace("_", "").lower(): normalize(v) for k, v in value.items()}
            if isinstance(value, list):
                return [normalize(v) for v in value]
            return value

        assert [normalize(r) for r in camel] == [normalize(r) for r in snake]

    def test_round_trip_bytes_identical_for_both_key_modes(self, proto_file, monkeypatch):
        """serialize(parse(x)) == x for both preserve_keys settings — decoded
        records re-encode to the exact original wire bytes."""
        DeviceEvent = _build_event_msg_class()
        records = _make_records(DeviceEvent)
        stream = _stream(records)

        for preserve in (False, True):
            s = _make_serializer(proto_file, preserve_keys=preserve)
            monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
            decoded = s.parse(stream)
            reencoded = s.serialize(decoded)
            assert reencoded == stream

    def test_framing_none_honors_preserve_keys(self, tmp_path, monkeypatch):
        from google.protobuf.json_format import MessageToDict

        DeviceEvent = _build_event_msg_class()
        records = _make_records(DeviceEvent)
        proto_file = tmp_path / "test.proto"
        proto_file.write_text("syntax = 'proto3';")
        s = ProtobufSerializer({
            "schema_file": str(proto_file),
            "message_class": "DeviceEvent",
            "framing": "none",
            "preserve_keys": True,
        })
        monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
        decoded = s.parse(records[0].SerializeToString())
        assert decoded == [MessageToDict(records[0], preserving_proto_field_name=True)]


class TestBatchEncodeEquivalence:
    def test_batch_encode_matches_per_record_encode(self, proto_file, monkeypatch):
        from google.protobuf.json_format import MessageToDict

        DeviceEvent = _build_event_msg_class()
        records = _make_records(DeviceEvent)

        camel_records = [MessageToDict(r) for r in records]  # upstream camelCase
        snake_records = [
            MessageToDict(r, preserving_proto_field_name=True) for r in records
        ]  # upstream snake_case

        for batch in (camel_records, snake_records):
            s = _make_serializer(proto_file)
            monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
            batched = s.serialize(batch)
            assert batched == _per_record_encode(DeviceEvent, batch)
            # and it equals the original wire stream (values preserved exactly)
            assert batched == _stream(records)

    def test_batch_encode_reuses_one_message_instance(self, proto_file, monkeypatch):
        """One message object per serialize() call (reused across the batch)."""
        from google.protobuf.json_format import MessageToDict

        DeviceEvent = _build_event_msg_class()
        records = _make_records(DeviceEvent)

        tracking = MagicMock(return_value=DeviceEvent())

        s = _make_serializer(proto_file)
        monkeypatch.setattr(s, "_get_message_class", lambda: tracking)
        out = s.serialize([MessageToDict(r) for r in records])
        assert tracking.call_count == 1  # reused across all 3 records
        assert out == _stream(records)

    def test_batch_encode_no_stale_values_between_records(self, proto_file, monkeypatch):
        """Instance reuse requires a fresh message per record: a record that
        omits a field the previous one set must not inherit it."""
        from google.protobuf.json_format import ParseDict

        DeviceEvent = _build_event_msg_class()
        records = [
            {"eventType": "a", "active": True, "nestedInfo": {"detailMsg": "stale"}},
            {"eventType": "b"},
        ]

        s = _make_serializer(proto_file)
        monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
        out = s.serialize(records)

        first = ParseDict(records[0], DeviceEvent())
        frame0 = _frame(first.SerializeToString())
        second = DeviceEvent()
        # Skip frame0 plus record 1's own 4-byte length prefix.
        second.ParseFromString(out[len(frame0) + 4:])
        assert second.event_type == "b"
        assert second.active is False  # no carry-over from record 0
        assert second.nested_info.detail_msg == ""

    def test_serialize_empty_batch(self, proto_file, monkeypatch):
        DeviceEvent = _build_event_msg_class()
        s = _make_serializer(proto_file)
        monkeypatch.setattr(s, "_get_message_class", lambda: DeviceEvent)
        assert s.serialize([]) == b""