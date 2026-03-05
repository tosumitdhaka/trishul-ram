"""Tests for AlertRuleConfig and AlertEvaluator."""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from tram.alerts.evaluator import AlertEvaluator
from tram.core.context import RunStatus
from tram.core.context import RunResult
from tram.models.pipeline import AlertRuleConfig
from tram.pipeline.loader import load_pipeline_from_yaml


# ── AlertRuleConfig validation ─────────────────────────────────────────────


class TestAlertRuleConfigValidation:
    def test_webhook_rule_valid(self):
        rule = AlertRuleConfig(
            name="high-error",
            condition="error_rate > 0.1",
            action="webhook",
            webhook_url="http://example.com/hook",
        )
        assert rule.action == "webhook"
        assert rule.webhook_url == "http://example.com/hook"

    def test_webhook_rule_missing_url_raises(self):
        with pytest.raises(Exception, match="webhook_url"):
            AlertRuleConfig(
                condition="failed",
                action="webhook",
            )

    def test_email_rule_valid(self):
        rule = AlertRuleConfig(
            name="fail-alert",
            condition="failed",
            action="email",
            email_to="ops@example.com",
        )
        assert rule.email_to == "ops@example.com"

    def test_email_rule_missing_to_raises(self):
        with pytest.raises(Exception, match="email_to"):
            AlertRuleConfig(
                condition="failed",
                action="email",
            )

    def test_default_cooldown_is_300(self):
        rule = AlertRuleConfig(
            condition="failed",
            action="webhook",
            webhook_url="http://x.com",
        )
        assert rule.cooldown_seconds == 300

    def test_subject_default(self):
        rule = AlertRuleConfig(
            condition="failed",
            action="webhook",
            webhook_url="http://x.com",
        )
        assert rule.subject == "TRAM Alert: {pipeline}"


# ── AlertRuleConfig on PipelineConfig ─────────────────────────────────────


class TestPipelineConfigAlerts:
    def test_pipeline_with_alerts_loads(self):
        yaml_text = textwrap.dedent("""
            pipeline:
              name: alert-test
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
              alerts:
                - name: on-failure
                  condition: "failed"
                  action: webhook
                  webhook_url: "http://hooks.example.com/tram"
                  cooldown_seconds: 60
        """)
        config = load_pipeline_from_yaml(yaml_text)
        assert len(config.alerts) == 1
        assert config.alerts[0].name == "on-failure"

    def test_pipeline_with_no_alerts_defaults_to_empty(self):
        yaml_text = textwrap.dedent("""
            pipeline:
              name: no-alerts
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
        """)
        config = load_pipeline_from_yaml(yaml_text)
        assert config.alerts == []


# ── AlertEvaluator ─────────────────────────────────────────────────────────


def _make_result(status=RunStatus.SUCCESS, records_in=10, records_out=10,
                 records_skipped=0, error=None):
    now = datetime.now(timezone.utc)
    return RunResult(
        run_id="test-run",
        pipeline_name="test-pipe",
        status=status,
        started_at=now - timedelta(seconds=5),
        finished_at=now,
        records_in=records_in,
        records_out=records_out,
        records_skipped=records_skipped,
        error=error,
    )


def _make_config_with_rule(rule: AlertRuleConfig):
    yaml_text = textwrap.dedent("""
        pipeline:
          name: test-pipe
          source:
            type: local
            path: /tmp
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: local
            path: /tmp/out
    """)
    config = load_pipeline_from_yaml(yaml_text)
    config.alerts = [rule]
    return config


class TestAlertEvaluatorCheck:
    def test_no_alerts_is_noop(self):
        evaluator = AlertEvaluator()
        result = _make_result()

        yaml_text = textwrap.dedent("""
            pipeline:
              name: no-alerts
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
        """)
        config = load_pipeline_from_yaml(yaml_text)
        # Should complete without error or side effects
        evaluator.check(result, config)

    def test_condition_true_fires_webhook(self):
        rule = AlertRuleConfig(
            name="fail-hook",
            condition="failed",
            action="webhook",
            webhook_url="http://hooks.test/alert",
            cooldown_seconds=0,
        )
        config = _make_config_with_rule(rule)
        result = _make_result(status=RunStatus.FAILED, error="boom")

        evaluator = AlertEvaluator()

        with patch("httpx.post") as mock_post:
            evaluator.check(result, config)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[0][0] == "http://hooks.test/alert"
        payload = call_kwargs[1]["json"]
        assert payload["pipeline"] == "test-pipe"
        assert payload["status"] == "failed"

    def test_condition_false_does_not_fire(self):
        rule = AlertRuleConfig(
            name="fail-hook",
            condition="failed",
            action="webhook",
            webhook_url="http://hooks.test/alert",
        )
        config = _make_config_with_rule(rule)
        result = _make_result(status=RunStatus.SUCCESS)

        evaluator = AlertEvaluator()

        with patch("httpx.post") as mock_post:
            evaluator.check(result, config)

        mock_post.assert_not_called()

    def test_cooldown_prevents_double_fire(self):
        mock_db = MagicMock()
        # Simulate last alerted 10 seconds ago, cooldown is 300s
        mock_db.get_alert_cooldown.return_value = datetime.now(timezone.utc) - timedelta(seconds=10)

        rule = AlertRuleConfig(
            name="cooldown-rule",
            condition="failed",
            action="webhook",
            webhook_url="http://x.com",
            cooldown_seconds=300,
        )
        config = _make_config_with_rule(rule)
        result = _make_result(status=RunStatus.FAILED)

        evaluator = AlertEvaluator(db=mock_db)

        with patch("httpx.post") as mock_post:
            evaluator.check(result, config)

        mock_post.assert_not_called()

    def test_cooldown_expired_fires_again(self):
        mock_db = MagicMock()
        # Last alerted 400 seconds ago, cooldown is 300s → expired
        mock_db.get_alert_cooldown.return_value = datetime.now(timezone.utc) - timedelta(seconds=400)

        rule = AlertRuleConfig(
            name="cooldown-rule",
            condition="failed",
            action="webhook",
            webhook_url="http://x.com",
            cooldown_seconds=300,
        )
        config = _make_config_with_rule(rule)
        result = _make_result(status=RunStatus.FAILED)

        evaluator = AlertEvaluator(db=mock_db)

        with patch("httpx.post"):
            evaluator.check(result, config)

        mock_db.set_alert_cooldown.assert_called_once()

    def test_webhook_error_is_swallowed(self, caplog):
        import logging

        rule = AlertRuleConfig(
            name="fail-hook",
            condition="failed",
            action="webhook",
            webhook_url="http://bad.host/hook",
            cooldown_seconds=0,
        )
        config = _make_config_with_rule(rule)
        result = _make_result(status=RunStatus.FAILED)

        evaluator = AlertEvaluator()

        with patch("httpx.post", side_effect=ConnectionError("no route")):
            with caplog.at_level(logging.ERROR):
                evaluator.check(result, config)

        assert "Alert webhook fire failed" in caplog.text

    def test_email_action_calls_smtplib(self, monkeypatch):
        rule = AlertRuleConfig(
            name="email-rule",
            condition="error_rate > 0.5",
            action="email",
            email_to="ops@example.com",
            cooldown_seconds=0,
        )
        config = _make_config_with_rule(rule)
        result = _make_result(records_in=10, records_skipped=8)  # error_rate = 0.8

        evaluator = AlertEvaluator()

        mock_smtp_instance = MagicMock()
        mock_smtp_cls = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)

        with patch("smtplib.SMTP", mock_smtp_cls):
            evaluator.check(result, config)

        mock_smtp_cls.assert_called_once()
        mock_smtp_instance.send_message.assert_called_once()

    def test_email_error_is_swallowed(self, caplog):
        import logging

        rule = AlertRuleConfig(
            name="email-rule",
            condition="failed",
            action="email",
            email_to="ops@example.com",
            cooldown_seconds=0,
        )
        config = _make_config_with_rule(rule)
        result = _make_result(status=RunStatus.FAILED)

        evaluator = AlertEvaluator()

        with patch("smtplib.SMTP", side_effect=ConnectionError("no smtp")):
            with caplog.at_level(logging.ERROR):
                evaluator.check(result, config)

        assert "Alert email fire failed" in caplog.text

    def test_error_rate_condition(self):
        rule = AlertRuleConfig(
            name="high-err",
            condition="error_rate > 0.3",
            action="webhook",
            webhook_url="http://x.com",
            cooldown_seconds=0,
        )
        config = _make_config_with_rule(rule)
        # 4 out of 10 skipped → error_rate = 0.4 > 0.3
        result = _make_result(records_in=10, records_out=6, records_skipped=4)

        evaluator = AlertEvaluator()

        with patch("httpx.post") as mock_post:
            evaluator.check(result, config)

        mock_post.assert_called_once()
