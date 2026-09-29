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


class TestSnmpPrivProtocolRestriction:
    """SNMPv3 priv-protocol validation (v1.5.0/v1.5.1, GH #72).

    DES is rejected on all three SNMPv3 config classes — it is obsoleted
    (RFC 8996 lineage) and upstream-dropped. 3DES-EDE was removed in v1.5.0
    but is accepted again in v1.5.1 (tsnmp 0.6.2 wire-fixed the #31 padding
    interop), in the ``3DES``/``3des``/``3des-ede`` spellings. The loader
    wraps Pydantic's ValidationError in ConfigError, and the DES migration
    message must name AES128 as the replacement.
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

    @pytest.mark.parametrize("algo", ["DES", "des", "Des"])
    def test_removed_priv_rejected_on_poll_source(self, algo):
        with pytest.raises(ConfigError, match="removed in v1.5.0") as exc_info:
            _load(self._poll_yaml(algo))
        message = str(exc_info.value)
        assert algo.upper() in message
        assert "AES128" in message

    @pytest.mark.parametrize("algo", ["DES", "des", "Des"])
    def test_removed_priv_rejected_on_trap_source(self, algo):
        with pytest.raises(ConfigError, match="removed in v1.5.0") as exc_info:
            _load(self._trap_source_yaml(algo))
        message = str(exc_info.value)
        assert algo.upper() in message
        assert "AES128" in message

    @pytest.mark.parametrize("algo", ["DES", "des", "Des"])
    def test_removed_priv_rejected_on_trap_sink(self, algo):
        with pytest.raises(ConfigError, match="removed in v1.5.0") as exc_info:
            _load(self._trap_sink_yaml(algo))
        message = str(exc_info.value)
        assert algo.upper() in message
        assert "AES128" in message

    @pytest.mark.parametrize(
        "algo", ["3DES", "3des", "3des-ede", "3DES-EDE"],
        ids=["3DES", "3des", "3des-ede", "3DES-EDE"],
    )
    def test_3des_accepted_on_poll_source(self, algo):
        """3DES-EDE is supported again in v1.5.1 (tsnmp 0.6.2 #31 fix)."""
        cfg = _load(self._poll_yaml(algo))
        assert cfg.source.priv_protocol == algo

    @pytest.mark.parametrize(
        "algo", ["3DES", "3des", "3des-ede", "3DES-EDE"],
        ids=["3DES", "3des", "3des-ede", "3DES-EDE"],
    )
    def test_3des_accepted_on_trap_source(self, algo):
        cfg = _load(self._trap_source_yaml(algo))
        assert cfg.source.priv_protocol == algo

    @pytest.mark.parametrize(
        "algo", ["3DES", "3des", "3des-ede", "3DES-EDE"],
        ids=["3DES", "3des", "3des-ede", "3DES-EDE"],
    )
    def test_3des_accepted_on_trap_sink(self, algo):
        cfg = _load(self._trap_sink_yaml(algo))
        assert cfg.sink.priv_protocol == algo

    @pytest.mark.parametrize("garbage", ["3DESEDE", "3des_ede", "AES-512", "foo"])
    def test_garbage_priv_rejected_on_poll_source(self, garbage):
        """Unknown spellings reject instead of silently mapping to AES128
        at runtime (review C4)."""
        with pytest.raises(ConfigError, match="not a supported SNMPv3 privacy protocol") as exc_info:
            _load(self._poll_yaml(garbage))
        message = str(exc_info.value)
        assert "AES128" in message
        assert "3DES" in message

    @pytest.mark.parametrize("garbage", ["3DESEDE", "3des_ede", "AES-512", "foo"])
    def test_garbage_priv_rejected_on_trap_source(self, garbage):
        with pytest.raises(ConfigError, match="not a supported SNMPv3 privacy protocol"):
            _load(self._trap_source_yaml(garbage))

    @pytest.mark.parametrize("garbage", ["3DESEDE", "3des_ede", "AES-512", "foo"])
    def test_garbage_priv_rejected_on_trap_sink(self, garbage):
        with pytest.raises(ConfigError, match="not a supported SNMPv3 privacy protocol"):
            _load(self._trap_sink_yaml(garbage))

    def test_valid_priv_protocols_still_accepted(self):
        for algo in ("AES", "AES128", "AES192", "AES256", "aes", "3DES", "3des-ede"):
            cfg = _load(self._poll_yaml(algo))
            assert cfg.source.priv_protocol == algo

    def test_validator_set_matches_both_usm_builders(self):
        """The validator's accepted set is exactly the union of the USM
        builders' mapping keys on BOTH stacks (+ DES, which is rejected) —
        nothing a builder maps can be a config 400 (review C4)."""
        from tram.connectors.snmp.mib_utils import (
            _PRIV_PROTO_NAMES,
            _TSNMP_PRIV_PROTOCOLS,
        )
        from tram.models.pipeline import _SNMP_PRIV_PROTOCOL_VALUES

        legacy_keys = set(_PRIV_PROTO_NAMES)
        tsnmp_keys = set(_TSNMP_PRIV_PROTOCOLS)
        assert legacy_keys == tsnmp_keys, (
            f"legacy/tsnmp priv mappings diverged: {legacy_keys} vs {tsnmp_keys}"
        )
        assert _SNMP_PRIV_PROTOCOL_VALUES | {"DES"} == legacy_keys