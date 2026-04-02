"""3GPP PM XML (measData) serializer — 3GPP TS 32.432 / Nokia NCOM format.

Pivots the p-indexed <measType>/<r> structure into flat records:

  <measInfo measInfoId="PCMTC">
    <granPeriod endTime="2026-03-30T09:15:00+00:00" duration="PT900S" />
    <measType p="1">container_cpu_usage</measType>
    <measType p="2">container_spec_cpu_limit</measType>
    <measValue measObjLdn="HOST=HOST,...,CON=cjee-wildfly">
      <r p="1">0.001</r>
      <r p="2">0.5</r>
    </measValue>
  </measInfo>

Becomes:
  {
    "end_time":                   "2026-03-30T09:15:00+00:00",
    "meas_info_id":               "PCMTC",
    "managed_element":            "snkncomcomkl51.xml",
    "meas_obj_ldn":               "HOST=HOST,...,CON=cjee-wildfly",
    "container_cpu_usage":        0.001,
    "container_spec_cpu_limit":   0.5
  }
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from typing import Iterator
from xml.etree import ElementTree as ET

from tram.interfaces.serializer import BaseSerializer
from tram.registry.registry import register_serializer

log = logging.getLogger(__name__)

# Namespace patterns used in 3GPP PM XML files
_NS_RE = re.compile(r"^\{.*?\}")


def _strip_ns(tag: str) -> str:
    """Strip XML namespace prefix from a tag name."""
    return _NS_RE.sub("", tag)


def _repair_truncated(raw: bytes) -> bytes:
    """Append missing closing tags when a PM file was cut off mid-write."""
    text = raw.rstrip()
    # Try to parse as-is first
    try:
        ET.fromstring(text)
        return text
    except ET.ParseError:
        pass
    # Append missing closing tags in innermost-first order
    for suffix in [
        b"</r>",
        b"</measValue>",
        b"</measInfo>",
        b"</measData>",
    ]:
        try:
            ET.fromstring(text + suffix)
            return text + suffix
        except ET.ParseError:
            text = text + suffix
    # Last-ditch: return with all suffixes appended
    return text


@register_serializer("pm_xml")
class PmXmlSerializer(BaseSerializer):
    """Deserializer for 3GPP TS 32.432 Performance Measurement XML files.

    Only deserialization (read) is supported — this format is source-only.
    Serialization (write) raises NotImplementedError.
    """

    def __init__(
        self,
        numeric_values: bool = True,
        add_managed_element: bool = True,
        add_duration: bool = False,
        encoding: str = "utf-8",
    ) -> None:
        self.numeric_values = numeric_values
        self.add_managed_element = add_managed_element
        self.add_duration = add_duration
        self.encoding = encoding

    # ── Deserialization ────────────────────────────────────────────────────

    def deserialize(self, data: bytes) -> list[dict]:
        """Parse a full PM XML file and return all records as a list."""
        return list(self._iter_records(data))

    def deserialize_stream(self, data: bytes) -> Iterator[dict]:
        """Parse a full PM XML file and yield records one at a time."""
        yield from self._iter_records(data)

    def _iter_records(self, raw: bytes) -> Iterator[dict]:
        repaired = _repair_truncated(raw)
        try:
            root = ET.fromstring(repaired)
        except ET.ParseError as exc:
            log.error("pm_xml: failed to parse XML: %s", exc)
            return

        # Resolve managed element name from the filename hint or <managedElement>
        managed_element = self._extract_managed_element(root)

        for meas_info in root.iter():
            if _strip_ns(meas_info.tag) != "measInfo":
                continue

            meas_info_id = meas_info.get("measInfoId", "")
            end_time = ""
            duration = ""

            # Build p → counter_name map
            p_map: dict[str, str] = {}

            for child in meas_info:
                local = _strip_ns(child.tag)

                if local == "granPeriod":
                    end_time = child.get("endTime", "")
                    duration = child.get("duration", "")

                elif local == "measType":
                    p_val = child.get("p", "")
                    if p_val and child.text:
                        p_map[p_val] = child.text.strip()

                elif local == "measValue":
                    meas_obj_ldn = child.get("measObjLdn", "")
                    record: dict = {
                        "end_time": end_time,
                        "meas_info_id": meas_info_id,
                    }

                    if self.add_managed_element:
                        record["managed_element"] = managed_element

                    if self.add_duration and duration:
                        record["duration"] = duration

                    record["meas_obj_ldn"] = meas_obj_ldn

                    # Pivot <r p="N">value</r> using the p_map
                    for r_elem in child:
                        if _strip_ns(r_elem.tag) == "r":
                            p_ref = r_elem.get("p", "")
                            counter = p_map.get(p_ref, f"_p{p_ref}")
                            raw_val = (r_elem.text or "").strip()
                            if self.numeric_values:
                                record[counter] = self._to_numeric(raw_val)
                            else:
                                record[counter] = raw_val

                    yield record

    @staticmethod
    def _extract_managed_element(root: ET.Element) -> str:
        """Try to find the managed element name from <managedElement> or file-level attrs."""
        for elem in root.iter():
            if _strip_ns(elem.tag) == "managedElement":
                # 3GPP: localDn or userLabel typically carries the NE name
                return (
                    elem.get("localDn")
                    or elem.get("userLabel")
                    or elem.get("swVersion", "")
                )
        # Fallback: check root attribute
        return root.get("dnPrefix", "")

    @staticmethod
    def _to_numeric(value: str):
        """Cast a string counter value to int or float; return original string on failure."""
        if not value:
            return None
        try:
            as_float = float(value)
            # Return int if the value has no fractional part
            if as_float == int(as_float) and "." not in value:
                return int(as_float)
            return as_float
        except ValueError:
            return value

    # ── Serialization (not supported) ─────────────────────────────────────

    def serialize(self, records: list[dict]) -> bytes:
        raise NotImplementedError(
            "pm_xml serializer is read-only. Use 'xml' or 'json' for output."
        )

    # ── Config factory ────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: dict) -> "PmXmlSerializer":
        return cls(
            numeric_values=config.get("numeric_values", True),
            add_managed_element=config.get("add_managed_element", True),
            add_duration=config.get("add_duration", False),
            encoding=config.get("encoding", "utf-8"),
        )
