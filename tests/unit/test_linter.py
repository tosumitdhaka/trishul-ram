"""Tests for the pipeline linter (tram/pipeline/linter.py)."""

from __future__ import annotations

import os
import textwrap
from unittest.mock import patch

from tram.pipeline.linter import lint
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
        assert any(f.rule_id == "L003" and f.severity == "warning" for f in findings)

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


class TestWorkersDefaultsAndManagerLint:
    def test_push_source_defaults_to_count_all(self):
        config = _load("""
            pipeline:
              name: push-default
              schedule:
                type: stream
              source:
                type: webhook
                path: /test
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        assert config.workers is not None
        assert config.workers.count == "all"

    def test_non_push_source_defaults_to_count_one(self):
        config = _load("""
            pipeline:
              name: batch-default
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
        assert config.workers is not None
        assert config.workers.count == 1

    def test_l006_fires_in_manager_for_push_source_without_all(self):
        config = _load("""
            pipeline:
              name: bad-push-workers
              schedule:
                type: stream
              source:
                type: webhook
                path: /test
              workers:
                count: 1
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config, tram_mode="manager")
        assert any(f.rule_id == "L006" and f.severity == "error" for f in findings)

    def test_l006_allows_workers_list_with_pipeline_service(self):
        config = _load("""
            pipeline:
              name: list-push-service
              schedule:
                type: stream
              source:
                type: webhook
                path: /test
              workers:
                list:
                  - tram-worker-0
                  - tram-worker-1
              kubernetes:
                enabled: true
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config, tram_mode="manager")
        assert not any(f.rule_id == "L006" for f in findings)

    def test_l006_fires_for_pipeline_service_with_count_n(self):
        config = _load("""
            pipeline:
              name: count-n-push-service
              schedule:
                type: stream
              source:
                type: webhook
                path: /test
              workers:
                count: 2
              kubernetes:
                enabled: true
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config, tram_mode="manager")
        assert any(f.rule_id == "L006" and f.severity == "error" for f in findings)

    def test_l007_fires_for_poll_source_with_count_all(self):
        config = _load("""
            pipeline:
              name: bad-poll-workers
              source:
                type: local
                path: /tmp
              workers:
                count: all
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config, tram_mode="manager")
        assert any(f.rule_id == "L007" and f.severity == "error" for f in findings)

    def test_l008_blocks_udp_push_in_manager(self):
        config = _load("""
            pipeline:
              name: blocked-syslog
              schedule:
                type: stream
              source:
                type: syslog
                host: 0.0.0.0
                port: 5514
                protocol: udp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config, tram_mode="manager")
        assert any(f.rule_id == "L008" and f.severity == "error" for f in findings)

    def test_l011_warns_for_risky_filename_partition_field(self):
        config = _load("""
            pipeline:
              name: risky-filename-field
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: ndjson
              sink:
                type: local
                path: /out
                filename_template: "{field.timestamp}_{part}.ndjson"
        """)
        findings = lint(config)
        assert any(f.rule_id == "L011" and f.severity == "warning" for f in findings)

    def test_l011_not_triggered_for_safe_filename_partition_field(self):
        config = _load("""
            pipeline:
              name: safe-filename-field
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: ndjson
              sink:
                type: local
                path: /out
                filename_template: "{field.nf_name}_{part}.ndjson"
        """)
        findings = lint(config)
        assert not any(f.rule_id == "L011" for f in findings)

    def test_l009_warns_when_queue_count_exceeds_pool(self):
        config = _load("""
            pipeline:
              name: queue-too-large
              schedule:
                type: stream
              source:
                type: kafka
                brokers: ["localhost:9092"]
                topic: demo
                group_id: g1
              workers:
                count: 4
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config, tram_mode="manager", worker_pool_size=2)
        assert any(f.rule_id == "L009" and f.severity == "warning" for f in findings)

    def test_lint_rules_suppressed_in_standalone(self):
        config = _load("""
            pipeline:
              name: standalone-workers
              source:
                type: local
                path: /tmp
              workers:
                count: all
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        findings = lint(config, tram_mode="standalone")
        assert not any(f.rule_id in {"L006", "L007", "L008", "L009", "L010"} for f in findings)
