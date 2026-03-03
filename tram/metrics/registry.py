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


try:
    from prometheus_client import Counter, Histogram

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
    DURATION = Histogram(
        "tram_chunk_duration_seconds",
        "Time spent processing one chunk",
        ["pipeline"],
        buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0],
    )
    _PROMETHEUS_AVAILABLE = True

except ImportError:
    RECORDS_IN = _NoOpCounter()  # type: ignore[assignment]
    RECORDS_OUT = _NoOpCounter()  # type: ignore[assignment]
    RECORDS_SKIP = _NoOpCounter()  # type: ignore[assignment]
    ERRORS = _NoOpCounter()  # type: ignore[assignment]
    DURATION = _NoOpHistogram()  # type: ignore[assignment]
    _PROMETHEUS_AVAILABLE = False
