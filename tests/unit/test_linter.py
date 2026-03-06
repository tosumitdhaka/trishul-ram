"""Tests for the pipeline linter (tram/pipeline/linter.py)."""

from __future__ import annotations

import os
import textwrap
from unittest.mock import patch

import pytest

from tram.pipeline.linter import LintResult, lint
from tram.pipeline.loader import load_pipeline_from_yaml


def _load(yaml_body: str):
    return load_pipeline_from_yaml(textwrap.dedent(yaml_body))


class TestL001SourceNoSink:
    def test_l001_not_triggered_when_sink_present(self):
        config = _load("""
            pipeline:
              name: l001-ok
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config)
        assert not any(f.rule_id == "L001" for f in findings)

    def test_l001_triggered_when_no_transforms_and_sink_still_present(self):
        """L001 is about *no global transforms AND no sinks* — sink IS present so no L001."""
        config = _load("""
            pipeline:
              name: l001-ok2
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config)
        # L001 should NOT fire because there's a sink
        assert not any(f.rule_id == "L001" for f in findings)


class TestL002SkipNoDlq:
    def test_l002_fires_for_continue_without_dlq(self):
        config = _load("""
            pipeline:
              name: l002-warn
              on_error: continue
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config)
        assert any(f.rule_id == "L002" and f.severity == "warning" for f in findings)

    def test_l002_not_triggered_when_dlq_configured(self):
        config = _load("""
            pipeline:
              name: l002-ok
              on_error: continue
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
              dlq:
                type: local
                path: /dlq
        """)
        findings = lint(config)
        assert not any(f.rule_id == "L002" for f in findings)


class TestL003StreamMultiWorker:
    def test_l003_fires_for_stream_with_workers_gt_1(self):
        config = _load("""
            pipeline:
              name: l003-err
              thread_workers: 2
              source:
                type: webhook
                path: /test
              serializer_in:
                type: json
              serializer_out:
                type: json
              schedule:
                type: stream
              sink:
                type: local
                path: /out
        """)
        findings = lint(config)
        assert any(f.rule_id == "L003" and f.severity == "error" for f in findings)

    def test_l003_not_triggered_for_batch_multi_worker(self):
        config = _load("""
            pipeline:
              name: l003-ok
              thread_workers: 4
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              schedule:
                type: interval
                interval_seconds: 60
              sink:
                type: local
                path: /out
        """)
        findings = lint(config)
        assert not any(f.rule_id == "L003" for f in findings)


class TestL004BatchSizeOnStream:
    def test_l004_fires_for_stream_with_batch_size(self):
        config = _load("""
            pipeline:
              name: l004-warn
              batch_size: 100
              source:
                type: webhook
                path: /test
              serializer_in:
                type: json
              serializer_out:
                type: json
              schedule:
                type: stream
              sink:
                type: local
                path: /out
        """)
        findings = lint(config)
        assert any(f.rule_id == "L004" and f.severity == "warning" for f in findings)

    def test_l004_not_triggered_for_batch_with_batch_size(self):
        config = _load("""
            pipeline:
              name: l004-ok
              batch_size: 100
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              schedule:
                type: interval
                interval_seconds: 60
              sink:
                type: local
                path: /out
        """)
        findings = lint(config)
        assert not any(f.rule_id == "L004" for f in findings)


class TestL005EmailNoSmtp:
    def test_l005_fires_for_email_action_without_smtp(self):
        config = _load("""
            pipeline:
              name: l005-warn
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
              alerts:
                - condition: "records_out == 0"
                  action: email
                  email_to: ops@example.com
        """)
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TRAM_SMTP_HOST", None)
            findings = lint(config)
        assert any(f.rule_id == "L005" and f.severity == "warning" for f in findings)

    def test_l005_not_triggered_when_smtp_configured(self):
        config = _load("""
            pipeline:
              name: l005-ok
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
              alerts:
                - condition: "records_out == 0"
                  action: email
                  email_to: ops@example.com
        """)
        with patch.dict(os.environ, {"TRAM_SMTP_HOST": "smtp.example.com"}):
            findings = lint(config)
        assert not any(f.rule_id == "L005" for f in findings)
