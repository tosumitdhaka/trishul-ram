"""Tests for Kafka source connector."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.kafka.source import KafkaSource
from tram.core.exceptions import SourceError


class _TopicPartition:
    """Stand-in for kafka.TopicPartition (the connector ignores poll-dict keys)."""

    def __init__(self, topic: str, partition: int) -> None:
        self.topic = topic
        self.partition = partition


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
        assert src.enable_auto_commit is False
        assert src.max_poll_records == 500
        assert src.security_protocol == "PLAINTEXT"

    def test_explicit_auto_commit_true_respected(self):
        src = _make_source({"enable_auto_commit": True})
        assert src.enable_auto_commit is True

    def test_consumer_built_with_auto_commit_disabled_by_default(self):
        mock_consumer = MagicMock()
        mock_consumer.poll.return_value = {}
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source({"_pipeline_name": "my-pipe"})
            src._build_consumer()

        call_kwargs = mock_kafka.KafkaConsumer.call_args[1]
        assert call_kwargs["enable_auto_commit"] is False

    def test_consumer_built_with_auto_commit_true_when_opted_in(self):
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = MagicMock()

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source({"_pipeline_name": "my-pipe", "enable_auto_commit": True})
            src._build_consumer()

        call_kwargs = mock_kafka.KafkaConsumer.call_args[1]
        assert call_kwargs["enable_auto_commit"] is True


class TestKafkaSourceConfigModel:
    def test_model_default_serializes_to_auto_commit_false(self):
        from tram.models.pipeline import KafkaSourceConfig

        cfg = KafkaSourceConfig(type="kafka", brokers=["b:9092"], topic="t")
        assert cfg.enable_auto_commit is False
        assert cfg.model_dump()["enable_auto_commit"] is False

    def test_model_explicit_auto_commit_true_preserved(self):
        from tram.models.pipeline import KafkaSourceConfig

        cfg = KafkaSourceConfig(
            type="kafka", brokers=["b:9092"], topic="t", enable_auto_commit=True
        )
        assert cfg.model_dump()["enable_auto_commit"] is True


class TestKafkaSourceImportError:
    def test_import_error_raises_source_error(self):
        with patch.dict(sys.modules, {"kafka": None}):
            src = _make_source()
            with pytest.raises(SourceError, match="kafka-python"):
                list(src.read())


class TestKafkaSourceRead:
    @staticmethod
    def _make_msg(value: bytes | None = b'{"x":1}', topic="events", partition=0,
                  offset=0, key: bytes | None = None):
        mock_msg = MagicMock()
        mock_msg.value = value
        mock_msg.topic = topic
        mock_msg.partition = partition
        mock_msg.offset = offset
        mock_msg.key = key
        return mock_msg

    @staticmethod
    def _mock_consumer(batches):
        """Mock kafka-python so ``poll()`` returns the given batches in order."""
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = []
        mock_consumer.end_offsets.return_value = {}
        mock_consumer.poll.side_effect = batches

        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer
        return mock_kafka, mock_consumer

    def test_read_yields_messages(self):
        msg = self._make_msg(offset=42, key=b"mykey")
        sentinel = self._make_msg(value=b"SENTINEL", offset=43)
        mock_kafka, mock_consumer = self._mock_consumer(
            [{_TopicPartition("events", 0): [msg]},
             {_TopicPartition("events", 0): [sentinel]}]
        )

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source({"_pipeline_name": "pm-ingest"})
            it = src.read()
            results = [next(it)]

            assert len(results) == 1
            payload, meta = results[0]
            assert payload == b'{"x":1}'
            assert meta["kafka_topic"] == "events"
            assert meta["kafka_offset"] == 42
            assert meta["kafka_key"] == "mykey"

            # Pulling the next message resumes the generator past the end of the
            # first poll batch: the batch is now committed and the next one read.
            second = next(it)
            assert second[0] == b"SENTINEL"
            mock_consumer.commit.assert_called_once()
            it.close()

    def test_skips_none_value_messages(self):
        tombstone = self._make_msg(value=None)
        sentinel = self._make_msg(value=b"SENTINEL")
        mock_kafka, mock_consumer = self._mock_consumer(
            [{_TopicPartition("events", 0): [tombstone]},
             {_TopicPartition("events", 0): [sentinel]}]
        )

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source()
            it = src.read()
            # The tombstone batch yields nothing; its offsets are still committed
            # (the record needs no processing) before the next batch is polled.
            result = next(it)

        assert result[0] == b"SENTINEL"
        mock_consumer.commit.assert_called_once()
        it.close()

    def test_commit_called_after_full_batch_consumed(self):
        # Two-message poll batch. Commit must fire only after BOTH messages have
        # been handed to the caller (sink write / DLQ / skip resolved), i.e.
        # when the generator is resumed past the last message of the batch.
        msg1 = self._make_msg(offset=0)
        msg2 = self._make_msg(offset=1)
        sentinel = self._make_msg(value=b"SENTINEL", offset=2)
        mock_kafka, mock_consumer = self._mock_consumer(
            [{_TopicPartition("events", 0): [msg1, msg2]},
             {_TopicPartition("events", 0): [sentinel]}]
        )

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source()
            it = src.read()
            assert next(it)[0] == b'{"x":1}'
            mock_consumer.commit.assert_not_called()  # mid-batch: no commit
            assert next(it)[0] == b'{"x":1}'
            mock_consumer.commit.assert_not_called()  # still mid-batch

            # Resume past the batch end → commit fires, then the next batch is polled.
            assert next(it)[0] == b"SENTINEL"
            mock_consumer.commit.assert_called_once()
            it.close()

    def test_no_commit_when_generator_closed_mid_batch(self):
        # A sink error / run abort in the executor closes the generator while
        # the batch is only partially consumed (GeneratorExit at a yield).
        # Nothing may be committed, so the whole batch is re-polled (at-least-once).
        msg1 = self._make_msg(offset=0)
        msg2 = self._make_msg(offset=1)
        mock_kafka, mock_consumer = self._mock_consumer(
            [{_TopicPartition("events", 0): [msg1, msg2]}]
        )

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source()
            it = src.read()
            assert next(it)[0] == b'{"x":1}'
            it.close()  # simulates the executor aborting mid-batch

        mock_consumer.commit.assert_not_called()
        mock_consumer.close.assert_called_once()

    def test_commit_persists_before_consumer_error_on_next_batch(self):
        # The consumed batch is committed when the generator resumes past its
        # end, even if the following poll (next batch) then fails. Nothing is
        # committed for the batch that was never handed out.
        msg = self._make_msg(offset=0)
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = []
        mock_consumer.end_offsets.return_value = {}
        mock_consumer.poll.side_effect = [
            {_TopicPartition("events", 0): [msg]},
            RuntimeError("conn lost"),
        ]
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source({"max_reconnect_attempts": 1})
            it = src.read()
            assert next(it)[0] == b'{"x":1}'
            with pytest.raises(SourceError, match="Kafka consumer error"):
                next(it)  # resume past batch → commit → poll raises → reconnect exhausted

        mock_consumer.commit.assert_called_once()
        it.close()

    def test_no_explicit_commit_when_auto_commit_enabled(self):
        # Opting back into enable_auto_commit=true restores the legacy
        # at-most-once behavior: the consumer commits on its own ~5s timer and
        # the connector performs no explicit commit.
        msg = self._make_msg(offset=0)
        sentinel = self._make_msg(value=b"SENTINEL", offset=1)
        mock_kafka, mock_consumer = self._mock_consumer(
            [{_TopicPartition("events", 0): [msg]},
             {_TopicPartition("events", 0): [sentinel]}]
        )

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source({"enable_auto_commit": True})
            it = src.read()
            assert next(it)[0] == b'{"x":1}'
            assert next(it)[0] == b"SENTINEL"

        mock_consumer.commit.assert_not_called()
        it.close()

    def test_consumer_uses_correct_group_id(self):
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = MagicMock()

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = KafkaSource({"brokers": ["b:9092"], "topic": "t", "_pipeline_name": "my-pipe"})
            src._build_consumer()

        call_kwargs = mock_kafka.KafkaConsumer.call_args[1]
        assert call_kwargs["group_id"] == "my-pipe"

    def test_consumer_error_raises_source_error(self):
        mock_consumer = MagicMock()
        mock_consumer.poll.side_effect = RuntimeError("conn lost")
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            # max_reconnect_attempts=1 ensures test exits after one retry
            src = _make_source({"max_reconnect_attempts": 1})
            with pytest.raises(SourceError, match="Kafka consumer error"):
                list(src.read())

    def test_consumer_error_never_commits(self):
        # No finally-commit: offsets must not be persisted for a batch that was
        # handed out but not fully consumed when the consumer errors.
        msg = self._make_msg(offset=0)
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = []
        mock_consumer.end_offsets.return_value = {}
        mock_consumer.poll.side_effect = [
            {_TopicPartition("events", 0): [msg]},
            RuntimeError("conn lost"),
        ]
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source({"max_reconnect_attempts": 1})
            it = src.read()
            assert next(it)[0] == b'{"x":1}'
            with pytest.raises(SourceError, match="Kafka consumer error"):
                next(it)

        # Commit fired exactly once (for the fully consumed batch) before the
        # error; the partial/next batch is never committed.
        mock_consumer.commit.assert_called_once()
        it.close()