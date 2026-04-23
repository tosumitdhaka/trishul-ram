"""hex_decode transform — interpret hex-string leaf values with heuristics or explicit codecs."""

from __future__ import annotations

import ipaddress
import string
from typing import Any

from tram.core.exceptions import TransformError
from tram.interfaces.base_transform import BaseTransform
from tram.registry.registry import register_transform
from tram.transforms.path_patterns import has_path_pattern, path_matches_pattern
from tram.transforms.path_utils import get_path


def _is_hex_string(value: str) -> bool:
    if len(value) % 2 != 0 or not value:
        return False
    try:
        bytes.fromhex(value)
        return True
    except ValueError:
        return False


def _is_printable_text(value: str) -> bool:
    allowed_controls = {"\n", "\r", "\t"}
    return all(char in string.printable or char in allowed_controls for char in value)


def _decode_text(data: bytes, mode: str) -> str | None:
    encodings = ["utf-8"] if mode == "utf8_or_hex" else ["latin-1"] if mode == "latin1_or_hex" else []
    if mode == "utf8_or_hex":
        encodings.append("latin-1")
    for encoding in encodings:
        try:
            decoded = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _is_printable_text(decoded):
            return decoded
    return None


def _decode_tbcd(data: bytes) -> str:
    digits: list[str] = []
    for byte in data:
        low = byte & 0x0F
        high = (byte >> 4) & 0x0F
        for nibble in (low, high):
            if nibble == 0xF:
                continue
            if 0 <= nibble <= 9:
                digits.append(str(nibble))
    return "".join(digits)


def _decode_bcd_timestamp(data: bytes) -> str:
    parts = [f"{(byte >> 4) & 0x0F}{byte & 0x0F}" for byte in data]
    if len(parts) >= 6:
        return f"20{parts[0]}-{parts[1]}-{parts[2]} {parts[3]}:{parts[4]}:{parts[5]}"
    return " ".join(parts)


def _decode_tbcd_timezone(data: bytes) -> str:
    if not data:
        raise TransformError("hex_decode: timezone tbcd_quarter_hour codec requires at least 1 byte")
    first = data[0]
    low = first & 0x0F
    high = (first >> 4) & 0x0F
    negative = bool(low & 0x08)
    tens = low & 0x07
    units = high
    if tens > 9 or units > 9:
        raise TransformError("hex_decode: invalid tbcd_quarter_hour timezone value")
    quarter_hours = tens * 10 + units
    total_minutes = quarter_hours * 15
    sign = "-" if negative else "+"
    result = f"UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"
    if len(data) > 1:
        result += f" DST={data[1]}"
    return result


def _decode_packed_ip(data: bytes) -> str:
    if len(data) == 4:
        return str(ipaddress.IPv4Address(data))
    if len(data) == 16:
        return str(ipaddress.IPv6Address(data))
    raise TransformError("hex_decode: packed IP codec expects 4 or 16 bytes")


def _decode_bit_flags(
    raw_hex: str,
    bit_length: int | None,
    mapping: dict[int, str],
    output: str | None,
):
    data = bytes.fromhex(raw_hex)
    total_bits = len(data) * 8
    if bit_length is None:
        bit_length = total_bits
    if bit_length < 0:
        raise TransformError("hex_decode: bit_length_field must be >= 0")
    if bit_length > total_bits:
        raise TransformError("hex_decode: bit_length_field exceeds available bits")

    indexes: list[int] = []
    for index in range(bit_length):
        byte_index = index // 8
        bit_index = 7 - (index % 8)
        if data[byte_index] & (1 << bit_index):
            indexes.append(index)

    mode = output or ("names" if mapping else "indexes")
    if mode not in {"names", "indexes", "both"}:
        raise TransformError("hex_decode: bit_flags output must be names, indexes, or both")

    names = [mapping[index] for index in indexes if index in mapping]
    if mode == "indexes":
        return indexes
    if mode == "both":
        return {"indexes": indexes, "names": names}
    return names if mapping else indexes


def _decode_value(
    raw_hex: str,
    decode_as: str,
    fmt: str | None,
    bit_length: int | None = None,
    mapping: dict[int, str] | None = None,
    output: str | None = None,
):
    data = bytes.fromhex(raw_hex)
    mapping = mapping or {}
    if decode_as == "text":
        encoding = fmt or "utf8"
        if encoding == "utf8":
            return data.decode("utf-8")
        if encoding == "latin1":
            return data.decode("latin-1")
        raise TransformError(f"hex_decode: unsupported text format {encoding!r}")
    if decode_as == "digits" and fmt == "tbcd":
        return _decode_tbcd(data)
    if decode_as == "timestamp" and fmt == "bcd_semi_octet":
        return _decode_bcd_timestamp(data)
    if decode_as == "timezone" and fmt == "tbcd_quarter_hour":
        return _decode_tbcd_timezone(data)
    if decode_as == "ip" and fmt == "packed":
        return _decode_packed_ip(data)
    if decode_as == "bit_flags":
        return _decode_bit_flags(raw_hex, bit_length, mapping, output)
    if decode_as == "hex":
        return raw_hex
    raise TransformError(
        f"hex_decode: unsupported decode pair decode_as={decode_as!r}, format={fmt!r}"
    )


@register_transform("hex_decode")
class HexDecodeTransform(BaseTransform):
    """Decode hex-string leaves heuristically or via explicit field-path overrides.

    Override paths are dict-key based (`a.b.c`). List indexes are intentionally unsupported;
    list members inherit the parent list path when traversed.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.mode: str = config.get("mode", "utf8_or_hex")
        self.preserve_original: bool = bool(config.get("preserve_original", False))
        self.original_suffix: str = config.get("original_suffix", "_hex")
        self.exact_overrides: dict[str, dict[str, Any]] = {}
        self.pattern_overrides: list[tuple[str, dict[str, Any]]] = []
        for item in config.get("overrides", []):
            override = {
                "decode_as": item["decode_as"],
                "format": item.get("format"),
                "bit_length_field": item.get("bit_length_field"),
                "mapping": item.get("mapping", {}),
                "output": item.get("output"),
            }
            path = item["path"]
            if has_path_pattern(path):
                self.pattern_overrides.append((path, override))
            else:
                self.exact_overrides[path] = override

        if self.mode not in {"hex", "utf8_or_hex", "latin1_or_hex"}:
            raise TransformError("hex_decode: mode must be hex, utf8_or_hex, or latin1_or_hex")

    def apply(self, records: list[dict]) -> list[dict]:
        return [self._transform_mapping(record, "", record) for record in records]

    def _resolve_bit_length(self, override: dict[str, Any], root: dict[str, Any]) -> int | None:
        bit_length_field = override.get("bit_length_field")
        if not bit_length_field:
            return None
        found, value = get_path(root, bit_length_field)
        if not found:
            raise TransformError(
                f"hex_decode: bit_length_field '{bit_length_field}' not found"
            )
        if not isinstance(value, int):
            raise TransformError(
                f"hex_decode: bit_length_field '{bit_length_field}' must be an int"
            )
        return value

    def _maybe_decode(self, value: Any, path: str, root: dict[str, Any]):
        if not isinstance(value, str) or not _is_hex_string(value):
            return value, False

        override = self.exact_overrides.get(path)
        if override is None:
            override = next(
                (candidate for pattern, candidate in self.pattern_overrides
                 if path_matches_pattern(path, pattern)),
                None,
            )
        if override is not None:
            bit_length = self._resolve_bit_length(override, root)
            return _decode_value(
                value,
                override["decode_as"],
                override.get("format"),
                bit_length=bit_length,
                mapping=override.get("mapping", {}),
                output=override.get("output"),
            ), True

        if self.mode == "hex":
            return value, False

        decoded = _decode_text(bytes.fromhex(value), self.mode)
        if decoded is None:
            return value, False
        return decoded, True

    def _transform_mapping(self, data: dict[str, Any], prefix: str, root: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result[key] = self._transform_mapping(value, path, root)
                continue
            if isinstance(value, list):
                result[key] = self._transform_list(value, path, root)
                continue
            decoded, changed = self._maybe_decode(value, path, root)
            result[key] = decoded
            if self.preserve_original and changed:
                result[f"{key}{self.original_suffix}"] = value
        return result

    def _transform_list(self, values: list[Any], prefix: str, root: dict[str, Any]) -> list[Any]:
        result: list[Any] = []
        for value in values:
            if isinstance(value, dict):
                result.append(self._transform_mapping(value, prefix, root))
                continue
            if isinstance(value, list):
                result.append(self._transform_list(value, prefix, root))
                continue
            decoded, _ = self._maybe_decode(value, prefix, root)
            result.append(decoded)
        return result
