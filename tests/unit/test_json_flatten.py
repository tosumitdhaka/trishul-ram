"""Tests for the json_flatten transform."""

from __future__ import annotations

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.json_flatten import JsonFlattenTransform


class TestJsonFlattenTransform:
    def test_explode_paths_use_current_row_state(self):
        transform = JsonFlattenTransform({
            "explode_paths": ["listOfServiceData", "listOfTrafficVolumes"],
        })
        records = [{
            "recordType": "pgw",
            "listOfServiceData": [
                {
                    "serviceId": "svc-a",
                    "listOfTrafficVolumes": [{"bytes": 1}, {"bytes": 2}],
                },
                {
                    "serviceId": "svc-b",
                    "listOfTrafficVolumes": [{"bytes": 3}],
                },
            ],
        }]

        result = transform.apply(records)

        assert result == [
            {"recordType": "pgw", "serviceId": "svc-a", "bytes": 1},
            {"recordType": "pgw", "serviceId": "svc-a", "bytes": 2},
            {"recordType": "pgw", "serviceId": "svc-b", "bytes": 3},
        ]

    def test_missing_explode_path_keeps_row_when_keep_empty_rows_true(self):
        transform = JsonFlattenTransform({
            "explode_paths": ["missingList"],
            "keep_empty_rows": True,
        })

        assert transform.apply([{"recordType": "lte"}]) == [{"recordType": "lte"}]

    def test_missing_explode_path_drops_row_when_keep_empty_rows_false(self):
        transform = JsonFlattenTransform({
            "explode_paths": ["missingList"],
            "keep_empty_rows": False,
        })

        assert transform.apply([{"recordType": "lte"}]) == []

    def test_non_list_explode_path_raises(self):
        transform = JsonFlattenTransform({"explode_paths": ["serviceData"]})

        with pytest.raises(TransformError, match="not a list"):
            transform.apply([{"serviceData": {"x": 1}}])

    def test_zip_groups_emit_positionally_zipped_rows(self):
        transform = JsonFlattenTransform({
            "zip_groups": [{
                "fields": {
                    "measTypes": "meas_type",
                    "measResults": "meas_result",
                },
            }],
        })
        records = [{
            "node": "n1",
            "measTypes": ["cpu", "mem"],
            "measResults": [10, 20],
        }]

        result = transform.apply(records)

        assert result == [
            {"node": "n1", "meas_type": "cpu", "meas_result": 10},
            {"node": "n1", "meas_type": "mem", "meas_result": 20},
        ]

    def test_choice_unwrap_both_keeps_type_and_value(self):
        transform = JsonFlattenTransform({
            "choice_unwrap": {
                "paths": ["pGWAddress"],
                "mode": "both",
                "type_suffix": "_type",
                "value_suffix": "",
            },
        })

        result = transform.apply([{
            "pGWAddress": {"type": "ipv4", "value": {"host": "1.2.3.4"}},
        }])

        assert result == [{
            "pGWAddress_type": "ipv4",
            "pGWAddress.host": "1.2.3.4",
        }]

    def test_choice_unwrap_value_replaces_choice_with_value_only(self):
        transform = JsonFlattenTransform({
            "choice_unwrap": {
                "paths": ["pGWAddress"],
                "mode": "value",
            },
        })

        result = transform.apply([{
            "pGWAddress": {"type": "ipv4", "value": {"host": "1.2.3.4"}},
        }])

        assert result == [{"pGWAddress.host": "1.2.3.4"}]

    def test_preserve_lists_false_raises_for_unexploded_list(self):
        transform = JsonFlattenTransform({"preserve_lists": False})

        with pytest.raises(TransformError, match="preserve_lists=true"):
            transform.apply([{"serviceIds": ["a", "b"]}])

    def test_drop_paths_uses_final_flattened_keys(self):
        transform = JsonFlattenTransform({
            "drop_paths": ["diagnostics.note"],
        })

        result = transform.apply([{
            "diagnostics": {"note": "drop-me"},
            "recordType": "sgw",
        }])

        assert result == [{"recordType": "sgw"}]

    def test_drop_paths_support_single_segment_wildcards(self):
        transform = JsonFlattenTransform({
            "drop_paths": ["*.note"],
        })

        result = transform.apply([{
            "diagnostics": {"note": "drop-me"},
            "details": {"note": "drop-me-too"},
            "recordType": "sgw",
        }])

        assert result == [{"recordType": "sgw"}]

    def test_drop_paths_wildcards_do_not_match_deeper_suffix_paths(self):
        transform = JsonFlattenTransform({
            "drop_paths": ["*.note"],
        })

        result = transform.apply([{
            "outer": {"diagnostics": {"note": "keep-me"}},
            "recordType": "sgw",
        }])

        assert result == [{"outer.diagnostics.note": "keep-me", "recordType": "sgw"}]

    def test_max_depth_preserves_subtree_beyond_limit(self):
        transform = JsonFlattenTransform({"max_depth": 1})

        result = transform.apply([{"outer": {"inner": {"leaf": 1}}}])

        assert result == [{"outer.inner": {"leaf": 1}}]
