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


def lint(config: "PipelineConfig") -> list[LintResult]:
    """Run all lint rules against *config* and return findings."""
    findings: list[LintResult] = []

    findings.extend(_l001_source_no_sink(config))
    findings.extend(_l002_skip_no_dlq(config))
    findings.extend(_l003_stream_multi_worker(config))
    findings.extend(_l004_batch_size_on_stream(config))
    findings.extend(_l005_email_no_smtp(config))

    return findings


# ── Rules ───────────────────────────────────────────────────────────────────


def _l001_source_no_sink(config: "PipelineConfig") -> list[LintResult]:
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


def _l002_skip_no_dlq(config: "PipelineConfig") -> list[LintResult]:
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


def _l003_stream_multi_worker(config: "PipelineConfig") -> list[LintResult]:
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


def _l004_batch_size_on_stream(config: "PipelineConfig") -> list[LintResult]:
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


def _l005_email_no_smtp(config: "PipelineConfig") -> list[LintResult]:
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
