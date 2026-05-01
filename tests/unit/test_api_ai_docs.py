"""Tests for AI schema context helper."""

from tram.api.routers.ai_docs import build_ai_context


def test_build_ai_context_uses_schema_defaults_when_plugins_missing():
    context = build_ai_context("read from kafka and write to local", {})

    assert "CRITICAL RULES" in context
    assert "SOURCES:" in context
    assert "SINKS:" in context
    assert "SERIALIZERS" in context
    assert "TRANSFORMS" in context
    assert "kafka" in context
    assert "local" in context
