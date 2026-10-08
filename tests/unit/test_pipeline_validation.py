"""Validation-time rejection tests for scalar execution knobs (review §2.14).

These knobs previously failed (or silently misbehaved) at runtime — APScheduler
schedule time, ``range()``/``sleep()``, or the token-bucket limiter. They must
be rejected by ``tram validate`` / model validation instead. The loader wraps
Pydantic ``ValidationError`` in ``ConfigError``, so the tests assert on that.
"""

from __future__ import annotations

import textwrap
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tram.core.exceptions import ConfigError
from tram.interfaces.base_sink import BaseSink, DeliveryTier, SinkCapability
from tram.interfaces.base_source import BaseSource
from tram.models.pipeline import (
    _sink_delivery_capability,
    _source_replay_identity_problem,
)
from tram.pipeline.loader import load_pipeline_from_yaml
from tram.registry.registry import _sinks, _sources


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


# ── delivery.contract: strict (V18-01 §6/§7/§9) ────────────────────────────


class _FakeUndeclaredSink(BaseSink):
    """Custom sink that never declares a delivery_capability (V18-01 §6)."""

    def write(self, data: bytes, meta: dict) -> None:
        return None


class _FakeDeclaredSink(BaseSink):
    """Custom sink that declares a delivery_capability."""

    delivery_capability = SinkCapability(
        tier=DeliveryTier.FSYNCED_LOCAL, replay_safe=False
    )

    def write(self, data: bytes, meta: dict) -> None:
        return None


class _FakeSourceNoIdentity(BaseSource):
    """Custom source that provides no source_unit_id-style identity."""

    def read(self):
        return iter(())
        yield  # pragma: no cover


class _FakeSourceWithIdentity(BaseSource):
    """Custom source that implements source_unit_id()."""

    def read(self):
        return iter(())
        yield  # pragma: no cover

    def source_unit_id(self, meta: dict) -> str | None:
        return "fake-ns/unit"


@contextmanager
def _registered(registry: dict, key: str, cls):
    """Temporarily register a plugin class so strict-mode resolution sees it."""
    registry[key] = cls
    try:
        yield
    finally:
        registry.pop(key, None)


def _strict_pipeline_yaml(body: str, name: str = "strict-pipe") -> str:
    """body holds pipeline-child keys at raw 6-space indent; the delivery
    contract header (raw 4-space indent, same base as ``_BASE``) is
    prepended and ``_load``'s dedent normalizes both together."""
    return (
        "    pipeline:\n"
        f"      name: {name}\n"
        "      delivery:\n"
        "        contract: strict\n"
    ) + body


class TestDeliveryContract:
    """delivery.contract strict-mode validation (V18-01 §6/§7/§9).

    The frozen field defaults to ``legacy`` — exactly today's behavior with
    zero new checks. ``strict`` requires every configured sink to declare a
    ``delivery_capability``, the source to provide durable replay identity
    (AMQP additionally requiring ``require_message_id: true``), and Kafka
    sources to run single-threaded. All checks run at model validation, which
    ``tram validate`` and API registration share.
    """

    _SOURCE_LOCAL = "      source:\n        type: local\n        path: /tmp/in\n"
    _SOURCE_AMQP = "      source:\n        type: amqp\n        queue: q\n"
    _SOURCE_AMQP_REQUIRED = (
        "      source:\n        type: amqp\n        queue: q\n"
        "        require_message_id: true\n"
    )
    _SOURCE_KAFKA = (
        "      source:\n        type: kafka\n"
        "        brokers: [broker:9092]\n        topic: t\n"
    )
    _SOURCE_REST = "      source:\n        type: rest\n        url: http://example.com\n"
    _SER = "      serializer_in:\n        type: json\n      serializer_out:\n        type: json\n"
    _SINK_LOCAL = "      sink:\n        type: local\n        path: /tmp/out\n"
    _SINK_SFTP = (
        "      sink:\n        type: sftp\n        host: example.com\n"
        "        username: u\n        password: p\n        remote_path: /out\n"
    )
    _SINK_REST = "      sink:\n        type: rest\n        url: http://example.com\n"

    def test_default_contract_is_legacy(self):
        cfg = _load(_BASE.format(name="legacy-default"))
        assert cfg.delivery.contract == "legacy"

    def test_legacy_passes_pipeline_that_would_fail_strict(self):
        """The canonical strict offender — AMQP without message-ID config, an
        undeclared rest sink, threaded parallelism — loads fine under the
        default legacy contract (zero behavior change)."""
        yaml_body = (
            "    pipeline:\n"
            "      name: legacy-strict-offender\n"
            + self._SOURCE_AMQP + self._SER + self._SINK_REST
            + "      thread_workers: 4\n"
        )
        cfg = _load(yaml_body)
        assert cfg.delivery.contract == "legacy"
        assert cfg.source.type == "amqp"
        assert cfg.thread_workers == 4

    def test_strict_accepts_fully_declared_pipeline(self):
        """Local source (fingerprint identity) + sftp sink (fsynced_local
        capability) satisfy every strict requirement."""
        cfg = _load(
            _strict_pipeline_yaml(self._SOURCE_LOCAL + self._SER + self._SINK_SFTP)
        )
        assert cfg.delivery.contract == "strict"

    def test_strict_rejects_undeclared_sink(self):
        """rest is a built-in sink with no delivery_capability declaration —
        strict rejects it with a documented capability error."""
        with pytest.raises(ConfigError, match="delivery_capability"):
            _load(_strict_pipeline_yaml(self._SOURCE_LOCAL + self._SER + self._SINK_REST))

    def test_strict_rejects_amqp_without_message_id_config(self):
        """AMQP identity is {queue}/{producer message ID}; without
        require_message_id: true the connector returns None identity."""
        with pytest.raises(ConfigError, match="require_message_id"):
            _load(_strict_pipeline_yaml(self._SOURCE_AMQP + self._SER + self._SINK_LOCAL))

    def test_strict_accepts_amqp_with_message_id_config(self):
        cfg = _load(
            _strict_pipeline_yaml(self._SOURCE_AMQP_REQUIRED + self._SER + self._SINK_LOCAL)
        )
        assert cfg.source.require_message_id is True

    def test_strict_rejects_kafka_with_thread_workers_gt_one(self):
        """Threaded frontiers are not yet broker-proven — rejected, not
        silently weakened (V18-01 §7)."""
        with pytest.raises(ConfigError, match="thread_workers"):
            _load(
                _strict_pipeline_yaml(
                    self._SOURCE_KAFKA + self._SER + self._SINK_LOCAL + "      thread_workers: 2\n"
                )
            )

    def test_strict_accepts_kafka_single_threaded(self):
        cfg = _load(_strict_pipeline_yaml(self._SOURCE_KAFKA + self._SER + self._SINK_LOCAL))
        assert cfg.source.type == "kafka"
        assert cfg.thread_workers == 1

    def test_strict_rejects_source_without_replay_identity(self):
        """rest source has no source_unit_id-style identity — rejected."""
        with pytest.raises(ConfigError, match="replay identity"):
            _load(_strict_pipeline_yaml(self._SOURCE_REST + self._SER + self._SINK_LOCAL))

    def test_strict_lists_every_unmet_condition(self):
        """The aggregated message names all problems, not just the first."""
        with pytest.raises(ConfigError) as exc_info:
            _load(_strict_pipeline_yaml(self._SOURCE_AMQP + self._SER + self._SINK_REST))
        message = str(exc_info.value)
        assert "delivery_capability" in message
        assert "require_message_id" in message


class TestDeliveryContractCustomPlugins:
    """Registry resolution for custom plugins (V18-01 §6: an unknown custom
    plugin with delivery_capability = None is rejected under strict).

    Custom source/sink types cannot reach the strict validator through YAML
    today (the discriminated SourceConfig/SinkConfig unions list only the
    built-in types), so the resolution helpers are exercised directly against
    the registry — the same code the validator calls.
    """

    def test_undeclared_custom_sink_is_undeclared(self):
        with _registered(_sinks, "test_undeclared_sink", _FakeUndeclaredSink):
            assert _sink_delivery_capability("test_undeclared_sink") is None

    def test_declared_custom_sink_is_declared(self):
        with _registered(_sinks, "test_declared_sink", _FakeDeclaredSink):
            capability = _sink_delivery_capability("test_declared_sink")
        assert capability is not None
        assert capability.tier == DeliveryTier.FSYNCED_LOCAL

    def test_unregistered_sink_type_is_undeclared(self):
        assert _sink_delivery_capability("no_such_sink") is None

    def test_custom_source_with_identity_is_ok(self):
        pipeline = SimpleNamespace(source=SimpleNamespace(type="test_identity_src"))
        with _registered(_sources, "test_identity_src", _FakeSourceWithIdentity):
            assert _source_replay_identity_problem(pipeline) is None

    def test_custom_source_without_identity_is_rejected(self):
        pipeline = SimpleNamespace(source=SimpleNamespace(type="test_no_identity_src"))
        with _registered(_sources, "test_no_identity_src", _FakeSourceNoIdentity):
            problem = _source_replay_identity_problem(pipeline)
        assert problem is not None
        assert "source_unit_id" in problem

    def test_unregistered_source_is_rejected(self):
        pipeline = SimpleNamespace(source=SimpleNamespace(type="no_such_source"))
        problem = _source_replay_identity_problem(pipeline)
        assert problem is not None
        assert "no_such_source" in problem