"""Tests for Protobuf serializer."""
from __future__ import annotations

import struct
import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.core.exceptions import SerializerError
from tram.serializers.protobuf_serializer import ProtobufSerializer


class TestProtobufSerializer:
    def test_missing_schema_file_raises(self):
        with pytest.raises(SerializerError, match="schema_file"):
            ProtobufSerializer({"message_class": "Foo"})

    def test_missing_message_class_raises(self):
        with pytest.raises(SerializerError, match="message_class"):
            ProtobufSerializer({"schema_file": "foo.proto"})

    def test_grpctools_import_error(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('syntax = "proto3"; message Foo { int32 x = 1; }')
        with patch.dict(sys.modules, {"grpc_tools": None, "grpc_tools.protoc": None}):
            s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
            with pytest.raises(SerializerError, match="grpcio-tools"):
                s._compile_proto()

    def test_parse_length_delimited(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        # Mock the entire compile+parse chain
        mock_msg = MagicMock()
        mock_msg.ParseFromString.return_value = None
        mock_msg_class = MagicMock(return_value=mock_msg)
        mock_module = MagicMock()
        mock_module.Foo = mock_msg_class

        mock_json_format = MagicMock()
        mock_json_format.MessageToDict.return_value = {"x": 1}

        mock_google = MagicMock()
        mock_google.protobuf.json_format = mock_json_format

        with patch.dict(sys.modules, {
            "google": mock_google,
            "google.protobuf": mock_google.protobuf,
            "google.protobuf.json_format": mock_json_format,
        }):
            s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
            s._module = mock_module
            # Patch _compile_proto to return mock_module
            with patch.object(s, "_compile_proto", return_value=mock_module):
                proto_bytes = b"fake_proto"
                frame = struct.pack(">I", len(proto_bytes)) + proto_bytes
                result = s.parse(frame)
        assert result == [{"x": 1}]

    def test_serialize_length_delimited(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        mock_msg = MagicMock()
        mock_msg.SerializeToString.return_value = b"proto_bytes"
        mock_msg_class = MagicMock(return_value=mock_msg)
        mock_module = MagicMock()
        mock_module.Foo = mock_msg_class

        mock_json_format = MagicMock()
        mock_json_format.ParseDict.return_value = mock_msg

        mock_google = MagicMock()
        mock_google.protobuf.json_format = mock_json_format

        with patch.dict(sys.modules, {
            "google": mock_google,
            "google.protobuf": mock_google.protobuf,
            "google.protobuf.json_format": mock_json_format,
        }):
            s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
            with patch.object(s, "_compile_proto", return_value=mock_module):
                result = s.serialize([{"x": 1}])
        expected_len = struct.pack(">I", len(b"proto_bytes"))
        assert result == expected_len + b"proto_bytes"

    def test_empty_data_returns_empty_list(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        mock_module = MagicMock()
        mock_json_format = MagicMock()
        mock_google = MagicMock()
        mock_google.protobuf.json_format = mock_json_format
        with patch.dict(sys.modules, {
            "google": mock_google,
            "google.protobuf": mock_google.protobuf,
            "google.protobuf.json_format": mock_json_format,
        }):
            s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
            with patch.object(s, "_compile_proto", return_value=mock_module):
                result = s.parse(b"")
        assert result == []

    def test_google_protobuf_import_error_in_compile(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        mock_grpc = MagicMock()
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        with patch.dict(sys.modules, {
            "grpc_tools": mock_grpc,
            "grpc_tools.protoc": mock_grpc.protoc,
            "google": None,
            "google.protobuf": None,
        }):
            with pytest.raises(SerializerError, match="protobuf"):
                s._compile_proto()

    def test_schema_file_not_found_raises(self, tmp_path):
        s = ProtobufSerializer({"schema_file": str(tmp_path / "missing.proto"), "message_class": "Foo"})
        mock_grpc = MagicMock()
        mock_pb = MagicMock()
        with patch.dict(sys.modules, {
            "grpc_tools": mock_grpc,
            "grpc_tools.protoc": mock_grpc.protoc,
            "google": mock_pb,
            "google.protobuf": mock_pb,
        }):
            with pytest.raises(SerializerError, match="not found"):
                s._compile_proto()

    def test_cache_hit_skips_compilation(self, tmp_path):
        from tram.serializers.protobuf_serializer import _MODULE_CACHE
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        mock_module = MagicMock()
        mtime = proto_file.stat().st_mtime
        cache_key = (s.schema_file, mtime)
        _MODULE_CACHE[cache_key] = mock_module
        mock_grpc = MagicMock()
        mock_pb = MagicMock()
        try:
            with patch.dict(sys.modules, {
                "grpc_tools": mock_grpc,
                "grpc_tools.protoc": mock_grpc.protoc,
                "google": mock_pb,
                "google.protobuf": mock_pb,
            }):
                result = s._compile_proto()
            assert result is mock_module
            mock_grpc.protoc.main.assert_not_called()
        finally:
            _MODULE_CACHE.pop(cache_key, None)

    def test_protoc_failure_raises(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        mock_grpc = MagicMock()
        mock_grpc.protoc.main.return_value = 1
        mock_pb = MagicMock()
        with patch.dict(sys.modules, {
            "grpc_tools": mock_grpc,
            "grpc_tools.protoc": mock_grpc.protoc,
            "google": mock_pb,
            "google.protobuf": mock_pb,
        }), patch("atexit.register"), patch("shutil.rmtree"):
            with pytest.raises(SerializerError, match="protoc compilation failed"):
                s._compile_proto()

    def test_import_module_failure_raises(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        mock_grpc = MagicMock()
        mock_grpc.protoc.main.return_value = 0
        mock_pb = MagicMock()
        with patch.dict(sys.modules, {
            "grpc_tools": mock_grpc,
            "grpc_tools.protoc": mock_grpc.protoc,
            "google": mock_pb,
            "google.protobuf": mock_pb,
        }), patch("atexit.register"), patch("shutil.rmtree"), \
           patch("importlib.import_module", side_effect=ImportError("no such module")):
            with pytest.raises(SerializerError, match="Failed to import compiled proto module"):
                s._compile_proto()

    def test_get_message_class_attribute_error(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "NoSuchClass"})
        mock_module = MagicMock(spec=[])  # no attributes
        with patch.object(s, "_compile_proto", return_value=mock_module):
            with pytest.raises(SerializerError, match="not found in compiled proto module"):
                s._get_message_class()

    def test_registry_schema_id_no_url(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        assert s._get_registry_schema_id() is None

    def test_registry_schema_id_cached(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({
            "schema_file": str(proto_file),
            "message_class": "Foo",
            "schema_registry_url": "http://registry:8081",
        })
        s._registry_schema_id = 55
        assert s._get_registry_schema_id() == 55

    def test_registry_schema_id_from_subject(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({
            "schema_file": str(proto_file),
            "message_class": "Foo",
            "schema_registry_url": "http://registry:8081",
            "schema_registry_subject": "my-topic-value",
        })
        mock_client = MagicMock()
        mock_client.get_latest_schema.return_value = (99, "schema")
        with patch("tram.schema_registry.client.SchemaRegistryClient", return_value=mock_client):
            result = s._get_registry_schema_id()
        assert result == 99

    def test_registry_schema_id_from_explicit_id(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({
            "schema_file": str(proto_file),
            "message_class": "Foo",
            "schema_registry_url": "http://registry:8081",
            "schema_registry_id": 7,
        })
        mock_client = MagicMock()
        with patch("tram.schema_registry.client.SchemaRegistryClient", return_value=mock_client):
            result = s._get_registry_schema_id()
        assert result == 7

    def test_parse_import_error(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        with patch.dict(sys.modules, {"google.protobuf.json_format": None}):
            with pytest.raises(SerializerError, match="requires protobuf"):
                s.parse(b"")

    def test_parse_magic_bytes_stripped(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({
            "schema_file": str(proto_file),
            "message_class": "Foo",
            "schema_registry_url": "http://registry:8081",
            "use_magic_bytes": True,
        })
        mock_msg_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.ParseFromString.return_value = None
        mock_msg_cls.return_value = mock_instance
        mock_json_format = MagicMock()
        mock_json_format.MessageToDict.return_value = {"field": "val"}

        inner = struct.pack(">I", 2) + b"\x08\x01"
        magic_data = b"\x00\x00\x00\x00\x05" + inner

        with patch.object(s, "_get_message_class", return_value=mock_msg_cls), \
             patch("tram.schema_registry.client.decode_magic", return_value=(5, inner)), \
             patch.dict(sys.modules, {"google.protobuf.json_format": mock_json_format}):
            result = s.parse(magic_data)
        assert result == [{"field": "val"}]

    def test_parse_framing_none(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({
            "schema_file": str(proto_file),
            "message_class": "Foo",
            "framing": "none",
        })
        mock_msg_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.ParseFromString.return_value = None
        mock_msg_cls.return_value = mock_instance
        mock_json_format = MagicMock()
        mock_json_format.MessageToDict.return_value = {"val": 1}

        with patch.object(s, "_get_message_class", return_value=mock_msg_cls), \
             patch.dict(sys.modules, {"google.protobuf.json_format": mock_json_format}):
            result = s.parse(b"\x08\x01")
        assert result == [{"val": 1}]

    def test_parse_framing_none_error(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({
            "schema_file": str(proto_file),
            "message_class": "Foo",
            "framing": "none",
        })
        mock_msg_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.ParseFromString.side_effect = Exception("bad proto")
        mock_msg_cls.return_value = mock_instance
        mock_json_format = MagicMock()

        with patch.object(s, "_get_message_class", return_value=mock_msg_cls), \
             patch.dict(sys.modules, {"google.protobuf.json_format": mock_json_format}):
            with pytest.raises(SerializerError, match="Protobuf parse error"):
                s.parse(b"\x08\x01")

    def test_parse_truncated_length_prefix(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        mock_json_format = MagicMock()
        with patch.object(s, "_get_message_class", return_value=MagicMock()), \
             patch.dict(sys.modules, {"google.protobuf.json_format": mock_json_format}):
            with pytest.raises(SerializerError, match="Truncated length prefix"):
                s.parse(b"\x00\x00")  # only 2 bytes

    def test_parse_truncated_record(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        mock_json_format = MagicMock()
        bad = struct.pack(">I", 10) + b"\x01\x02"  # claims 10 bytes, only 2
        with patch.object(s, "_get_message_class", return_value=MagicMock()), \
             patch.dict(sys.modules, {"google.protobuf.json_format": mock_json_format}):
            with pytest.raises(SerializerError, match="Truncated protobuf record"):
                s.parse(bad)

    def test_parse_record_decode_error(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        mock_msg_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.ParseFromString.side_effect = Exception("decode error")
        mock_msg_cls.return_value = mock_instance
        mock_json_format = MagicMock()
        data = struct.pack(">I", 2) + b"\x08\x01"
        with patch.object(s, "_get_message_class", return_value=mock_msg_cls), \
             patch.dict(sys.modules, {"google.protobuf.json_format": mock_json_format}):
            with pytest.raises(SerializerError, match="Protobuf parse error"):
                s.parse(data)

    def test_serialize_import_error(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        with patch.dict(sys.modules, {"google.protobuf.json_format": None}):
            with pytest.raises(SerializerError, match="requires protobuf"):
                s.serialize([{"val": 1}])

    def test_serialize_record_error(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({"schema_file": str(proto_file), "message_class": "Foo"})
        mock_json_format = MagicMock()
        mock_json_format.ParseDict.side_effect = Exception("bad field")
        with patch.object(s, "_get_message_class", return_value=MagicMock()), \
             patch.dict(sys.modules, {"google.protobuf.json_format": mock_json_format}):
            with pytest.raises(SerializerError, match="Protobuf serialize error"):
                s.serialize([{"val": "bad"}])

    def test_serialize_adds_magic_bytes(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({
            "schema_file": str(proto_file),
            "message_class": "Foo",
            "schema_registry_url": "http://registry:8081",
            "use_magic_bytes": True,
        })
        mock_msg_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.SerializeToString.return_value = b"\x08\x01"
        mock_msg_cls.return_value = mock_instance
        mock_json_format = MagicMock()
        mock_json_format.ParseDict.return_value = mock_instance

        with patch.object(s, "_get_message_class", return_value=mock_msg_cls), \
             patch.object(s, "_get_registry_schema_id", return_value=5), \
             patch("tram.schema_registry.client.encode_with_magic", return_value=b"\x00magic") as mock_enc, \
             patch.dict(sys.modules, {"google.protobuf.json_format": mock_json_format}):
            result = s.serialize([{"val": 1}])
        mock_enc.assert_called_once()
        assert result == b"\x00magic"

    def test_serialize_no_magic_when_no_registry_id(self, tmp_path):
        proto_file = tmp_path / "test.proto"
        proto_file.write_text('')
        s = ProtobufSerializer({
            "schema_file": str(proto_file),
            "message_class": "Foo",
            "schema_registry_url": "http://registry:8081",
            "use_magic_bytes": True,
        })
        mock_msg_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.SerializeToString.return_value = b"\x08\x01"
        mock_msg_cls.return_value = mock_instance
        mock_json_format = MagicMock()
        mock_json_format.ParseDict.return_value = mock_instance

        with patch.object(s, "_get_message_class", return_value=mock_msg_cls), \
             patch.object(s, "_get_registry_schema_id", return_value=None), \
             patch.dict(sys.modules, {"google.protobuf.json_format": mock_json_format}):
            result = s.serialize([{"val": 1}])
        # No magic, raw length-delimited
        assert result[:4] == struct.pack(">I", 2)
