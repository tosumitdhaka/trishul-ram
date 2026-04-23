from __future__ import annotations

import pytest

from tram.core.exceptions import TransformError
from tram.transforms.path_utils import delete_path, get_path, rename_path, set_path


class TestPathUtils:
    def test_get_path_existing_top_level(self):
        found, value = get_path({"a": 1}, "a")
        assert found is True
        assert value == 1

    def test_get_path_existing_nested(self):
        found, value = get_path({"a": {"b": {"c": 1}}}, "a.b.c")
        assert found is True
        assert value == 1

    def test_get_path_missing_nested(self):
        found, value = get_path({"a": {}}, "a.b")
        assert found is False
        assert value is None

    def test_set_path_creates_intermediates(self):
        record = {}
        set_path(record, "a.b.c", 1)
        assert record == {"a": {"b": {"c": 1}}}

    def test_set_path_non_dict_intermediate_raises(self):
        with pytest.raises(TransformError):
            set_path({"a": 5}, "a.b", 1)

    def test_delete_path_existing_nested(self):
        record = {"a": {"b": {"c": 1}}}
        assert delete_path(record, "a.b.c") is True
        assert record == {"a": {"b": {}}}

    def test_delete_path_missing_nested(self):
        assert delete_path({"a": {}}, "a.b") is False

    def test_delete_path_non_dict_intermediate_is_false(self):
        assert delete_path({"a": 5}, "a.b") is False

    def test_rename_path_nested_to_top_level(self):
        record = {"a": {"b": 1}}
        assert rename_path(record, "a.b", "c") is True
        assert record == {"a": {}, "c": 1}

    def test_rename_path_top_level_to_nested(self):
        record = {"a": 1}
        assert rename_path(record, "a", "b.c") is True
        assert record == {"b": {"c": 1}}

    def test_rename_path_nested_to_nested(self):
        record = {"a": {"b": 1}, "x": 2}
        assert rename_path(record, "a.b", "c.d") is True
        assert record == {"a": {}, "c": {"d": 1}, "x": 2}

    def test_rename_path_missing_source(self):
        record = {"a": 1}
        assert rename_path(record, "x.y", "b") is False
        assert record == {"a": 1}
