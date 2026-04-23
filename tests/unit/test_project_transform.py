"""Tests for the project transform."""

from __future__ import annotations

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.project import ProjectTransform


class TestProjectTransform:
    def test_simple_string_rule_projects_value(self):
        transform = ProjectTransform({"fields": {"record_type": "recordType"}})

        result = transform.apply([{"recordType": "pgw", "extra": 1}])

        assert result == [{"record_type": "pgw"}]

    def test_expanded_rule_uses_default_when_source_missing(self):
        transform = ProjectTransform({
            "fields": {
                "served_msisdn": {"source": "servedMSISDN", "default": None},
            }
        })

        result = transform.apply([{"recordType": "lte"}])

        assert result == [{"served_msisdn": None}]

    def test_required_field_raises_when_unresolved(self):
        transform = ProjectTransform({
            "fields": {
                "served_imsi": {"source": "servedIMSI", "required": True},
            }
        })

        with pytest.raises(TransformError, match="required field 'served_imsi'"):
            transform.apply([{"recordType": "lte"}])

    def test_dotted_path_extracts_nested_value(self):
        transform = ProjectTransform({
            "fields": {
                "realm": {"source": "policy.realm"},
            }
        })

        result = transform.apply([{"policy": {"realm": "epc.example.org"}}])

        assert result == [{"realm": "epc.example.org"}]

    def test_source_any_picks_first_found_path(self):
        transform = ProjectTransform({
            "fields": {
                "served_imsi": {
                    "source_any": ["servedIMSI", "subscription.imsi", "fallback.imsi"],
                }
            }
        })

        result = transform.apply([{"subscription": {"imsi": "310450000767352"}}])

        assert result == [{"served_imsi": "310450000767352"}]

    def test_source_any_returns_none_value_if_path_exists(self):
        transform = ProjectTransform({
            "fields": {
                "served_imsi": {
                    "source_any": ["servedIMSI", "subscription.imsi"],
                    "default": "fallback",
                }
            }
        })

        result = transform.apply([{"subscription": {"imsi": None}}])

        assert result == [{"served_imsi": None}]

    def test_default_takes_precedence_over_required(self):
        transform = ProjectTransform({
            "fields": {
                "served_imsi": {
                    "source": "servedIMSI",
                    "default": "fallback",
                    "required": True,
                }
            }
        })

        result = transform.apply([{"recordType": "lte"}])

        assert result == [{"served_imsi": "fallback"}]
