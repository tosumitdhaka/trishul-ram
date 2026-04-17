"""Pipeline linter — static analysis of PipelineConfig.

Produces LintResult findings (warnings/errors) without executing any I/O.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tram.models.pipeline import PipelineConfig


@dataclass
class LintResult:
    rule_id: str
    severity: str   # "warning" | "error"
    message: str


HTTP_PUSH_SOURCES = {"webhook", "prometheus_rw"}
UDP_BLOCKED_SOURCES = {"syslog", "snmp_trap"}
QUEUE_SOURCES = {"kafka", "nats", "amqp"}
POLL_BATCH_SOURCES = {
    "sftp", "s3", "rest", "sql", "local", "ftp", "gcs", "azure_blob",
    "gnmi", "redis", "influxdb", "websocket", "corba", "elasticsearch", "clickhouse",
}


def lint(
    config: PipelineConfig,
    tram_mode: str | None = None,
    worker_pool_size: int | None = None,
) -> list[LintResult]:
    """Run all lint rules against *config* and return findings."""
    findings: list[LintResult] = []
    resolved_mode = (tram_mode or os.environ.get("TRAM_MODE", "standalone")).lower()
    resolved_pool_size = worker_pool_size if worker_pool_size is not None else _configured_pool_size()

    findings.extend(_l001_source_no_sink(config))
    findings.extend(_l002_skip_no_dlq(config))
    findings.extend(_l003_stream_multi_worker(config))
    findings.extend(_l004_batch_size_on_stream(config))
    findings.extend(_l005_email_no_smtp(config))
    findings.extend(_l006_http_push_requires_all(config, resolved_mode))
    findings.extend(_l007_poll_batch_multi_worker(config, resolved_mode))
    findings.extend(_l008_udp_blocked_in_manager(config, resolved_mode))
    findings.extend(_l009_queue_count_exceeds_pool(config, resolved_mode, resolved_pool_size))
    findings.extend(_l010_count_exceeds_pool(config, resolved_mode, resolved_pool_size))

    return findings


# ── Rules ───────────────────────────────────────────────────────────────────


def _l001_source_no_sink(config: PipelineConfig) -> list[LintResult]:
    """L001 — source configured but no transforms and no sinks have meaningful output."""
    if not config.transforms and not config.sinks:
        return [LintResult(
            rule_id="L001",
            severity="warning",
            message=(
                f"Pipeline '{config.name}': source reads data but no global transforms "
                "and no sinks are defined."
            ),
        )]
    return []


def _l002_skip_no_dlq(config: PipelineConfig) -> list[LintResult]:
    """L002 — on_error=skip (continue) with no DLQ means failed records are silently dropped."""
    if config.on_error == "continue" and config.dlq is None:
        return [LintResult(
            rule_id="L002",
            severity="warning",
            message=(
                f"Pipeline '{config.name}': on_error='continue' with no DLQ configured — "
                "failed records will be silently dropped."
            ),
        )]
    return []


def _l003_stream_multi_worker(config: PipelineConfig) -> list[LintResult]:
    """L003 — thread_workers > 1 on a stream pipeline uses a bounded queue for backpressure."""
    if config.schedule.type == "stream" and config.thread_workers > 1:
        return [LintResult(
            rule_id="L003",
            severity="warning",
            message=(
                f"Pipeline '{config.name}': thread_workers={config.thread_workers} on a "
                "stream pipeline uses a bounded queue for backpressure — ensure your "
                "source is thread-safe or use thread_workers=1."
            ),
        )]
    return []


def _l004_batch_size_on_stream(config: PipelineConfig) -> list[LintResult]:
    """L004 — batch_size is ignored for stream pipelines."""
    if config.schedule.type == "stream" and config.batch_size is not None:
        return [LintResult(
            rule_id="L004",
            severity="warning",
            message=(
                f"Pipeline '{config.name}': batch_size={config.batch_size} is set on a "
                "stream pipeline — this setting is only used for batch runs and will be ignored."
            ),
        )]
    return []


def _l005_email_no_smtp(config: PipelineConfig) -> list[LintResult]:
    """L005 — alert rule with action=email but no SMTP env vars configured."""
    findings = []
    smtp_host = os.environ.get("TRAM_SMTP_HOST", "")
    for rule in config.alerts:
        if rule.action == "email" and not smtp_host:
            findings.append(LintResult(
                rule_id="L005",
                severity="warning",
                message=(
                    f"Pipeline '{config.name}': alert rule '{rule.name or rule.condition}' "
                    "uses action='email' but TRAM_SMTP_HOST is not set — emails will not be sent."
                ),
            ))
    return findings


def _configured_pool_size() -> int | None:
    explicit = os.environ.get("TRAM_WORKER_URLS", "").strip()
    if explicit:
        return len([u for u in explicit.split(",") if u.strip()])
    replicas = os.environ.get("TRAM_WORKER_REPLICAS", "").strip()
    if replicas:
        try:
            return int(replicas)
        except ValueError:
            return None
    return None


def _is_multi_worker_spec(config: PipelineConfig) -> bool:
    workers = config.workers
    if workers is None:
        return False
    if workers.worker_ids:
        return True
    if workers.count == "all":
        return True
    return isinstance(workers.count, int) and workers.count > 1


def _l006_http_push_requires_all(config: PipelineConfig, tram_mode: str) -> list[LintResult]:
    if tram_mode != "manager" or config.source.type not in HTTP_PUSH_SOURCES:
        return []
    workers = config.workers
    if workers and workers.count == "all":
        return []
    return [LintResult(
        rule_id="L006",
        severity="error",
        message=(
            f"Pipeline '{config.name}': source '{config.source.type}' requires workers.count='all' "
            "in manager mode — any narrower placement loses HTTP push traffic."
        ),
    )]


def _l007_poll_batch_multi_worker(config: PipelineConfig, tram_mode: str) -> list[LintResult]:
    if tram_mode != "manager" or config.source.type not in POLL_BATCH_SOURCES:
        return []
    if not _is_multi_worker_spec(config):
        return []
    return [LintResult(
        rule_id="L007",
        severity="error",
        message=(
            f"Pipeline '{config.name}': poll/batch source '{config.source.type}' cannot use "
            "multi-worker placement in manager mode — reads would be duplicated."
        ),
    )]


def _l008_udp_blocked_in_manager(config: PipelineConfig, tram_mode: str) -> list[LintResult]:
    if tram_mode != "manager" or config.source.type not in UDP_BLOCKED_SOURCES:
        return []
    return [LintResult(
        rule_id="L008",
        severity="error",
        message=(
            f"Pipeline '{config.name}': source '{config.source.type}' is blocked in manager mode "
            "until the UDP push-source architecture lands."
        ),
    )]


def _l009_queue_count_exceeds_pool(
    config: PipelineConfig,
    tram_mode: str,
    worker_pool_size: int | None,
) -> list[LintResult]:
    if tram_mode != "manager" or worker_pool_size is None or config.source.type not in QUEUE_SOURCES:
        return []
    count = config.workers.count if config.workers else None
    if not isinstance(count, int) or count <= worker_pool_size:
        return []
    return [LintResult(
        rule_id="L009",
        severity="warning",
        message=(
            f"Pipeline '{config.name}': workers.count={count} exceeds configured worker pool size "
            f"{worker_pool_size} for queue source '{config.source.type}' — placement will degrade."
        ),
    )]


def _l010_count_exceeds_pool(
    config: PipelineConfig,
    tram_mode: str,
    worker_pool_size: int | None,
) -> list[LintResult]:
    if tram_mode != "manager" or worker_pool_size is None or config.source.type in QUEUE_SOURCES:
        return []
    count = config.workers.count if config.workers else None
    if not isinstance(count, int) or count <= worker_pool_size:
        return []
    return [LintResult(
        rule_id="L010",
        severity="warning",
        message=(
            f"Pipeline '{config.name}': workers.count={count} exceeds configured worker pool size "
            f"{worker_pool_size}."
        ),
    )]
