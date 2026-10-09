"""Live-broker delivery-contract tests for the Kafka connectors (V18-10 gate).

Env-gated: the class skips unless ``TRAM_TEST_KAFKA_BROKERS`` names a
reachable broker (comma-separated ``host:port`` list) — the live-PostgreSQL
pattern from ``test_execution_ledger.py::TestLivePostgres``. Point it at a
listener whose *advertised* address is reachable from the test host (the
broker's EXTERNAL listener), not a forwarded port whose advertised
``PLAINTEXT`` host is unresolvable.

Every test uses a unique ``uuid``-suffixed topic and consumer group: the
broker is shared and outlives the test session, so fixed names would collide
on a second run.

Coverage (the V18-10 broker-test gate):
  * sink durable-tier commit — acks=all + producer flush before the commit
    barrier reports ``remote_durable``, and the records are observable on the
    topic afterwards;
  * source round-trip — consume exactly what the sink wrote, the per-partition
    ack frontier advances to the last offset, and a fresh consumer in the same
    group resumes at the end (nothing re-read);
  * assignment-epoch fencing — a completion whose epoch is stale is refused:
    it never records a pending commit and never moves the broker frontier;
  * threaded mode — worker-thread acks (forced out of order) on a
    multi-partition topic: gap-aware frontiers bridge tombstones, commits run
    only on the reader thread (never workers), per-partition committed offsets
    are monotone (one consolidated commit per partition per drain), and the
    final broker state is fully committed;
  * replay identity — ``{cluster_incarnation}/{topic}/{partition}`` with the
    broker-reported cluster ID, stable per partition across the run.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from tram.connectors.kafka.sink import KafkaSink
from tram.connectors.kafka.source import KafkaSource
from tram.interfaces.base_sink import DeliveryTier
from tram.interfaces.base_source import AckDisposition
from tram.serializers.json_serializer import JsonSerializer

KAFKA_BROKERS = os.environ.get("TRAM_TEST_KAFKA_BROKERS", "")

try:  # kafka-python is an optional extra; the gate only needs it when set
    from kafka import KafkaConsumer, KafkaProducer
    from kafka.admin import KafkaAdminClient, NewTopic

    _KAFKA_OK = True
except ImportError:  # pragma: no cover - exercised only without the extra
    _KAFKA_OK = False


def _broker_list() -> list[str]:
    return [b.strip() for b in KAFKA_BROKERS.split(",") if b.strip()]


def _admin() -> KafkaAdminClient:
    # kafka-python 2.x check_version is not reliable against the modern broker
    # here (NodeNotReadyError), so the API version is pinned explicitly;
    # KafkaProducer/KafkaConsumer negotiate fine on their own.
    return KafkaAdminClient(
        bootstrap_servers=_broker_list(),
        api_version=(2, 5, 0),
        request_timeout_ms=10000,
    )


def _close_sink_producer(sink: KafkaSink) -> None:
    producer = sink._producer
    if producer is not None:
        producer.close()


@pytest.mark.skipif(
    not KAFKA_BROKERS or not _KAFKA_OK,
    reason="TRAM_TEST_KAFKA_BROKERS not set (or kafka-python missing) — no live Kafka fixture",
)
class TestLiveKafkaBroker:
    """Live-broker delivery-contract tests (V18-10 broker-test gate)."""

    @pytest.fixture
    def topics(self):
        created: list[str] = []
        yield created
        admin = None
        try:
            admin = _admin()
            admin.delete_topics(created, timeout_ms=5000)
        except Exception:
            pass  # best-effort hygiene on a shared broker
        finally:
            if admin is not None:
                admin.close()

    def _make_topic(self, topics: list[str], partitions: int = 1) -> str:
        name = f"tram-live-{uuid.uuid4().hex[:12]}"
        admin = _admin()
        try:
            admin.create_topics(
                [NewTopic(name, num_partitions=partitions, replication_factor=1)],
                timeout_ms=10000,
            )
        finally:
            admin.close()
        topics.append(name)
        return name

    def _cluster_id(self) -> str:
        admin = _admin()
        try:
            cluster_id = getattr(admin._client.cluster, "cluster_id", None)
            assert isinstance(cluster_id, str) and cluster_id, "broker cluster ID unresolved"
            return cluster_id
        finally:
            admin.close()

    @staticmethod
    def _producer() -> KafkaProducer:
        return KafkaProducer(bootstrap_servers=_broker_list(), acks="all")

    @staticmethod
    def _read_topic(topic: str, group: str, timeout_ms: int = 15000) -> list[tuple]:
        """Read every message of *topic* as (value, partition, offset)."""
        consumer = KafkaConsumer(
            topic,
            bootstrap_servers=_broker_list(),
            group_id=group,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            consumer_timeout_ms=timeout_ms,
        )
        try:
            return [(m.value, m.partition, m.offset) for m in consumer]
        finally:
            consumer.close()

    def _plant_records(self, topic: str, count: int, *, partition: int) -> None:
        producer = self._producer()
        try:
            for i in range(count):
                producer.send(
                    topic,
                    value=json.dumps({"seq": i, "tag": "live-src"}).encode(),
                    partition=partition,
                )
            producer.flush()
        finally:
            producer.close()

    # ── Sink: durable-tier commit barrier ──────────────────────────────────

    def test_live_sink_durable_commit_barrier_lands_records(self, topics):
        topic = self._make_topic(topics, partitions=1)
        records = [{"seq": i, "tag": "live-sink"} for i in range(4)]
        data = JsonSerializer({}).serialize(records)

        sink = KafkaSink({"brokers": _broker_list(), "topic": topic})
        try:
            sink.write(data, {"serializer_type": "json", "serializer_config": {"type": "json"}})
            receipt = sink.commit()
        finally:
            _close_sink_producer(sink)

        # The commit barrier only confirms after the broker acknowledged every
        # message (acks=all + producer flush) — the landed tier.
        assert receipt.tier == DeliveryTier.REMOTE_DURABLE
        assert receipt.confirmed is True
        assert "acks=all" in receipt.notes

        # The batch is within both caps, so it lands as ONE byte-faithful
        # message (the chunking unit tests pin the multi-message split); the
        # payload parses back to every record.
        readback = self._read_topic(topic, group=f"g-{uuid.uuid4().hex[:8]}")
        assert [(o, p) for _v, p, o in readback] == [(0, 0)]
        assert json.loads(readback[0][0].decode("utf-8")) == records

    # ── Source: round-trip, frontier advance, epoch fencing ────────────────

    def test_live_source_roundtrip_and_frontier_advance(self, topics):
        topic = self._make_topic(topics, partitions=1)
        self._plant_records(topic, count=4, partition=0)

        group = f"g-{uuid.uuid4().hex[:8]}"
        source = KafkaSource(
            {"brokers": _broker_list(), "topic": topic, "group_id": group,
             "auto_offset_reset": "earliest"}
        )
        seen: list[tuple[bytes, dict]] = []
        errors: list[BaseException] = []
        target = 4
        done = threading.Event()

        def _reader():
            try:
                it = source.read()
                # Keep pulling past the target: every resume runs the poll
                # loop, which drains the ack-recorded frontier commits on the
                # reader thread (and fires the legacy per-batch commit at each
                # batch boundary).
                while True:
                    payload, meta = next(it)
                    seen.append((payload, meta))
                    source.ack(meta, AckDisposition.DELIVERED)
                    if len(seen) >= target:
                        done.set()
            except StopIteration:
                pass
            except Exception as exc:
                errors.append(exc)
                done.set()

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        try:
            assert done.wait(timeout=30), f"timed out reading; errors={errors!r}"
            # The last ack's frontier commit must drain (next poll cycle).
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if source._pending_commits == {} and source._completed.get((topic, 0)) == 3:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(
                    f"ack frontier never drained: pending={source._pending_commits!r} "
                    f"completed={source._completed!r}"
                )

            # Replay identity: the broker cluster ID is the incarnation, and
            # the unit is the partition — the format is frozen as
            # {cluster_incarnation}/{topic}/{partition}. Resolved while the
            # consumer is still active (stop() clears it).
            cluster_id = self._cluster_id()
            for _p, meta in seen:
                assert source.source_unit_id(meta) == f"{cluster_id}/{topic}/0"
        finally:
            source.stop()
            reader.join(timeout=10)

        assert errors == []
        assert len(seen) == target
        payloads = [json.loads(p.decode("utf-8")) for p, _m in seen]
        assert payloads == [{"seq": i, "tag": "live-src"} for i in range(4)]

        # Frontier advanced: a fresh consumer in the same group resumes at the
        # end — nothing is re-read.
        leftover = self._read_topic(topic, group=group)
        assert leftover == []

    def test_live_stale_epoch_ack_refused(self, topics):
        topic = self._make_topic(topics, partitions=1)
        self._plant_records(topic, count=2, partition=0)

        group = f"g-{uuid.uuid4().hex[:8]}"
        source = KafkaSource(
            {"brokers": _broker_list(), "topic": topic, "group_id": group,
             "auto_offset_reset": "earliest"}
        )
        it = source.read()
        try:
            _payload, meta = next(it)
            assert meta["kafka_offset"] == 0

            # Fabricate a rebalance: bump the assignment epoch so the meta's
            # captured epoch is stale (mirrors the unit-test fencing setup).
            source._assignment_epoch += 1
            source.ack(meta, AckDisposition.DELIVERED)
            assert source._pending_commits == {}
            assert source._completed == {}

            # Positive control: the same completion with the current epoch is
            # accepted — the refusal above was the epoch fence, not the ack path.
            current = dict(meta, kafka_epoch=source._assignment_epoch)
            source.ack(current, AckDisposition.DELIVERED)
            assert source._pending_commits == {(topic, 0): 0}
            assert source._completed == {(topic, 0): 0}
        finally:
            # Mid-batch close: the legacy per-batch commit never fires, and the
            # accepted pending commit is never drained (the reader thread owns
            # commits) — so nothing moves on the broker.
            it.close()
            source.stop()

        # A fresh consumer in the same group re-reads both records: the stale
        # completion never advanced the committed frontier.
        leftover = self._read_topic(topic, group=group)
        assert [o for _v, _p, o in leftover] == [0, 1]

    # ── Threaded mode: gap-aware frontiers, tombstones, commit serialization ─

    def test_live_threaded_multi_partition_gap_aware_commits(self, topics):
        """thread_workers > 1 simulation: worker-thread acks on 3 partitions.

        Payloads at even offsets, tombstones (value=None) at odd offsets.
        Workers complete offset%4==2 records immediately and offset%4==0
        records after a delay, so every partition's completion order is
        out-of-order — the gap-aware frontier must hold behind the missing low
        offsets and bridge the tombstones, and only the reader thread may
        commit (one consolidated per-partition commit per drain).
        """
        topic = self._make_topic(topics, partitions=3)
        group = f"g-{uuid.uuid4().hex[:8]}"
        partitions = (0, 1, 2)
        end_offset = 7  # offsets 0..6: payloads 0,2,4,6; tombstones 1,3,5

        planted: dict[int, list[int]] = {}
        producer = self._producer()
        try:
            for p in partitions:
                payload_offsets = []
                for offset in range(end_offset):
                    if offset % 2 == 0:
                        value = json.dumps({"part": p, "seq": offset}).encode()
                        key = None
                        payload_offsets.append(offset)
                    else:
                        value = None  # tombstone — needs no processing
                        key = b"tombstone"
                    producer.send(topic, value=value, key=key, partition=p)
                planted[p] = payload_offsets
            producer.flush()
        finally:
            producer.close()

        source = KafkaSource(
            {"brokers": _broker_list(), "topic": topic, "group_id": group,
             "auto_offset_reset": "earliest"}
        )

        # Commit spy: wrap the live consumer's commit so every broker commit
        # records its calling thread and per-partition offsets. The wrapper is
        # installed on the consumer the source builds, before any commit can
        # run.
        commits: list[tuple[int, dict[int, int]]] = []
        commits_lock = threading.Lock()
        original_build = source._build_consumer

        def _spied_build():
            consumer = original_build()
            real_commit = consumer.commit

            def _spy(offsets=None, *args, **kwargs):
                tid = threading.get_ident()
                with commits_lock:
                    commits.append(
                        (tid, {tp.partition: om.offset for tp, om in (offsets or {}).items()})
                    )
                return real_commit(offsets, *args, **kwargs)

            consumer.commit = _spy
            return consumer

        source._build_consumer = _spied_build

        seen: list[tuple[bytes, dict]] = []
        seen_lock = threading.Lock()
        acked = 0
        acked_lock = threading.Lock()
        errors: list[BaseException] = []
        target = sum(len(o) for o in planted.values())  # 12 payloads
        reader_tid: list[int] = []

        def _worker_ack(meta: dict) -> None:
            nonlocal acked
            if meta["kafka_offset"] % 4 == 0:
                time.sleep(0.4)  # completes after the %4==2 records
            source.ack(meta, AckDisposition.DELIVERED)
            with acked_lock:
                acked += 1

        pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="tram-live-worker")

        def _reader():
            reader_tid.append(threading.get_ident())
            try:
                for payload, meta in source.read():
                    with seen_lock:
                        seen.append((payload, meta))
                    pool.submit(_worker_ack, meta)
            except Exception as exc:
                errors.append(exc)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                with acked_lock:
                    done_acks = acked
                with commits_lock:
                    latest: dict[int, int] = {}
                    for _tid, offsets in commits:
                        for p, o in offsets.items():
                            latest[p] = o
                if done_acks >= target and all(
                    latest.get(p, -1) == end_offset for p in partitions
                ):
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(
                    f"timed out: acked={done_acks}/{target} latest={latest} errors={errors!r}"
                )

            # Replay identity is per partition, with the real cluster
            # incarnation — resolved while the consumer is still active.
            cluster_id = self._cluster_id()
            identities = {source.source_unit_id(meta) for _p, meta in seen}
        finally:
            source.stop()
            reader.join(timeout=15)
            pool.shutdown(wait=True)

        assert errors == []
        # Tombstones are never yielded: exactly the 12 payloads, round-tripped
        # per partition at their planted offsets.
        assert len(seen) == target
        by_partition: dict[int, list[int]] = {p: [] for p in partitions}
        for payload, meta in seen:
            assert meta["kafka_partition"] in partitions
            by_partition[meta["kafka_partition"]].append(meta["kafka_offset"])
            assert json.loads(payload.decode("utf-8")) == {
                "part": meta["kafka_partition"], "seq": meta["kafka_offset"],
            }
        for p in partitions:
            assert sorted(by_partition[p]) == planted[p]

        # Gap-aware ack frontiers: every payload completed, every tombstone
        # bridged, and every recorded advance drained.
        assert source._completed == {(topic, p): end_offset - 1 for p in partitions}
        assert source._tombstone_offsets == {}
        assert source._pending_commits == {}

        # Reader-thread-owned commit serialization: no worker-thread commits,
        # and per-partition committed offsets never regress (the oldest-last
        # race that would re-process messages on restart).
        assert commits, "no broker commits observed"
        for tid, _offsets in commits:
            assert tid == reader_tid[0], "commit issued from a non-reader thread"
        monotone: dict[int, int] = {}
        for _tid, offsets in commits:
            for p, o in offsets.items():
                assert o >= monotone.get(p, -1), (
                    f"partition {p} commit regressed {monotone.get(p, -1)} -> {o}"
                )
                monotone[p] = o
        assert monotone == {p: end_offset for p in partitions}

        # Replay identity is per partition, with the real cluster incarnation.
        assert identities == {f"{cluster_id}/{topic}/{p}" for p in partitions}

        # Final broker state: a fresh consumer in the same group resumes at the
        # end of every partition — nothing is re-read (frontier fully advanced).
        leftover = self._read_topic(topic, group=group)
        assert leftover == []