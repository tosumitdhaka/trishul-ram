"""Tests for the json_flatten transform."""

from __future__ import annotations

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.json_flatten import JsonFlattenTransform


class TestJsonFlattenTransform:
    def test_auto_expands_nested_list_of_dicts(self):
        transform = JsonFlattenTransform({"explode_mode": "auto"})
        records = [{
            "measFileHeader": {"fileFormatVersion": 1},
            "measData": [
                {"nEId": {"name": "A"}, "value": 10},
                {"nEId": {"name": "B"}, "value": 20},
            ],
        }]

        result = transform.apply(records)

        assert result == [
            {"fileFormatVersion": 1, "name": "A", "value": 10},
            {"fileFormatVersion": 1, "name": "B", "value": 20},
        ]

    def test_choice_mode_unwrap_value(self):
        transform = JsonFlattenTransform({"choice_mode": "unwrap_value"})
        result = transform.apply([{"event": {"type": "iValue", "value": 7}}])
        assert result == [{"event": 7}]

    def test_choice_mode_type_value_hoists_choice_fields(self):
        transform = JsonFlattenTransform({"choice_mode": "type_value"})
        result = transform.apply([{"event": {"type": "iValue", "value": 7}}])
        assert result == [{"type": "iValue", "value": 7}]

    def test_choice_mode_keep_preserves_choice_under_field(self):
        transform = JsonFlattenTransform({"choice_mode": "keep"})
        result = transform.apply([{"event": {"type": "iValue", "value": 7}}])
        assert result == [{"event": {"type": "iValue", "value": 7}}]

    def test_zip_lists_auto_pairs_scalar_and_value_lists(self):
        transform = JsonFlattenTransform({"zip_lists": "auto", "choice_mode": "type_value"})
        records = [{
            "measTypes": ["cpu", "mem"],
            "measResults": [
                {"type": "iValue", "value": 10},
                {"type": "iValue", "value": 20},
            ],
        }]

        result = transform.apply(records)

        assert result == [
            {"measTypes": "cpu", "type": "iValue", "value": 10},
            {"measTypes": "mem", "type": "iValue", "value": 20},
        ]

    def test_ambiguity_mode_keep_preserves_multiple_auto_explode_candidates(self):
        transform = JsonFlattenTransform({"ambiguity_mode": "keep"})
        records = [{"a": [{"x": 1}], "b": [{"y": 2}]}]

        result = transform.apply(records)

        assert result == records

    def test_ambiguity_mode_error_raises(self):
        transform = JsonFlattenTransform({"ambiguity_mode": "error"})
        with pytest.raises(TransformError, match="ambiguous"):
            transform.apply([{"a": [{"x": 1}], "b": [{"y": 2}]}])

    def test_rename_style_snake_case(self):
        transform = JsonFlattenTransform({"rename_style": "snake_case"})
        result = transform.apply([{"measFileHeader": {"fileFormatVersion": 1}}])
        assert result == [{"file_format_version": 1}]

    def test_max_depth_preserves_nested_dict_beyond_limit(self):
        transform = JsonFlattenTransform({"max_depth": 1})
        result = transform.apply([{"outer": {"inner": {"leaf": 1}}}])
        assert result == [{"inner": {"leaf": 1}}]
