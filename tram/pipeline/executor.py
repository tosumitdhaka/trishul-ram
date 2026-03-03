"""PipelineExecutor — two execution modes: batch_run() and stream_run()."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from tram.core.context import PipelineRunContext, RunResult, RunStatus
from tram.core.exceptions import TramError
from tram.registry.registry import get_serializer, get_sink, get_source, get_transform

if TYPE_CHECKING:
    from tram.models.pipeline import PipelineConfig

logger = logging.getLogger(__name__)


def _make_evaluator():
    try:
        from simpleeval import EvalWithCompoundTypes, DEFAULT_FUNCTIONS
        funcs = dict(DEFAULT_FUNCTIONS)
        funcs.update({
            "round": round, "abs": abs, "int": int, "float": float,
            "str": str, "len": len, "min": min, "max": max,
        })
        return EvalWithCompoundTypes, funcs
    except ImportError:
        return None, None


_EvalCls, _EVAL_FUNCS = _make_evaluator()


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


class PipelineExecutor:
    """Executes pipeline configurations in batch or stream mode."""

    def __init__(self) -> None:
        self._last_refill: float = 0.0
        self._tokens: float = 0.0

    # ── Rate limiting ──────────────────────────────────────────────────────

    def _rate_limit(self, rps: float) -> None:
        """Token-bucket rate limiter. Blocks until a token is available."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(rps, self._tokens + elapsed * rps)
        self._last_refill = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
        else:
            sleep_time = (1.0 - self._tokens) / rps
            time.sleep(sleep_time)
            self._tokens = 0.0
            self._last_refill = time.monotonic()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _build_source(self, config: "PipelineConfig"):
        src_cls = get_source(config.source.type)
        return src_cls(config.source.model_dump())

    def _build_sinks(self, config: "PipelineConfig") -> list[tuple]:
        """Returns list of (sink_instance, condition_str | None)."""
        result = []
        for sink_cfg in config.sinks:
            sink_cls = get_sink(sink_cfg.type)
            condition = getattr(sink_cfg, "condition", None)
            result.append((sink_cls(sink_cfg.model_dump()), condition))
        return result

    def _build_serializer_in(self, config: "PipelineConfig"):
        ser_cls = get_serializer(config.serializer_in.type)
        return ser_cls(config.serializer_in.model_dump())

    def _build_serializer_out(self, config: "PipelineConfig"):
        ser_cls = get_serializer(config.serializer_out.type)
        return ser_cls(config.serializer_out.model_dump())

    def _build_transforms(self, config: "PipelineConfig") -> list:
        transforms = []
        for t_cfg in config.transforms:
            t_cls = get_transform(t_cfg.type)
            transforms.append(t_cls(t_cfg.model_dump()))
        return transforms

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
    ) -> bool:
        """Process one (raw, meta) chunk. Returns True on success."""
        from tram.metrics.registry import RECORDS_IN, RECORDS_OUT, RECORDS_SKIP, ERRORS, DURATION

        meta = dict(meta)
        meta["pipeline_name"] = ctx.pipeline_name
        t_start = time.monotonic()

        try:
            records = serializer_in.parse(raw)
            ctx.records_in += len(records)
            RECORDS_IN.labels(pipeline=ctx.pipeline_name).inc(len(records))

            for transform in transforms:
                records = transform.apply(records)

            # Multi-sink routing
            wrote_any = False
            for sink_instance, condition in sinks:
                if condition:
                    filtered = _filter_by_condition(records, condition)
                else:
                    filtered = records

                if not filtered:
                    continue

                serialized = serializer_out.serialize(filtered)

                if rate_limit_rps is not None:
                    self._rate_limit(rate_limit_rps)

                sink_instance.write(serialized, meta)
                wrote_any = True

            if wrote_any:
                ctx.records_out += len(records)
                RECORDS_OUT.labels(pipeline=ctx.pipeline_name).inc(len(records))
            else:
                ctx.records_skipped += len(records)
                RECORDS_SKIP.labels(pipeline=ctx.pipeline_name).inc(len(records))

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
            return False

    # ── Batch run ──────────────────────────────────────────────────────────

    def batch_run(self, config: "PipelineConfig") -> RunResult:
        """Execute one discrete batch run."""
        ctx = PipelineRunContext(pipeline_name=config.name)
        logger.info(
            "Batch run started",
            extra={"pipeline": config.name, "run_id": ctx.run_id},
        )

        source = self._build_source(config)
        sinks = self._build_sinks(config)
        serializer_in = self._build_serializer_in(config)
        serializer_out = self._build_serializer_out(config)
        transforms = self._build_transforms(config)

        retry_count = config.retry_count if config.on_error == "retry" else 0
        retry_delay = config.retry_delay_seconds

        for attempt in range(max(1, retry_count + 1)):
            try:
                for raw, meta in source.read():
                    self._process_chunk(
                        raw, meta, serializer_in, transforms,
                        serializer_out, sinks, ctx, config.on_error,
                        config.rate_limit_rps,
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
                    # Reset counters for retry
                    ctx = PipelineRunContext(pipeline_name=config.name)
                    source = self._build_source(config)
                    continue
                else:
                    result = RunResult.from_context(ctx, RunStatus.FAILED, error=str(exc))
                    logger.error(
                        "Batch run failed",
                        extra={"pipeline": config.name, "run_id": ctx.run_id, "error": str(exc)},
                    )
                    return result

        # Should not reach here, but safety net
        return RunResult.from_context(ctx, RunStatus.FAILED, error="Max retries exceeded")

    # ── Stream run ─────────────────────────────────────────────────────────

    def stream_run(self, config: "PipelineConfig", stop_event: threading.Event) -> None:
        """Run indefinitely until stop_event is set."""
        logger.info("Stream run started", extra={"pipeline": config.name})

        source = self._build_source(config)
        sinks = self._build_sinks(config)
        serializer_in = self._build_serializer_in(config)
        serializer_out = self._build_serializer_out(config)
        transforms = self._build_transforms(config)

        ctx = PipelineRunContext(pipeline_name=config.name)

        try:
            for raw, meta in source.read():
                if stop_event.is_set():
                    logger.info("Stream stop requested", extra={"pipeline": config.name})
                    break
                self._process_chunk(
                    raw, meta, serializer_in, transforms,
                    serializer_out, sinks, ctx, config.on_error,
                    config.rate_limit_rps,
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

    # ── Dry run ────────────────────────────────────────────────────────────

    def dry_run(self, config: "PipelineConfig") -> dict:
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

        return {"valid": len(issues) == 0, "issues": issues}
