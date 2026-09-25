"""Validation-time rejection tests for scalar execution knobs (review §2.14).

These knobs previously failed (or silently misbehaved) at runtime — APScheduler
schedule time, ``range()``/``sleep()``, or the token-bucket limiter. They must
be rejected by ``tram validate`` / model validation instead. The loader wraps
Pydantic ``ValidationError`` in ``ConfigError``, so the tests assert on that.
"""

from __future__ import annotations

import textwrap

import pytest

from tram.core.exceptions import ConfigError
from tram.pipeline.loader import load_pipeline_from_yaml


def _load(yaml_body: str):
    return load_pipeline_from_yaml(textwrap.dedent(yaml_body))


_BASE = """
    pipeline:
      name: {name}
      source:
        type: local
        path: /tmp/in
      serializer_in:
        type: json
      serializer_out:
        type: json
      sink:
        type: local
        path: /tmp/out
"""


def _with_schedule(schedule_block: str) -> str:
    """schedule_block is written at raw 6-space indent (pipeline children)."""
    return _BASE.format(name="sched-pipe") + schedule_block


def _with_extra(extra: str) -> str:
    """extra holds pipeline-child keys at raw 6-space indent."""
    return _BASE.format(name="knob-pipe") + extra


class TestIntervalSeconds:
    def test_interval_requires_positive_value(self):
        for bad in (0, -5):
            with pytest.raises(ConfigError, match="interval_seconds"):
                _load(_with_schedule(f"      schedule:\n        type: interval\n        interval_seconds: {bad}\n"))

    def test_positive_interval_ok(self):
        cfg = _load(_with_schedule("      schedule:\n        type: interval\n        interval_seconds: 60\n"))
        assert cfg.schedule.interval_seconds == 60

    def test_interval_missing_seconds_still_rejected(self):
        with pytest.raises(ConfigError, match="interval_seconds required"):
            _load(_with_schedule("      schedule:\n        type: interval\n"))


class TestCronValidation:
    def test_malformed_cron_rejected_at_validate_time(self):
        with pytest.raises(ConfigError, match="invalid cron expression"):
            _load(_with_schedule("      schedule:\n        type: cron\n        cron: '*/x * * * *'\n"))

    def test_valid_cron_ok(self):
        cfg = _load(_with_schedule("      schedule:\n        type: cron\n        cron: '0 * * * *'\n"))
        assert cfg.schedule.cron == "0 * * * *"


class TestScalarKnobs:
    def test_batch_size_zero_rejected(self):
        """batch_size=0 silently meant 'unlimited'; now rejected at validate time."""
        with pytest.raises(ConfigError, match="batch_size"):
            _load(_with_extra("      batch_size: 0\n"))

    def test_thread_workers_zero_rejected(self):
        with pytest.raises(ConfigError, match="thread_workers"):
            _load(_with_extra("      thread_workers: 0\n"))

    def test_negative_retry_count_rejected(self):
        with pytest.raises(ConfigError, match="retry_count"):
            _load(_with_extra("      retry_count: -1\n"))

    def test_negative_retry_delay_rejected(self):
        with pytest.raises(ConfigError, match="retry_delay_seconds"):
            _load(_with_extra("      retry_delay_seconds: -5\n"))

    def test_positive_knobs_still_accepted(self):
        cfg = _load(_with_extra(
            "      batch_size: 100\n      thread_workers: 4\n      retry_count: 3\n      retry_delay_seconds: 10\n"
        ))
        assert cfg.batch_size == 100
        assert cfg.thread_workers == 4
        assert cfg.retry_count == 3
        assert cfg.retry_delay_seconds == 10

    def test_defaults_unchanged(self):
        cfg = _load(_BASE.format(name="defaults"))
        assert cfg.batch_size is None
        assert cfg.thread_workers == 1
        assert cfg.retry_count == 3
        assert cfg.retry_delay_seconds == 10