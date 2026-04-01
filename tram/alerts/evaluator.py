"""Alert rule evaluation and firing."""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import UTC, datetime
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tram.core.context import RunResult
    from tram.models.pipeline import AlertRuleConfig, PipelineConfig
    from tram.persistence.db import TramDB

logger = logging.getLogger(__name__)


class AlertEvaluator:
    """Evaluates alert rules after each pipeline run and fires configured actions."""

    def __init__(self, db: TramDB | None = None) -> None:
        self._db = db

    def check(self, result: RunResult, config: PipelineConfig) -> None:
        """Check all alert rules for a pipeline after a run."""
        if not config.alerts:
            return

        try:
            from simpleeval import EvalWithCompoundTypes
        except ImportError:
            logger.warning(
                "simpleeval not installed; alert rules will not be evaluated",
                extra={"pipeline": config.name},
            )
            return

        records_in = result.records_in
        records_skipped = result.records_skipped
        error_rate = records_skipped / records_in if records_in > 0 else 0.0
        duration_seconds = (result.finished_at - result.started_at).total_seconds()

        namespace = {
            "records_in": records_in,
            "records_out": result.records_out,
            "records_skipped": records_skipped,
            "error_rate": error_rate,
            "status": result.status.value,
            "failed": result.status.value == "failed",
            "duration_seconds": duration_seconds,
        }

        for rule in config.alerts:
            try:
                evaluator = EvalWithCompoundTypes(names=namespace)
                if not evaluator.eval(rule.condition):
                    continue

                if self._is_in_cooldown(config.name, rule):
                    logger.debug(
                        "Alert rule in cooldown, skipping",
                        extra={"pipeline": config.name, "rule": rule.name},
                    )
                    continue

                if rule.action == "webhook":
                    self._fire_webhook(rule, result, config)
                elif rule.action == "email":
                    self._fire_email(rule, result, config)

                self._set_cooldown(config.name, rule)

            except Exception as exc:
                logger.error(
                    "Alert rule evaluation error",
                    extra={"pipeline": config.name, "rule": rule.name, "error": str(exc)},
                )

    def _is_in_cooldown(self, pipeline_name: str, rule: AlertRuleConfig) -> bool:
        if self._db is None:
            return False
        last_alerted = self._db.get_alert_cooldown(pipeline_name, rule.name)
        if last_alerted is None:
            return False
        elapsed = (datetime.now(UTC) - last_alerted).total_seconds()
        return elapsed < rule.cooldown_seconds

    def _set_cooldown(self, pipeline_name: str, rule: AlertRuleConfig) -> None:
        if self._db is not None:
            self._db.set_alert_cooldown(
                pipeline_name, rule.name, datetime.now(UTC)
            )

    def _fire_webhook(
        self,
        rule: AlertRuleConfig,
        result: RunResult,
        config: PipelineConfig,
    ) -> None:
        try:
            import httpx

            payload = {
                "pipeline": config.name,
                "rule": rule.name,
                "status": result.status.value,
                "records_in": result.records_in,
                "records_out": result.records_out,
                "records_skipped": result.records_skipped,
                "error": result.error,
                "run_id": result.run_id,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            httpx.post(rule.webhook_url, json=payload, timeout=10)
            logger.info(
                "Alert webhook fired",
                extra={"pipeline": config.name, "rule": rule.name, "url": rule.webhook_url},
            )
        except Exception as exc:
            logger.error(
                "Alert webhook fire failed",
                extra={"pipeline": config.name, "rule": rule.name, "error": str(exc)},
            )

    def _fire_email(
        self,
        rule: AlertRuleConfig,
        result: RunResult,
        config: PipelineConfig,
    ) -> None:
        try:
            smtp_host = os.environ.get("TRAM_SMTP_HOST", "localhost")
            smtp_port = int(os.environ.get("TRAM_SMTP_PORT", "587"))
            smtp_user = os.environ.get("TRAM_SMTP_USER")
            smtp_pass = os.environ.get("TRAM_SMTP_PASS")
            smtp_tls = os.environ.get("TRAM_SMTP_TLS", "true").lower() != "false"
            smtp_from = os.environ.get("TRAM_SMTP_FROM", "tram@localhost")

            subject = rule.subject.format(pipeline=config.name)
            body = (
                f"TRAM Alert for pipeline: {config.name}\n"
                f"Rule: {rule.name}\n"
                f"Condition: {rule.condition}\n"
                f"Status: {result.status.value}\n"
                f"Records in: {result.records_in}\n"
                f"Records out: {result.records_out}\n"
                f"Records skipped: {result.records_skipped}\n"
                f"Run ID: {result.run_id}\n"
                f"Error: {result.error}\n"
            )

            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = smtp_from
            msg["To"] = rule.email_to

            with smtplib.SMTP(smtp_host, smtp_port) as smtp:
                if smtp_tls:
                    smtp.starttls()
                if smtp_user and smtp_pass:
                    smtp.login(smtp_user, smtp_pass)
                smtp.send_message(msg)

            logger.info(
                "Alert email fired",
                extra={"pipeline": config.name, "rule": rule.name, "to": rule.email_to},
            )
        except Exception as exc:
            logger.error(
                "Alert email fire failed",
                extra={"pipeline": config.name, "rule": rule.name, "error": str(exc)},
            )
