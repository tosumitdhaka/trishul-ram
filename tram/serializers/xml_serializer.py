"""XML serializer — defusedxml for safe parsing, lxml for output."""

from __future__ import annotations

from tram.core.exceptions import SerializerError
from tram.interfaces.base_serializer import BaseSerializer
from tram.registry.registry import register_serializer


@register_serializer("xml")
class XmlSerializer(BaseSerializer):
    """Serialize/deserialize XML data.

    Parsing uses defusedxml to prevent XXE attacks.
    Serialization uses lxml for standards-compliant output.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.root_element: str = config.get("root_element", "records")
        self.record_element: str = config.get("record_element", "record")
        self.encoding: str = config.get("encoding", "utf-8")

    def parse(self, data: bytes) -> list[dict]:
        try:
            import defusedxml.ElementTree as ET
            root = ET.fromstring(data.decode(self.encoding))
        except Exception as exc:
            raise SerializerError(f"XML parse error: {exc}") from exc

        records: list[dict] = []
        # Support two layouts:
        #   1. Root has direct child elements that are records
        #   2. Root is a wrapper and children match record_element
        children = list(root)
        if not children:
            # Root itself is a single record
            return [self._element_to_dict(root)]

        for child in children:
            records.append(self._element_to_dict(child))
        return records

    def _element_to_dict(self, element) -> dict:
        """Convert an XML element to a dict (attributes + child text)."""
        result: dict = dict(element.attrib)
        for child in element:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag  # strip namespace
            result[tag] = child.text or ""
        if not list(element) and element.text:
            result["_text"] = element.text
        return result

    def serialize(self, records: list[dict]) -> bytes:
        try:
            from lxml import etree
            root = etree.Element(self.root_element)
            for rec in records:
                child = etree.SubElement(root, self.record_element)
                for key, val in rec.items():
                    field = etree.SubElement(child, str(key))
                    field.text = str(val) if val is not None else ""
            return etree.tostring(
                root,
                pretty_print=True,
                xml_declaration=True,
                encoding=self.encoding,
            )
        except Exception as exc:
            raise SerializerError(f"XML serialize error: {exc}") from exc
