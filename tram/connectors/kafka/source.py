"""Kafka source connector — infinite stream consumer."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
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
        cluster_incarnation (str, optional)         Operator-declared cluster
                                                    incarnation token for the
                                                    §7 replay identity;
                                                    overrides the broker
                                                    cluster ID.

    Commit semantics (at-least-once default):
    With ``enable_auto_commit: false`` (the default) offsets are committed
    explicitly as per-partition completed frontiers (V18-01 §7 / plan C).
    Two paths share the epoch-fenced explicit commits:

    * Direct-reader legacy batch path — each poll batch is committed once the caller has
      consumed every message in it (the at-least-once contract of the
      direct synchronous reader: a crash at any point leaves the uncommitted
      batch to be re-polled on restart). The executor disables this path with
      its internal ``_ack_managed`` flag before intake begins.
    * Per-record ack path — ``ack(meta, disposition)`` marks one record
      complete; a partition's committable frontier advances only across its
      contiguous completed prefix (V18-01 §7 / plan C: threaded execution
      must not commit past queued/in-flight records; completion order is
      independent of read order). Out-of-order completions are tracked per
      partition until the missing offsets complete; each time the frontier
      advances, the explicit offset (frontier + 1) is queued as a pending
      commit for that partition on the owning consumer. Tombstone records
      (value None) need no processing: read() records their offsets and the
      frontier sweep bridges them. Managed readers also sweep at tombstone
      intake, so tombstone-only batches and trailing tombstones can advance a
      decided prefix without waiting for another payload. Undecided payloads
      still block that prefix. Direct legacy readers commit tombstone-only
      batches at intake completion.

    The per-record ack path never commits from the acking thread: kafka-python
    consumers are not thread-safe for concurrent commits, and racing commits
    could land oldest-last (re-processing on restart). ``ack()`` records the
    frontier advance in a pending-commit set under the lock; the poll loop —
    the reader thread — drains it once per poll iteration and issues at most
    one consolidated commit per partition per drain (V18-02). Completion is
    bound to the consumer session and partition-assignment epoch: any
    revoked partition receives a new ownership epoch when reassigned;
    in-flight completions from its old epoch are ignored — they cannot advance a new
    assignment's frontier. Revocation drops the revoked partition's frontier,
    out-of-order, tombstone, and pending-commit tracking, and a new consumer
    session resets the whole set. The gap-aware threaded frontier
    implementation is broker-proven (V18-10 live-broker gate) and is
    exercised end to end by the env-gated live suite
    (``tests/unit/test_kafka_live_broker.py``) alongside the deterministic
    unit tests.

    Under stream micro-batching (GH #78) the last message of each poll batch
    carries ``source_batch_end: true`` in its meta; the executor flushes its
    micro-batch buffer when it sees that marker. Executor-managed readers still
    require the decided per-record ack frontier before committing offsets;
    resuming the generator past a batch alone cannot authorize a commit.
    Setting ``enable_auto_commit:
    true`` restores the legacy at-most-once behavior: the consumer commits on
    its own ~5s timer regardless of sink progress, and the explicit commits
    are disabled.

    Replay identity (V18-01 §7): ``source_unit_id(meta)`` returns the frozen
    ``{cluster_incarnation}/{topic}/{partition}`` unit identity. The
    incarnation is the broker cluster ID — the consumer-group metadata that
    defines offset continuity: committed offsets live in the cluster's
    ``__consumer_offsets`` topic, so the same cluster ID means the same offset
    store. Rebalances, consumer-session resets, and worker/broker restarts of
    the same cluster all keep the identity and the per-partition frontier
    (the committed offset) advances in place; a new cluster (fresh format /
    new log storage) reports a new cluster ID and offsets never carry over —
    a new identity. An operator-declared ``cluster_incarnation`` config
    override wins (e.g. after an approved state reset). The identity never
    embeds the offset: the checkpoint row keys on
    ``{incarnation}/{topic}/{partition}`` and upserts the offset frontier.
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
        # Executor-managed readers decide delivery through ack(); exhausting
        # a poll batch only proves intake, not completion of its records.
        self._ack_managed: bool = bool(config.get("_ack_managed", False))
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
        # The global counter allocates per-partition ownership epochs. Reader
        # metas retain their partition's token while ownership is uninterrupted;
        # revoke/reassign and a new consumer session fence old completions.
        self._lock = threading.Lock()
        self._assignment_epoch: int = 0
        self._partition_epochs: dict[tuple[str, int], int] = {}
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
        # Ordered observed offsets distinguish compacted log holes from actual
        # undecided payloads. Only a decided observed prefix can be committed.
        self._observed_offsets: dict[tuple[str, int], deque[int]] = {}
        # Pending per-partition frontier commits recorded by ack() (V18-02).
        # ack() NEVER commits directly: kafka-python consumers are not
        # thread-safe for concurrent commits, and racing worker-thread commits
        # could land oldest-last (re-processing on restart). The poll loop
        # (reader thread) drains this set once per poll iteration and issues at
        # most one consolidated commit per partition per drain, epoch-fenced
        # (revoked partitions and reset sessions drop their pending entries).
        self._pending_commits: dict[tuple[str, int], int] = {}
        # V18-01 §7 replay identity: the cluster-incarnation token that feeds
        # ``source_unit_id(meta)``. This is the component that changes only
        # when committed offsets stop being meaningful (a new cluster, or an
        # operator-approved reset), so the ``{cluster_incarnation}/{topic}/
        # {partition}`` identity is stable across rebalances and worker
        # restarts within one incarnation. An operator-declared
        # ``cluster_incarnation`` config override wins; otherwise the token is
        # captured lazily from the active consumer's broker metadata (the
        # cluster ID is the offset-continuity metadata) and cached on first
        # use — never a guessed value.
        configured_incarnation = config.get("cluster_incarnation")
        self._incarnation: str | None = (
            configured_incarnation
            if isinstance(configured_incarnation, str) and configured_incarnation
            else None
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Request reader shutdown and interrupt reconnect backoff.

        Executor-managed readers retain the consumer until the owning thread
        calls ``close()`` after deciding the final tail. Legacy readers close
        immediately to wake a blocked poll.
        """
        self._stop_event.set()
        if self._ack_managed:
            # The reader polls at most once per second. Keep its consumer
            # available until the executor decides and acknowledges the tail;
            # the stop-watcher must not close a reader-owned consumer.
            return
        consumer = self._consumer
        if consumer is not None:
            try:
                consumer.close()
            except Exception:
                pass

    def close(self) -> None:
        """Drain decided offsets and release the consumer on its owning thread."""
        consumer = self._consumer
        if consumer is None:
            return
        if self._ack_managed and not self.enable_auto_commit:
            self._drain_pending_commits(consumer)
        try:
            consumer.close()
            logger.info("Kafka consumer closed", extra={"topics": self.topics})
        except Exception:
            pass
        finally:
            if self._consumer is consumer:
                self._consumer = None

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
            from kafka import ConsumerRebalanceListener, KafkaConsumer
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

        source = self

        class Listener(ConsumerRebalanceListener):
            def on_partitions_revoked(self, partitions) -> None:
                source._on_partitions_revoked(partitions)

            def on_partitions_assigned(self, partitions) -> None:
                source._on_partitions_assigned(partitions)

        consumer = KafkaConsumer(**kwargs)
        try:
            consumer.subscribe(topics=self.topics, listener=Listener())
        except Exception:
            consumer.close()
            raise
        return consumer

    # ── V18-01 §7: session/assignment-epoch fencing and frontiers ─────────

    def _reset_session_state(self) -> None:
        """New consumer session: drop all frontier/ack/pending state.

        Bumps the assignment epoch so in-flight completions from the previous
        session can never commit on this consumer, and clears the assignment,
        frontier, gap, tombstone, read-floor, and pending-commit tracking.
        """
        with self._lock:
            self._assignment_epoch += 1
            self._partition_epochs.clear()
            self._assigned = set()
            self._completed.clear()
            self._completed_ooo.clear()
            self._tombstone_offsets.clear()
            self._read_min.clear()
            self._observed_offsets.clear()
            self._pending_commits.clear()

    def _forget_partition(self, tp) -> None:
        """Drop revoked ownership while holding ``_lock``."""
        key = (tp.topic, tp.partition)
        self._partition_epochs.pop(key, None)
        self._completed.pop(key, None)
        self._completed_ooo.pop(key, None)
        self._tombstone_offsets.pop(key, None)
        self._read_min.pop(key, None)
        self._observed_offsets.pop(key, None)
        self._pending_commits.pop(key, None)

    def _on_partitions_revoked(self, partitions) -> None:
        """Authoritative revocation fences even a same-set reassignment."""
        revoked = set(partitions)
        with self._lock:
            self._assignment_epoch += 1
            for tp in revoked:
                self._forget_partition(tp)
            self._assigned -= revoked

    def _on_partitions_assigned(self, partitions) -> None:
        current_set = set(partitions)
        with self._lock:
            self._assignment_epoch += 1
            for tp in self._assigned - current_set:
                self._forget_partition(tp)
            for tp in current_set:
                self._partition_epochs.setdefault((tp.topic, tp.partition), self._assignment_epoch)
            self._assigned = current_set

    def _sync_assignment(self, consumer) -> None:
        """Reconcile set changes while preserving uninterrupted ownership.

        Real rebalances use the listener, including revoke/reassign to the
        same set. This reconciliation also handles direct/manual assignments.
        """
        current_set = set(consumer.assignment())
        with self._lock:
            if current_set == self._assigned:
                return
            self._assignment_epoch += 1
            for tp in self._assigned - current_set:
                self._forget_partition(tp)
            for tp in current_set:
                self._partition_epochs.setdefault((tp.topic, tp.partition), self._assignment_epoch)
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
        with self._lock:
            for tp, frontier in batch_tps.items():
                observed = self._observed_offsets.get((tp.topic, tp.partition))
                if observed is not None:
                    while observed and observed[0] <= frontier:
                        observed.popleft()

    def _drain_pending_commits(self, consumer) -> None:
        """Reader-thread commit dispatch (V18-02).

        Called by the poll loop (the reader thread) once per poll iteration:
        the pending-commit set recorded by ``ack()`` is captured whole under
        the lock and issued as one consolidated commit per partition (the
        latest frontier per partition), then released for the next drain.
        ``ack()`` itself never commits — only this drain touches
        ``consumer.commit`` outside the lock, so concurrent worker-thread acks
        can never race a commit (two racing commits could land oldest-last).
        Epoch-fenced by construction: session resets clear the pending set and
        ``_sync_assignment`` drops entries for revoked partitions before the
        drain runs, so everything drained here belongs to the current
        assignment epoch.
        """
        if consumer is None:
            return
        with self._lock:
            if not self._pending_commits:
                return
            pending = self._pending_commits
            self._pending_commits = {}
            epochs = {
                key: self._partition_epochs.get(key, self._assignment_epoch) for key in pending
            }
        try:
            from kafka import TopicPartition

            self._commit_offsets(
                consumer,
                {TopicPartition(t, p): f for (t, p), f in pending.items()},
                raise_on_error=True,
            )
        except Exception as exc:
            with self._lock:
                # An uncertain/failed commit still owns this frontier. Keep
                # it for the next reader-thread drain, without overwriting a
                # newer concurrent completion or resurrecting a stale session.
                if consumer is self._consumer:
                    for key, frontier in pending.items():
                        if epochs[key] == self._partition_epochs.get(key, self._assignment_epoch):
                            self._pending_commits[key] = max(
                                self._pending_commits.get(key, -1), frontier
                            )
            logger.warning(
                "Kafka pending commit drain failed",
                extra={
                    "partitions": sorted(str(tp) for tp in pending),
                    "error": str(exc),
                },
            )

    # ── V18-01 §7: replay identity ─────────────────────────────────────────

    def _resolve_incarnation(self) -> str | None:
        """Cluster-incarnation token for the replay identity, resolved once.

        Committed offsets live in the cluster's ``__consumer_offsets`` topic,
        so the broker-reported cluster ID is the consumer-group metadata that
        defines offset continuity: the same cluster ID means the same offset
        store. Rebalances, consumer-session resets, and worker/broker restarts
        of the same cluster all keep the identity and the per-partition
        frontier advances in place; a new cluster (fresh format / new log
        storage) reports a new cluster ID and the old offsets no longer carry
        over — a new identity. An operator-declared ``cluster_incarnation``
        config override wins (e.g. after an approved state reset). The token
        is captured from the active consumer's broker metadata on first use
        and cached; ``None`` while no consumer is active and no override is
        declared — no durable identity (never a guessed one).
        """
        if self._incarnation is not None:
            return self._incarnation
        consumer = self._consumer
        if consumer is None:
            return None
        cluster = getattr(getattr(consumer, "_client", None), "cluster", None)
        cluster_id = getattr(cluster, "cluster_id", None)
        if isinstance(cluster_id, str) and cluster_id:
            self._incarnation = cluster_id
            return cluster_id
        return None

    def source_unit_id(self, meta: dict) -> str | None:
        """Frozen V18-01 §7 replay identity: ``{cluster_incarnation}/{topic}/{partition}``.

        The unit is the partition, never the record: the identity never embeds
        the offset, so ``(pipeline_name, source_unit)`` checkpoint rows upsert
        in place and the frontier — the per-partition committed offset — moves
        within the identity. ``None`` when the meta lacks the partition
        coordinates or no incarnation is resolvable (no active consumer and no
        operator-declared incarnation); the §7 matrix counts Kafka as
        identity-present, so a strict pipeline is accepted at validation and
        an identity-less meta simply is not delivery-checkpointed.
        """
        topic = meta.get("kafka_topic")
        partition = meta.get("kafka_partition")
        if topic is None or partition is None:
            return None
        incarnation = self._resolve_incarnation()
        if incarnation is None:
            return None
        return f"{incarnation}/{topic}/{partition}"

    def _advance_decided_frontier(self, key: tuple[str, int]) -> None:
        """Sweep decided payloads/tombstones while holding ``_lock``.

        The first observed offset establishes the floor. Any subsequently
        observed payload without an ack blocks both kinds of completion.
        """
        frontier = max(self._completed.get(key, -1), self._read_min[key] - 1)
        ooo = self._completed_ooo.get(key, set())
        tombstones = self._tombstone_offsets.get(key, set())
        advanced = False
        observed = self._observed_offsets.get(key)
        while True:
            # Kafka compaction/control records may leave numerical holes.
            # Once read() supplies coverage, the next observed offset is the
            # barrier; an absent offset is not an outstanding delivery.
            if observed is not None:
                if not observed:
                    break
                next_offset = observed[0]
            else:
                # Direct ack callers without poll coverage retain the legacy
                # numerical frontier used by the interface's unit harnesses.
                next_offset = frontier + 1
            if next_offset not in ooo and next_offset not in tombstones:
                break
            frontier = next_offset
            if observed is not None:
                observed.popleft()
            ooo.discard(frontier)
            tombstones.discard(frontier)
            advanced = True
        if not ooo:
            self._completed_ooo.pop(key, None)
        if not tombstones:
            self._tombstone_offsets.pop(key, None)
        if advanced:
            self._completed[key] = frontier
            self._pending_commits[key] = max(
                self._pending_commits.get(key, -1), frontier
            )

    def ack(self, meta: dict, disposition) -> None:
        """Advance the per-partition completed frontier and record its commit.

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
        bypassing undecided payloads. Fenced by the assignment epoch captured
        at yield time: a completion whose epoch no longer matches (rebalance/
        revoke, or a new consumer session since the message was read) is
        ignored and never commits.

        A frontier advance is recorded in ``_pending_commits`` under the lock —
        this thread NEVER calls ``consumer.commit`` (kafka-python consumers are
        not thread-safe for concurrent commits). The poll loop (reader thread)
        drains the pending set and issues at most one consolidated commit per
        partition per drain (``_drain_pending_commits``). The legacy
        batch-boundary commit path (_commit_batch) serves direct readers only;
        executor-managed readers commit exclusively through this ack path.
        """
        if self.enable_auto_commit:
            return
        topic = meta.get("kafka_topic")
        partition = meta.get("kafka_partition")
        offset = meta.get("kafka_offset")
        if topic is None or partition is None or offset is None:
            return
        with self._lock:
            key = (topic, partition)
            epoch = self._partition_epochs.get(key, self._assignment_epoch)
            if meta.get("kafka_epoch", -1) != epoch:
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
                self._advance_decided_frontier(key)
                return
            # Record the frontier advance as a pending commit for the reader
            # thread's drain — never commit from this thread. The max guard
            # keeps the pending value monotone under concurrent acks.
            self._pending_commits[key] = max(
                self._pending_commits.get(key, -1), frontier
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

                # New consumer session: bump the epoch so in-flight
                # completions from the previous session can never commit on
                # this consumer, and reset assignment/frontier/pending state.
                self._reset_session_state()
                self._consumer = consumer
                while True:
                    if self._stop_event.is_set():
                        return
                    batch = consumer.poll(timeout_ms=1000)
                    # Construction alone can succeed while every poll fails.
                    # Reset the failure streak only after successful intake.
                    attempt = 0
                    self._sync_assignment(consumer)
                    # Reader-thread commit dispatch (V18-02): drain the pending
                    # commits recorded by ack() — at most one consolidated
                    # commit per partition per drain, epoch-fenced (revokes
                    # were dropped by _sync_assignment above). Runs on every
                    # poll iteration, empty or not, so frontier advances are
                    # not gated on message traffic.
                    self._drain_pending_commits(consumer)
                    if not batch:
                        continue
                    with self._lock:
                        batch_epoch = self._assignment_epoch
                        batch_epochs = {
                            (tp.topic, tp.partition): self._partition_epochs.get(
                                (tp.topic, tp.partition), batch_epoch
                            ) for tp in batch
                        }
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
                            key = (topic, partition)
                            if key not in self._read_min:
                                self._read_min[key] = _m.offset
                            observed = self._observed_offsets.setdefault(key, deque())
                            floor = observed[-1] if observed else self._completed.get(key, -1)
                            if _m.offset > floor:
                                observed.append(_m.offset)
                    live_count = sum(1 for _t, _p, m in ordered if m.value is not None)
                    yielded = 0
                    for topic, partition, msg in ordered:
                        value = msg.value
                        if value is None:
                            # Tombstone: needs no processing, so it is never
                            # yielded and never acked. Record its offset so the
                            # ack-path frontier sweep can bridge it without a
                            # per-record completion. Managed readers advance
                            # the decided prefix here too, while undecided
                            # payloads remain barriers. Covered offsets are
                            # not retained. The sweep
                            # discards each bridged offset, bounding the set.
                            with self._lock:
                                if msg.offset > self._completed.get(
                                    (topic, partition), -1
                                ):
                                    self._tombstone_offsets.setdefault(
                                        (topic, partition), set()
                                    ).add(msg.offset)
                                    if self._ack_managed and not self.enable_auto_commit:
                                        self._advance_decided_frontier((topic, partition))
                            continue
                        yielded += 1
                        yield value, {
                            "kafka_topic": topic,
                            "kafka_partition": partition,
                            "kafka_offset": msg.offset,
                            "kafka_key": msg.key.decode("utf-8") if msg.key else None,
                            "kafka_epoch": batch_epochs[(topic, partition)],
                            "source_batch_end": yielded == live_count,
                        }
                    if not self.enable_auto_commit and not self._ack_managed:
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
                    # Managed readers may have a final decided frontier when
                    # batch_size/stop closes the generator before another
                    # poll. Drain only ack/tombstone decisions on this reader
                    # thread; never commit the poll batch's observed offsets.
                    if self._ack_managed and self._stop_event.is_set():
                        # Natural stop returns control to the executor before
                        # its buffered tail has been decided. The executor's
                        # source.close() owns the final drain and close.
                        pass
                    else:
                        self.close()
