"""Unit tests for PmXmlSerializer (3GPP PM XML / measData format)."""

from __future__ import annotations

import pytest

from tram.core.exceptions import SerializerError
from tram.serializers.pm_xml_serializer import PmXmlSerializer

# Minimal valid measData XML
_BASIC_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<measData>
  <managedElement localDn="SubNetwork=ONRM_ROOT_MO,MeContext=NodeB1"/>
  <measInfo measInfoId="PM_IF">
    <granPeriod endTime="2026-04-09T10:15:00Z" duration="PT15M"/>
    <measType p="1">ifInOctets</measType>
    <measType p="2">ifOutOctets</measType>
    <measValue measObjLdn="ManagedElement=1,ENodeBFunction=1">
      <r p="1">1000</r>
      <r p="2">2000</r>
    </measValue>
    <measValue measObjLdn="ManagedElement=1,ENodeBFunction=2">
      <r p="1">3000</r>
      <r p="2">4000</r>
    </measValue>
  </measInfo>
</measData>
"""

_TWO_MEAS_INFO_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<measData>
  <managedElement localDn="SubNetwork=Test"/>
  <measInfo measInfoId="PM_A">
    <granPeriod endTime="2026-04-09T10:00:00Z" duration="PT5M"/>
    <measType p="1">counter_a</measType>
    <measValue measObjLdn="obj=1">
      <r p="1">42</r>
    </measValue>
  </measInfo>
  <measInfo measInfoId="PM_B">
    <granPeriod endTime="2026-04-09T10:05:00Z" duration="PT5M"/>
    <measType p="1">counter_b</measType>
    <measValue measObjLdn="obj=2">
      <r p="1">99</r>
    </measValue>
  </measInfo>
</measData>
"""


class TestPmXmlSerializerBasic:
    def test_parse_returns_records(self):
        s = PmXmlSerializer({})
        records = s.parse(_BASIC_XML)
        assert len(records) == 2

    def test_counter_values_numeric_by_default(self):
        s = PmXmlSerializer({})
        records = s.parse(_BASIC_XML)
        assert records[0]["ifInOctets"] == 1000.0
        assert records[0]["ifOutOctets"] == 2000.0
        assert records[1]["ifInOctets"] == 3000.0
        assert records[1]["ifOutOctets"] == 4000.0

    def test_counter_values_string_when_numeric_false(self):
        s = PmXmlSerializer({"numeric_values": False})
        records = s.parse(_BASIC_XML)
        assert records[0]["ifInOctets"] == "1000"

    def test_end_time_populated(self):
        s = PmXmlSerializer({})
        records = s.parse(_BASIC_XML)
        assert records[0]["end_time"] == "2026-04-09T10:15:00Z"

    def test_meas_info_id_populated(self):
        s = PmXmlSerializer({})
        records = s.parse(_BASIC_XML)
        assert records[0]["meas_info_id"] == "PM_IF"

    def test_meas_obj_ldn_populated(self):
        s = PmXmlSerializer({})
        records = s.parse(_BASIC_XML)
        assert records[0]["meas_obj_ldn"] == "ManagedElement=1,ENodeBFunction=1"
        assert records[1]["meas_obj_ldn"] == "ManagedElement=1,ENodeBFunction=2"


class TestPmXmlSerializerManagedElement:
    def test_managed_element_included_by_default(self):
        s = PmXmlSerializer({})
        records = s.parse(_BASIC_XML)
        assert "managed_element" in records[0]
        assert records[0]["managed_element"] == "SubNetwork=ONRM_ROOT_MO,MeContext=NodeB1"

    def test_managed_element_excluded(self):
        s = PmXmlSerializer({"add_managed_element": False})
        records = s.parse(_BASIC_XML)
        assert "managed_element" not in records[0]

    def test_duration_excluded_by_default(self):
        s = PmXmlSerializer({})
        records = s.parse(_BASIC_XML)
        assert "duration" not in records[0]

    def test_duration_included(self):
        s = PmXmlSerializer({"add_duration": True})
        records = s.parse(_BASIC_XML)
        assert records[0]["duration"] == "PT15M"


class TestPmXmlSerializerMultipleMeasInfo:
    def test_two_meas_info_blocks(self):
        s = PmXmlSerializer({})
        records = s.parse(_TWO_MEAS_INFO_XML)
        assert len(records) == 2
        assert records[0]["meas_info_id"] == "PM_A"
        assert records[1]["meas_info_id"] == "PM_B"
        assert records[0]["counter_a"] == 42.0
        assert records[1]["counter_b"] == 99.0

    def test_each_record_has_correct_end_time(self):
        s = PmXmlSerializer({})
        records = s.parse(_TWO_MEAS_INFO_XML)
        assert records[0]["end_time"] == "2026-04-09T10:00:00Z"
        assert records[1]["end_time"] == "2026-04-09T10:05:00Z"


class TestPmXmlSerializerTruncatedFile:
    def test_truncated_file_auto_closed(self):
        # File cut off mid-way — should still parse what's there
        truncated = b"""<?xml version="1.0" encoding="utf-8"?>
<measData>
  <measInfo measInfoId="PM_T">
    <granPeriod endTime="2026-01-01T00:00:00Z" duration="PT1M"/>
    <measType p="1">rx</measType>
    <measValue measObjLdn="obj=1">
      <r p="1">500</r>
    </measValue>"""
        s = PmXmlSerializer({})
        records = s.parse(truncated)
        assert len(records) == 1
        assert records[0]["rx"] == 500.0

    def test_invalid_xml_raises_serializer_error(self):
        s = PmXmlSerializer({})
        with pytest.raises(SerializerError, match="XML parse error"):
            s.parse(b"not xml at all <<<>>>")


class TestPmXmlSerializerNonNumericValues:
    def test_non_numeric_counter_kept_as_string(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
<measData>
  <measInfo measInfoId="PM_X">
    <granPeriod endTime="2026-01-01T00:00:00Z" duration="PT1M"/>
    <measType p="1">status</measType>
    <measValue measObjLdn="obj=1">
      <r p="1">ACTIVE</r>
    </measValue>
  </measInfo>
</measData>
"""
        s = PmXmlSerializer({"numeric_values": True})
        records = s.parse(xml)
        assert records[0]["status"] == "ACTIVE"

    def test_empty_measdata_returns_empty_list(self):
        xml = b"<?xml version=\"1.0\"?><measData><managedElement localDn=\"ME=1\"/></measData>"
        s = PmXmlSerializer({})
        assert s.parse(xml) == []

    def test_unknown_p_index_gets_fallback_name(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
<measData>
  <measInfo measInfoId="PM_Y">
    <granPeriod endTime="2026-01-01T00:00:00Z" duration="PT1M"/>
    <measValue measObjLdn="obj=1">
      <r p="99">777</r>
    </measValue>
  </measInfo>
</measData>
"""
        s = PmXmlSerializer({})
        records = s.parse(xml)
        assert records[0].get("counter_99") == 777.0


class TestPmXmlSerializerNamespaced:
    def test_namespaced_tags_parsed(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
<measCollecFile xmlns="http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measData">
  <measData>
    <managedElement localDn="NE=1"/>
    <measInfo measInfoId="PM_NS">
      <granPeriod endTime="2026-04-09T00:00:00Z" duration="PT1M"/>
      <measType p="1">cpu</measType>
      <measValue measObjLdn="obj=1">
        <r p="1">80</r>
      </measValue>
    </measInfo>
  </measData>
</measCollecFile>
"""
        s = PmXmlSerializer({})
        records = s.parse(xml)
        assert len(records) == 1
        assert records[0]["cpu"] == 80.0
