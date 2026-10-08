"""Tests for Kafka source connector and Kafka sink fast-path eligibility."""
from __future__ import annotations

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from tram.connectors.kafka.sink import KafkaSink
from tram.connectors.kafka.source import KafkaSource
from tram.core.exceptions import SinkError, SourceError
from tram.interfaces.base_sink import DeliveryTier
from tram.interfaces.base_source import AckDisposition
from tram.serializers.json_serializer import JsonSerializer


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


class TestKafkaSourceLag:
    def test_lag_sampled_once_per_poll_batch(self):
        """B4 regression: the end_offsets broker round-trip runs once per poll
        batch, never per message."""
        tp = _TopicPartition("events", 0)
        msg1 = TestKafkaSourceRead._make_msg(offset=0)
        msg2 = TestKafkaSourceRead._make_msg(offset=1)
        sentinel = TestKafkaSourceRead._make_msg(value=b"SENTINEL", offset=2)
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = [tp]
        mock_consumer.end_offsets.return_value = {tp: 100}
        mock_consumer.position.return_value = 90
        mock_consumer.poll.side_effect = [
            {tp: [msg1, msg2]},
            {tp: [sentinel]},
        ]
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with (
            patch.dict(sys.modules, {"kafka": mock_kafka}),
            patch("tram.metrics.registry.KAFKA_LAG") as mock_gauge,
        ):
            src = _make_source({"_pipeline_name": "pm-ingest"})
            it = src.read()
            assert next(it)[0] == b'{"x":1}'   # msg1
            assert next(it)[0] == b'{"x":1}'   # msg2 — same batch
            assert next(it)[0] == b"SENTINEL"  # second poll batch
            it.close()

        # Three messages across two poll batches → exactly two end_offsets
        # calls, and one lag value recorded per batch.
        assert mock_consumer.end_offsets.call_count == 2
        assert mock_consumer.position.call_count == 2
        assert mock_gauge.labels.call_count == 2
        mock_gauge.labels.assert_called_with(
            pipeline="pm-ingest", topic="events", partition="0"
        )
        mock_gauge.labels.return_value.set.assert_called_with(10)


class TestKafkaSourceStop:
    def test_stop_terminates_idle_poll_loop(self):
        """A quiet topic (empty polls forever) must terminate promptly on stop()."""
        started = threading.Event()
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = []
        mock_consumer.end_offsets.return_value = {}
        mock_consumer.poll.return_value = {}
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source({"_pipeline_name": "pm-ingest"})

            def _poll(**kwargs):
                started.set()
                return {}

            mock_consumer.poll.side_effect = _poll
            it = src.read()
            results = []
            reader = threading.Thread(target=lambda: results.append(list(it)))
            reader.start()
            assert started.wait(timeout=2.0)
            src.stop()
            reader.join(timeout=2.0)

        assert results == [[]]
        mock_consumer.close.assert_called()
        mock_consumer.commit.assert_not_called()

    def test_stop_interrupts_reconnect_backoff(self):
        """stop() during a reconnect backoff must exit without retrying."""
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = []
        mock_consumer.end_offsets.return_value = {}
        mock_consumer.poll.side_effect = RuntimeError("conn lost")
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source({"reconnect_delay_seconds": 60})
            it = src.read()
            results = []
            reader = threading.Thread(target=lambda: results.append(list(it)))
            reader.start()
            time.sleep(0.2)  # let the first failure enter the backoff sleep
            src.stop()
            reader.join(timeout=2.0)

        assert results == [[]]
        assert mock_consumer.poll.call_count == 1

    def test_stop_mid_batch_drains_batch_then_commits(self):
        """stop() must not abort an in-flight poll batch: the remaining messages
        are delivered and the batch committed (at-least-once), then the loop
        exits on the stop flag without reconnecting."""
        msg1 = TestKafkaSourceRead._make_msg(offset=0)
        msg2 = TestKafkaSourceRead._make_msg(offset=1)
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = []
        mock_consumer.end_offsets.return_value = {}
        mock_consumer.poll.return_value = {_TopicPartition("events", 0): [msg1, msg2]}
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = _make_source()
            it = src.read()
            assert next(it)[0] == b'{"x":1}'
            src.stop()  # stop mid-batch
            assert next(it)[0] == b'{"x":1}'  # remaining message still delivered
            with pytest.raises(StopIteration):
                next(it)  # batch committed, then the stop flag ends the loop

        mock_consumer.commit.assert_called_once()
        # Note: this mock's commit() always succeeds, which simplifies the
        # real behavior — in production stop() has already closed the consumer
        # by the time the read loop reaches the per-batch commit, and
        # kafka-python's commit() on a closed consumer raises. That raise lands
        # in the read loop's stop-flag branch, which exits without reconnecting
        # and without committing (safe no-commit direction: the batch is
        # re-polled on restart, preserving at-least-once). The mock here only
        # pins the loop structure, not the closed-consumer failure mode.
        assert mock_consumer.poll.call_count == 1


class TestKafkaSourceEpochFrontier:
    """V18-01 §7: per-partition completed frontiers + assignment-epoch fencing.

    Uses the real ``kafka`` structs (installed in the venv) for the commit
    offsets so the explicit frontier values are asserted directly; the
    consumer itself stays a MagicMock.
    """

    @staticmethod
    def _make_source(extra: dict | None = None) -> KafkaSource:
        cfg = {"brokers": ["kafka:9092"], "topic": "events"}
        if extra:
            cfg.update(extra)
        return KafkaSource(cfg)

    def test_ack_commits_completed_partition_frontier(self):
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 3

        src.ack(
            {"kafka_topic": "events", "kafka_partition": 0, "kafka_offset": 5,
             "kafka_epoch": 3},
            AckDisposition.DELIVERED,
        )

        offsets = mock_consumer.commit.call_args[0][0]
        assert len(offsets) == 1
        tp = next(iter(offsets))
        assert tp.topic == "events"
        assert tp.partition == 0
        assert offsets[tp].offset == 6  # frontier + 1 = next resume offset
        assert src._completed == {("events", 0): 5}

    def test_stale_epoch_ack_cannot_commit(self):
        """A completion read under an old assignment epoch never advances a
        new assignment's frontier and never commits."""
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 4

        src.ack(
            {"kafka_topic": "events", "kafka_partition": 0, "kafka_offset": 5,
             "kafka_epoch": 3},
            AckDisposition.DELIVERED,
        )

        mock_consumer.commit.assert_not_called()
        assert src._completed == {}

    def test_ack_without_active_consumer_skips(self):
        src = self._make_source()
        src._consumer = None
        src._assignment_epoch = 0

        src.ack(
            {"kafka_topic": "events", "kafka_partition": 0, "kafka_offset": 5,
             "kafka_epoch": 0},
            AckDisposition.DELIVERED,
        )

        assert src._completed == {}

    def test_ack_commits_only_completed_partitions(self):
        """Each completion commits exactly its own partition's explicit offset."""
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 1

        src.ack(
            {"kafka_topic": "events", "kafka_partition": 0, "kafka_offset": 9,
             "kafka_epoch": 1},
            AckDisposition.FILTERED,
        )
        offsets = mock_consumer.commit.call_args[0][0]
        assert len(offsets) == 1
        assert next(iter(offsets)).partition == 0
        assert next(iter(offsets.values())).offset == 10

        # A different partition commits only its own frontier.
        src.ack(
            {"kafka_topic": "events", "kafka_partition": 2, "kafka_offset": 4,
             "kafka_epoch": 1},
            AckDisposition.DLQ,
        )
        offsets2 = mock_consumer.commit.call_args[0][0]
        assert len(offsets2) == 1
        assert next(iter(offsets2)).partition == 2
        assert next(iter(offsets2.values())).offset == 5

    def test_assignment_change_bumps_epoch_and_drops_revoked_frontier(self):
        from kafka import TopicPartition

        src = self._make_source()
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = [TopicPartition("events", 1)]
        src._assigned = {TopicPartition("events", 0)}
        src._completed = {("events", 0): 5, ("events", 1): 3}

        src._sync_assignment(mock_consumer)

        assert src._assignment_epoch == 1
        assert src._assigned == {TopicPartition("events", 1)}
        # Revoked partition's frontier is dropped; the still-owned one is kept.
        assert src._completed == {("events", 1): 3}

    def test_stale_epoch_batch_commit_skipped(self):
        """A batch consumed across a rebalance is not committed by the legacy
        path — the new owner re-polls from the last committed offsets."""
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 2

        src._commit_batch(mock_consumer, {"tp": 9}, epoch=1)  # stale
        mock_consumer.commit.assert_not_called()

        src._commit_batch(mock_consumer, {"tp": 9}, epoch=2)  # current
        mock_consumer.commit.assert_called_once()

    def test_read_meta_carries_epoch_and_ack_commits(self):
        """End-to-end: the read loop tags metas with the assignment epoch and
        acking that meta commits the partition frontier on the consumer."""
        msg = TestKafkaSourceRead._make_msg(offset=42)
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = []
        mock_consumer.end_offsets.return_value = {}
        mock_consumer.poll.side_effect = [
            {_TopicPartition("events", 0): [msg]},
            {_TopicPartition("events", 0): [
                TestKafkaSourceRead._make_msg(value=b"SENTINEL", offset=43)]},
        ]
        mock_kafka = MagicMock()
        mock_kafka.KafkaConsumer.return_value = mock_consumer

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            src = self._make_source()
            it = src.read()
            _payload, meta = next(it)
            assert "kafka_epoch" in meta
            src.ack(meta, AckDisposition.DELIVERED)
            offsets = mock_consumer.commit.call_args[0][0]
            assert len(offsets) == 1
            assert mock_consumer.commit.call_count == 1
            it.close()

    def test_ack_noop_when_auto_commit_enabled(self):
        src = self._make_source({"enable_auto_commit": True})
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 0

        src.ack(
            {"kafka_topic": "events", "kafka_partition": 0, "kafka_offset": 1,
             "kafka_epoch": 0},
            AckDisposition.DELIVERED,
        )

        mock_consumer.commit.assert_not_called()
        assert src._completed == {}


class TestKafkaSourceGapFrontier:
    """V18-01 §7 / plan C: gap-aware per-partition completed frontiers.

    Per-record ack completion for threaded execution: a partition's committable
    frontier advances only across its contiguous completed prefix — never past
    a queued/in-flight record — and completion order is independent of read
    order. Out-of-order completions sit in a per-partition gap set until the
    missing offsets complete; tombstones (recorded by read()) are bridged by
    the frontier sweep without firing a commit of their own.
    """

    @staticmethod
    def _make_source(extra: dict | None = None) -> KafkaSource:
        cfg = {"brokers": ["kafka:9092"], "topic": "events"}
        if extra:
            cfg.update(extra)
        return KafkaSource(cfg)

    @staticmethod
    def _ack(src: KafkaSource, partition: int, offset: int, epoch: int = 1) -> None:
        src.ack(
            {"kafka_topic": "events", "kafka_partition": partition,
             "kafka_offset": offset, "kafka_epoch": epoch},
            AckDisposition.DELIVERED,
        )

    @staticmethod
    def _committed_offsets(consumer) -> dict[int, int]:
        """Map partition → committed resume offset from the last commit call."""
        offsets = consumer.commit.call_args[0][0]
        return {tp.partition: om.offset for tp, om in offsets.items()}

    def test_out_of_order_completion_never_commits_past_gap(self):
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 1
        src._read_min = {("events", 0): 5}  # partition read from offset 5

        # Record 7 completes before 5 and 6: the gap at 5/6 blocks any commit.
        self._ack(src, 0, 7)
        mock_consumer.commit.assert_not_called()
        assert src._completed == {}  # frontier did not advance
        assert src._completed_ooo == {("events", 0): {7}}

        # Record 5 completes; 6 is still queued/in-flight → frontier 5, so the
        # commit (resume 6) never passes the in-flight record 6.
        self._ack(src, 0, 5)
        assert self._committed_offsets(mock_consumer) == {0: 6}
        assert src._completed == {("events", 0): 5}
        assert src._completed_ooo == {("events", 0): {7}}

    def test_closing_gap_advances_frontier_across_it(self):
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 1
        src._read_min = {("events", 0): 5}  # partition read from offset 5

        self._ack(src, 0, 7)
        mock_consumer.commit.assert_not_called()
        self._ack(src, 0, 5)
        assert self._committed_offsets(mock_consumer) == {0: 6}

        # Acknowledging 6 closes the gap: the frontier sweeps 6→7 (7 was
        # already completed out of order) and commits resume offset 8.
        self._ack(src, 0, 6)
        assert self._committed_offsets(mock_consumer) == {0: 8}
        assert src._completed == {("events", 0): 7}
        assert src._completed_ooo == {}

    def test_in_flight_record_blocks_the_commit(self):
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 1
        src._read_min = {("events", 0): 5}  # partition read from offset 5

        self._ack(src, 0, 5)  # contiguous prefix 5 → commit resume 6
        assert self._committed_offsets(mock_consumer) == {0: 6}

        # Record 8 completes while 6/7 are still queued/in-flight: no advance.
        self._ack(src, 0, 8)
        assert mock_consumer.commit.call_count == 1
        assert src._completed == {("events", 0): 5}
        assert src._completed_ooo == {("events", 0): {8}}

        # Record 6 completes; 7 still in flight → frontier 6 only.
        self._ack(src, 0, 6)
        assert mock_consumer.commit.call_count == 2
        assert self._committed_offsets(mock_consumer) == {0: 7}
        assert src._completed == {("events", 0): 6}
        assert src._completed_ooo == {("events", 0): {8}}

        # Record 7 completes → sweep 6→7→8, commit resume 9.
        self._ack(src, 0, 7)
        assert mock_consumer.commit.call_count == 3
        assert self._committed_offsets(mock_consumer) == {0: 9}
        assert src._completed == {("events", 0): 8}
        assert src._completed_ooo == {}

    def test_revoke_drops_gap_tracking(self):
        from kafka import TopicPartition

        src = self._make_source()
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = [TopicPartition("events", 1)]
        src._assigned = {TopicPartition("events", 0)}
        src._completed = {("events", 0): 4}
        src._completed_ooo = {("events", 0): {7}, ("events", 1): {5}}
        src._tombstone_offsets = {("events", 0): {6}, ("events", 1): {4}}
        src._read_min = {("events", 0): 4, ("events", 1): 5}

        src._sync_assignment(mock_consumer)

        assert src._assignment_epoch == 1
        assert src._assigned == {TopicPartition("events", 1)}
        # Revoked partition's frontier, out-of-order, tombstone, and read
        # tracking are dropped; the still-owned partition keeps its state.
        assert src._completed == {}
        assert src._completed_ooo == {("events", 1): {5}}
        assert src._tombstone_offsets == {("events", 1): {4}}
        assert src._read_min == {("events", 1): 5}

    def test_stale_epoch_completion_never_touches_gap_state(self):
        """A completion read under an old assignment epoch never advances a new
        assignment's frontier and never touches its gap state."""
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 4
        src._completed = {("events", 0): 3}
        src._completed_ooo = {("events", 0): {9}}

        self._ack(src, 0, 4, epoch=3)  # stale

        mock_consumer.commit.assert_not_called()
        assert src._completed == {("events", 0): 3}
        assert src._completed_ooo == {("events", 0): {9}}

    def test_tombstone_offset_bridged_without_its_own_commit(self):
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 1
        src._read_min = {("events", 0): 5}  # partition read from offset 5
        src._tombstone_offsets = {("events", 0): {6}}  # tombstone read at 6

        # Acknowledging 5 sweeps the tombstone at 6 → frontier 6, commit resume 7.
        self._ack(src, 0, 5)
        assert self._committed_offsets(mock_consumer) == {0: 7}
        assert src._completed == {("events", 0): 6}
        assert src._tombstone_offsets == {}

    def test_tombstone_only_partition_never_commits_via_ack_path(self):
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 1
        src._tombstone_offsets = {("events", 0): {0, 1, 2}}

        # No payload record is ever acked → the frontier never advances and no
        # ack-path commit fires; a tombstone-only partition is covered only by
        # the legacy batch path.
        assert src._completed == {}
        mock_consumer.commit.assert_not_called()

    def test_multi_partition_interleaved_completion_commits_independently(self):
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 1
        src._read_min = {("events", 0): 5, ("events", 1): 9}

        self._ack(src, 0, 5)  # p0 frontier 5 → commit p0 resume 6
        assert self._committed_offsets(mock_consumer) == {0: 6}
        self._ack(src, 1, 9)  # p1 frontier 9 → commit p1 resume 10
        assert self._committed_offsets(mock_consumer) == {1: 10}
        self._ack(src, 0, 8)  # p0 gap at 6/7 → no commit
        assert mock_consumer.commit.call_count == 2
        assert src._completed_ooo == {("events", 0): {8}}
        self._ack(src, 0, 6)  # p0 frontier 6, gap at 7 → commit p0 resume 7
        assert self._committed_offsets(mock_consumer) == {0: 7}
        self._ack(src, 0, 7)  # p0 sweep 6→7→8 → commit p0 resume 9
        assert self._committed_offsets(mock_consumer) == {0: 9}

        assert src._completed == {("events", 0): 8, ("events", 1): 9}
        assert src._completed_ooo == {}

    def test_duplicate_ack_does_not_recommit(self):
        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 1
        src._read_min = {("events", 0): 5}

        self._ack(src, 0, 5)
        assert mock_consumer.commit.call_count == 1
        self._ack(src, 0, 5)  # duplicate → ignored, no spurious commit
        assert mock_consumer.commit.call_count == 1
        assert src._completed == {("events", 0): 5}
        self._ack(src, 0, 6)  # sequential completion still advances
        assert mock_consumer.commit.call_count == 2
        assert self._committed_offsets(mock_consumer) == {0: 7}

    def test_legacy_batch_commit_path_unchanged(self):
        """The batch-boundary commit path keeps its exact behavior for
        non-strict/legacy pipelines: epoch-guarded, commits the batch-observed
        frontier, and never touches the per-record gap tracking."""
        from kafka import TopicPartition

        src = self._make_source()
        mock_consumer = MagicMock()
        src._consumer = mock_consumer
        src._assignment_epoch = 2
        src._completed = {("events", 0): 3}
        src._completed_ooo = {("events", 0): {8}}
        src._tombstone_offsets = {("events", 0): {6}}
        src._read_min = {("events", 0): 3}

        src._commit_batch(mock_consumer, {TopicPartition("events", 0): 9}, epoch=1)
        mock_consumer.commit.assert_not_called()  # stale epoch: skipped

        src._commit_batch(mock_consumer, {TopicPartition("events", 0): 9}, epoch=2)
        assert self._committed_offsets(mock_consumer) == {0: 10}  # resume past 9

        # Gap tracking is untouched by the batch path.
        assert src._completed == {("events", 0): 3}
        assert src._completed_ooo == {("events", 0): {8}}
        assert src._tombstone_offsets == {("events", 0): {6}}
        assert src._read_min == {("events", 0): 3}

    def test_read_records_tombstones_so_ack_can_bridge_them(self):
        """read() records tombstone offsets; a payload ack sweeps across them
        (no per-record completion for the tombstone) and commits the payload
        frontier."""
        tp = _TopicPartition("events", 0)
        tombstone0 = TestKafkaSourceRead._make_msg(value=None, offset=0)
        payload1 = TestKafkaSourceRead._make_msg(offset=1)
        tombstone2 = TestKafkaSourceRead._make_msg(value=None, offset=2)
        payload3 = TestKafkaSourceRead._make_msg(offset=3)
        sentinel = TestKafkaSourceRead._make_msg(value=b"SENTINEL", offset=4)
        mock_consumer = MagicMock()
        mock_consumer.assignment.return_value = []
        mock_consumer.end_offsets.return_value = {}
        mock_consumer.poll.side_effect = [
            {tp: [tombstone0, payload1, tombstone2, payload3]},
            {tp: [sentinel]},
        ]

        # Patch only KafkaConsumer so the real kafka TopicPartition /
        # OffsetAndMetadata structs resolve inside the source and the commit
        # offsets can be asserted directly.
        with patch("kafka.KafkaConsumer", return_value=mock_consumer):
            src = self._make_source()
            it = src.read()
            _payload, meta1 = next(it)  # offset 1
            _payload, meta3 = next(it)  # offset 3
            assert src._read_min == {("events", 0): 0}
            assert src._tombstone_offsets == {("events", 0): {0, 2}}
            assert src._completed == {}

            # Out-of-order completion (3 before 1): the sweep bridges tombstone
            # 0 only — record 1 is still queued/in-flight, so the frontier stops
            # at 0 and the commit (resume 1) never passes it.
            src.ack(meta3, AckDisposition.DELIVERED)
            assert self._committed_offsets(mock_consumer) == {0: 1}
            assert src._completed == {("events", 0): 0}
            assert src._completed_ooo == {("events", 0): {3}}
            assert src._tombstone_offsets == {("events", 0): {2}}

            # Closing the gap (1) sweeps record 1, tombstone 2, and the
            # already-completed 3 → frontier 3, commit resume 4.
            src.ack(meta1, AckDisposition.DELIVERED)
            assert self._committed_offsets(mock_consumer) == {0: 4}
            assert src._completed == {("events", 0): 3}
            assert src._completed_ooo == {}
            assert src._tombstone_offsets == {}
            it.close()


class TestKafkaSinkFastPath:
    """Kafka sink single-message fast path (perf follow-up 2026-10-07).

    When the executor already supplied ``output_record_count`` and the payload
    is already-serialized bytes within both caps (and the sink is keyless),
    ``KafkaSink.write`` sends it as ONE message without re-parsing records or
    re-serializing via ``chunk_records_by_caps``. Every ineligibility reason
    falls back to the legacy parse-and-chunk path (parse is invoked).
    """

    _JSON_META = {"serializer_type": "json", "serializer_config": {"type": "json"}}

    @staticmethod
    def _make_sink(config_extra: dict | None = None) -> KafkaSink:
        cfg = {"brokers": ["kafka:9092"], "topic": "events"}
        if config_extra:
            cfg.update(config_extra)
        return KafkaSink(cfg)

    @staticmethod
    def _serialized_records(count: int = 2) -> bytes:
        return JsonSerializer({}).serialize([{"seq": i} for i in range(count)])

    def test_fast_path_sends_single_message_without_reparse(self):
        """Eligible batch: no re-parse, no chunk re-serialization, exactly one
        send carrying the exact payload bytes (keyless → no key)."""
        data = self._serialized_records(2)
        sink = self._make_sink()
        producer = MagicMock()
        sink._producer = producer

        with (
            patch.object(sink, "_parse_payload") as mock_parse,
            patch("tram.connectors.kafka.sink.chunk_records_by_caps") as mock_chunk,
        ):
            sink.write(data, dict(self._JSON_META, output_record_count=2))

        mock_parse.assert_not_called()  # no re-parse of the payload
        mock_chunk.assert_not_called()  # no per-record re-serialization
        assert producer.send.call_count == 1
        call = producer.send.call_args
        assert call.kwargs["value"] is data  # exact payload bytes, byte-faithful
        assert call.kwargs["key"] is None  # keyless batch → no key

    @pytest.mark.parametrize(
        ("config_extra", "meta_extra"),
        [
            (None, None),  # count missing
            (None, {"output_record_count": 0}),  # count 0
            ({"chunk_records": 2}, {"output_record_count": 3}),  # count > record cap
            ({"chunk_bytes": 10}, {"output_record_count": 2}),  # payload > byte cap
            ({"key_field": "seq"}, {"output_record_count": 2}),  # key configured
        ],
    )
    def test_fast_path_falls_back_to_legacy_when_ineligible(
        self, config_extra, meta_extra
    ):
        """Each ineligibility reason takes the legacy path: parse is invoked."""
        data = self._serialized_records(2)
        sink = self._make_sink(config_extra)
        producer = MagicMock()
        sink._producer = producer
        meta = dict(self._JSON_META)
        if meta_extra:
            meta.update(meta_extra)

        with patch.object(sink, "_parse_payload", wraps=sink._parse_payload) as mock_parse:
            sink.write(data, meta)

        mock_parse.assert_called_once_with(data, meta)


class TestKafkaSinkDelivery:
    """Kafka sink delivery tier and commit barrier (V18-01 sections 6 and 12.1).

    Audit finding (pending decision 1): the producer default is already
    ``acks=all``, and weaker acks configurations are now rejected at
    construction — no path may restore weaker-ack false success silently.
    Real broker durability (acks=all honored, flush drains) is only provable
    against a live broker (V18-02 broker-test gate).
    """

    @staticmethod
    def _make_sink(config_extra: dict | None = None) -> KafkaSink:
        cfg = {"brokers": ["kafka:9092"], "topic": "events"}
        if config_extra:
            cfg.update(config_extra)
        return KafkaSink(cfg)

    def test_delivery_capability_declared_remote_durable(self):
        cap = KafkaSink.delivery_capability
        assert cap is not None
        assert cap.tier == DeliveryTier.REMOTE_DURABLE
        assert cap.replay_safe is False

    def test_default_acks_is_all(self):
        assert self._make_sink().acks == "all"

    def test_producer_built_with_acks_all(self):
        mock_kafka = MagicMock()
        mock_kafka.KafkaProducer.return_value = MagicMock()

        with patch.dict(sys.modules, {"kafka": mock_kafka}):
            sink = self._make_sink()
            sink._get_producer()

        assert mock_kafka.KafkaProducer.call_args[1]["acks"] == "all"

    @pytest.mark.parametrize("weak", ["0", "1", 0, 1, "none", None])
    def test_weaker_acks_rejected_fail_closed(self, weak):
        with pytest.raises(SinkError, match="acks"):
            self._make_sink({"acks": weak})

    def test_minus_one_acks_accepted(self):
        assert self._make_sink({"acks": "-1"}).acks == "-1"
        assert self._make_sink({"acks": -1}).acks == -1

    def test_commit_confirms_remote_durable_and_flushes(self):
        sink = self._make_sink()
        producer = MagicMock()
        sink._producer = producer

        receipt = sink.commit()

        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True
        assert "acks=all" in receipt.notes
        producer.flush.assert_called_once()

    def test_commit_without_producer_is_confirmed_barrier(self):
        sink = self._make_sink()
        receipt = sink.commit()
        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True

    def test_commit_flush_failure_raises_sink_error(self):
        sink = self._make_sink()
        producer = MagicMock()
        producer.flush.side_effect = RuntimeError("broker unreachable")
        sink._producer = producer

        with pytest.raises(SinkError, match="commit flush failed"):
            sink.commit()

    def test_commit_honors_deadline(self):
        sink = self._make_sink()
        producer = MagicMock()
        sink._producer = producer

        with pytest.raises(SinkError, match="deadline"):
            sink.commit(deadline=time.monotonic() - 1)
        producer.flush.assert_not_called()

    def test_latched_error_defaults_to_none(self):
        # kafka-python buffers no delivery errors this path can miss: every
        # send future is awaited in write() (future.get), so failures surface
        # synchronously as SinkError. Only a live broker could prove acks=all
        # is honored end to end (V18-02 broker-test gate).
        assert self._make_sink().latched_error() is None