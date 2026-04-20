"""PipelineExecutor — execution modes: batch_run(), stream_run(), dry_run()."""

from __future__ import annotations

import base64
import json
import logging
import queue as _queue
import random
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tram.connectors.file_sink_common import extract_field_paths
from tram.core.context import PipelineRunContext, RunResult, RunStatus
from tram.core.exceptions import TramError
from tram.registry.registry import get_serializer, get_sink, get_source, get_transform

if TYPE_CHECKING:
    from tram.agent.metrics import PipelineStats
    from tram.models.pipeline import PipelineConfig
    from tram.persistence.file_tracker import ProcessedFileTracker

logger = logging.getLogger(__name__)


def _make_evaluator():
    try:
        from simpleeval import DEFAULT_FUNCTIONS, EvalWithCompoundTypes
        funcs = dict(DEFAULT_FUNCTIONS)
        funcs.update({
            "round": round, "abs": abs, "int": int, "float": float,
            "str": str, "len": len, "min": min, "max": max,
        })
        return EvalWithCompoundTypes, funcs
    except ImportError:
        logger.warning(
            "simpleeval is not installed — condition-based routing is disabled. "
            "Install it with: pip install simpleeval"
        )
        return None, None


_EvalCls, _EVAL_FUNCS = _make_evaluator()


def _payload_size_bytes(payload) -> int:
    """Best-effort byte size for raw source payloads and serialized sink payloads."""
    if payload is None:
        return 0
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return len(payload)
    if isinstance(payload, str):
        return len(payload.encode("utf-8"))
    try:
        return len(json.dumps(payload, default=str).encode("utf-8"))
    except Exception:
        return len(str(payload).encode("utf-8"))


def _filter_by_condition(records: list[dict], condition: str) -> list[dict]:
    """Return subset of records where condition evaluates to truthy."""
    if _EvalCls is None:
        raise TramError("simpleeval is required for conditional routing")
    result = []
    for record in records:
        try:
            evaluator = _EvalCls(names=record, functions=_EVAL_FUNCS)
            if evaluator.eval(condition):
                result.append(record)
        except Exception as exc:
            raise TramError(f"Condition eval error: {condition!r} — {exc}") from exc
    return result


_FILE_TEMPLATE_ATTRS = ("filename_template", "key_template", "blob_template")


def _lookup_record_field(record: dict, path: str) -> str:
    current: object = record
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return "unknown"
        current = current[segment]
    if current in (None, ""):
        return "unknown"
    return str(current)


def _sink_filename_template(sink_cfg, sink_instance) -> str | None:
    for attr in _FILE_TEMPLATE_ATTRS:
        value = getattr(sink_cfg, attr, None) if sink_cfg is not None else None
        if isinstance(value, str):
            return value
        value = getattr(sink_instance, attr, None)
        if isinstance(value, str):
            return value
    return None


def _partition_records_for_template(
    records: list[dict],
    template: str | None,
) -> list[tuple[dict[str, str], list[dict]]]:
    if not template:
        return [({}, records)]
    field_paths = extract_field_paths(template)
    if not field_paths:
        return [({}, records)]

    grouped: OrderedDict[tuple[tuple[str, str], ...], dict[str, object]] = OrderedDict()
    for record in records:
        field_values = {path: _lookup_record_field(record, path) for path in field_paths}
        key = tuple((path, field_values[path]) for path in field_paths)
        if key not in grouped:
            grouped[key] = {"field_values": field_values, "records": []}
        grouped[key]["records"].append(record)
    return [(entry["field_values"], entry["records"]) for entry in grouped.values()]


def _write_dlq_envelope(
    dlq_sink,
    ctx: PipelineRunContext,
    *,
    stage: str,
    error: str,
    record=None,
    raw: bytes | None = None,
) -> None:
    """Write a DLQ envelope to the DLQ sink. Errors are logged and swallowed."""
    envelope: dict = {
        "_error": error,
        "_stage": stage,
        "_pipeline": ctx.pipeline_name,
        "_run_id": ctx.run_id,
        "_timestamp": datetime.now(UTC).isoformat(),
        "record": record,
    }
    if stage == "parse" and raw is not None:
        envelope["raw"] = base64.b64encode(raw).decode()
    try:
        dlq_sink.write(json.dumps(envelope).encode(), {})
    except Exception as dlq_exc:
        logger.error(
            "DLQ write failed",
            extra={"pipeline": ctx.pipeline_name, "error": str(dlq_exc)},
        )


class PipelineExecutor:
    """Executes pipeline configurations in batch or stream mode."""

    def __init__(self, file_tracker: ProcessedFileTracker | None = None) -> None:
        self._last_refill: float = 0.0
        self._tokens: float = 0.0
        self._rate_lock = threading.Lock()  # guards _tokens and _last_refill
        self._file_tracker = file_tracker
        # Circuit breaker state: {sink_key: (failure_count, open_until_monotonic)}
        self._cb_state: dict[str, tuple[int, float]] = {}
        self._cb_lock = threading.Lock()

    # ── Rate limiting ────────────────────────────────────────────────────────────

    def _rate_limit(self, rps: float) -> None:
        """Token-bucket rate limiter. Blocks until a token is available.

        Thread-safe: _tokens and _last_refill are protected by _rate_lock.
        Note: rate_limit_rps is approximate when thread_workers > 1 because
        the sleep happens outside the lock to avoid holding it during sleep.
        """
        with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(rps, self._tokens + elapsed * rps)
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                sleep_time = 0.0
            else:
                sleep_time = (1.0 - self._tokens) / rps
                self._tokens = 0.0
                self._last_refill = time.monotonic()
        if sleep_time > 0:
            time.sleep(sleep_time)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _build_source(self, config: PipelineConfig):
        src_cls = get_source(config.source.type)
        src_conf = config.source.model_dump()
        src_conf["_pipeline_name"] = config.name   # connectors use as default group/queue id
        if self._file_tracker is not None:
            src_conf["_file_tracker"] = self._file_tracker
        return src_cls(src_conf)

    def _build_sinks(self, config: PipelineConfig) -> list[tuple]:
        """Returns list of (sink_instance, condition_str|None, sink_transforms, sink_cfg, per_sink_ser|None)."""
        result = []
        for sink_cfg in config.sinks:
            sink_cls = get_sink(sink_cfg.type)
            condition = getattr(sink_cfg, "condition", None)
            sink_transforms = []
            pipeline_ctx = {"name": config.name, "source": config.source.model_dump()}
            for t_cfg in getattr(sink_cfg, "transforms", []):
                t_cls = get_transform(t_cfg.type)
                d = t_cfg.model_dump()
                d["_pipeline"] = pipeline_ctx
                sink_transforms.append(t_cls(d))
            # Per-sink serializer_out override (None → use global serializer_out)
            per_sink_ser = None
            sink_ser_cfg = getattr(sink_cfg, "serializer_out", None)
            if sink_ser_cfg is not None:
                ser_cls = get_serializer(sink_ser_cfg.type)
                per_sink_ser = ser_cls(sink_ser_cfg.model_dump())
            result.append((sink_cls(sink_cfg.model_dump()), condition, sink_transforms, sink_cfg, per_sink_ser))
        return result

    def _build_dlq_sink(self, config: PipelineConfig):
        """Build the DLQ sink instance, or None if not configured."""
        if config.dlq is None:
            return None
        sink_cls = get_sink(config.dlq.type)
        return sink_cls(config.dlq.model_dump())

    def _build_serializer_in(self, config: PipelineConfig):
        ser_cls = get_serializer(config.serializer_in.type)
        return ser_cls(config.serializer_in.model_dump())

    def _build_serializer_out(self, config: PipelineConfig):
        if config.serializer_out is None:
            from tram.serializers.json_serializer import JsonSerializer
            return JsonSerializer({})
        ser_cls = get_serializer(config.serializer_out.type)
        return ser_cls(config.serializer_out.model_dump())

    def _build_transforms(self, config: PipelineConfig) -> list:
        pipeline_ctx = {"name": config.name, "source": config.source.model_dump()}
        transforms = []
        for t_cfg in config.transforms:
            t_cls = get_transform(t_cfg.type)
            d = t_cfg.model_dump()
            d["_pipeline"] = pipeline_ctx
            transforms.append(t_cls(d))
        return transforms

    @staticmethod
    def _set_transform_runtime_meta(transform, meta: dict) -> None:
        """Pass per-chunk metadata to transforms that opt into it."""
        setter = getattr(transform, "set_runtime_meta", None)
        if callable(setter):
            setter(meta)

    @staticmethod
    def _make_sink_cb_key(config: PipelineConfig, index: int) -> str:
        """Stable circuit-breaker key that survives object re-creation.

        Keyed by (pipeline_name, sink_type, sink_index) rather than
        ``id(sink_instance)`` to prevent misidentification when a sink object
        is garbage-collected and a new one lands at the same memory address.
        """
        sink_type = config.sinks[index].type if index < len(config.sinks) else "unknown"
        return f"{config.name}:{sink_type}:{index}"

    def _process_chunk(
        self,
        raw: bytes,
        meta: dict,
        serializer_in,
        transforms: list,
        serializer_out,
        sinks: list[tuple],
        ctx: PipelineRunContext,
        on_error: str,
        rate_limit_rps: float | None = None,
        dlq_sink=None,
        parallel_sinks: bool = False,
        sink_cb_keys: list[str] | None = None,
        stats: PipelineStats | None = None,
    ) -> bool:
        """Process one (raw, meta) chunk. Returns True on success.

        Thread-safe: all ctx mutations go through locked helper methods.
        """
        from tram.metrics.registry import (
            DLQ_RECORDS,
            DURATION,
            ERRORS,
            RECORDS_IN,
            RECORDS_OUT,
            RECORDS_SKIP,
        )

        meta = dict(meta)
        meta["pipeline_name"] = ctx.pipeline_name
        meta["run_id"] = ctx.run_id
        # Human-readable timestamp (run start, consistent across all chunks)
        meta["run_timestamp"] = ctx.started_at.strftime("%Y%m%dT%H%M%S")
        t_start = time.monotonic()

        try:
            raw_size = _payload_size_bytes(raw)
            ctx.inc_bytes_in(raw_size)
            if stats is not None:
                stats.increment(bytes_in=raw_size)

            # ── Parse ───────────────────────────────────────────────────────────
            try:
                records = serializer_in.parse(raw)
            except Exception as exc:
                if dlq_sink is not None:
                    _write_dlq_envelope(
                        dlq_sink, ctx,
                        stage="parse", error=str(exc), record=None, raw=raw,
                    )
                    ctx.record_dlq()
                    DLQ_RECORDS.labels(pipeline=ctx.pipeline_name).inc()
                if stats is not None:
                    stats.increment(
                        dlq=1 if dlq_sink is not None else 0,
                        errors=[f"Parse error: {exc}"],
                    )
                raise TramError(f"Parse error: {exc}") from exc

            ctx.inc_records_in(len(records))
            RECORDS_IN.labels(pipeline=ctx.pipeline_name).inc(len(records))
            if stats is not None:
                stats.increment(records_in=len(records))

            # ── Per-record global transforms ─────────────────────────────────
            surviving_records = []
            for record in records:
                try:
                    processed = [record]
                    for t in transforms:
                        self._set_transform_runtime_meta(t, meta)
                        processed = t.apply(processed)
                    surviving_records.extend(processed)
                except Exception as exc:
                    if dlq_sink is not None:
                        _write_dlq_envelope(
                            dlq_sink, ctx,
                            stage="transform", error=str(exc), record=record,
                        )
                        ctx.record_dlq()
                        DLQ_RECORDS.labels(pipeline=ctx.pipeline_name).inc()
                    ctx.record_error(str(exc))
                    if stats is not None:
                        stats.increment(
                            skipped=1,
                            dlq=1 if dlq_sink is not None else 0,
                            errors=[str(exc)],
                        )
            records = surviving_records

            # ── Multi-sink routing with per-sink transforms ───────────────────
            # Use threading.Event so parallel sink threads can set it atomically.
            wrote_any_event = threading.Event()

            def _write_one_sink(sink_tuple, records_in, sink_index):
                """Process one sink entry. Returns True if write succeeded."""
                # Accept 3-tuple (legacy/test), 4-tuple, or 5-tuple (current with per-sink ser)
                if len(sink_tuple) == 5:
                    sink_instance, condition, sink_transforms, sink_cfg, per_sink_ser = sink_tuple
                elif len(sink_tuple) == 4:
                    sink_instance, condition, sink_transforms, sink_cfg = sink_tuple
                    per_sink_ser = None
                else:
                    sink_instance, condition, sink_transforms = sink_tuple
                    sink_cfg = None
                    per_sink_ser = None

                if condition:
                    filtered = _filter_by_condition(records_in, condition)
                else:
                    filtered = list(records_in)

                if not filtered:
                    return False

                # Apply per-sink transforms
                sink_records = filtered
                sink_transform_failed = False
                for t in sink_transforms:
                    pre_transform = sink_records
                    try:
                        self._set_transform_runtime_meta(t, meta)
                        sink_records = t.apply(sink_records)
                    except Exception as exc:
                        if dlq_sink is not None:
                            _write_dlq_envelope(
                                dlq_sink, ctx,
                                stage="transform", error=str(exc), record=pre_transform,
                            )
                            ctx.record_dlq()
                            DLQ_RECORDS.labels(pipeline=ctx.pipeline_name).inc()
                        ctx.record_error(str(exc))
                        if stats is not None:
                            stats.increment(
                                skipped=1,
                                dlq=1 if dlq_sink is not None else 0,
                                errors=[str(exc)],
                            )
                        sink_transform_failed = True
                        break

                if sink_transform_failed or not sink_records:
                    return False

                active_ser = per_sink_ser if per_sink_ser is not None else serializer_out

                if rate_limit_rps is not None:
                    self._rate_limit(rate_limit_rps)

                # Circuit breaker check — use stable string key, not id()
                cb_threshold = getattr(sink_cfg, "circuit_breaker_threshold", 0)
                sink_key = (
                    sink_cb_keys[sink_index]
                    if sink_cb_keys and sink_index < len(sink_cb_keys)
                    else f"__dynamic:{sink_index}"
                )
                if cb_threshold > 0:
                    with self._cb_lock:
                        failures, open_until = self._cb_state.get(sink_key, (0, 0.0))
                    if open_until > time.monotonic():
                        logger.warning(
                            "Circuit breaker open — skipping sink",
                            extra={"pipeline": ctx.pipeline_name},
                        )
                        ctx.record_error("Circuit breaker open")
                        if stats is not None:
                            stats.increment(skipped=1, errors=["Circuit breaker open"])
                        return False

                # Per-sink retry loop
                retry_count = getattr(sink_cfg, "retry_count", 0)
                retry_delay = getattr(sink_cfg, "retry_delay_seconds", 1.0)
                wrote_partition = False
                partitions = _partition_records_for_template(
                    sink_records,
                    _sink_filename_template(sink_cfg, sink_instance),
                )
                for field_values, partition_records in partitions:
                    serialized = active_ser.serialize(partition_records)
                    sink_meta = dict(meta)
                    if field_values:
                        sink_meta["field_values"] = dict(field_values)
                    sink_meta["serializer_type"] = str(active_ser.config.get("type", "json"))
                    sink_meta["serializer_config"] = dict(active_ser.config)
                    sink_meta["output_record_count"] = len(partition_records)

                    last_exc = None
                    partition_succeeded = False
                    for attempt in range(retry_count + 1):
                        try:
                            sink_instance.write(serialized, sink_meta)
                            # Count total sink egress, not logical record size. If the same
                            # batch fans out to multiple sinks, bytes_out includes each
                            # successful sink write because load scoring cares about total I/O.
                            serialized_size = _payload_size_bytes(serialized)
                            ctx.inc_bytes_out(serialized_size)
                            if stats is not None:
                                stats.increment(bytes_out=serialized_size)
                            # Reset circuit breaker on success
                            if cb_threshold > 0:
                                with self._cb_lock:
                                    self._cb_state[sink_key] = (0, 0.0)
                            wrote_any_event.set()
                            wrote_partition = True
                            partition_succeeded = True
                            break
                        except Exception as exc:
                            last_exc = exc
                            if attempt < retry_count:
                                delay = retry_delay * (2 ** attempt) + random.uniform(0, 0.5)
                                logger.warning(
                                    "Sink write failed, retrying",
                                    extra={
                                        "pipeline": ctx.pipeline_name,
                                        "attempt": attempt + 1,
                                        "retry_count": retry_count,
                                        "delay": delay,
                                    },
                                )
                                time.sleep(delay)

                    if partition_succeeded:
                        continue

                    # All retries exhausted for this partition
                    if cb_threshold > 0:
                        with self._cb_lock:
                            failures, _ = self._cb_state.get(sink_key, (0, 0.0))
                            failures += 1
                            if failures >= cb_threshold:
                                open_until = time.monotonic() + 60.0
                                logger.warning(
                                    "Circuit breaker tripped — disabling sink for 60s",
                                    extra={"pipeline": ctx.pipeline_name, "failures": failures},
                                )
                            else:
                                open_until = 0.0
                            self._cb_state[sink_key] = (failures, open_until)

                    if dlq_sink is not None:
                        _write_dlq_envelope(
                            dlq_sink, ctx,
                            stage="sink", error=str(last_exc), record=partition_records,
                        )
                        ctx.record_dlq()
                        DLQ_RECORDS.labels(pipeline=ctx.pipeline_name).inc()
                    if on_error == "abort":
                        raise TramError(f"Sink write error: {last_exc}") from last_exc
                    ctx.record_error(str(last_exc))
                    if stats is not None:
                        stats.increment(
                            skipped=1,
                            dlq=1 if dlq_sink is not None else 0,
                            errors=[str(last_exc)],
                        )
                    return False

                return wrote_partition

            if parallel_sinks and len(sinks) > 1:
                with ThreadPoolExecutor(max_workers=len(sinks)) as pool:
                    futures = [
                        pool.submit(_write_one_sink, s, records, i)
                        for i, s in enumerate(sinks)
                    ]
                    for f in futures:
                        try:
                            f.result()
                        except TramError:
                            raise
            else:
                for i, sink_tuple in enumerate(sinks):
                    _write_one_sink(sink_tuple, records, i)

            if wrote_any_event.is_set():
                ctx.inc_records_out(len(records))
                RECORDS_OUT.labels(pipeline=ctx.pipeline_name).inc(len(records))
                if stats is not None:
                    stats.increment(records_out=len(records))
            else:
                ctx.inc_records_skipped(len(records))
                RECORDS_SKIP.labels(pipeline=ctx.pipeline_name).inc(len(records))
                if stats is not None:
                    stats.increment(skipped=len(records))
                skip_msg = (
                    "Records skipped — no sink wrote successfully "
                    "(condition filtered all records or every sink failed/circuit-open)"
                )
                ctx.note_skip(skip_msg)
                logger.warning(
                    skip_msg,
                    extra={"pipeline": ctx.pipeline_name, "run_id": ctx.run_id,
                           "skipped": len(records)},
                )

            duration = time.monotonic() - t_start
            DURATION.labels(pipeline=ctx.pipeline_name).observe(duration)
            return True

        except TramError as exc:
            msg = f"Processing error: {exc}"
            logger.error(msg, extra={"pipeline": ctx.pipeline_name, "run_id": ctx.run_id})
            ERRORS.labels(pipeline=ctx.pipeline_name).inc()
            if on_error == "abort":
                raise
            ctx.record_error(msg)
            if stats is not None:
                stats.increment(skipped=1, errors=[msg])
            return False

    # ── Batch run ────────────────────────────────────────────────────────────

    def batch_run(
        self,
        config: PipelineConfig,
        run_id: str | None = None,
        stats: PipelineStats | None = None,
    ) -> RunResult:
        """Execute one discrete batch run."""
        import contextlib
        try:
            from tram.telemetry.tracing import get_tracer
            tracer = get_tracer()
            span_ctx = tracer.start_as_current_span("batch_run")
        except Exception:
            span_ctx = contextlib.nullcontext()

        with span_ctx:
            return self._batch_run_inner(config, run_id=run_id, stats=stats)

    def _batch_run_inner(
        self,
        config: PipelineConfig,
        run_id: str | None = None,
        stats: PipelineStats | None = None,
    ) -> RunResult:
        kw = {"run_id": run_id} if run_id else {}
        ctx = PipelineRunContext(pipeline_name=config.name, **kw)
        logger.info(
            "Batch run started",
            extra={"pipeline": config.name, "run_id": ctx.run_id},
        )

        source = self._build_source(config)
        sinks = self._build_sinks(config)
        serializer_in = self._build_serializer_in(config)
        serializer_out = self._build_serializer_out(config)
        transforms = self._build_transforms(config)
        dlq_sink = self._build_dlq_sink(config)
        # Pre-compute stable circuit-breaker keys for all sinks.
        sink_cb_keys = [self._make_sink_cb_key(config, i) for i in range(len(sinks))]

        retry_count = config.retry_count if config.on_error == "retry" else 0
        retry_delay = config.retry_delay_seconds

        for attempt in range(max(1, retry_count + 1)):
            try:
                self._run_batch_chunks(
                    config, source, sinks, serializer_in, serializer_out,
                    transforms, dlq_sink, ctx, sink_cb_keys=sink_cb_keys, stats=stats,
                )

                result = RunResult.from_context(ctx, RunStatus.SUCCESS)
                logger.info(
                    "Batch run completed",
                    extra={
                        "pipeline": config.name,
                        "run_id": ctx.run_id,
                        "records_in": ctx.records_in,
                        "records_out": ctx.records_out,
                        "records_skipped": ctx.records_skipped,
                    },
                )
                return result

            except TramError as exc:
                if config.on_error == "retry" and attempt < retry_count:
                    logger.warning(
                        "Run failed, retrying",
                        extra={
                            "pipeline": config.name,
                            "attempt": attempt + 1,
                            "retry_count": retry_count,
                            "error": str(exc),
                        },
                    )
                    time.sleep(retry_delay)
                    # Reset counters and rebuild ALL components for a clean retry.
                    # Rebuilding only the source on retry would reuse a potentially
                    # broken sink connection that caused the original failure.
                    ctx = PipelineRunContext(pipeline_name=config.name)
                    source = self._build_source(config)
                    sinks = self._build_sinks(config)
                    serializer_in = self._build_serializer_in(config)
                    serializer_out = self._build_serializer_out(config)
                    transforms = self._build_transforms(config)
                    dlq_sink = self._build_dlq_sink(config)
                    sink_cb_keys = [self._make_sink_cb_key(config, i) for i in range(len(sinks))]
                    continue
                else:
                    result = RunResult.from_context(ctx, RunStatus.FAILED, error=str(exc))
                    logger.error(
                        "Batch run failed",
                        extra={"pipeline": config.name, "run_id": ctx.run_id, "error": str(exc)},
                    )
                    return result

        return RunResult.from_context(ctx, RunStatus.FAILED, error="Max retries exceeded")

    def _run_batch_chunks(
        self,
        config: PipelineConfig,
        source,
        sinks,
        serializer_in,
        serializer_out,
        transforms,
        dlq_sink,
        ctx: PipelineRunContext,
        sink_cb_keys: list[str] | None = None,
        stats: PipelineStats | None = None,
    ) -> None:
        """Inner loop: read source chunks and process with optional thread pool."""
        batch_size = config.batch_size
        on_error = config.on_error
        rate_limit_rps = config.rate_limit_rps

        parallel_sinks = getattr(config, "parallel_sinks", False)

        if config.thread_workers > 1:
            # Multi-threaded: submit chunks to a thread pool
            with ThreadPoolExecutor(max_workers=config.thread_workers) as pool:
                futures = []
                for raw, meta in source.read():
                    fut = pool.submit(
                        self._process_chunk,
                        raw, meta, serializer_in, transforms,
                        serializer_out, sinks, ctx, on_error,
                        rate_limit_rps, dlq_sink, parallel_sinks, sink_cb_keys, stats,
                    )
                    futures.append(fut)
                    # batch_size is checked after ctx.records_in is updated by workers
                    # slight over-submission is acceptable
                    if batch_size and ctx.records_in >= batch_size:
                        logger.info(
                            "batch_size limit reached, stopping source read",
                            extra={"pipeline": config.name, "batch_size": batch_size},
                        )
                        break

                for f in as_completed(futures):
                    try:
                        f.result()
                    except TramError as exc:
                        if on_error == "abort":
                            # Cancel remaining futures (best-effort)
                            for remaining in futures:
                                remaining.cancel()
                            raise
                        ctx.record_error(str(exc))
                        if stats is not None:
                            stats.increment(skipped=1, errors=[str(exc)])
        else:
            # Single-threaded
            for raw, meta in source.read():
                self._process_chunk(
                    raw, meta, serializer_in, transforms,
                    serializer_out, sinks, ctx, on_error,
                    rate_limit_rps, dlq_sink, parallel_sinks, sink_cb_keys, stats,
                )
                if batch_size and ctx.records_in >= batch_size:
                    logger.info(
                        "batch_size limit reached, stopping source read",
                        extra={"pipeline": config.name, "batch_size": batch_size},
                    )
                    break

    # ── Stream run ────────────────────────────────────────────────────────────

    def stream_run(
        self,
        config: PipelineConfig,
        stop_event: threading.Event,
        stats: PipelineStats | None = None,
    ) -> None:
        """Run indefinitely until stop_event is set."""
        logger.info("Stream run started", extra={"pipeline": config.name})

        source = self._build_source(config)
        sinks = self._build_sinks(config)
        serializer_in = self._build_serializer_in(config)
        serializer_out = self._build_serializer_out(config)
        transforms = self._build_transforms(config)
        dlq_sink = self._build_dlq_sink(config)
        sink_cb_keys = [self._make_sink_cb_key(config, i) for i in range(len(sinks))]

        ctx = PipelineRunContext(pipeline_name=config.name)

        # Watcher: when the APScheduler stop_event fires, also call source.stop()
        # so that blocking sources (e.g. WebhookSource.read()) unblock immediately.
        def _stop_watcher() -> None:
            stop_event.wait()
            if hasattr(source, "stop"):
                try:
                    source.stop()
                except Exception:
                    pass

        watcher = threading.Thread(target=_stop_watcher, daemon=True, name="tram-stop-watcher")
        watcher.start()

        try:
            if config.thread_workers > 1:
                self._stream_run_threaded(
                    config, source, sinks, serializer_in, serializer_out,
                    transforms, dlq_sink, ctx, stop_event, stats,
                    sink_cb_keys=sink_cb_keys,
                )
            else:
                for raw, meta in source.read():
                    if stop_event.is_set():
                        logger.info("Stream stop requested", extra={"pipeline": config.name})
                        break
                    self._process_chunk(
                        raw, meta, serializer_in, transforms,
                        serializer_out, sinks, ctx, config.on_error,
                        config.rate_limit_rps, dlq_sink,
                        getattr(config, "parallel_sinks", False),
                        sink_cb_keys,
                        stats,
                    )
        except Exception as exc:
            logger.error(
                "Stream run error",
                extra={"pipeline": config.name, "error": str(exc)},
                exc_info=True,
            )
            raise
        finally:
            logger.info(
                "Stream run ended",
                extra={
                    "pipeline": config.name,
                    "records_in": ctx.records_in,
                    "records_out": ctx.records_out,
                    "records_skipped": ctx.records_skipped,
                },
            )

    def _stream_run_threaded(
        self,
        config: PipelineConfig,
        source,
        sinks,
        serializer_in,
        serializer_out,
        transforms,
        dlq_sink,
        ctx: PipelineRunContext,
        stop_event: threading.Event,
        stats: PipelineStats | None = None,
        sink_cb_keys: list[str] | None = None,
    ) -> None:
        """Stream mode with N worker threads. Producer reads; workers process."""
        # Bounded queue gives backpressure: producer blocks if workers are slow
        chunk_q: _queue.Queue = _queue.Queue(maxsize=config.thread_workers * 2)
        on_error = config.on_error

        def _worker() -> None:
            while True:
                item = chunk_q.get()
                if item is None:
                    return
                raw, meta = item
                try:
                    self._process_chunk(
                        raw, meta, serializer_in, transforms,
                        serializer_out, sinks, ctx, on_error,
                        config.rate_limit_rps, dlq_sink,
                        getattr(config, "parallel_sinks", False),
                        sink_cb_keys,
                        stats,
                    )
                except Exception as exc:
                    logger.error(
                        "Stream worker error",
                        extra={"pipeline": config.name, "error": str(exc)},
                    )
                finally:
                    chunk_q.task_done()

        threads = [
            threading.Thread(target=_worker, daemon=True, name=f"tram-stream-{i}")
            for i in range(config.thread_workers)
        ]
        for t in threads:
            t.start()

        try:
            from tram.metrics.registry import STREAM_QUEUE_DEPTH
        except Exception:
            STREAM_QUEUE_DEPTH = None

        try:
            for raw, meta in source.read():
                if stop_event.is_set():
                    logger.info("Stream stop requested", extra={"pipeline": config.name})
                    break
                chunk_q.put((raw, meta))  # blocks if queue full (backpressure)
                if STREAM_QUEUE_DEPTH is not None:
                    try:
                        STREAM_QUEUE_DEPTH.labels(pipeline=config.name).set(chunk_q.qsize())
                    except Exception:
                        pass
        finally:
            # Signal all workers to stop
            for _ in threads:
                chunk_q.put(None)
            for t in threads:
                t.join(timeout=30)
            if STREAM_QUEUE_DEPTH is not None:
                try:
                    STREAM_QUEUE_DEPTH.labels(pipeline=config.name).set(0)
                except Exception:
                    pass

    # ── Dry run ─────────────────────────────────────────────────────────────

    def dry_run(self, config: PipelineConfig) -> dict:
        """Validate pipeline wiring without performing any I/O."""
        issues = []

        try:
            self._build_source(config)
        except Exception as exc:
            issues.append(f"source: {exc}")

        try:
            self._build_sinks(config)
        except Exception as exc:
            issues.append(f"sinks: {exc}")

        try:
            self._build_serializer_in(config)
        except Exception as exc:
            issues.append(f"serializer_in: {exc}")

        try:
            self._build_serializer_out(config)
        except Exception as exc:
            issues.append(f"serializer_out: {exc}")

        try:
            self._build_transforms(config)
        except Exception as exc:
            issues.append(f"transforms: {exc}")

        if config.dlq is not None:
            try:
                self._build_dlq_sink(config)
            except Exception as exc:
                issues.append(f"dlq: {exc}")

        return {"valid": len(issues) == 0, "issues": issues}
