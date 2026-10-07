"""Validated same-schema Protobuf passthrough (v1.7.0 pilot B).

An opt-in execution capability for pipelines that only transport Protobuf
records: eligible pipelines ship the *validated* length-delimited frame stream
end-to-end without ever decoding records into dictionaries. Every frame is
parsed with the configured message class (enough to reject malformed messages
and obtain accurate counts), the original message bytes are preserved exactly,
and the re-framed payload is written through the sink directly.

Eligibility is enforced at registration time (``PipelineConfig`` model
validation, listing every unmet condition) and re-checked at run start; any
runtime re-check failure falls back to the existing dictionary path with one
WARNING. The flag is off by default, so existing pipelines are byte-identical.
"""

from __future__ import annotations

import logging
import os
import random
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from tram.connectors.file_sink_common import extract_field_paths
from tram.core.exceptions import SerializerError, TramError
from tram.serializers.protobuf_serializer import _proto_content_hash

if TYPE_CHECKING:
    from tram.models.pipeline import PipelineConfig

logger = logging.getLogger(__name__)

# Sink types that transport the payload without internally parsing records.
# Initially limited to the local file sink; Kafka chunking re-serializes
# records and is deliberately excluded (expand later if measured).
_ELIGIBLE_SINK_TYPES = frozenset({"local"})

# Filename-template attributes that may carry record-field tokens.
_FILE_TEMPLATE_ATTRS = ("filename_template", "key_template", "blob_template")


def _schema_content_hash(schema_file: str) -> str | None:
    """sha256 over the schema file plus sibling .proto files.

    Uses the same content hash the ProtobufSerializer keys its compile cache
    with (compilation processes every .proto in the schema directory), so two
    serializers hash equal exactly when they compile identical contracts.
    Returns None when the file is unreadable (reported as an unmet condition
    rather than raising at registration).
    """
    try:
        return _proto_content_hash(os.path.abspath(schema_file))
    except OSError:
        return None


def protobuf_passthrough_reasons(config: PipelineConfig) -> list[str]:
    """Return every unmet protobuf-passthrough eligibility condition.

    An empty list means the pipeline is eligible. Each reason is a standalone,
    user-actionable sentence; registration surfaces all of them at once so the
    user can fix the whole configuration in one pass.
    """
    reasons: list[str] = []
    ser_in = config.serializer_in
    ser_out = config.serializer_out

    # 1. Both serializer ends must be protobuf.
    if ser_in.type != "protobuf":
        reasons.append(f"serializer_in must be type=protobuf (got {ser_in.type!r})")
    if ser_out is None:
        reasons.append(
            "serializer_out must be type=protobuf (unset — the default is json)"
        )
    elif ser_out.type != "protobuf":
        reasons.append(f"serializer_out must be type=protobuf (got {ser_out.type!r})")

    if ser_in.type == "protobuf" and ser_out is not None and ser_out.type == "protobuf":
        # 2. Identical wire contracts — compared by CONTENT hash, not path/URL.
        in_hash = _schema_content_hash(ser_in.schema_file)
        out_hash = _schema_content_hash(ser_out.schema_file)
        if in_hash is None:
            reasons.append(f"serializer_in schema file not found or unreadable: {ser_in.schema_file}")
        if out_hash is None:
            reasons.append(f"serializer_out schema file not found or unreadable: {ser_out.schema_file}")
        if in_hash is not None and out_hash is not None and in_hash != out_hash:
            reasons.append(
                "serializer_in and serializer_out schema content differ "
                f"(sha256 {in_hash[:12]} != {out_hash[:12]}) — the compiled wire "
                "contracts are not identical"
            )
        if ser_in.message_class != ser_out.message_class:
            reasons.append(
                f"serializer_in message_class {ser_in.message_class!r} != "
                f"serializer_out message_class {ser_out.message_class!r}"
            )
        if ser_in.framing != ser_out.framing:
            reasons.append(
                f"serializer_in framing {ser_in.framing!r} != "
                f"serializer_out framing {ser_out.framing!r}"
            )
        if ser_in.framing != "length_delimited":
            reasons.append(
                f"serializer_in framing must be 'length_delimited' "
                f"(got {ser_in.framing!r})"
            )
        if ser_out.framing != "length_delimited":
            reasons.append(
                f"serializer_out framing must be 'length_delimited' "
                f"(got {ser_out.framing!r})"
            )
        # Registry/Confluent magic-byte framing wraps the payload before the
        # length-delimited frame stream; frame iteration would misparse it.
        # Mirror the serializer's resolution: the registry URL may come from
        # the pipeline config OR the deployment-level env default
        # (TRAM_SCHEMA_REGISTRY_URL — see protobuf_serializer's registry_url).
        if (
            ser_in.schema_registry_url
            or ser_out.schema_registry_url
            or os.environ.get("TRAM_SCHEMA_REGISTRY_URL")
        ):
            reasons.append(
                "schema_registry configuration is not eligible for passthrough "
                "(magic-byte framing wraps the frame stream)"
            )

    # 3. No global transforms — passthrough never decodes records.
    if config.transforms:
        reasons.append(
            "global transforms are configured "
            f"({', '.join(t.type for t in config.transforms)}) — passthrough "
            "never decodes records"
        )

    # 4-8. Per-sink conditions: local byte-transporting sink only, no
    # transforms, no conditions, no per-sink serializer override, no
    # record-field-dependent filename templates.
    for index, sink in enumerate(config.sinks):
        label = f"sinks[{index}] ({sink.type})"
        if sink.type not in _ELIGIBLE_SINK_TYPES:
            if sink.type == "kafka":
                reasons.append(
                    f"{label}: kafka sinks are not eligible — their bounded-batch "
                    "chunking re-serializes records"
                )
            else:
                reasons.append(
                    f"{label}: sink type {sink.type!r} is not eligible — only the "
                    "'local' file sink transports bytes without parsing records"
                )
        sink_transforms = getattr(sink, "transforms", [])
        if sink_transforms:
            reasons.append(
                f"{label}: per-sink transforms are configured "
                f"({', '.join(t.type for t in sink_transforms)})"
            )
        if getattr(sink, "condition", None):
            reasons.append(
                f"{label}: a sink condition is configured — conditions route on "
                "decoded records"
            )
        per_sink_ser = getattr(sink, "serializer_out", None)
        if per_sink_ser is not None:
            reasons.append(
                f"{label}: a per-sink serializer_out override is configured — "
                "passthrough writes the validated frames directly"
            )
        for attr in _FILE_TEMPLATE_ATTRS:
            template = getattr(sink, attr, None)
            if not isinstance(template, str):
                continue
            field_paths = extract_field_paths(template)
            if field_paths:
                reasons.append(
                    f"{label}: {attr} {template!r} uses record-field token(s) "
                    "field." + ", field.".join(field_paths) + " — the template "
                    "cannot be rendered without decoded records"
                )

    # 9. DLQ off — DLQ envelopes are record-dict-based.
    if config.dlq is not None:
        reasons.append(
            f"a DLQ sink ({config.dlq.type}) is configured — DLQ envelopes "
            "require record dictionaries, which passthrough never builds"
        )

    return reasons


def protobuf_passthrough_runtime_reasons(
    config: PipelineConfig, *, record_chunk_size: int | None = None
) -> list[str]:
    """Runtime re-check used at run start (batch and stream).

    Re-runs the registration predicate — belt and braces against a schema file
    changing on disk after registration — and adds the runtime-mode conditions
    that the config-only gate cannot see (incremental chunked parsing). Any
    reason means the run falls back to the dictionary path with one WARNING.
    """
    reasons = list(protobuf_passthrough_reasons(config))
    if record_chunk_size:
        reasons.append(
            "record_chunk_size is set — incremental chunked parsing is not "
            "supported in passthrough mode"
        )
    return reasons


def _iter_validated_frames(data: bytes, message_class) -> list[bytes]:
    """Split *data* into validated length-delimited frames.

    Mirrors the ProtobufSerializer batch decode's frame boundary checks (same
    error messages for parity) and additionally parses every frame with
    *message_class* — enough to reject malformed messages and obtain accurate
    counts. Returns the ORIGINAL message bytes of each frame, untouched.
    """
    frames: list[bytes] = []
    view = memoryview(data)
    offset = 0
    total = len(view)
    while offset < total:
        if offset + 4 > total:
            raise SerializerError("Truncated length prefix in protobuf stream")
        (length,) = struct.unpack_from(">I", view, offset)
        offset += 4
        end = offset + length
        if end > total:
            raise SerializerError("Truncated protobuf record")
        frame = bytes(view[offset:end])
        msg = message_class()
        try:
            msg.ParseFromString(frame)
        except Exception as exc:
            raise SerializerError(f"Protobuf parse error: {exc}") from exc
        frames.append(frame)
        offset = end
    return frames


def _reframe(frames: list[bytes]) -> bytes:
    """Re-emit the preserved message bytes with the configured framing.

    Each frame is written as [4-byte BE length][original message bytes], which
    is byte-identical to the validated input stream for length-delimited input
    while being explicit about the output contract.
    """
    return b"".join(struct.pack(">I", len(frame)) + frame for frame in frames)


def _unpack_sink(sink_tuple: tuple):
    """Accept the executor's 3/4/5-tuple sink entry shapes."""
    if len(sink_tuple) == 5:
        sink_instance, condition, sink_transforms, sink_cfg, per_sink_ser = sink_tuple
    elif len(sink_tuple) == 4:
        sink_instance, condition, sink_transforms, sink_cfg = sink_tuple
        per_sink_ser = None
    else:
        sink_instance, condition, sink_transforms = sink_tuple
        sink_cfg = None
        per_sink_ser = None
    return sink_instance, condition, sink_transforms, sink_cfg, per_sink_ser


def _write_sinks(
    executor,
    payload: bytes,
    frames: list[bytes],
    meta: dict,
    serializer_out,
    sinks: list[tuple],
    ctx,
    on_error: str,
    *,
    rate_limit_rps: float | None,
    parallel_sinks: bool,
    sink_cb_keys: list[str] | None,
    stats,
) -> list[int]:
    """Write the re-framed payload through every sink, mirroring the executor's
    per-sink retry / circuit-breaker / rate-limit / accounting semantics.

    Eligibility guarantees the simplified conditions: no sink condition, no
    per-sink transforms, no per-sink serializer override, no DLQ sink — so the
    general ``_write_one_sink`` machinery from ``_process_records`` collapses
    to the cases that can actually occur here.
    """

    def _write_one_sink(sink_tuple, sink_index) -> int:
        sink_instance, _condition, _sink_transforms, sink_cfg, _per_sink_ser = _unpack_sink(
            sink_tuple
        )

        if rate_limit_rps is not None:
            executor._rate_limit(rate_limit_rps)

        # Circuit breaker — same stable-key scheme as the normal path.
        cb_threshold = (
            getattr(sink_cfg, "circuit_breaker_threshold", 0)
            if sink_cfg is not None
            else 0
        )
        sink_key = (
            sink_cb_keys[sink_index]
            if sink_cb_keys and sink_index < len(sink_cb_keys)
            else f"__dynamic:{sink_index}"
        )
        if cb_threshold > 0:
            with executor._cb_lock:
                failures, open_until = executor._cb_state.get(sink_key, (0, 0.0))
            if open_until > time.monotonic():
                logger.warning(
                    "Circuit breaker open — skipping sink",
                    extra={"pipeline": ctx.pipeline_name},
                )
                ctx.note_skip("Circuit breaker open")
                if stats is not None:
                    stats.increment(errors=["Circuit breaker open"])
                return 0

        retry_count = (
            getattr(sink_cfg, "retry_count", 0) if sink_cfg is not None else 0
        )
        retry_delay = (
            getattr(sink_cfg, "retry_delay_seconds", 1.0)
            if sink_cfg is not None
            else 1.0
        )

        # The local file sink's append/rollover logic reads output_record_count
        # from the meta, exactly like the normal path.
        sink_meta = dict(meta)
        sink_meta["serializer_type"] = "protobuf"
        sink_meta["serializer_config"] = dict(getattr(serializer_out, "config", {}) or {})
        sink_meta["output_record_count"] = len(frames)

        last_exc = None
        for attempt in range(retry_count + 1):
            try:
                sink_instance.write(payload, sink_meta)
                # bytes_out counts total sink egress (per successful write),
                # matching the normal path's load-scoring semantics.
                serialized_size = len(payload)
                ctx.inc_bytes_out(serialized_size)
                if stats is not None:
                    stats.increment(bytes_out=serialized_size)
                if cb_threshold > 0:
                    with executor._cb_lock:
                        executor._cb_state[sink_key] = (0, 0.0)
                return len(frames)
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

        # All retries exhausted for this sink.
        if cb_threshold > 0:
            with executor._cb_lock:
                failures, _ = executor._cb_state.get(sink_key, (0, 0.0))
                failures += 1
                if failures >= cb_threshold:
                    cb_window = float(
                        getattr(sink_cfg, "circuit_breaker_window_seconds", 60.0)
                        or 60.0
                    )
                    open_until = time.monotonic() + cb_window
                    logger.warning(
                        "Circuit breaker tripped — disabling sink for %gs",
                        cb_window,
                        extra={
                            "pipeline": ctx.pipeline_name,
                            "failures": failures,
                            "window_seconds": cb_window,
                        },
                    )
                else:
                    open_until = 0.0
                executor._cb_state[sink_key] = (failures, open_until)

        if on_error == "abort":
            raise TramError(f"Sink write error: {last_exc}") from last_exc
        # Issue #84 parity: the chunk-level skip accounting below counts the
        # records; note_skip records the reason without double-counting.
        ctx.note_skip(str(last_exc))
        if stats is not None:
            stats.increment(errors=[str(last_exc)])
        return 0

    if parallel_sinks and len(sinks) > 1:
        with ThreadPoolExecutor(max_workers=len(sinks)) as pool:
            futures = [
                pool.submit(_write_one_sink, s, i) for i, s in enumerate(sinks)
            ]
            written_counts = []
            for future in futures:
                try:
                    written_counts.append(future.result())
                except TramError:
                    raise
                except Exception as exc:
                    raise TramError(f"Sink write error: {exc}") from exc
            return written_counts
    return [_write_one_sink(s, i) for i, s in enumerate(sinks)]


def process_chunk(
    executor,
    raw: bytes,
    meta: dict,
    serializer_in,
    serializer_out,
    sinks: list[tuple],
    ctx,
    on_error: str,
    *,
    rate_limit_rps: float | None = None,
    parallel_sinks: bool = False,
    sink_cb_keys: list[str] | None = None,
    stats=None,
) -> bool:
    """Process one (raw, meta) chunk through the passthrough path.

    The executor dispatches here (instead of parse→dicts→serialize) when the
    runtime eligibility re-check passed. Returns True on success; malformed
    frames and sink failures follow the same error semantics as the dictionary
    path (``TramError("Parse error: ...")`` / ``TramError("Sink write error:
    ...")``, with ``on_error`` applying identically).
    """
    from tram.metrics.registry import DURATION, ERRORS, RECORDS_IN, RECORDS_OUT, RECORDS_SKIP

    t_start = time.monotonic()
    raw_size = len(raw)
    ctx.inc_bytes_in(raw_size)
    if stats is not None:
        stats.increment(bytes_in=raw_size)

    try:
        try:
            message_class = serializer_in._get_message_class()
            frames = _iter_validated_frames(raw, message_class)
        except Exception as exc:
            # Parity with the dictionary path's parse-error handling (no DLQ
            # in eligibility, so no DLQ envelope write here).
            if stats is not None:
                stats.increment(errors=[f"Parse error: {exc}"])
            raise TramError(f"Parse error: {exc}") from exc

        if not frames:
            DURATION.labels(pipeline=ctx.pipeline_name).observe(
                time.monotonic() - t_start
            )
            return True

        payload = _reframe(frames)
        ctx.inc_records_in(len(frames))
        RECORDS_IN.labels(pipeline=ctx.pipeline_name).inc(len(frames))
        if stats is not None:
            stats.increment(records_in=len(frames))

        written_counts = _write_sinks(
            executor, payload, frames, meta, serializer_out, sinks, ctx, on_error,
            rate_limit_rps=rate_limit_rps, parallel_sinks=parallel_sinks,
            sink_cb_keys=sink_cb_keys, stats=stats,
        )

        # records_out counts records delivered to at least one sink (per
        # record, not per sink-fanout) — same max() rule as the normal path.
        records_written = max(written_counts) if written_counts else 0
        if records_written > 0:
            ctx.inc_records_out(records_written)
            RECORDS_OUT.labels(pipeline=ctx.pipeline_name).inc(records_written)
            if stats is not None:
                stats.increment(records_out=records_written)
        else:
            ctx.inc_records_skipped(len(frames))
            RECORDS_SKIP.labels(pipeline=ctx.pipeline_name).inc(len(frames))
            if stats is not None:
                stats.increment(skipped=len(frames))
            msg = (
                "Records skipped — no sink wrote successfully "
                "(every sink failed/circuit-open)"
            )
            ctx.note_skip(msg)
            logger.warning(
                msg,
                extra={
                    "pipeline": ctx.pipeline_name,
                    "run_id": ctx.run_id,
                    "skipped": len(frames),
                },
            )

        DURATION.labels(pipeline=ctx.pipeline_name).observe(
            time.monotonic() - t_start
        )
        return True

    except TramError as exc:
        # Same error taxonomy as the dictionary path: a parse failure surfaces
        # as TramError("Parse error: ...") (wrapped below), a sink write
        # failure as TramError("Sink write error: ..."). Abort re-raises so
        # the run fails; continue/retry records the error and returns False.
        msg = f"Processing error: {exc}"
        logger.error(
            msg, extra={"pipeline": ctx.pipeline_name, "run_id": ctx.run_id}
        )
        ERRORS.labels(pipeline=ctx.pipeline_name).inc()
        if on_error == "abort":
            raise
        ctx.record_error(msg)
        if stats is not None:
            stats.increment(skipped=1, errors=[msg])
        return False