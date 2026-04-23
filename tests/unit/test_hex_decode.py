"""Tests for the hex_decode transform."""

from __future__ import annotations

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.hex_decode import HexDecodeTransform


class TestHexDecodeTransform:
    def test_utf8_or_hex_decodes_printable_text(self):
        transform = HexDecodeTransform({"mode": "utf8_or_hex"})
        result = transform.apply([{"method": "494e56495445"}])
        assert result == [{"method": "INVITE"}]

    def test_non_text_hex_is_preserved(self):
        transform = HexDecodeTransform({"mode": "utf8_or_hex"})
        result = transform.apply([{"blob": "deadbeef"}])
        assert result == [{"blob": "deadbeef"}]

    def test_preserve_original_adds_shadow_field(self):
        transform = HexDecodeTransform({"mode": "utf8_or_hex", "preserve_original": True})
        result = transform.apply([{"method": "494e56495445"}])
        assert result == [{"method": "INVITE", "method_hex": "494e56495445"}]

    def test_override_decodes_tbcd_digits(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{"path": "servedIMSI", "decode_as": "digits", "format": "tbcd"}],
            }
        )
        result = transform.apply([{"servedIMSI": "130254214365f7"}])
        assert result == [{"servedIMSI": "3120451234567"}]

    def test_override_decodes_nested_packed_ip(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{"path": "pGWAddress.value.value", "decode_as": "ip", "format": "packed"}],
            }
        )
        records = [{"pGWAddress": {"type": "iPBinaryAddress", "value": {"type": "iPBinV4Address", "value": "c0a80101"}}}]

        result = transform.apply(records)

        assert result == [{"pGWAddress": {"type": "iPBinaryAddress", "value": {"type": "iPBinV4Address", "value": "192.168.1.1"}}}]

    def test_override_decodes_deep_nested_path(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{"path": "a.b.c.value", "decode_as": "ip", "format": "packed"}],
            }
        )
        records = [{"a": {"b": {"c": {"value": "7f000001"}}}}]

        result = transform.apply(records)

        assert result == [{"a": {"b": {"c": {"value": "127.0.0.1"}}}}]

    def test_wildcard_override_matches_single_segment_path(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{"path": "*.pGWAddress", "decode_as": "ip", "format": "packed"}],
            }
        )

        result = transform.apply([{"serviceInformation": {"pGWAddress": "c0a80101"}}])

        assert result == [{"serviceInformation": {"pGWAddress": "192.168.1.1"}}]

    def test_wildcard_override_does_not_match_deeper_suffix_path(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{"path": "*.pGWAddress", "decode_as": "ip", "format": "packed"}],
            }
        )

        result = transform.apply([{"outer": {"serviceInformation": {"pGWAddress": "c0a80101"}}}])

        assert result == [{"outer": {"serviceInformation": {"pGWAddress": "c0a80101"}}}]

    def test_exact_override_takes_precedence_over_wildcard(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [
                    {"path": "*.pGWAddress", "decode_as": "hex"},
                    {"path": "serviceInformation.pGWAddress", "decode_as": "text", "format": "utf8"},
                ],
            }
        )

        result = transform.apply([{"serviceInformation": {"pGWAddress": "3132"}}])

        assert result == [{"serviceInformation": {"pGWAddress": "12"}}]

    def test_override_decodes_bit_flags_to_names(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{
                    "path": "service_condition_change_hex",
                    "decode_as": "bit_flags",
                    "bit_length_field": "service_condition_change_bits",
                    "mapping": {0: "qosChange", 1: "tariffTime", 5: "recordClosure"},
                }],
            }
        )

        result = transform.apply([{
            "service_condition_change_hex": "c4000000",
            "service_condition_change_bits": 32,
        }])

        assert result == [{
            "service_condition_change_hex": ["qosChange", "tariffTime", "recordClosure"],
            "service_condition_change_bits": 32,
        }]

    def test_override_decodes_bit_flags_to_indexes(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{
                    "path": "service_condition_change_hex",
                    "decode_as": "bit_flags",
                    "bit_length_field": "service_condition_change_bits",
                    "output": "indexes",
                }],
            }
        )

        result = transform.apply([{
            "service_condition_change_hex": "c4000000",
            "service_condition_change_bits": 32,
        }])

        assert result == [{
            "service_condition_change_hex": [0, 1, 5],
            "service_condition_change_bits": 32,
        }]

    def test_override_decodes_bit_flags_to_both(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{
                    "path": "service_condition_change_hex",
                    "decode_as": "bit_flags",
                    "bit_length_field": "service_condition_change_bits",
                    "mapping": {0: "qosChange", 1: "tariffTime", 5: "recordClosure"},
                    "output": "both",
                }],
            }
        )

        result = transform.apply([{
            "service_condition_change_hex": "c4000000",
            "service_condition_change_bits": 32,
        }])

        assert result == [{
            "service_condition_change_hex": {
                "indexes": [0, 1, 5],
                "names": ["qosChange", "tariffTime", "recordClosure"],
            },
            "service_condition_change_bits": 32,
        }]

    def test_bit_flags_respects_bit_length_truncation(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{
                    "path": "flags",
                    "decode_as": "bit_flags",
                    "bit_length_field": "flag_bits",
                    "output": "indexes",
                }],
            }
        )

        result = transform.apply([{"flags": "80", "flag_bits": 1}])

        assert result == [{"flags": [0], "flag_bits": 1}]

    def test_bit_flags_missing_bit_length_field_raises(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{
                    "path": "flags",
                    "decode_as": "bit_flags",
                    "bit_length_field": "flag_bits",
                }],
            }
        )

        with pytest.raises(TransformError, match="bit_length_field 'flag_bits' not found"):
            transform.apply([{"flags": "80"}])

    def test_bit_flags_bit_length_exceeds_available_bits_raises(self):
        transform = HexDecodeTransform(
            {
                "mode": "hex",
                "overrides": [{
                    "path": "flags",
                    "decode_as": "bit_flags",
                    "bit_length_field": "flag_bits",
                }],
            }
        )

        with pytest.raises(TransformError, match="exceeds available bits"):
            transform.apply([{"flags": "80", "flag_bits": 9}])
