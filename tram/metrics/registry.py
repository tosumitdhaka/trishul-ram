"""Prometheus metrics for TRAM pipeline execution.

If prometheus_client is not installed, all metrics are no-ops.
"""

from __future__ import annotations


class _NoOpCounter:
    def labels(self, **_kwargs):
        return self

    def inc(self, amount: float = 1) -> None:
        pass


class _NoOpHistogram:
    def labels(self, **_kwargs):
        return self

    def observe(self, value: float) -> None:
        pass


class _NoOpGauge:
    def labels(self, **_kwargs):
        return self

    def set(self, value: float) -> None:
        pass

    def inc(self, amount: float = 1) -> None:
        pass

    def dec(self, amount: float = 1) -> None:
        pass


try:
    from prometheus_client import Counter, Gauge, Histogram

    # ── Manager operational metrics ────────────────────────────────────────
    # These series are process-local to the manager. Worker-side execution
    # metrics (tram_records_*, tram_chunk_duration_seconds, etc.) require
    # scraping each worker pod separately.

    MGR_DISPATCH_TOTAL = Counter(
        "tram_mgr_dispatch_total",
        "Pipeline dispatch attempts by manager",
        ["pipeline", "result"],
    )
    MGR_REDISPATCH_TOTAL = Counter(
        "tram_mgr_redispatch_total",
        "Reconciler-triggered re-dispatches",
        ["pipeline"],
    )
    MGR_RECONCILE_ACTION_TOTAL = Counter(
        "tram_mgr_reconcile_action_total",
        "Reconciler per-slot actions",
        ["pipeline", "action"],
    )
    MGR_PLACEMENT_STATUS = Gauge(
        "tram_mgr_placement_status",
        "1 when placement is in the given status, 0 otherwise",
        ["pipeline", "status"],
    )
    MGR_WORKER_HEALTHY = Gauge(
        "tram_mgr_worker_healthy",
        "Currently healthy worker count",
    )
    MGR_WORKER_TOTAL = Gauge(
        "tram_mgr_worker_total",
        "Total configured worker count",
    )
    MGR_RUN_COMPLETE_RECEIVED_TOTAL = Counter(
        "tram_mgr_run_complete_received_total",
        "Run-complete callbacks received at manager",
        ["pipeline", "status"],
    )
    MGR_PIPELINE_STATS_RECEIVED_TOTAL = Counter(
        "tram_mgr_pipeline_stats_received_total",
        "Pipeline-stats callbacks received at manager",
    )

    RECORDS_IN = Counter(
        "tram_records_in_total",
        "Total records read from source",
        ["pipeline"],
    )
    RECORDS_OUT = Counter(
        "tram_records_out_total",
        "Total records written to sink",
        ["pipeline"],
    )
    RECORDS_SKIP = Counter(
        "tram_records_skipped_total",
        "Total records skipped (filtered out or error)",
        ["pipeline"],
    )
    ERRORS = Counter(
        "tram_errors_total",
        "Total processing errors",
        ["pipeline"],
    )
    DLQ_RECORDS = Counter(
        "tram_dlq_total",
        "Total records routed to the dead-letter queue",
        ["pipeline"],
    )
    DURATION = Histogram(
        "tram_chunk_duration_seconds",
        "Time spent processing one chunk",
        ["pipeline"],
        buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0],
    )
    KAFKA_LAG = Gauge(
        "tram_kafka_consumer_lag",
        "Kafka consumer lag (messages behind high watermark)",
        ["pipeline", "topic", "partition"],
    )
    STREAM_QUEUE_DEPTH = Gauge(
        "tram_stream_queue_depth",
        "Number of messages buffered in the stream pipeline queue",
        ["pipeline"],
    )
    _PROMETHEUS_AVAILABLE = True

except ImportError:
    MGR_DISPATCH_TOTAL = _NoOpCounter()  # type: ignore[assignment]
    MGR_REDISPATCH_TOTAL = _NoOpCounter()  # type: ignore[assignment]
    MGR_RECONCILE_ACTION_TOTAL = _NoOpCounter()  # type: ignore[assignment]
    MGR_PLACEMENT_STATUS = _NoOpGauge()  # type: ignore[assignment]
    MGR_WORKER_HEALTHY = _NoOpGauge()  # type: ignore[assignment]
    MGR_WORKER_TOTAL = _NoOpGauge()  # type: ignore[assignment]
    MGR_RUN_COMPLETE_RECEIVED_TOTAL = _NoOpCounter()  # type: ignore[assignment]
    MGR_PIPELINE_STATS_RECEIVED_TOTAL = _NoOpCounter()  # type: ignore[assignment]
    RECORDS_IN = _NoOpCounter()  # type: ignore[assignment]
    RECORDS_OUT = _NoOpCounter()  # type: ignore[assignment]
    RECORDS_SKIP = _NoOpCounter()  # type: ignore[assignment]
    ERRORS = _NoOpCounter()  # type: ignore[assignment]
    DLQ_RECORDS = _NoOpCounter()  # type: ignore[assignment]
    DURATION = _NoOpHistogram()  # type: ignore[assignment]
    KAFKA_LAG = _NoOpGauge()  # type: ignore[assignment]
    STREAM_QUEUE_DEPTH = _NoOpGauge()  # type: ignore[assignment]
    _PROMETHEUS_AVAILABLE = False
