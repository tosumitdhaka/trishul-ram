"""Tests for MessagePack serializer."""
from __future__ import annotations
import sys
from unittest.mock import MagicMock, patch
import pytest
from tram.serializers.msgpack_serializer import MsgpackSerializer
from tram.core.exceptions import SerializerError

class TestMsgpackSerializer:
    def test_parse_list(self):
        mock_msgpack = MagicMock()
        mock_msgpack.unpackb.return_value = [{"x": 1}, {"x": 2}]
        with patch.dict(sys.modules, {"msgpack": mock_msgpack}):
            s = MsgpackSerializer({})
            result = s.parse(b"packed")
        assert result == [{"x": 1}, {"x": 2}]

    def test_parse_dict_wrapped(self):
        mock_msgpack = MagicMock()
        mock_msgpack.unpackb.return_value = {"x": 1}
        with patch.dict(sys.modules, {"msgpack": mock_msgpack}):
            s = MsgpackSerializer({})
            result = s.parse(b"packed")
        assert result == [{"x": 1}]

    def test_serialize(self):
        mock_msgpack = MagicMock()
        mock_msgpack.packb.return_value = b"packed_data"
        with patch.dict(sys.modules, {"msgpack": mock_msgpack}):
            s = MsgpackSerializer({})
            result = s.serialize([{"x": 1}])
        assert result == b"packed_data"
        mock_msgpack.packb.assert_called_once_with([{"x": 1}], use_bin_type=True)

    def test_parse_import_error(self):
        with patch.dict(sys.modules, {"msgpack": None}):
            s = MsgpackSerializer({})
            with pytest.raises(SerializerError, match="msgpack"):
                s.parse(b"data")

    def test_serialize_import_error(self):
        with patch.dict(sys.modules, {"msgpack": None}):
            s = MsgpackSerializer({})
            with pytest.raises(SerializerError, match="msgpack"):
                s.serialize([{"x": 1}])
