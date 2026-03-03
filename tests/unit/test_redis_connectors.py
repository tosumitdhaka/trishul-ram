"""Tests for Redis source and sink connectors."""
from __future__ import annotations
import json
import sys
from unittest.mock import MagicMock, patch
import pytest
from tram.connectors.redis.source import RedisSource
from tram.connectors.redis.sink import RedisSink
from tram.core.exceptions import SinkError, SourceError


class TestRedisSource:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"redis": None}):
            source = RedisSource({"key": "mylist"})
            with pytest.raises(SourceError, match="redis"):
                list(source.read())

    def test_list_mode_lrange(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_client.lrange.return_value = [b'{"x":1}', b'{"x":2}']
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            source = RedisSource({"key": "mylist", "mode": "list"})
            results = list(source.read())

        assert len(results) == 1
        mock_client.lrange.assert_called_once_with("mylist", 0, 99)

    def test_list_mode_delete_after_read(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_client.lrange.return_value = [b"item1"]
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            source = RedisSource({"key": "mylist", "delete_after_read": True})
            list(source.read())

        mock_client.delete.assert_called_once_with("mylist")

    def test_stream_mode_xread(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        # First call returns data, second call raises to stop iteration (or we test with a finite iterator)
        msg_id = b"1234-0"
        fields = {b"data": b'{"x":1}'}
        # Make xread raise StopIteration on second call
        call_count = [0]
        def fake_xread(streams, count, block):
            call_count[0] += 1
            if call_count[0] == 1:
                return [(b"mystream", [(msg_id, fields)])]
            raise SourceError("stop")
        mock_client.xread.side_effect = fake_xread
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            source = RedisSource({"key": "mystream", "mode": "stream"})
            with pytest.raises(SourceError):
                results = list(source.read())

    def test_invalid_mode_raises(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            source = RedisSource({"key": "k", "mode": "invalid"})
            with pytest.raises(SourceError, match="unknown mode"):
                list(source.read())

    def test_meta_has_key(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_client.lrange.return_value = [b"data"]
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            source = RedisSource({"key": "mykey"})
            _, meta = list(source.read())[0]

        assert meta["redis_key"] == "mykey"


class TestRedisSink:
    def test_import_error_raises_sink_error(self):
        with patch.dict(sys.modules, {"redis": None}):
            sink = RedisSink({"key": "mylist"})
            with pytest.raises(SinkError, match="redis"):
                sink.write(b"data", {})

    def test_list_mode_rpush(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            sink = RedisSink({"key": "mylist", "mode": "list"})
            sink.write(b'{"x":1}', {})

        mock_client.rpush.assert_called_once_with("mylist", b'{"x":1}')

    def test_pubsub_mode_publish(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            sink = RedisSink({"key": "mychannel", "mode": "pubsub"})
            sink.write(b'{"x":1}', {})

        mock_client.publish.assert_called_once_with("mychannel", b'{"x":1}')

    def test_stream_mode_xadd(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            sink = RedisSink({"key": "mystream", "mode": "stream"})
            sink.write(b'{"x":1}', {})

        mock_client.xadd.assert_called_once()
        call_kwargs = mock_client.xadd.call_args[1]
        assert call_kwargs["name"] == "mystream"
        assert call_kwargs["fields"] == {"data": b'{"x":1}'}

    def test_stream_mode_max_len(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            sink = RedisSink({"key": "mystream", "mode": "stream", "max_len": 1000})
            sink.write(b"data", {})

        call_kwargs = mock_client.xadd.call_args[1]
        assert call_kwargs["maxlen"] == 1000

    def test_invalid_mode_raises(self):
        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_redis.Redis.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            sink = RedisSink({"key": "k", "mode": "invalid"})
            with pytest.raises(SinkError, match="unknown mode"):
                sink.write(b"data", {})
