"""Tests for plugin registry."""

import pytest

from tram.registry.registry import (
    get_serializer,
    get_sink,
    get_source,
    get_transform,
    list_plugins,
)
from tram.core.exceptions import PluginNotFoundError


def test_list_plugins_returns_all_categories():
    plugins = list_plugins()
    assert "sources" in plugins
    assert "sinks" in plugins
    assert "transforms" in plugins
    assert "serializers" in plugins


def test_all_expected_sources_registered():
    plugins = list_plugins()
    assert "sftp" in plugins["sources"]


def test_all_expected_sinks_registered():
    plugins = list_plugins()
    assert "sftp" in plugins["sinks"]


def test_all_expected_serializers_registered():
    plugins = list_plugins()
    assert "json" in plugins["serializers"]
    assert "csv" in plugins["serializers"]
    assert "xml" in plugins["serializers"]


def test_all_expected_transforms_registered():
    plugins = list_plugins()
    expected = {"rename", "cast", "add_field", "drop", "value_map", "filter"}
    assert expected.issubset(set(plugins["transforms"]))


def test_get_source_returns_class():
    cls = get_source("sftp")
    assert cls is not None
    assert hasattr(cls, "read")


def test_get_sink_returns_class():
    cls = get_sink("sftp")
    assert cls is not None
    assert hasattr(cls, "write")


def test_get_serializer_returns_class():
    cls = get_serializer("json")
    assert cls is not None
    assert hasattr(cls, "parse")
    assert hasattr(cls, "serialize")


def test_get_transform_returns_class():
    cls = get_transform("rename")
    assert cls is not None
    assert hasattr(cls, "apply")


def test_get_source_unknown_raises():
    with pytest.raises(PluginNotFoundError):
        get_source("nonexistent_source_xyz")


def test_get_transform_unknown_raises():
    with pytest.raises(PluginNotFoundError):
        get_transform("nonexistent_transform_xyz")
