"""PipelineExecutor — execution modes: batch_run(), stream_run(), dry_run()."""

from __future__ import annotations

import base64
import gc
import json
import logging
import os
import queue as _queue
import random
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from tram.connectors.file_sink_common import extract_field_paths, validate_template_tokens
from tram.core.config import (
    stream_flush_interval_seconds as _default_stream_flush_interval,
)
from tram.core.config import (
    stream_flush_records as _default_stream_flush_records,
)
from tram.core.context import PipelineRunContext, RunResult, RunStatus
from tram.core.exceptions import TramError
from tram.registry.registry import get_serializer, get_sink, get_source, get_transform
from tram.transforms.stateful import StatefulTransform

if TYPE_CHECKING:
    from tram.agent.metrics import PipelineStats
    from tram.models.pipeline import PipelineConfig
    from tram.persistence.file_tracker import ProcessedFileTracker
    from tram.pipeline.state_store import TransformStateStore

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


def _source_unit_key(meta: dict) -> tuple[str, str] | None:
    source_path = str(meta.get("source_path", "") or "").strip()
    source_filename = str(meta.get("source_filename", "") or "").strip()
    if not source_path and not source_filename:
        return None
    return source_path, source_filename


def _augment_chunk_meta(meta: dict, ctx: PipelineRunContext) -> dict:
    """Attach stable run-scoped metadata used by sinks and filename templates."""
    enriched = dict(meta)
    enriched["pipeline_name"] = ctx.pipeline_name
    enriched["run_id"] = ctx.run_id
    enriched["run_timestamp"] = ctx.started_at.strftime("%Y%m%dT%H%M%S")
    return enriched


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


def _dlq_spool_dir() -> str:
    """Directory for spooled DLQ envelopes (review D1 fallback).

    The primary DLQ sink usually fails for the same reason the primary sink
    did (a shared network partition), so a remote DLQ is not a safe last
    resort. When the DLQ write itself fails, the envelope is durably spooled
    to local disk instead of being dropped. Configurable via
    ``TRAM_DLQ_SPOOL_DIR``. In worker mode the default resolves under the
    ``TRAM_DATA_DIR`` root (``<data_dir>/dlq-spool``, following the
    ``TRAM_SCHEMA_DIR``/``TRAM_MIB_DIR`` pattern) so spooled envelopes land on
    the mounted data volume instead of the container overlay; standalone and
    manager mode default under ``~/.tram`` alongside the SQLite fallback DB.
    """
    configured = os.environ.get("TRAM_DLQ_SPOOL_DIR")
    if configured:
        return configured
    if os.environ.get("TRAM_MODE", "standalone").lower() == "worker":
        data_dir = os.environ.get("TRAM_DATA_DIR", "/data")
        return str(Path(data_dir).expanduser() / "dlq-spool")
    return "~/.tram/dlq-spool"


def _spool_dlq_envelope(
    envelope: dict,
    ctx: PipelineRunContext,
    error: str,
) -> str | None:
    """Write a failed DLQ envelope to the local disk spool (review D1).

    Returns the spool file path on success, or ``None`` when even the spool
    failed (then the record is genuinely lost and the failure is logged at
    ERROR with the ``DLQ_WRITE_FAILED`` counter incremented — the loud path,
    never a silent drop).
    """
    from tram.metrics.registry import DLQ_WRITE_FAILED

    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", ctx.pipeline_name)
    filename = (
        f"{safe_name}-{ctx.run_id}-"
        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}.json"
    )
    payload = dict(envelope)
    payload["_dlq_sink_error"] = str(error)
    try:
        spool_dir = Path(_dlq_spool_dir()).expanduser()
        spool_dir.mkdir(parents=True, exist_ok=True)
        path = spool_dir / filename
        path.write_text(json.dumps(payload))
        logger.error(
            "DLQ write failed — envelope spooled to disk",
            extra={
                "pipeline": ctx.pipeline_name,
                "spool_path": str(path),
                "error": str(error),
            },
        )
        return str(path)
    except Exception as spool_exc:
        DLQ_WRITE_FAILED.labels(pipeline=ctx.pipeline_name).inc()
        logger.error(
            "DLQ write failed and local spool failed — envelope lost",
            extra={
                "pipeline": ctx.pipeline_name,
                "error": str(error),
                "spool_error": str(spool_exc),
            },
        )
        return None


def _write_dlq_envelope(
    dlq_sink,
    ctx: PipelineRunContext,
    *,
    stage: str,
    error: str,
    record=None,
    raw: bytes | None = None,
) -> None:
    """Write a DLQ envelope to the DLQ sink.

    A DLQ sink write failure never silently discards the record (review D1):
    the envelope is spooled to local disk as a durable fallback (replayable
    JSON file), or — when even the spool fails — surfaced via ERROR logs and
    the ``DLQ_WRITE_FAILED`` counter.
    """
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
        _spool_dlq_envelope(envelope, ctx, error=str(dlq_exc))


def _try_trim_process_heap() -> bool:
    """Best-effort release of unused process heap pages after large batch runs."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
        malloc_trim = getattr(libc, "malloc_trim", None)
        if malloc_trim is None:
            return False
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        return bool(malloc_trim(0))
    except Exception:
        return False


def _batch_inflight_cap(thread_workers: int) -> int:
    """Bounded in-flight window for the threaded batch path (~2x workers).

    The producer never submits more than this many chunks ahead of completion.
    With ``thread_workers`` worker threads that keeps queued-but-unprocessed
    payloads bounded instead of buffering the entire source (RCA #16: an
    unbounded ``ThreadPoolExecutor`` queue doubles the peak heap at
    ``thread_workers=2`` and OOMKills the worker pod).
    """
    return max(1, thread_workers * 2)


def _effective_stream_flush_records(config: PipelineConfig) -> int:
    """Effective stream micro-batch record threshold (GH #78).

    Per-pipeline ``stream_flush_records`` overrides the
    ``TRAM_STREAM_FLUSH_RECORDS`` environment default (500, mirroring kafka
    ``max_poll_records``). ``1`` restores the pre-v1.6.0 per-message flush.
    """
    if config.stream_flush_records is not None:
        return config.stream_flush_records
    return _default_stream_flush_records()


def _effective_stream_flush_interval(config: PipelineConfig) -> float:
    """Effective stream micro-batch flush interval in seconds (GH #78).

    Per-pipeline ``stream_flush_interval_s`` overrides the
    ``TRAM_STREAM_FLUSH_INTERVAL_SECONDS`` environment default (1.0). This is
    the bounded end-to-end latency budget for buffered records: a flush fires
    when the oldest buffered record has waited this long, even if the record
    threshold has not been reached.
    """
    if config.stream_flush_interval_s is not None:
        return config.stream_flush_interval_s
    return _default_stream_flush_interval()


class _StreamFlushBuffer:
    """Thread-safe micro-batch buffer for the stream sink path (GH #78).

    Stream records are appended as they arrive (post-parse, post-global-
    transform); the buffer is drained and routed through the batch executor's
    sink path on three triggers:

      1. record threshold (``stream_flush_records``, default 500) — mirrors
         kafka ``max_poll_records``;
      2. flush interval (``stream_flush_interval_s``, default 1s) measured from
         the *oldest* buffered record — the bounded end-to-end latency budget;
      3. ``source_batch_end`` meta (set by the kafka source at each poll-batch
         boundary): replayable sources commit their offsets only once the
         executor resumes the generator past the batch's last message, so
         flushing at the boundary guarantees commit-after-flush (at-least-once,
         GH #78 design care 1).

    ``append`` returns True when a flush is due; the caller (a stream worker or
    the loop thread) then flushes. ``drain`` is atomic, so concurrent flushers
    (the interval timer thread and whichever worker crossed a trigger) are safe
    — the loser of a drain race finds the buffer empty.
    """

    def __init__(self, record_threshold: int, interval_s: float) -> None:
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._meta: dict | None = None
        self._oldest_at: float | None = None
        self.record_threshold = record_threshold
        self.interval_s = interval_s

    def append(
        self, records: list[dict], meta: dict, *, batch_end: bool = False
    ) -> bool:
        """Append post-transform records; True when the buffer should be flushed.

        The first chunk's (augmented) meta is kept as the flush write's meta —
        the batch sink path writes one batch per meta, so a flush is a
        super-chunk carrying the meta of its first contributing chunk.
        """
        if not records and not batch_end:
            return False
        with self._lock:
            if records:
                if not self._records:
                    self._meta = dict(meta)
                    self._oldest_at = time.monotonic()
                self._records.extend(records)
            if not self._records:
                return False
            return (
                batch_end
                or len(self._records) >= self.record_threshold
                or time.monotonic() - (self._oldest_at or 0.0) >= self.interval_s
            )

    def due(self) -> bool:
        """True when the interval since the oldest buffered record has elapsed."""
        with self._lock:
            return bool(self._records) and (
                time.monotonic() - (self._oldest_at or 0.0) >= self.interval_s
            )

    def drain(self) -> tuple[list[dict], dict] | None:
        """Atomically take the buffered records + their representative meta."""
        with self._lock:
            if not self._records:
                return None
            records, meta = self._records, self._meta
            self._records = []
            self._meta = None
            self._oldest_at = None
            return records, meta or {}


class PipelineExecutor:
    """Executes pipeline configurations in batch or stream mode."""

    def __init__(
        self,
        file_tracker: ProcessedFileTracker | None = None,
        state_store: TransformStateStore | None = None,
    ) -> None:
        self._last_refill: float = 0.0
        self._tokens: float = 0.0
        self._rate_lock = threading.Lock()  # guards _tokens and _last_refill
        self._file_tracker = file_tracker
        # Durable transform-state store (design F.1 §3.2b) — None disables
        # persistence entirely (stateful transforms then stay in-memory only,
        # which is correct for single-run manual execution).
        self._state_store = state_store
        # Circuit breaker state: {sink_key: (failure_count, open_until_monotonic)}
        self._cb_state: dict[str, tuple[int, float]] = {}
        self._cb_lock = threading.Lock()

    # ── Rate limiting ────────────────────────────────────────────────────────────

    def _rate_limit(self, rps: float) -> None:
        """Token-bucket rate limiter. Blocks until a token is available.

        Thread-safe: _tokens and _last_refill are protected by _rate_lock.
        Note: rate_limit_rps is approximate when thread_workers > 1 because
        the sleep happens outside the lock to avoid holding it during sleep.

        A non-positive *rps* is a configuration error (rejected by the model's
        ``gt=0`` constraint) — fail loudly as such rather than with a raw
        ZeroDivisionError from the token math.
        """
        if rps is None or rps <= 0:
            raise ValueError(f"rate_limit_rps must be > 0, got {rps!r}")
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
        for idx, t_cfg in enumerate(config.transforms):
            t_cls = get_transform(t_cfg.type)
            d = t_cfg.model_dump()
            d["_pipeline"] = pipeline_ctx
            transform = t_cls(d)
            if isinstance(transform, StatefulTransform):
                # Stable state blob key: transform type + position in the
                # transforms list (design F.1 §3.2a).
                transform.state_key = f"{t_cfg.type}:{idx}"
            transforms.append(transform)
        return transforms

    # ── Stateful transform state (design F.1 §3.2c) ────────────────────────

    @staticmethod
    def _stateful_transforms(transforms: list) -> list:
        return [t for t in transforms if isinstance(t, StatefulTransform)]

    @staticmethod
    def _apply_state(transforms: list, blob: dict) -> None:
        """Hydrate stateful transforms from a blob (best-effort per transform)."""
        for transform in transforms:
            if not isinstance(transform, StatefulTransform):
                continue
            try:
                transform.set_state(blob.get(transform.state_key, {}))
            except Exception as exc:
                logger.warning(
                    "Transform state hydration failed",
                    extra={"state_key": transform.state_key, "error": str(exc)},
                )

    def _hydrate_state_from_store(
        self, config: PipelineConfig, transforms: list, config_sha256: str
    ) -> dict:
        """Load the pipeline's durable state and hydrate stateful transforms.

        Returns the *in-run snapshot* (the state as loaded) so a retry rebuild
        can re-hydrate from the same snapshot — a failed attempt's partial
        writes are discarded (design §3.2c). Discards the blob on config-sha
        mismatch (D.2 §6.1 pattern → one first-sight interval).
        """
        if self._state_store is None or not self._stateful_transforms(transforms):
            return {}
        try:
            loaded = self._state_store.get(config.name)
        except Exception as exc:
            logger.warning(
                "Transform state load failed — continuing unhydrated",
                extra={"pipeline": config.name, "error": str(exc)},
            )
            return {}
        if loaded is None:
            return {}
        if config_sha256 and loaded.config_sha256 != config_sha256:
            logger.info(
                "Transform state discarded — config_sha256 mismatch",
                extra={"pipeline": config.name},
            )
            return {}
        self._apply_state(transforms, loaded.state)
        return loaded.state

    def _save_state_to_store(
        self, config: PipelineConfig, transforms: list, config_sha256: str, run_id: str
    ) -> None:
        """Collect stateful transforms' blobs and persist them (best-effort)."""
        if self._state_store is None:
            return
        stateful = self._stateful_transforms(transforms)
        if not stateful:
            return
        blob: dict = {}
        for transform in stateful:
            try:
                blob[transform.state_key] = transform.get_state() or {}
            except Exception as exc:
                blob[transform.state_key] = {}
                logger.warning(
                    "Transform state collection failed",
                    extra={"state_key": transform.state_key, "error": str(exc)},
                )
        try:
            self._state_store.put(config.name, blob, config_sha256, run_id=run_id)
        except Exception as exc:
            logger.warning(
                "Transform state save failed — counters self-heal over the "
                "longer interval",
                extra={"pipeline": config.name, "error": str(exc)},
            )

    @staticmethod
    def _close_stateful_transforms(
        transforms: list,
        flush: bool = False,
        flush_resolver=None,
    ) -> list:
        """Best-effort ``close(flush)`` on stateful transforms at run end.

        Returns the partial-output records a transform emitted during close
        (``window_aggregate`` returns its flushed open windows on ``flush=True``;
        transforms whose ``close`` is a no-op return ``None``). Batch runs pass
        ``flush=False`` per tick — flushing per tick would emit a partial window
        record and then re-emit the same window after state rehydration (double
        counting); a manual flush run passes ``flush=True`` (authoritative over
        each transform's ``flush_on_close`` field). The stream ``finally``
        passes a ``flush_resolver`` — a callable(transform) → bool consulted
        per transform — so each stateful transform's ``flush_on_close`` field
        gates its graceful-stop flush, and a crash path resolves to ``False``
        (windows stay in state for a redispatch to continue).
        """
        emitted: list = []
        for transform in transforms:
            if not isinstance(transform, StatefulTransform):
                continue
            try:
                effective = flush if flush_resolver is None else flush_resolver(transform)
                out = transform.close(flush=effective)
            except Exception as exc:
                logger.warning(
                    "Stateful transform close failed",
                    extra={"state_key": transform.state_key, "error": str(exc)},
                )
                continue
            if isinstance(out, (list, tuple)):
                emitted.extend(out)
        return emitted

    def _route_stateful_flush_records(
        self,
        config: PipelineConfig,
        flush_records: list,
        serializer_out,
        sinks: list[tuple],
        ctx: PipelineRunContext,
        dlq_sink=None,
        sink_cb_keys: list[str] | None = None,
    ) -> None:
        """Write partial-window records from ``close(flush=True)`` to the sinks.

        Runs after the chunk loop (in the flush path / stream finally) so a
        stopped or flushed pipeline does not silently lose its open windows.
        Failures are logged, never raised (the run result is already decided).
        """
        if not flush_records:
            return
        try:
            self._process_records(
                flush_records,
                {},
                [],
                serializer_out,
                sinks,
                ctx,
                config.on_error,
                dlq_sink=dlq_sink,
                parallel_sinks=getattr(config, "parallel_sinks", False),
                sink_cb_keys=sink_cb_keys,
            )
        except Exception as exc:
            logger.warning(
                "Stateful flush records not delivered to sinks",
                extra={"pipeline": config.name, "error": str(exc)},
            )

    @staticmethod
    def _post_batch_cleanup(config: PipelineConfig) -> None:
        """Reclaim temporary batch-run heap without affecting stream workers."""
        gc.collect()
        trimmed = _try_trim_process_heap()
        logger.debug(
            "Post-batch cleanup completed",
            extra={"pipeline": config.name, "heap_trimmed": trimmed},
        )

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

    @staticmethod
    def _finalize_source_for_sinks(
        sinks: list[tuple],
        meta: dict,
        *,
        success: bool,
        ctx: PipelineRunContext | None = None,
    ) -> None:
        """Run each sink's ``finalize_source`` hook, degrading on failure.

        A sink finalize failure (e.g. a staged-file rename) after all chunks
        were drained must NOT flip the whole run to FAILED (review B11): the
        data is already written, so the error is logged loudly and recorded on
        the run context as a note — the run stays on its decided result with
        the error attached, consistent with the at-least-once story.
        """
        for sink_tuple in sinks:
            sink_instance = sink_tuple[0]
            finalize = getattr(sink_instance, "finalize_source", None)
            if not callable(finalize):
                continue
            try:
                finalize(meta, success)
            except Exception as exc:
                msg = f"Sink finalize failed: {exc}"
                logger.error(
                    msg,
                    extra={
                        "sink_type": type(sink_instance).__name__,
                        "success": success,
                        "source_path": meta.get("source_path"),
                        "source_filename": meta.get("source_filename"),
                    },
                )
                if ctx is not None:
                    ctx.note_skip(msg)

    @staticmethod
    def _close_sinks(sinks: list[tuple], dlq_sink=None) -> None:
        """Best-effort, idempotent close of sink instances after a batch run.

        Releases run-scoped resources (timers, buffers, connections) that sinks
        otherwise pin for the process lifetime (e.g. the ClickHouse flush
        timer/buffer). close() failures are logged but never mask the run result.
        """
        instances = [sink_tuple[0] for sink_tuple in sinks]
        if dlq_sink is not None:
            instances.append(dlq_sink)
        for sink_instance in instances:
            close = getattr(sink_instance, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:
                logger.warning(
                    "Sink close failed",
                    extra={"sink_type": type(sink_instance).__name__, "error": str(exc)},
                )

    @staticmethod
    def _close_source(source) -> None:
        """Best-effort close of a batch-run source instance.

        File sources keep their connection open across read()/finalize() and
        release it here. close() failures are logged but never mask the run
        result.
        """
        close = getattr(source, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception as exc:
            logger.warning(
                "Source close failed",
                extra={"source_type": type(source).__name__, "error": str(exc)},
            )

    def _apply_global_transforms(
        self,
        records: list[dict],
        transforms: list,
        meta: dict,
        ctx: PipelineRunContext,
        on_error: str,
        *,
        dlq_sink=None,
        stats: PipelineStats | None = None,
    ) -> list:
        """Run the top-level transform chain over the records, per record.

        Shared by the batch chunk path (``_process_records``) and the stream
        arrival path (``_process_chunk`` with a flush buffer): on the stream
        path the survivors are buffered for the next micro-batch flush instead
        of being written immediately, so transform semantics are identical to
        the per-chunk application. A failing record follows ``on_error``:
        abort raises; continue/dlq DLQs it (when configured) and counts it via
        ``record_error`` (one skip — true skipped = in − out, GH #84).
        """
        from tram.metrics.registry import DLQ_RECORDS

        surviving_records = []
        for record in records:
            try:
                processed = [record]
                for t in transforms:
                    self._set_transform_runtime_meta(t, meta)
                    processed = t.apply(processed)
                surviving_records.extend(processed)
            except Exception as exc:
                if on_error == "abort":
                    # GH #48 §2.9: abort must fail the run like the parse and
                    # sink-write abort paths, not silently DLQ+continue.
                    raise TramError(f"Transform error: {exc}") from exc
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
        return surviving_records

    def _process_records(
        self,
        records: list[dict],
        meta: dict,
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
        *,
        count_records_in: bool = True,
    ) -> bool:
        """Process one decoded record batch.

        *count_records_in* is False on the stream micro-batch flush path (GH
        #78): records_in is counted once at arrival (``_process_chunk`` buffer
        mode), so the flush must not bump it again — the GH #84 identity
        (skipped = in − out) holds across flush boundaries.
        """
        from tram.metrics.registry import (
            DLQ_RECORDS,
            DURATION,
            ERRORS,
            RECORDS_IN,
            RECORDS_OUT,
            RECORDS_SKIP,
        )

        t_start = time.monotonic()

        try:
            if count_records_in:
                ctx.inc_records_in(len(records))
                RECORDS_IN.labels(pipeline=ctx.pipeline_name).inc(len(records))
                if stats is not None:
                    stats.increment(records_in=len(records))

            # ── Per-record global transforms ─────────────────────────────────
            records = self._apply_global_transforms(
                records, transforms, meta, ctx, on_error,
                dlq_sink=dlq_sink, stats=stats,
            )

            # ── Multi-sink routing with per-sink transforms ───────────────────

            def _write_one_sink(sink_tuple, records_in, sink_index):
                """Process one sink entry. Returns the number of records the
                sink actually wrote (0 if nothing was written)."""
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
                    return 0

                # Apply per-sink transforms
                sink_records = filtered
                sink_transform_failed = False
                for t in sink_transforms:
                    pre_transform = sink_records
                    try:
                        self._set_transform_runtime_meta(t, meta)
                        sink_records = t.apply(sink_records)
                    except Exception as exc:
                        if on_error == "abort":
                            # GH #48 §2.9 parity: a failing sink-level transform
                            # under abort must fail the run like the global
                            # transform and sink-write abort paths — not
                            # silently DLQ and continue.
                            raise TramError(f"Transform error: {exc}") from exc
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
                    return 0

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
                        # Issue #84: the chunk-level skip accounting below counts
                        # the records when no sink wrote them — record_error
                        # would double-count. note_skip records the reason only.
                        ctx.note_skip("Circuit breaker open")
                        if stats is not None:
                            stats.increment(errors=["Circuit breaker open"])
                        return 0

                # Per-sink retry loop
                retry_count = getattr(sink_cfg, "retry_count", 0)
                retry_delay = getattr(sink_cfg, "retry_delay_seconds", 1.0)
                written = 0
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
                            written += len(partition_records)
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
                                cb_window = float(
                                    getattr(sink_cfg, "circuit_breaker_window_seconds", 60.0)
                                    or 60.0
                                )
                                open_until = time.monotonic() + cb_window
                                logger.warning(
                                    "Circuit breaker tripped — disabling sink "
                                    f"for {cb_window:g}s",
                                    extra={
                                        "pipeline": ctx.pipeline_name,
                                        "failures": failures,
                                        "window_seconds": cb_window,
                                    },
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
                    # Issue #84: the chunk-level skip accounting below
                    # (inc_records_skipped when records_written == 0) already
                    # counts these records — record_error would double-count.
                    # note_skip keeps the error message without bumping the
                    # counter, so true skipped = records_in - records_out.
                    ctx.note_skip(str(last_exc))
                    if stats is not None:
                        stats.increment(
                            dlq=1 if dlq_sink is not None else 0,
                            errors=[str(last_exc)],
                        )
                    # A partition failure stops this sink; earlier partitions
                    # that already wrote still count toward records_out.
                    return written

                return written

            if parallel_sinks and len(sinks) > 1:
                with ThreadPoolExecutor(max_workers=len(sinks)) as pool:
                    futures = [
                        pool.submit(_write_one_sink, s, records, i)
                        for i, s in enumerate(sinks)
                    ]
                    written_counts = []
                    for f in futures:
                        try:
                            written_counts.append(f.result())
                        except TramError:
                            raise
                        except Exception as exc:
                            # GH #48 §2.16: unexpected exceptions from
                            # _write_one_sink (e.g. a serializer bug outside
                            # the per-partition retry loop) escape the error
                            # taxonomy and would bypass on_error=retry. Fold
                            # them into TramError so the retry/abort paths in
                            # the run loop apply, preserving the cause.
                            raise TramError(f"Sink write error: {exc}") from exc
            else:
                written_counts = [
                    _write_one_sink(sink_tuple, records, i)
                    for i, sink_tuple in enumerate(sinks)
                ]

            # records_out counts records delivered to at least one sink (per
            # record, not per sink-fanout — multi-sink pipelines must not
            # inflate it). Each sink reports how many records it actually
            # wrote; the largest single-sink count is the delivered set when
            # sinks overlap (the common case) and a conservative lower bound
            # otherwise. Condition-filtered or failed sinks no longer bump the
            # count for records they never wrote (review D4).
            records_written = max(written_counts) if written_counts else 0
            if records_written > 0:
                ctx.inc_records_out(records_written)
                RECORDS_OUT.labels(pipeline=ctx.pipeline_name).inc(records_written)
                if stats is not None:
                    stats.increment(records_out=records_written)
            elif records:
                # Issue #84: only count a chunk as skipped when it actually
                # carried records that no sink wrote. Empty/no-op chunks
                # (records == []) must not emit the "no sink wrote" error or
                # bump the skip counter — true skipped = records_in - records_out.
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
        flush_buffer: _StreamFlushBuffer | None = None,
    ) -> bool:
        """Process one (raw, meta) chunk. Returns True on success.

        Thread-safe: all ctx mutations go through locked helper methods.

        When *flush_buffer* is given (stream micro-batching, GH #78) the sink
        write is deferred: the chunk is parsed, counted, transformed, and
        appended to the buffer, and the return value means "flush the buffer
        now" (record threshold / flush interval / ``source_batch_end``
        triggers). Without a buffer the chunk is written to the sinks
        immediately and True means success.
        """
        from tram.metrics.registry import DLQ_RECORDS, ERRORS, RECORDS_IN

        meta = _augment_chunk_meta(meta, ctx)
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

            if flush_buffer is not None:
                # Stream micro-batching (GH #78): count records_in and apply the
                # global transforms at arrival — exactly like the per-chunk
                # path — but defer the sink write to the micro-batch flush. The
                # return value is "flush the buffer now".
                ctx.inc_records_in(len(records))
                RECORDS_IN.labels(pipeline=ctx.pipeline_name).inc(len(records))
                if stats is not None:
                    stats.increment(records_in=len(records))
                survivors = self._apply_global_transforms(
                    records, transforms, meta, ctx, on_error,
                    dlq_sink=dlq_sink, stats=stats,
                )
                return flush_buffer.append(
                    survivors, meta, batch_end=bool(meta.get("source_batch_end"))
                )
            return self._process_records(
                records, meta, transforms, serializer_out, sinks, ctx, on_error,
                rate_limit_rps, dlq_sink, parallel_sinks, sink_cb_keys, stats,
            )

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

    def _process_chunk_incrementally(
        self,
        raw: bytes,
        meta: dict,
        serializer_in,
        transforms: list,
        serializer_out,
        sinks: list[tuple],
        ctx: PipelineRunContext,
        on_error: str,
        record_chunk_size: int,
        batch_size: int | None = None,
        rate_limit_rps: float | None = None,
        dlq_sink=None,
        parallel_sinks: bool = False,
        sink_cb_keys: list[str] | None = None,
        stats: PipelineStats | None = None,
    ) -> bool:
        """Process one raw chunk through serializer-provided record batches."""
        from tram.metrics.registry import DLQ_RECORDS, ERRORS

        meta = _augment_chunk_meta(meta, ctx)

        try:
            raw_size = _payload_size_bytes(raw)
            ctx.inc_bytes_in(raw_size)
            if stats is not None:
                stats.increment(bytes_in=raw_size)

            try:
                record_batches = serializer_in.parse_chunks(raw, record_chunk_size)
                for records in record_batches:
                    remaining = (batch_size - ctx.records_in) if batch_size is not None else None
                    if remaining is not None and remaining <= 0:
                        return True
                    if remaining is not None and len(records) > remaining:
                        records = records[:remaining]
                    if not records:
                        continue
                    self._process_records(
                        records, meta, transforms, serializer_out, sinks, ctx, on_error,
                        rate_limit_rps, dlq_sink, parallel_sinks, sink_cb_keys, stats,
                    )
                    if batch_size and ctx.records_in >= batch_size:
                        return True
                return True
            except TramError:
                raise
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

    # ── Stream micro-batch flush (GH #78) ─────────────────────────────────

    def _flush_stream_buffer(
        self,
        flush_buffer: _StreamFlushBuffer,
        serializer_out,
        sinks: list[tuple],
        ctx: PipelineRunContext,
        on_error: str,
        rate_limit_rps: float | None,
        dlq_sink,
        parallel_sinks: bool,
        sink_cb_keys: list[str] | None,
        stats: PipelineStats | None,
    ) -> bool:
        """Drain the stream micro-batch buffer through the shared sink path (GH #78).

        The batch executor's sink write path (``_process_records``) is reused,
        not duplicated: one serialization + one sink write per flush. Records
        are flushed with the buffer's representative meta (the first chunk's
        augmented meta) — a flush is a super-chunk.

        Counter semantics (GH #84) hold across flush boundaries: records_in was
        already counted at arrival, so the flush only counts records_out /
        records_skipped, and the "no sink wrote" error fires only for a flush
        that actually carried records. The local-sink part cap (GH #77) is
        consumed per flush: ``max_index`` advances one part per flush, and a
        past-cap flush raises exactly like a past-cap batch chunk (run never
        reports clean success).

        Per-sink error handling is batch-scoped: one flush is one sink-write
        unit, exactly like a batch chunk. Under ``on_error: continue`` a
        failing sink's flush records are counted once (via the chunk-level
        skip accounting when no sink delivered them), noted in the run errors,
        DLQ'd when a DLQ sink is configured, and dropped — the sink's own
        ``retry_count`` loop still applies per flush. ``on_error: abort``
        raises out of the flush.

        Returns True when a batch was flushed, False for an empty buffer.
        """
        entry = flush_buffer.drain()
        if entry is None:
            return False
        records, meta = entry
        self._process_records(
            records, meta, [], serializer_out, sinks, ctx, on_error,
            rate_limit_rps, dlq_sink, parallel_sinks, sink_cb_keys, stats,
            count_records_in=False,
        )
        return True

    # ── Batch run ────────────────────────────────────────────────────────────

    def batch_run(
        self,
        config: PipelineConfig,
        run_id: str | None = None,
        stats: PipelineStats | None = None,
        config_sha256: str = "",
        flush: bool = False,
    ) -> RunResult:
        """Execute one discrete batch run.

        *config_sha256* is the D.2 §6.1 YAML fingerprint used to discard stale
        transform state on config change (design F.1 §3.2d).

        *flush* is the manual flush-run flag (design §5): ``close(flush=True)``
        then emits any open windows as partials (``window_complete: false``)
        and clears them from state before the final PUT — the saved blob
        reflects the cleared windows, so the partials are never re-emitted.
        """
        import contextlib
        try:
            from tram.telemetry.tracing import get_tracer
            tracer = get_tracer()
            span_ctx = tracer.start_as_current_span("batch_run")
        except Exception:
            span_ctx = contextlib.nullcontext()

        with span_ctx:
            return self._batch_run_inner(
                config, run_id=run_id, stats=stats, config_sha256=config_sha256,
                flush=flush,
            )

    def _batch_run_inner(
        self,
        config: PipelineConfig,
        run_id: str | None = None,
        stats: PipelineStats | None = None,
        config_sha256: str = "",
        flush: bool = False,
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
        # Load the durable state once at run start and hydrate; retries re-hydrate
        # from this same in-run snapshot (failed attempts persist nothing).
        in_run_snapshot = self._hydrate_state_from_store(config, transforms, config_sha256)

        retry_count = config.retry_count if config.on_error == "retry" else 0
        retry_delay = config.retry_delay_seconds

        result = RunResult.from_context(ctx, RunStatus.FAILED, error="Max retries exceeded")
        try:
            for attempt in range(max(1, retry_count + 1)):
                try:
                    self._run_batch_chunks(
                        config, source, sinks, serializer_in, serializer_out,
                        transforms, dlq_sink, ctx, sink_cb_keys=sink_cb_keys, stats=stats,
                    )

                    if flush:
                        # Manual flush run (design §5): emit open windows as
                        # partials (window_complete: false) and clear them from
                        # state BEFORE the save, so the persisted blob reflects
                        # the cleared windows — a later run rehydrates empty
                        # windows and never re-emits the partials (no double
                        # counting).
                        flush_records = self._close_stateful_transforms(
                            transforms, flush=True
                        )
                        if flush_records:
                            self._route_stateful_flush_records(
                                config, flush_records, serializer_out, sinks,
                                ctx, dlq_sink=dlq_sink, sink_cb_keys=sink_cb_keys,
                            )

                    result = RunResult.from_context(ctx, RunStatus.SUCCESS)
                    # Persist transform state only on the success path: a failed
                    # run keeps the previous snapshot intact (counters are
                    # cumulative, so a lost update spans the gap correctly).
                    self._save_state_to_store(
                        config, transforms, config_sha256, ctx.run_id
                    )
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
                        # Close per-attempt resources from the failed attempt
                        # before rebuilding, so sinks from failed attempts
                        # (e.g. ClickHouse flush timers) and source connections
                        # do not leak across retries.
                        self._close_sinks(sinks, dlq_sink)
                        self._close_source(source)
                        # Reset counters and rebuild ALL components for a clean retry.
                        # Rebuilding only the source on retry would reuse a potentially
                        # broken sink connection that caused the original failure.
                        # Keep the ORIGINAL run_id across the rebuild so the final
                        # RunResult (and the worker run-complete callback) carry the
                        # run_id the trigger returned (E.2 §4.1 contract).
                        ctx = PipelineRunContext(pipeline_name=config.name, run_id=ctx.run_id)
                        if stats is not None:
                            # Retry parity: the context is rebuilt for the new
                            # attempt, so the stats accumulator must be reset
                            # alongside it — otherwise live totals accumulate
                            # across attempts and can exceed the final
                            # run-history numbers (which come from the last
                            # attempt's context).
                            stats.reset()
                        source = self._build_source(config)
                        sinks = self._build_sinks(config)
                        serializer_in = self._build_serializer_in(config)
                        serializer_out = self._build_serializer_out(config)
                        transforms = self._build_transforms(config)
                        dlq_sink = self._build_dlq_sink(config)
                        sink_cb_keys = [self._make_sink_cb_key(config, i) for i in range(len(sinks))]
                        # Re-hydrate the rebuilt transforms from the in-run
                        # snapshot — never from a fresh GET — so a failed
                        # attempt's partial writes are discarded.
                        self._apply_state(transforms, in_run_snapshot)
                        continue

                    result = RunResult.from_context(ctx, RunStatus.FAILED, error=str(exc))
                    logger.error(
                        "Batch run failed",
                        extra={"pipeline": config.name, "run_id": ctx.run_id, "error": str(exc)},
                    )
                    return result
            return result
        finally:
            self._close_stateful_transforms(transforms)
            self._close_sinks(sinks, dlq_sink)
            self._close_source(source)
            if getattr(config, "post_batch_cleanup", False):
                self._post_batch_cleanup(config)

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
        if config.thread_workers > 1:
            self._run_batch_chunks_threaded(
                config, source, sinks, serializer_in, serializer_out,
                transforms, dlq_sink, ctx, sink_cb_keys=sink_cb_keys, stats=stats,
            )
            return
        self._run_batch_chunks_sequential(
            config, source, sinks, serializer_in, serializer_out,
            transforms, dlq_sink, ctx, sink_cb_keys=sink_cb_keys, stats=stats,
        )

    def _run_batch_chunks_sequential(
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
        """Single-threaded batch loop. Each chunk is fully processed before the
        next one is pulled, so source finalize runs strictly after the chunk
        writes complete."""
        batch_size = config.batch_size
        record_chunk_size = getattr(config, "record_chunk_size", None)
        on_error = config.on_error
        rate_limit_rps = config.rate_limit_rps
        parallel_sinks = getattr(config, "parallel_sinks", False)

        current_source_key: tuple[str, str] | None = None
        current_source_meta: dict | None = None
        stopped_early = False
        try:
            for raw, meta in source.read():
                meta = _augment_chunk_meta(meta, ctx)
                source_key = _source_unit_key(meta)
                if source_key is not None:
                    meta["enable_safe_finalize"] = True
                    if current_source_key is not None and source_key != current_source_key:
                        self._finalize_source_for_sinks(
                            sinks, current_source_meta, success=True, ctx=ctx
                        )
                        source.finalize(current_source_meta, success=True)
                        current_source_key = None
                        current_source_meta = None
                    if current_source_key is None:
                        current_source_key = source_key
                    current_source_meta = dict(meta)

                if record_chunk_size:
                    self._process_chunk_incrementally(
                        raw, meta, serializer_in, transforms,
                        serializer_out, sinks, ctx, on_error, record_chunk_size,
                        batch_size, rate_limit_rps, dlq_sink,
                        parallel_sinks, sink_cb_keys, stats,
                    )
                else:
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
                    stopped_early = True
                    break
        except Exception:
            if current_source_meta is not None:
                self._finalize_source_for_sinks(
                    sinks, current_source_meta, success=False, ctx=ctx
                )
                source.finalize(current_source_meta, success=False)
            raise
        else:
            if current_source_meta is not None:
                self._finalize_source_for_sinks(
                    sinks, current_source_meta, success=True, ctx=ctx
                )
                if not stopped_early:
                    # On a batch_size stop the source generator was abandoned
                    # mid-file; the current file must stay unmarked so the next
                    # run reprocesses it (matches the pre-hook behavior).
                    source.finalize(current_source_meta, success=True)

    def _run_batch_chunks_threaded(
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
        """Multi-threaded batch loop: bounded in-flight chunks + deferred finalize.

        Chunks are submitted in read order, but once ``_batch_inflight_cap``
        futures are outstanding the oldest one is drained (blocking) before the
        source generator is advanced again. This bounds queued-but-unprocessed
        payloads (~2x thread_workers) instead of buffering the entire source
        (RCA #16).

        Source units (files) are finalized only after every chunk they yielded
        has been drained — never while writes are still pending — so
        move/delete/mark happen strictly after the chunks were actually written
        (code review A2). On abort the in-flight unit is left untouched so a
        retry can reprocess it.
        """
        batch_size = config.batch_size
        on_error = config.on_error
        cap = _batch_inflight_cap(config.thread_workers)
        parallel_sinks = getattr(config, "parallel_sinks", False)
        record_chunk_size = getattr(config, "record_chunk_size", None)

        in_flight: deque[tuple[Future, tuple | None, dict]] = deque()
        # Source units in submission order; each entry:
        # [source_key, first_chunk_meta, remaining_chunks, fully_submitted]
        units: deque[list] = deque()

        def _submit(raw: bytes, meta: dict) -> None:
            if record_chunk_size:
                # GH #48 §2.10: honor record_chunk_size on the threaded path
                # too — parse_chunks bounds the fan-out per chunk instead of
                # materializing the whole split (asn1) in one parse(). The
                # in-flight cap is unchanged (futures are what get bounded);
                # batch_size truncation stays approximate like the sequential
                # path's accepted over-submission.
                fut = pool.submit(
                    self._process_chunk_incrementally,
                    raw, meta, serializer_in, transforms,
                    serializer_out, sinks, ctx, on_error,
                    record_chunk_size, batch_size, config.rate_limit_rps,
                    dlq_sink, parallel_sinks, sink_cb_keys, stats,
                )
            else:
                fut = pool.submit(
                    self._process_chunk,
                    raw, meta, serializer_in, transforms,
                    serializer_out, sinks, ctx, on_error,
                    config.rate_limit_rps, dlq_sink, parallel_sinks, sink_cb_keys, stats,
                )
            key = _source_unit_key(meta)
            if key is not None:
                if units and units[-1][0] == key:
                    units[-1][2] += 1
                else:
                    if units:
                        units[-1][3] = True  # previous unit fully submitted
                    units.append([key, dict(meta), 1, False])
            in_flight.append((fut, key, meta))

        def _drain_one() -> None:
            fut, key, _meta = in_flight.popleft()
            try:
                fut.result()
            except TramError as exc:
                if on_error == "abort":
                    raise
                ctx.record_error(str(exc))
                if stats is not None:
                    stats.increment(skipped=1, errors=[str(exc)])
            if key is not None and units and units[0][0] == key:
                units[0][2] -= 1
                if units[0][2] == 0 and units[0][3]:
                    finished = units.popleft()
                    source.finalize(finished[1], success=True)

        with ThreadPoolExecutor(max_workers=config.thread_workers) as pool:
            try:
                stopped_early = False
                for raw, meta in source.read():
                    _submit(raw, meta)
                    while len(in_flight) >= cap:
                        _drain_one()
                    # batch_size is checked after ctx.records_in is updated by
                    # workers; slight over-submission is acceptable.
                    if batch_size and ctx.records_in >= batch_size:
                        logger.info(
                            "batch_size limit reached, stopping source read",
                            extra={"pipeline": config.name, "batch_size": batch_size},
                        )
                        stopped_early = True
                        break
                if units and not stopped_early:
                    # Natural end of the source: the last unit is fully
                    # submitted, so it may be finalized once its chunks drain.
                    # On a batch_size stop the generator was abandoned mid-file
                    # and the current unit must stay unmarked.
                    units[-1][3] = True
                while in_flight:
                    _drain_one()
            except Exception:
                # Abort or source error: never finalize units whose chunks were
                # not all drained — their files stay unmarked/unmoved so a retry
                # can reprocess them. Cancel what can be cancelled; the pool
                # shutdown in the with-block waits for any running futures.
                for fut, _key, _meta in in_flight:
                    fut.cancel()
                raise

    # ── Stream run ────────────────────────────────────────────────────────────

    def stream_run(
        self,
        config: PipelineConfig,
        stop_event: threading.Event,
        stats: PipelineStats | None = None,
        config_sha256: str = "",
    ) -> None:
        """Run indefinitely until stop_event is set.

        *config_sha256* is the D.2 §6.1 YAML fingerprint used to discard stale
        transform state on config change (design F.1 §3.2d). When the pipeline
        sets ``state_persist_interval_s``, the durable state blob is PUT
        periodically (timed with the chunk loop, not a new thread) so a D.2
        redispatch that hydrates recovers the state up to the last snapshot.
        """
        logger.info("Stream run started", extra={"pipeline": config.name})

        source = self._build_source(config)
        sinks = self._build_sinks(config)
        serializer_in = self._build_serializer_in(config)
        serializer_out = self._build_serializer_out(config)
        transforms = self._build_transforms(config)
        dlq_sink = self._build_dlq_sink(config)
        sink_cb_keys = [self._make_sink_cb_key(config, i) for i in range(len(sinks))]

        ctx = PipelineRunContext(pipeline_name=config.name)

        # Hydrate stateful transforms at run start; a D.2 redispatch then
        # recovers state up to the last persisted snapshot.
        self._hydrate_state_from_store(config, transforms, config_sha256)

        persist_interval = float(getattr(config, "state_persist_interval_s", 0) or 0)
        last_persist = time.monotonic()

        def _maybe_persist_state() -> None:
            nonlocal last_persist
            if persist_interval <= 0:
                return
            if time.monotonic() - last_persist >= persist_interval:
                self._save_state_to_store(config, transforms, config_sha256, ctx.run_id)
                last_persist = time.monotonic()

        # Stream micro-batching (GH #78): buffer records and flush to sinks per
        # batch (record threshold OR flush interval, mirroring kafka
        # max_poll_records) instead of one serialized sink write per message.
        # The interval timer thread bounds the end-to-end latency of buffered
        # records when the source is quieter than the record threshold; it is
        # joined in the finally before the sinks close.
        flush_buffer = _StreamFlushBuffer(
            record_threshold=_effective_stream_flush_records(config),
            interval_s=_effective_stream_flush_interval(config),
        )
        flusher_stop = threading.Event()

        def _flush_now() -> None:
            self._flush_stream_buffer(
                flush_buffer, serializer_out, sinks, ctx, config.on_error,
                config.rate_limit_rps, dlq_sink,
                getattr(config, "parallel_sinks", False), sink_cb_keys, stats,
            )

        def _interval_flusher() -> None:
            wake = max(flush_buffer.interval_s / 2, 0.05)
            while not flusher_stop.wait(wake):
                if not flush_buffer.due():
                    continue
                try:
                    _flush_now()
                except Exception as exc:
                    logger.error(
                        "Stream interval flush failed",
                        extra={"pipeline": config.name, "error": str(exc)},
                    )

        flusher_thread = threading.Thread(
            target=_interval_flusher, daemon=True, name="tram-stream-flusher"
        )
        flusher_thread.start()

        # Watcher: when the APScheduler stop_event fires, also call source.stop()
        # so that blocking sources (e.g. WebhookSource.read()) unblock immediately.
        # The local stream_exit event ends the watcher when the stream run exits
        # any other way (crash/exception path), so a crash-looping stream never
        # accumulates one leaked watcher thread per cycle.
        stream_exit = threading.Event()

        def _stop_watcher() -> None:
            while not stream_exit.is_set():
                if stop_event.wait(0.5):
                    break
            if stream_exit.is_set():
                return
            if hasattr(source, "stop"):
                try:
                    source.stop()
                except Exception:
                    pass

        watcher = threading.Thread(target=_stop_watcher, daemon=True, name="tram-stop-watcher")
        watcher.start()

        graceful_stop = False
        try:
            if config.thread_workers > 1:
                self._stream_run_threaded(
                    config, source, sinks, serializer_in, serializer_out,
                    transforms, dlq_sink, ctx, stop_event, stats,
                    sink_cb_keys=sink_cb_keys,
                    on_persist=_maybe_persist_state,
                    flush_buffer=flush_buffer,
                )
            else:
                current_source_key: tuple[str, str] | None = None
                current_source_meta: dict | None = None
                stopped = False
                for raw, meta in source.read():
                    if stop_event.is_set():
                        logger.info("Stream stop requested", extra={"pipeline": config.name})
                        stopped = True
                        break
                    source_key = _source_unit_key(meta)
                    if source_key is not None:
                        if current_source_key is not None and source_key != current_source_key:
                            # Drain the micro-batch buffer at source-unit
                            # boundaries so one file's records never ride a
                            # flush meta from (or into) another file
                            # (source_filename templates must not mix files).
                            _flush_now()
                            source.finalize(current_source_meta, success=True)
                            current_source_key = None
                            current_source_meta = None
                        if current_source_key is None:
                            current_source_key = source_key
                        current_source_meta = dict(meta)
                    if self._process_chunk(
                        raw, meta, serializer_in, transforms,
                        serializer_out, sinks, ctx, config.on_error,
                        config.rate_limit_rps, dlq_sink,
                        getattr(config, "parallel_sinks", False),
                        sink_cb_keys,
                        stats,
                        flush_buffer=flush_buffer,
                    ):
                        # Record threshold / flush interval / source-batch-end.
                        _flush_now()
                    _maybe_persist_state()
                # On a stop the generator was abandoned mid-file; the current
                # file stays unmarked (matches the pre-hook behavior). On a
                # natural end, drain the micro-batch buffer BEFORE finalizing
                # the current source unit so the file is never marked processed
                # ahead of its records' flush (GH #78).
                if current_source_meta is not None and not stopped:
                    _flush_now()
                    source.finalize(current_source_meta, success=True)
            # The chunk loop drained (or was stopped) without an exception:
            # drain the micro-batch buffer so records counted in at arrival are
            # never silently stranded at stop (GH #78 crash-window accounting).
            _flush_now()
            graceful_stop = True
        except Exception as exc:
            logger.error(
                "Stream run error",
                extra={"pipeline": config.name, "error": str(exc)},
                exc_info=True,
            )
            # Crash path: best-effort drain so buffered-but-unflushed records
            # are surfaced (flushed to the sinks, or counted as skipped with the
            # error recorded) instead of silently vanishing with the run.
            try:
                _flush_now()
            except Exception as flush_exc:
                logger.error(
                    "Stream crash-path buffer drain failed",
                    extra={"pipeline": config.name, "error": str(flush_exc)},
                )
            raise
        finally:
            # End the stop-watcher as early as possible: on the crash path the
            # stop_event never fires, so without this the watcher thread would
            # leak (one per crash cycle). On the graceful path the controller
            # already set stop_event and the source was unblocked, so the
            # watcher has nothing left to do.
            stream_exit.set()
            # Stop the interval flusher before the final drains / sink close so
            # no concurrent flush races the stateful-transform flush records or
            # the sink teardown.
            flusher_stop.set()
            flusher_thread.join(timeout=2)
            # Graceful stop: close hooks honoring each stateful transform's
            # flush_on_close field (window_aggregate emits its open windows as
            # partials, then the final state blob reflects the cleared windows
            # — no double emission after a redispatch). An exception/crash
            # path never flushes: partials are not emitted and the open
            # windows stay in the saved state so a redispatch continues them
            # (the D.2 snapshot-recovery story).
            if graceful_stop:
                flush_records = self._close_stateful_transforms(
                    transforms,
                    flush_resolver=lambda t: bool(
                        getattr(t, "flush_on_close", False)
                    ),
                )
            else:
                flush_records = self._close_stateful_transforms(
                    transforms, flush=False
                )
            if flush_records:
                self._route_stateful_flush_records(
                    config, flush_records, serializer_out, sinks, ctx,
                    dlq_sink=dlq_sink, sink_cb_keys=sink_cb_keys,
                )
            self._save_state_to_store(config, transforms, config_sha256, ctx.run_id)
            # Close sinks AFTER flush-record routing (the flush writes ride the
            # same sink instances) and BEFORE the source — mirrors the batch
            # finally. Releases run-scoped resources (ClickHouse flush
            # timer/buffer, SFTP/AMQP/NATS connections) that a stopped or
            # crashed stream would otherwise pin for the process lifetime.
            self._close_sinks(sinks, dlq_sink)
            self._close_source(source)
            watcher.join(timeout=1)
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
        on_persist=None,
        flush_buffer: _StreamFlushBuffer | None = None,
    ) -> None:
        """Stream mode with N worker threads. Producer reads; workers process.

        With a *flush_buffer* (stream micro-batching, GH #78) workers defer the
        sink write to the micro-batch flush: parse/transform at arrival, one
        serialized sink write per flush. The final drain of whatever is left in
        the buffer happens in ``stream_run`` after the workers have joined.
        Strict at-least-once for replayable sources (kafka poll-batch commits)
        holds on the single-threaded path; with ``thread_workers > 1`` the
        pre-existing poll-batch commit race applies (see the kafka source
        docstring).
        """
        # Bounded queue gives backpressure: producer blocks if workers are slow
        chunk_q: _queue.Queue = _queue.Queue(maxsize=config.thread_workers * 2)
        on_error = config.on_error

        def _flush_now() -> None:
            self._flush_stream_buffer(
                flush_buffer, serializer_out, sinks, ctx, on_error,
                config.rate_limit_rps, dlq_sink,
                getattr(config, "parallel_sinks", False), sink_cb_keys, stats,
            )

        def _worker() -> None:
            while True:
                item = chunk_q.get()
                if item is None:
                    return
                raw, meta = item
                try:
                    needs_flush = self._process_chunk(
                        raw, meta, serializer_in, transforms,
                        serializer_out, sinks, ctx, on_error,
                        config.rate_limit_rps, dlq_sink,
                        getattr(config, "parallel_sinks", False),
                        sink_cb_keys,
                        stats,
                        flush_buffer=flush_buffer,
                    )
                    if flush_buffer is not None and needs_flush:
                        _flush_now()
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
            current_source_key: tuple[str, str] | None = None
            current_source_meta: dict | None = None
            stopped = False
            for raw, meta in source.read():
                if stop_event.is_set():
                    logger.info("Stream stop requested", extra={"pipeline": config.name})
                    stopped = True
                    break
                source_key = _source_unit_key(meta)
                if source_key is not None:
                    if current_source_key is not None and source_key != current_source_key:
                        source.finalize(current_source_meta, success=True)
                        current_source_key = None
                        current_source_meta = None
                    if current_source_key is None:
                        current_source_key = source_key
                    current_source_meta = dict(meta)
                chunk_q.put((raw, meta))  # blocks if queue full (backpressure)
                if on_persist is not None:
                    on_persist()
                if STREAM_QUEUE_DEPTH is not None:
                    try:
                        STREAM_QUEUE_DEPTH.labels(pipeline=config.name).set(chunk_q.qsize())
                    except Exception:
                        pass
            if current_source_meta is not None and not stopped:
                source.finalize(current_source_meta, success=True)
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
        """Validate pipeline wiring without performing any I/O.

        Successfully built source/sink instances are closed best-effort before
        returning (in a finally), so a dry-run of e.g. a ClickHouse-sink
        pipeline does not leak its self-rescheduling flush timer — the API
        router builds a fresh PipelineExecutor per request.
        """
        issues = []
        built_source = None
        built_sinks: list[tuple] = []
        built_dlq = None

        try:
            try:
                built_source = self._build_source(config)
            except Exception as exc:
                issues.append(f"source: {exc}")

            try:
                built_sinks = self._build_sinks(config)
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
                    built_dlq = self._build_dlq_sink(config)
                except Exception as exc:
                    issues.append(f"dlq: {exc}")

            issues.extend(self._validate_sink_templates(config))
        finally:
            # Best-effort close of successfully built instances only — a
            # constructor failure leaves the variable None/[] and close errors
            # are swallowed, so cleanup never masks the validation result.
            self._close_sinks(built_sinks, built_dlq)
            if built_source is not None:
                self._close_source(built_source)

        return {"valid": len(issues) == 0, "issues": issues}

    def _validate_sink_templates(self, config: PipelineConfig) -> list[str]:
        issues: list[str] = []
        sink_entries = [(sink.type, sink) for sink in config.sinks]
        if config.dlq is not None:
            sink_entries.append(("dlq", config.dlq))
        for sink_label, sink_cfg in sink_entries:
            for attr in _FILE_TEMPLATE_ATTRS:
                template = getattr(sink_cfg, attr, None)
                if not isinstance(template, str):
                    continue
                for issue in validate_template_tokens(template):
                    issues.append(f"{sink_label}.{attr}: {issue}")
        return issues
