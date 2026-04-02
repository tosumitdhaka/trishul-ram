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
