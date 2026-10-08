"""Delivery-tier interface defaults (V18-01 frozen contracts, section 6).

Pins the additive BaseSink/BaseSource defaults: every new method is
default-implemented so existing and unknown custom plugins keep loading,
instantiating, and behaving identically.
"""

from tram.interfaces.base_sink import (
    BaseSink,
    DeliveryTier,
    SinkCapability,
    SinkCommitReceipt,
)
from tram.interfaces.base_source import AckDisposition, BaseSource


class MinimalSink(BaseSink):
    """Overrides only the abstract write(); relies on every new default."""

    def write(self, data, meta):
        pass


class MinimalSource(BaseSource):
    """Overrides only the abstract read(); relies on every new default."""

    def read(self):
        return iter(())


def test_default_commit_returns_none_tier_confirmed_receipt():
    receipt = MinimalSink({}).commit()
    assert isinstance(receipt, SinkCommitReceipt)
    assert receipt.tier == "none"
    assert receipt.tier == DeliveryTier.NONE
    assert receipt.confirmed is True
    assert receipt.sink_key


def test_default_latched_error_is_none():
    assert MinimalSink({}).latched_error() is None


def test_default_source_unit_id_is_none():
    assert MinimalSource({}).source_unit_id({}) is None


def test_default_ack_and_stop_are_noops():
    source = MinimalSource({})
    assert source.stop() is None
    assert source.ack({}, AckDisposition.DELIVERED) is None
    assert source.ack({}, AckDisposition.FILTERED) is None
    assert source.ack({}, AckDisposition.DLQ) is None
    assert source.ack({}, AckDisposition.DROPPED) is None


def test_delivery_capability_defaults_to_none_on_fresh_subclass():
    assert MinimalSink.delivery_capability is None


def test_minimal_subclass_overriding_nothing_loads_and_instantiates():
    sink = MinimalSink({"some": "config"})
    source = MinimalSource({"some": "config"})
    assert isinstance(sink, BaseSink)
    assert isinstance(source, BaseSource)
    assert sink.config == {"some": "config"}
    assert source.config == {"some": "config"}


def test_delivery_tier_value_set():
    assert {tier.value for tier in DeliveryTier} == {
        "fsynced_local",
        "remote_durable",
        "remote_accepted",
        "none",
    }


def test_ack_disposition_value_set():
    assert {disposition.value for disposition in AckDisposition} == {
        "delivered",
        "filtered",
        "dlq",
        "dropped",
    }


def test_sink_capability_carries_tier_and_replay_safe():
    capability = SinkCapability(tier=DeliveryTier.REMOTE_DURABLE, replay_safe=True)
    assert capability.tier == "remote_durable"
    assert capability.replay_safe is True