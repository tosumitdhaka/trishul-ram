from __future__ import annotations

import textwrap

import pytest

from tram.core.exceptions import ConfigError, TransformError
from tram.pipeline.loader import load_pipeline_from_yaml
from tram.transforms.coalesce_fields import CoalesceFieldsTransform
from tram.transforms.select_from_list import SelectFromListTransform


class TestCoalesceFieldsTransform:
    def test_picks_first_non_empty_source(self):
        t = CoalesceFieldsTransform(
            {
                "fields": {
                    "served_id": {
                        "sources": ["a.msisdn", "a.imsi", "a.nai"],
                        "default": None,
                    }
                }
            }
        )
        result = t.apply([{"a": {"msisdn": "", "imsi": "3104501", "nai": "user@nai"}}])
        assert result == [{"a": {"msisdn": "", "imsi": "3104501", "nai": "user@nai"}, "served_id": "3104501"}]

    def test_uses_default_when_all_sources_empty(self):
        t = CoalesceFieldsTransform(
            {
                "fields": {
                    "served_id": {
                        "sources": ["a.msisdn", "a.imsi"],
                        "default": "UNKNOWN",
                    }
                }
            }
        )
        result = t.apply([{"a": {"msisdn": "", "imsi": None}}])
        assert result == [{"a": {"msisdn": "", "imsi": None}, "served_id": "UNKNOWN"}]


class TestSelectFromListTransform:
    def test_multi_select_projects_multiple_outputs(self):
        t = SelectFromListTransform(
            {
                "field": "subscriptionID",
                "select": [
                    {
                        "name": "imsi",
                        "match": {"subscriptionIDType": "endUserIMSI"},
                        "output": {"subscriptionIDData": "served_imsi"},
                    },
                    {
                        "name": "msisdn",
                        "match": {"subscriptionIDType": "endUserE164"},
                        "output": {"subscriptionIDData": "served_msisdn"},
                    },
                ],
            }
        )
        record = {
            "subscriptionID": [
                {"subscriptionIDType": "endUserE164", "subscriptionIDData": "15551234567"},
                {"subscriptionIDType": "endUserIMSI", "subscriptionIDData": "310450000767352"},
            ]
        }
        result = t.apply([record])
        assert result == [
            {
                "subscriptionID": record["subscriptionID"],
                "served_imsi": "310450000767352",
                "served_msisdn": "15551234567",
            }
        ]

    def test_no_match_null_fields(self):
        t = SelectFromListTransform(
            {
                "field": "subscriptionID",
                "on_no_match": "null_fields",
                "select": [
                    {
                        "name": "sip",
                        "match": {"subscriptionIDType": "endUserSIP"},
                        "output": {"subscriptionIDData": "served_sip"},
                    }
                ],
            }
        )
        result = t.apply([{"subscriptionID": []}])
        assert result == [{"subscriptionID": [], "served_sip": None}]

    def test_no_match_raise(self):
        t = SelectFromListTransform(
            {
                "field": "subscriptionID",
                "on_no_match": "raise",
                "select": [
                    {
                        "name": "sip",
                        "match": {"subscriptionIDType": "endUserSIP"},
                        "output": {"subscriptionIDData": "served_sip"},
                    }
                ],
            }
        )
        with pytest.raises(TransformError, match="no match"):
            t.apply([{"subscriptionID": []}])

    def test_first_item_projects_nested_field(self):
        t = SelectFromListTransform(
            {
                "field": "servingNodeAddress",
                "select": [
                    {
                        "name": "primary_node",
                        "first_item": True,
                        "output": {"value.value": "primary_serving_node"},
                    }
                ],
            }
        )
        record = {
            "servingNodeAddress": [
                {"type": "iPBinaryAddress", "value": {"type": "iPBinV4Address", "value": "89536066"}},
                {"type": "iPBinaryAddress", "value": {"type": "iPBinV4Address", "value": "6b4d5223"}},
            ]
        }
        result = t.apply([record])
        assert result == [{**record, "primary_serving_node": "89536066"}]

    def test_missing_projected_field_becomes_null(self):
        t = SelectFromListTransform(
            {
                "field": "subscriptionID",
                "select": [
                    {
                        "match": {"subscriptionIDType": "endUserIMSI"},
                        "output": {"missingField": "served_imsi"},
                    }
                ],
            }
        )
        record = {
            "subscriptionID": [
                {"subscriptionIDType": "endUserIMSI", "subscriptionIDData": "310450000767352"}
            ]
        }
        result = t.apply([record])
        assert result == [{**record, "served_imsi": None}]


class TestPhase2TransformConfigValidation:
    def test_loader_accepts_conditional_drop(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: test-drop
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: drop
                  fields:
                    served_sip_uri: [null, ""]
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        config = load_pipeline_from_yaml(yaml_text)
        assert config.transforms[0].type == "drop"

    def test_conditional_drop_rejects_non_list_values(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: bad-drop
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: drop
                  fields:
                    served_sip_uri: null
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        with pytest.raises(ConfigError, match="Input should be a valid list"):
            load_pipeline_from_yaml(yaml_text)

    def test_loader_accepts_project(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: test-project
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: project
                  fields:
                    served_imsi:
                      source_any: [servedIMSI, subscription.imsi]
                    apn: accessPointNameNI
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        config = load_pipeline_from_yaml(yaml_text)
        assert config.transforms[0].type == "project"

    def test_project_config_rejects_invalid_source_mode(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: bad-project
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: project
                  fields:
                    served_imsi:
                      required: true
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        with pytest.raises(ConfigError, match="exactly one of source or source_any must be set"):
            load_pipeline_from_yaml(yaml_text)

    def test_loader_accepts_json_flatten(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: test-json-flatten
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: json_flatten
                  explode_paths: [records]
                  choice_unwrap:
                    paths: [address]
                    mode: both
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        config = load_pipeline_from_yaml(yaml_text)
        assert config.transforms[0].type == "json_flatten"

    def test_loader_accepts_phase2_transforms(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: test-phase2
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: select_from_list
                  field: subscriptionID
                  select:
                    - name: imsi
                      match:
                        subscriptionIDType: endUserIMSI
                      output:
                        subscriptionIDData: served_imsi
                - type: coalesce_fields
                  fields:
                    served_id:
                      sources: [served_msisdn, served_imsi]
                      default: null
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        config = load_pipeline_from_yaml(yaml_text)
        assert config.transforms[0].type == "select_from_list"
        assert config.transforms[1].type == "coalesce_fields"

    def test_select_from_list_rejects_duplicate_output_fields(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: bad-dup-outputs
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: select_from_list
                  field: subscriptionID
                  select:
                    - match:
                        subscriptionIDType: endUserIMSI
                      output:
                        subscriptionIDData: served_id
                    - match:
                        subscriptionIDType: endUserE164
                      output:
                        subscriptionIDData: served_id
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        with pytest.raises(ConfigError, match="duplicate output field"):
            load_pipeline_from_yaml(yaml_text)

    def test_select_from_list_requires_exactly_one_selector_mode(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: bad-selector-mode
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: select_from_list
                  field: subscriptionID
                  select:
                    - match:
                        subscriptionIDType: endUserIMSI
                      first_item: true
                      output:
                        subscriptionIDData: served_imsi
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        with pytest.raises(ConfigError, match="exactly one of match or first_item=true"):
            load_pipeline_from_yaml(yaml_text)

    def test_select_from_list_allows_empty_match_dict(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: empty-match-ok
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: select_from_list
                  field: subscriptionID
                  select:
                    - match: {}
                      output:
                        subscriptionIDData: served_id
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        config = load_pipeline_from_yaml(yaml_text)
        assert config.transforms[0].type == "select_from_list"

    def test_coalesce_fields_requires_non_empty_sources(self):
        yaml_text = textwrap.dedent(
            """
            pipeline:
              name: bad-coalesce-sources
              source:
                type: local
                path: /tmp/in
              serializer_in:
                type: json
              transforms:
                - type: coalesce_fields
                  fields:
                    served_id:
                      sources: []
              serializer_out:
                type: json
              sink:
                type: local
                path: /tmp/out
            """
        )
        with pytest.raises(ConfigError, match="sources must not be empty"):
            load_pipeline_from_yaml(yaml_text)
