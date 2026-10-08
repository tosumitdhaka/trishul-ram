"""Kafka source connector — infinite stream consumer."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator

from tram.core.exceptions import SourceError
from tram.interfaces.base_source import BaseSource
from tram.registry.registry import register_source

logger = logging.getLogger(__name__)


@register_source("kafka")
class KafkaSource(BaseSource):
    """Consume messages from a Kafka topic as an infinite stream.

    Requires ``kafka-python`` (``pip install kafka-python``).
    Use with ``schedule.type: stream``.

    Config keys:
        brokers           (list[str], required)    Bootstrap server list.
        topic             (str or list[str], req.)  Topic(s) to subscribe to.
        group_id          (str, default pipeline name)  Consumer group ID.
        auto_offset_reset (str, default "latest")   "latest" | "earliest"
        enable_auto_commit (bool, default False)    Auto-commit offsets. Default
                                                    False for at-least-once (see
                                                    commit semantics below).
        max_poll_records  (int, default 500)        Max records per poll.
        session_timeout_ms (int, default 30000)     Session timeout.
        security_protocol (str, default "PLAINTEXT") "PLAINTEXT" | "SSL" | "SASL_PLAINTEXT" | "SASL_SSL"
        sasl_mechanism    (str, optional)           "PLAIN" | "SCRAM-SHA-256" | "SCRAM-SHA-512"
        sasl_username     (str, optional)           SASL username.
        sasl_password     (str, optional)           SASL password.
        ssl_cafile        (str, optional)           CA certificate path.

    Commit semantics (at-least-once default):
    With ``enable_auto_commit: false`` (the default) offsets are committed
    explicitly as per-partition completed frontiers (V18-01 §7 / plan C).
    Two paths share the epoch-fenced explicit commits:

    * Legacy batch path — each poll batch is committed once the caller has
      consumed every message in it (the at-least-once contract of the
      single-threaded executor: a crash at any point leaves the uncommitted
      batch to be re-polled on restart). Serves non-strict/legacy pipelines.
    * Per-record ack path — ``ack(meta, disposition)`` marks one record
      complete; a partition's committable frontier advances only across its
      contiguous completed prefix (V18-01 §7 / plan C: threaded execution
      must not commit past queued/in-flight records; completion order is
      independent of read order). Out-of-order completions are tracked per
      partition until the missing offsets complete; each time the frontier
      advances, the explicit offset (frontier + 1) is committed for that
      partition on the owning consumer. Tombstone records (value None) need
      no processing: read() records their offsets and the frontier sweep
      bridges them, but a tombstone never advances a frontier or fires a
      commit by itself — a tombstone-only partition is committed only by the
      legacy batch path.

    Completion is bound to the consumer session and partition-assignment
    epoch: any assignment change (rebalance/revoke) bumps the epoch and
    in-flight completions from the old epoch are ignored — they can never
    advance a new assignment's frontier. Revocation drops the revoked
    partition's frontier, out-of-order, and tombstone tracking. ``thread_workers
    > 1`` under ``delivery.contract: strict`` remains rejected at validation
    until the gap-aware threaded frontier implementation passes broker tests
    (plan C); the per-record path is exercised by deterministic unit tests.

    Under stream micro-batching (GH #78) the last message of each poll batch
    carries ``source_batch_end: true`` in its meta; the executor flushes its
    micro-batch buffer when it sees that marker and only then resumes the
    generator past it, so the per-batch commit can never precede the flush of
    the batch's records (commit-after-flush). Setting ``enable_auto_commit:
    true`` restores the legacy at-most-once behavior: the consumer commits on
    its own ~5s timer regardless of sink progress, and the explicit commits
    are disabled.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        brokers = config["brokers"]
        self.brokers: list[str] = brokers if isinstance(brokers, list) else [brokers]
        topics = config["topic"]
        self.topics: list[str] = topics if isinstance(topics, list) else [topics]
        self.group_id: str = config.get("group_id") or config.get("_pipeline_name", "tram")
        self.auto_offset_reset: str = config.get("auto_offset_reset", "latest")
        self.enable_auto_commit: bool = bool(config.get("enable_auto_commit", False))
        self.max_poll_records: int = int(config.get("max_poll_records", 500))
        self.session_timeout_ms: int = int(config.get("session_timeout_ms", 30000))
        self.security_protocol: str = config.get("security_protocol", "PLAINTEXT")
        self.sasl_mechanism: str | None = config.get("sasl_mechanism")
        self.sasl_username: str | None = config.get("sasl_username")
        self.sasl_password: str | None = config.get("sasl_password")
        self.ssl_cafile: str | None = config.get("ssl_cafile")
        self.reconnect_delay_seconds: float = float(config.get("reconnect_delay_seconds", 5.0))
        self.max_reconnect_attempts: int = int(config.get("max_reconnect_attempts", 0))
        self._stop_event: threading.Event = threading.Event()
        self._consumer = None
        # V18-01 §7: consumer-session / partition-assignment epoch fencing.
        # ``_assignment_epoch`` increments on every assignment change and on
        # every new consumer session; metas carry the epoch they were read
        # under and ``ack()`` ignores completions whose epoch is stale.
        self._lock = threading.Lock()
        self._assignment_epoch: int = 0
        self._assigned: set = set()
        # Per-partition completed frontier: {(topic, partition): offset} — the
        # highest offset whose entire prefix (≤ offset) has been completed via
        # ack(). Only this offset + 1 is ever committed per partition.
        self._completed: dict[tuple[str, int], int] = {}
        # Out-of-order completions per partition (plan C, per-record ack
        # path): offsets acked beyond the current frontier, waiting for the
        # missing (queued/in-flight) offsets to complete before the frontier
        # can advance across them. Never committed on their own.
        self._completed_ooo: dict[tuple[str, int], set[int]] = {}
        # Tombstone offsets per partition observed by read() (value None):
        # they need no processing, so the frontier sweep bridges them without
        # a per-record completion. A tombstone never advances the frontier or
        # fires a commit by itself; only a payload ack can.
        self._tombstone_offsets: dict[tuple[str, int], set[int]] = {}
        # Lowest offset read per partition this session: offsets below it were
        # never handed to the executor (consumer joined mid-log, or a previous
        # session/batch already committed them), so they are implicitly
        # complete — the frontier floor for the partition is read_min - 1.
        self._read_min: dict[tuple[str, int], int] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Interrupt read(): wake a blocked poll and skip the reconnect backoff.

        Called by the executor's stop-watcher thread when the pipeline stop
        event fires.  Closes the active consumer so a blocked ``poll()``
        unblocks immediately instead of waiting out the poll timeout; the read
        loop then observes the stop flag and exits without reconnecting.
        """
        self._stop_event.set()
        consumer = self._consumer
        if consumer is not None:
            try:
                consumer.close()
            except Exception:
                pass

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Sleep in slices so stop() interrupts the delay. Returns True if stopped."""
        deadline = time.monotonic() + seconds
        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._stop_event.wait(timeout=min(0.25, remaining))
        return True

    def _build_consumer(self):
        try:
            from kafka import KafkaConsumer
        except ImportError as exc:
            raise SourceError(
                "Kafka source requires kafka-python: pip install kafka-python"
            ) from exc

        kwargs: dict = dict(
            group_id=self.group_id,
            auto_offset_reset=self.auto_offset_reset,
            enable_auto_commit=self.enable_auto_commit,
            max_poll_records=self.max_poll_records,
            session_timeout_ms=self.session_timeout_ms,
            security_protocol=self.security_protocol,
            bootstrap_servers=self.brokers,
        )
        if self.sasl_mechanism:
            kwargs["sasl_mechanism"] = self.sasl_mechanism
            kwargs["sasl_plain_username"] = self.sasl_username
            kwargs["sasl_plain_password"] = self.sasl_password
        if self.ssl_cafile:
            kwargs["ssl_cafile"] = self.ssl_cafile

        return KafkaConsumer(*self.topics, **kwargs)

    # ── V18-01 §7: session/assignment-epoch fencing and frontiers ─────────

    def _sync_assignment(self, consumer) -> None:
        """Detect partition-assignment changes by diffing ``consumer.assignment()``.

        kafka-python exposes no rebalance callbacks, so the assignment set is
        compared across polls. Any change bumps ``_assignment_epoch``: metas
        yielded after the bump carry the new epoch, and completions read under
        the old epoch can no longer advance the frontier or commit (plan C —
        "old-epoch completions cannot advance a new assignment"). Frontier,
        out-of-order, and tombstone tracking for revoked partitions is dropped;
        the new owner re-polls from the last committed offsets (at-least-once,
        never loss).
        """
        current_set = set(consumer.assignment())
        with self._lock:
            previous = self._assigned
            if current_set == previous:
                return
            self._assignment_epoch += 1
            for tp in previous - current_set:
                key = (tp.topic, tp.partition)
                self._completed.pop(key, None)
                self._completed_ooo.pop(key, None)
                self._tombstone_offsets.pop(key, None)
                self._read_min.pop(key, None)
            self._assigned = current_set

    def _commit_offsets(self, consumer, offsets: dict, *, raise_on_error: bool) -> None:
        """Explicit per-partition commit: each mapping TopicPartition → frontier
        offset commits ``frontier + 1`` (the next offset the group resumes at)."""
        if not offsets:
            return
        try:
            from kafka import OffsetAndMetadata

            consumer.commit(
                {tp: OffsetAndMetadata(offset + 1, "", -1) for tp, offset in offsets.items()}
            )
        except Exception:
            if raise_on_error:
                raise
            logger.warning(
                "Kafka ack offset commit failed",
                extra={"partitions": sorted(str(tp) for tp in offsets)},
            )

    def _commit_batch(self, consumer, batch_tps: dict, epoch: int) -> None:
        """Legacy per-batch explicit commit (single-consumer path, plan C).

        Commits the batch-observed frontier (the highest offset per partition)
        for each partition the poll returned — the at-least-once contract of
        the current pre-ack executor, reached only after the caller resumed
        the generator past the batch's last message. Epoch-guarded: if a
        rebalance fired while the batch was being consumed, the commit is
        skipped and the new owner re-polls from the last committed offsets.
        """
        if not batch_tps or consumer is None:
            return
        with self._lock:
            if epoch != self._assignment_epoch:
                return
        self._commit_offsets(consumer, batch_tps, raise_on_error=True)

    def ack(self, meta: dict, disposition) -> None:
        """Advance the per-partition completed frontier and commit it.

        Called by the executor for a decided unit (delivered/filtered/dlq/
        dropped) — once per record under threaded execution (plan C: threaded
        execution must not commit past queued/in-flight records; completion
        order is independent of read order). Each record completion is tracked
        individually per partition; a partition's committable frontier advances
        only across its contiguous completed prefix. Out-of-order completions
        sit in a per-partition gap set until the missing offsets complete, and
        the frontier never commits past a queued/in-flight record (an unacked
        payload offset is a permanent gap). Tombstone offsets recorded by
        read() need no processing and are bridged by the frontier sweep without
        firing a commit of their own. Fenced by the assignment epoch captured
        at yield time: a completion whose epoch no longer matches (rebalance/
        revoke, or a new consumer session since the message was read) is
        ignored and never commits. When the frontier advances, the explicit
        offset (frontier + 1) is committed on the owning consumer; commit
        failures are logged, never raised into the executor. The legacy
        batch-boundary commit path (_commit_batch) is unchanged and continues
        to serve non-strict/legacy pipelines.
        """
        if self.enable_auto_commit:
            return
        topic = meta.get("kafka_topic")
        partition = meta.get("kafka_partition")
        offset = meta.get("kafka_offset")
        if topic is None or partition is None or offset is None:
            return
        with self._lock:
            if meta.get("kafka_epoch", -1) != self._assignment_epoch:
                logger.warning(
                    "Kafka stale-epoch completion ignored",
                    extra={"topic": topic, "partition": partition, "offset": offset},
                )
                return
            consumer = self._consumer
            if consumer is None:
                logger.warning(
                    "Kafka ack skipped: no active consumer",
                    extra={"topic": topic, "partition": partition, "offset": offset},
                )
                return
            key = (topic, partition)
            frontier = self._completed.get(key, -1)
            read_min = self._read_min.get(key)
            if read_min is None:
                # No read() coverage recorded for this partition (acks driven
                # directly, e.g. unit tests): the first completion establishes
                # the frontier — offsets below it were never read, so none of
                # them can be queued/in-flight.
                if offset <= frontier:
                    return
                self._completed[key] = offset
                frontier = offset
            else:
                # Offsets below the first read were never handed to the
                # executor (mid-log join, or already committed by a previous
                # session/batch) — they are implicitly complete.
                if read_min - 1 > frontier:
                    frontier = read_min - 1
                if offset <= frontier:
                    # Already covered by the contiguous prefix (duplicate ack):
                    # nothing new is committable.
                    return
                ooo = self._completed_ooo.setdefault(key, set())
                if offset in ooo:
                    # Already recorded as an out-of-order completion.
                    return
                ooo.add(offset)
                tombstones = self._tombstone_offsets.get(key)
                # Sweep the contiguous completed prefix: the next offset is
                # complete when it was acked out of order or is a tombstone
                # that needs no processing. The frontier can never pass an
                # unacked payload record (queued/in-flight).
                next_offset = frontier + 1
                advanced = False
                while next_offset in ooo or (
                    tombstones is not None and next_offset in tombstones
                ):
                    ooo.discard(next_offset)
                    if tombstones is not None:
                        tombstones.discard(next_offset)
                    frontier = next_offset
                    next_offset += 1
                    advanced = True
                if not ooo:
                    self._completed_ooo.pop(key, None)
                if tombstones is not None and not tombstones:
                    self._tombstone_offsets.pop(key, None)
                if not advanced:
                    return  # gap remains: the frontier did not move, no commit
                self._completed[key] = frontier
        try:
            from kafka import TopicPartition

            self._commit_offsets(
                consumer,
                {TopicPartition(topic, partition): frontier},
                raise_on_error=False,
            )
        except Exception as exc:
            logger.warning(
                "Kafka ack commit failed",
                extra={"topic": topic, "partition": partition, "offset": offset,
                       "error": str(exc)},
            )

    def test_connection(self) -> dict:
        t0 = time.monotonic()
        try:
            from kafka import KafkaAdminClient
        except ImportError:
            raise RuntimeError("kafka-python not installed — pip install tram[kafka]")
        brokers = self.config.get("brokers", [])
        if isinstance(brokers, str):
            brokers = [brokers]
        client = KafkaAdminClient(
            bootstrap_servers=brokers,
            request_timeout_ms=5000,
            connections_max_idle_ms=5000,
        )
        try:
            topics = client.list_topics()
        finally:
            client.close()
        latency = int((time.monotonic() - t0) * 1000)
        return {"ok": True, "latency_ms": latency,
                "detail": f"Connected to {len(brokers)} broker(s), {len(topics)} topics"}

    def _update_lag(self, consumer) -> None:
        """Best-effort lag metric, sampled once per poll batch.

        The previous implementation called the synchronous ``end_offsets``
        broker round-trip once per message, destroying throughput on busy
        topics (code review B4); per-batch sampling bounds that cost.
        """
        try:
            from tram.metrics.registry import KAFKA_LAG

            partitions = consumer.assignment()
            end_offsets = consumer.end_offsets(list(partitions))
            for tp, end in end_offsets.items():
                pos = consumer.position(tp)
                lag = max(0, end - pos)
                KAFKA_LAG.labels(
                    pipeline=self.group_id,
                    topic=tp.topic,
                    partition=str(tp.partition),
                ).set(lag)
        except Exception:
            pass  # Lag metric is best-effort

    def read(self) -> Iterator[tuple[bytes, dict]]:
        logger.info(
            "Kafka consumer starting",
            extra={"brokers": self.brokers, "topics": self.topics, "group": self.group_id},
        )

        attempt = 0
        max_attempts = self.max_reconnect_attempts  # 0 = infinite

        while True:
            if self._stop_event.is_set():
                return
            consumer = None
            try:
                try:
                    consumer = self._build_consumer()
                except SourceError:
                    raise
                except Exception as exc:
                    raise SourceError(f"Kafka consumer init failed: {exc}") from exc

                attempt = 0  # Reset on successful connect
                with self._lock:
                    # New consumer session: bump the epoch so in-flight
                    # completions from the previous session can never commit
                    # on this consumer, and reset assignment/frontier state.
                    self._assignment_epoch += 1
                    self._assigned = set()
                    self._completed.clear()
                    self._completed_ooo.clear()
                    self._tombstone_offsets.clear()
                    self._read_min.clear()
                self._consumer = consumer
                while True:
                    if self._stop_event.is_set():
                        return
                    batch = consumer.poll(timeout_ms=1000)
                    self._sync_assignment(consumer)
                    if not batch:
                        continue
                    with self._lock:
                        batch_epoch = self._assignment_epoch
                    self._update_lag(consumer)
                    # Materialize the poll batch in yield order so the
                    # batch-end marker can be pinned to the final message the
                    # executor will actually receive (GH #78): the executor
                    # flushes its micro-batch buffer when it sees
                    # ``source_batch_end``, and this source commits the batch
                    # offsets only when the executor resumes the generator past
                    # that message — so the commit can never precede the flush
                    # (at-least-once, single-threaded path).
                    ordered = [
                        (msg.topic, msg.partition, msg)
                        for _tp, msgs in batch.items()
                        for msg in msgs
                    ]
                    # Highest offset per partition in this batch — the
                    # batch-observed frontier committed by the legacy path.
                    batch_tps = {
                        tp: max(m.offset for m in msgs) for tp, msgs in batch.items()
                    }
                    with self._lock:
                        # Lowest offset read per partition this session (the
                        # first record of each partition's poll list): offsets
                        # below it were never handed to the executor and are
                        # implicitly complete — the ack-path frontier floor.
                        for topic, partition, _m in ordered:
                            if (topic, partition) not in self._read_min:
                                self._read_min[(topic, partition)] = _m.offset
                    live_count = sum(1 for _t, _p, m in ordered if m.value is not None)
                    yielded = 0
                    for topic, partition, msg in ordered:
                        value = msg.value
                        if value is None:
                            # Tombstone: needs no processing, so it is never
                            # yielded and never acked. Record its offset so the
                            # ack-path frontier sweep can bridge it without a
                            # per-record completion; a tombstone alone never
                            # advances a frontier or fires a commit (only a
                            # payload ack can), and offsets already covered by
                            # the frontier are not retained. The sweep
                            # discards each bridged offset, bounding the set.
                            with self._lock:
                                if msg.offset > self._completed.get(
                                    (topic, partition), -1
                                ):
                                    self._tombstone_offsets.setdefault(
                                        (topic, partition), set()
                                    ).add(msg.offset)
                            continue
                        yielded += 1
                        yield value, {
                            "kafka_topic": topic,
                            "kafka_partition": partition,
                            "kafka_offset": msg.offset,
                            "kafka_key": msg.key.decode("utf-8") if msg.key else None,
                            "kafka_epoch": batch_epoch,
                            "source_batch_end": yielded == live_count,
                        }
                    if not self.enable_auto_commit:
                        # Explicit per-partition commit. Reached only when the
                        # caller has resumed the generator past the last message
                        # of this poll batch (i.e. it fully consumed the batch),
                        # so a mid-batch abort never commits. With auto-commit
                        # enabled the consumer handles commits on its own timer.
                        self._commit_batch(consumer, batch_tps, batch_epoch)

            except SourceError:
                raise
            except Exception as exc:
                if self._stop_event.is_set():
                    # stop() closed the consumer mid-poll; exit instead of
                    # treating the closure as a connection loss to retry.
                    return
                attempt += 1
                if max_attempts > 0 and attempt >= max_attempts:
                    raise SourceError(
                        f"Kafka consumer error after {attempt} reconnect attempts: {exc}"
                    ) from exc
                logger.warning(
                    "Kafka consumer error — reconnecting",
                    extra={
                        "topics": self.topics,
                        "attempt": attempt,
                        "delay": self.reconnect_delay_seconds,
                        "error": str(exc),
                    },
                )
                if self._sleep_interruptible(self.reconnect_delay_seconds):
                    return
            finally:
                if consumer is not None:
                    # Deliberately NO commit() here: a finally-commit would
                    # persist offsets for a partially consumed batch when the
                    # caller aborts mid-batch (sink error / run stop), silently
                    # losing those messages. At-least-once means the uncommitted
                    # tail is re-polled instead.
                    try:
                        consumer.close()
                        logger.info("Kafka consumer closed", extra={"topics": self.topics})
                    except Exception:
                        pass
                    finally:
                        self._consumer = None
