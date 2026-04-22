"""Tests for the hex_decode transform."""

from __future__ import annotations

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
