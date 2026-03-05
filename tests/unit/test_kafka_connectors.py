"""Tests for Kafka source connector."""
from __future__ import annotations
import sys
from unittest.mock import MagicMock, patch
import pytest
from tram.connectors.kafka.source import KafkaSource
from tram.core.exceptions import SourceError


def _make_source(extra: dict | None = None) -> KafkaSource:
    cfg = {"brokers": ["kafka:9092"], "topic": "events"}
    if extra:
        cfg.update(extra)
    return KafkaSource(cfg)


class TestKafkaSourceGroupId:
    def test_default_group_id_uses_pipeline_name(self):
        src = KafkaSource({"brokers": ["b:9092"], "topic": "t", "_pipeline_name": "pm-ingest"})
        assert src.group_id == "pm-ingest"

    def test_explicit_group_id_overrides_pipeline_name(self):
        src = KafkaSource({"brokers": ["b:9092"], "topic": "t",
                           "group_id": "my-group", "_pipeline_name": "pm-ingest"})
        assert src.group_id == "my-group"

    def test_no_pipeline_name_fallback_to_tram(self):
        src = KafkaSource({"brokers": ["b:9092"], "topic": "t"})
        assert src.group_id == "tram"

    def test_none_group_id_falls_back_to_pipeline_name(self):
        # model_dump() returns None when group_id not set by user
        src = KafkaSource({"brokers": ["b:9092"], "topic": "t",
                           "group_id": None, "_pipeline_name": "fm-collect"})
        assert src.group_id == "fm-collect"

    def test_empty_string_group_id_falls_back_to_pipeline_name(self):
        # Empty string is also treated as "not set" since Kafka rejects empty group_id
        src = KafkaSource({"brokers": ["b:9092"], "topic": "t",
                           "group_id": "", "_pipeline_name": "my-pipeline"})
        assert src.group_id == "my-pipeline"


class TestKafkaSourceConfig:
    def test_topic_string_wrapped_in_list(self):
        src = _make_source()
        assert src.topics == ["events"]

    def test_topic_list_preserved(self):
        src = KafkaSource({"brokers": ["b:9092"], "topic": ["a", "b"]})
        assert src.topics == ["a", "b"]

    def test_brokers_string_wrapped(self):
        src = KafkaSource({"brokers": "kafka:9092", "topic": "t"})
        assert src.brokers == ["kafka:9092"]

    def test_defaults(self):
        src = _make_source()
        assert src.auto_offset_reset == "latest"
        assert src.enable_auto_commit is True
        assert src.max_poll_records == 500
        assert src.security_protocol == "PLAINTEXT"


class TestKafkaSourceImportError:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"kafka": None}):
            src = _make_source()
            with pytest.raises(SourceError, match="kafka-python"):
                list(src.read())


class TestKafkaSourceRead:
    def _mock_consumer(self, messages):
        mock_msg = MagicMock()
        mock_msg.value = b'{"x":1}'
        mock_msg.topic = "events"
        mock_msg.partition = 0
        mock_msg.offset = 0
        mock_msg.key = None

        mock_consumer = MagicMock()
        mock_consumer.__iter__ = MagicMock(return_value=iter(messages))

        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer
        return mock_kafka, mock_consumer

    def test_read_yields_messages(self):
        mock_msg = MagicMock()
        mock_msg.value = b'{"x":1}'
        mock_msg.topic = "events"
        mock_msg.partition = 0
        mock_msg.offset = 42
        mock_msg.key = b"mykey"

        mock_consumer = MagicMock()
        mock_consumer.__iter__ = MagicMock(return_value=iter([mock_msg]))
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source({"_pipeline_name": "pm-ingest"})
            results = list(src.read())

        assert len(results) == 1
        payload, meta = results[0]
        assert payload == b'{"x":1}'
        assert meta["kafka_topic"] == "events"
        assert meta["kafka_offset"] == 42
        assert meta["kafka_key"] == "mykey"

    def test_skips_none_value_messages(self):
        mock_msg = MagicMock()
        mock_msg.value = None

        mock_consumer = MagicMock()
        mock_consumer.__iter__ = MagicMock(return_value=iter([mock_msg]))
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source()
            results = list(src.read())

        assert results == []

    def test_consumer_commit_called_on_close(self):
        mock_consumer = MagicMock()
        mock_consumer.__iter__ = MagicMock(return_value=iter([]))
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source()
            list(src.read())

        mock_consumer.commit.assert_called_once()
        mock_consumer.close.assert_called_once()

    def test_consumer_uses_correct_group_id(self):
        mock_consumer = MagicMock()
        mock_consumer.__iter__ = MagicMock(return_value=iter([]))
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = KafkaSource({"brokers": ["b:9092"], "topic": "t", "_pipeline_name": "my-pipe"})
            list(src.read())

        call_kwargs = mock_kafka.KafkaConsumer.call_args[1]
        assert call_kwargs["group_id"] == "my-pipe"

    def test_consumer_error_raises_source_error(self):
        mock_consumer = MagicMock()
        mock_consumer.__iter__ = MagicMock(side_effect=RuntimeError("conn lost"))
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source()
            with pytest.raises(SourceError, match="Kafka consumer error"):
                list(src.read())
