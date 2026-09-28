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


class TestSnmpPrivProtocolRemoval:
    """v1.5.0 (GH #72) — DES and 3DES rejected on all three SNMPv3 config classes.

    DES is obsoleted (RFC 8996 lineage) and upstream-dropped; 3DES was never
    standardized. The loader wraps Pydantic's ValidationError in ConfigError,
    and the migration message must name AES128 as the replacement.
    """

    _USM = """
    security_name: usr
    auth_key: authpass
    priv_key: privpass
"""

    def _poll_yaml(self, priv: str) -> str:
        return f"""
pipeline:
  name: snmp-poll-pipe
  source:
    type: snmp_poll
    host: 10.0.0.1
    oids: ["1.3.6.1.2.1.1.1.0"]
    version: "3"
    {self._USM}    priv_protocol: {priv}
  serializer_in:
    type: json
  serializer_out:
    type: json
  sink:
    type: local
    path: /tmp/out
"""

    def _trap_source_yaml(self, priv: str) -> str:
        return f"""
pipeline:
  name: snmp-trap-source-pipe
  source:
    type: snmp_trap
    version: "3"
    {self._USM}    priv_protocol: {priv}
  serializer_in:
    type: json
  serializer_out:
    type: json
  sink:
    type: local
    path: /tmp/out
"""

    def _trap_sink_yaml(self, priv: str) -> str:
        return f"""
pipeline:
  name: snmp-trap-sink-pipe
  source:
    type: local
    path: /tmp/in
  serializer_in:
    type: json
  serializer_out:
    type: json
  sink:
    type: snmp_trap
    host: manager.example.com
    version: "3"
    {self._USM}    priv_protocol: {priv}
"""

    @pytest.mark.parametrize("algo", ["DES", "3DES", "des", "3des", "Des"])
    def test_removed_priv_rejected_on_poll_source(self, algo):
        with pytest.raises(ConfigError, match="removed in v1.5.0") as exc_info:
            _load(self._poll_yaml(algo))
        message = str(exc_info.value)
        assert algo.upper() in message
        assert "AES128" in message

    @pytest.mark.parametrize("algo", ["DES", "3DES", "des", "3des", "Des"])
    def test_removed_priv_rejected_on_trap_source(self, algo):
        with pytest.raises(ConfigError, match="removed in v1.5.0") as exc_info:
            _load(self._trap_source_yaml(algo))
        message = str(exc_info.value)
        assert algo.upper() in message
        assert "AES128" in message

    @pytest.mark.parametrize("algo", ["DES", "3DES", "des", "3des", "Des"])
    def test_removed_priv_rejected_on_trap_sink(self, algo):
        with pytest.raises(ConfigError, match="removed in v1.5.0") as exc_info:
            _load(self._trap_sink_yaml(algo))
        message = str(exc_info.value)
        assert algo.upper() in message
        assert "AES128" in message

    def test_valid_priv_protocols_still_accepted(self):
        for algo in ("AES", "AES128", "AES192", "AES256", "aes"):
            cfg = _load(self._poll_yaml(algo))
            assert cfg.source.priv_protocol == algo