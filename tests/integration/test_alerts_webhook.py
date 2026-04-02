"""Integration test: alert webhook fires for the right conditions.

Uses AlertEvaluator directly with a real PipelineConfig to verify that:
- The evaluator correctly detects a fired condition
- The webhook POST is issued with the right payload
- No webhook fires when condition is false
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

from tram.alerts.evaluator import AlertEvaluator
from tram.pipeline.executor import PipelineExecutor
from tram.pipeline.loader import load_pipeline_from_yaml


def _make_pipeline(src: Path, dst: Path, condition: str) -> object:
    return load_pipeline_from_yaml(textwrap.dedent(f"""
        pipeline:
          name: alert-test
          source:
            type: local
            path: {src}
          serializer_in:
            type: json
          serializer_out:
            type: json
          sink:
            type: local
            path: {dst}
          alerts:
            - condition: "{condition}"
              action: webhook
              webhook_url: http://alerts.example.com/notify
              cooldown_seconds: 0
    """))


class TestAlertWebhookIntegration:
    def test_alert_fires_when_records_out_zero(self, tmp_path):
        """Alert fires a webhook POST when records_out == 0 (empty source)."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir() 
        dst.mkdir()
        # No input files → records_out == 0

        config = _make_pipeline(src, dst, condition="records_out == 0")
        executor = PipelineExecutor()
        result = executor.batch_run(config)

        assert result.records_out == 0

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            evaluator = AlertEvaluator()
            evaluator.check(result, config)

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "alerts.example.com" in url

    def test_alert_does_not_fire_when_condition_false(self, tmp_path):
        """No webhook fires when the condition evaluates to False."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir() 
        dst.mkdir()
        (src / "data.json").write_text(json.dumps([{"id": 1}]))

        # Condition: records_out == 0 — but we have a record, so records_out == 1
        config = _make_pipeline(src, dst, condition="records_out == 0")
        executor = PipelineExecutor()
        result = executor.batch_run(config)

        assert result.records_out == 1

        with patch("httpx.post") as mock_post:
            evaluator = AlertEvaluator()
            evaluator.check(result, config)

        mock_post.assert_not_called()

    def test_alert_fires_on_records_in_condition(self, tmp_path):
        """Alert fires when records_in > 0."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir() 
        dst.mkdir()
        (src / "data.json").write_text(json.dumps([{"id": 1}, {"id": 2}]))

        config = _make_pipeline(src, dst, condition="records_in > 0")
        executor = PipelineExecutor()
        result = executor.batch_run(config)

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            evaluator = AlertEvaluator()
            evaluator.check(result, config)

        mock_post.assert_called_once()

    def test_webhook_payload_contains_pipeline_name(self, tmp_path):
        """Webhook POST payload includes pipeline name and result metadata."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir() 
        dst.mkdir()

        config = _make_pipeline(src, dst, condition="records_out == 0")
        executor = PipelineExecutor()
        result = executor.batch_run(config)

        captured = {}

        def capture_post(url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json", {})
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("httpx.post", side_effect=capture_post):
            evaluator = AlertEvaluator()
            evaluator.check(result, config)

        assert captured.get("url"), "No webhook POST was made"
        payload = captured.get("json", {})
        assert payload.get("pipeline") == "alert-test"
        assert "records_in" in payload
        assert "records_out" in payload
        assert "status" in payload

    def test_no_alert_without_alert_config(self, tmp_path):
        """No webhook fires for a pipeline with no alerts configured."""
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir() 
        dst.mkdir()

        config = load_pipeline_from_yaml(textwrap.dedent(f"""
            pipeline:
              name: no-alerts
              source:
                type: local
                path: {src}
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: {dst}
        """))

        executor = PipelineExecutor()
        result = executor.batch_run(config)

        with patch("httpx.post") as mock_post:
            evaluator = AlertEvaluator()
            evaluator.check(result, config)

        mock_post.assert_not_called()
