"""3GPP PM XML serializer (Nokia NCOM / 3GPP TS 32.432 measData format).

Parses the measData structure and produces one flat record per measValue:

    {
        "end_time":       "<granPeriod endTime>",
        "duration":       "<granPeriod duration>",
        "meas_info_id":   "<measInfo measInfoId>",
        "managed_element": "<managedElement localDn>",
        "meas_obj_ldn":   "<measValue measObjLdn>",
        "<counter_name>": "<value>",   # one field per measType
        ...
    }

Config keys:
    encoding        (str, default "utf-8")   File encoding.
    add_managed_element (bool, default True) Include managed_element field.
    add_duration    (bool, default False)    Include granPeriod duration field.
    numeric_values  (bool, default True)     Cast counter values to float where possible.
"""

from __future__ import annotations

import logging

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer

logger = logging.getLogger(__name__)


@register_serializer("pm_xml")
class PmXmlSerializer(BaseSerializer):
    """3GPP PM XML (measData) deserializer — produces one record per measValue."""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.encoding: str = config.get("encoding", "utf-8")
        self.add_managed_element: bool = config.get("add_managed_element", True)
        self.add_duration: bool = config.get("add_duration", False)
        self.numeric_values: bool = config.get("numeric_values", True)

    def parse(self, data: bytes) -> list[dict]:
        try:
            import defusedxml.ElementTree as ET
        except ImportError as exc:
            raise SerializerError("defusedxml is required for pm_xml serializer") from exc

        try:
            raw = data.decode(self.encoding)
            # Auto-close truncated files: append missing closing tags if needed
            raw = raw.rstrip()
            if not raw.endswith("</measData>"):
                # Count unclosed tags and append closers
                open_measvalue = raw.count("<measValue") - raw.count("</measValue>")
                open_measinfo = raw.count("<measInfo") - raw.count("</measInfo>")
                open_measdata = raw.count("<measData") - raw.count("</measData>")
                raw += "</measValue>" * max(open_measvalue, 0)
                raw += "</measInfo>" * max(open_measinfo, 0)
                raw += "</measData>" * max(open_measdata, 0)
            root = ET.fromstring(raw)
        except Exception as exc:
            raise SerializerError(f"pm_xml: XML parse error: {exc}") from exc

        # Strip namespace prefix from tags (e.g. {urn:3gpp:...}measData → measData)
        def _tag(element) -> str:
            t = element.tag
            return t.split("}")[-1] if "}" in t else t

        def _attr(element, name: str, default: str = "") -> str:
            # Try with and without namespace on attribute
            return element.attrib.get(name, element.attrib.get(f"{{{element.tag.split('{')[1].split('}')[0]}}}{name}" if "{" in element.tag else name, default))

        # Locate root — may be measData directly or wrapped
        meas_data = root if _tag(root) == "measData" else next(
            (c for c in root if _tag(c) == "measData"), root
        )

        # Extract managedElement attributes
        managed_element = ""
        for child in meas_data:
            if _tag(child) == "managedElement":
                managed_element = child.attrib.get("localDn") or child.attrib.get("userLabel", "")
                break

        records: list[dict] = []

        for child in meas_data:
            if _tag(child) != "measInfo":
                continue

            meas_info_id = child.attrib.get("measInfoId", "")
            end_time = ""
            duration = ""

            # Build counter map: {p_index: counter_name}
            counter_map: dict[str, str] = {}

            for el in child:
                tag = _tag(el)
                if tag == "granPeriod":
                    end_time = el.attrib.get("endTime", "")
                    duration = el.attrib.get("duration", "")
                elif tag == "measType":
                    p = el.attrib.get("p", "")
                    name = (el.text or "").strip()
                    if p and name:
                        counter_map[p] = name

            # Base fields shared by all measValues in this measInfo block
            base: dict = {
                "end_time": end_time,
                "meas_info_id": meas_info_id,
            }
            if self.add_managed_element:
                base["managed_element"] = managed_element
            if self.add_duration:
                base["duration"] = duration

            for el in child:
                if _tag(el) != "measValue":
                    continue

                record = dict(base)
                record["meas_obj_ldn"] = el.attrib.get("measObjLdn", "")

                # Map <r p="N"> values to counter names
                for r_el in el:
                    if _tag(r_el) == "r":
                        p = r_el.attrib.get("p", "")
                        counter_name = counter_map.get(p, f"counter_{p}")
                        raw_val = (r_el.text or "").strip()
                        if self.numeric_values:
                            try:
                                record[counter_name] = float(raw_val)
                            except ValueError:
                                record[counter_name] = raw_val
                        else:
                            record[counter_name] = raw_val

                records.append(record)

        logger.debug("pm_xml: parsed %d records from measData", len(records))
        return records

    def serialize(self, records: list[dict]) -> bytes:
        """Serialize records back to PM XML (measData format)."""
        try:
            from lxml import etree
        except ImportError as exc:
            raise SerializerError("lxml is required for pm_xml serialization") from exc

        meas_data = etree.Element("measData")
        for rec in records:
            meas_info = etree.SubElement(meas_data, "measInfo")
            meas_info.attrib["measInfoId"] = str(rec.get("meas_info_id", ""))
            meas_val = etree.SubElement(meas_info, "measValue")
            meas_val.attrib["measObjLdn"] = str(rec.get("meas_obj_ldn", ""))
            for k, v in rec.items():
                if k not in ("end_time", "meas_info_id", "managed_element", "duration", "meas_obj_ldn"):
                    r_el = etree.SubElement(meas_val, "r")
                    r_el.text = str(v) if v is not None else ""

        return etree.tostring(meas_data, pretty_print=True, xml_declaration=True, encoding=self.encoding)
