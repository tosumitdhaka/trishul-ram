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


class PipelineExecutor:
    """Executes pipeline configurations in batch or stream mode."""

    # ── Internal helpers ───────────────────────────────────────────────────

    def _build_source(self, config: "PipelineConfig"):
        src_cls = get_source(config.source.type)
        return src_cls(config.source.model_dump())

    def _build_sink(self, config: "PipelineConfig"):
        sink_cls = get_sink(config.sink.type)
        return sink_cls(config.sink.model_dump())

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
        sink,
        ctx: PipelineRunContext,
        on_error: str,
    ) -> bool:
        """Process one (raw, meta) chunk. Returns True on success."""
        meta = dict(meta)
        meta["pipeline_name"] = ctx.pipeline_name

        try:
            records = serializer_in.parse(raw)
            ctx.records_in += len(records)

            for transform in transforms:
                records = transform.apply(records)

            ctx.records_out += len(records)

            serialized = serializer_out.serialize(records)
            sink.write(serialized, meta)
            return True

        except TramError as exc:
            msg = f"Processing error: {exc}"
            logger.error(msg, extra={"pipeline": ctx.pipeline_name, "run_id": ctx.run_id})
            if on_error == "abort":
                raise
            ctx.record_error(msg)
            return False

    # ── Batch run ──────────────────────────────────────────────────────────

    def batch_run(self, config: "PipelineConfig") -> RunResult:
        """Execute one discrete batch run.

        Reads all matching source items, transforms, writes to sink, returns RunResult.
        """
        ctx = PipelineRunContext(pipeline_name=config.name)
        logger.info(
            "Batch run started",
            extra={"pipeline": config.name, "run_id": ctx.run_id},
        )

        source = self._build_source(config)
        sink = self._build_sink(config)
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
                        serializer_out, sink, ctx, config.on_error,
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
        """Run indefinitely until stop_event is set.

        Processes records one by one as they arrive from the source.
        Suitable for Kafka, NATS, SNMP, or any infinite generator source.
        """
        logger.info("Stream run started", extra={"pipeline": config.name})

        source = self._build_source(config)
        sink = self._build_sink(config)
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
                    serializer_out, sink, ctx, config.on_error,
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
            self._build_sink(config)
        except Exception as exc:
            issues.append(f"sink: {exc}")

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
