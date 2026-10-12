"""Tests for AI schema context helper."""

from tram.api.routers.ai_docs import build_ai_context, build_template_examples


def test_build_ai_context_uses_schema_defaults_when_plugins_missing():
    context = build_ai_context("read from kafka and write to local", {})

    assert "CRITICAL RULES" in context
    assert "SOURCES:" in context
    assert "SINKS:" in context
    assert "SERIALIZERS" in context
    assert "TRANSFORMS" in context
    assert "kafka" in context
    assert "local" in context


# ── A6: template-grounded generation (few-shot selection) ────────────────────


def _template(name, source, sinks, yaml_text):
    return {
        "id": name,
        "name": name,
        "description": "",
        "tags": [source] + sinks + ["interval"],
        "source_type": source,
        "sink_types": sinks,
        "schedule_type": "interval",
        "yaml": yaml_text,
    }


def test_build_template_examples_matches_source_and_sink_tags():
    templates = [
        _template("kafka-to-local", "kafka", ["local"], "name: kafka-to-local\n..."),
        _template("sftp-to-s3", "sftp", ["s3"], "name: sftp-to-s3\n..."),
    ]
    out = build_template_examples(templates, "read kafka, write to local")
    assert "WORKED TEMPLATE EXAMPLES" in out
    assert "Template: kafka-to-local" in out
    assert "Template: sftp-to-s3" not in out  # no tag match → excluded


def test_build_template_examples_prefers_best_match():
    templates = [
        _template("sink-only", "rest", ["local"], "name: sink-only"),
        _template("full-match", "kafka", ["local"], "name: full-match"),
    ]
    out = build_template_examples(templates, "kafka to local")
    # full-match scores 2 (source + sink); sink-only scores 1
    assert out.index("Template: full-match") < out.index("Template: sink-only")


def test_build_template_examples_empty_when_no_match():
    templates = [_template("sftp-to-s3", "sftp", ["s3"], "name: x")]
    assert build_template_examples(templates, "read kafka") == ""
    assert build_template_examples([], "anything") == ""


def test_build_template_examples_bounds_count_and_size():
    big = "name: big\n" + ("# pad\n" * 800)  # ~4 KB — exceeds the per-template cap
    templates = [_template(f"t{i}", "kafka", ["local"], big) for i in range(6)]
    out = build_template_examples(templates, "kafka local")
    assert "### Template:" in out
    # count capped at 3, and the total section stays within the total cap
    # (per-template truncation to 1200 chars, total ≤ 3000 + header)
    assert out.count("### Template:") <= 3
    assert "… (truncated)" in out
    assert len(out) < 3200


def test_build_template_examples_includes_name_and_content():
    yaml_text = "name: kafka-to-local\nschedule:\n  type: manual\n"
    templates = [_template("kafka-to-local", "kafka", ["local"], yaml_text)]
    out = build_template_examples(templates, "kafka to local")
    assert "### Template: kafka-to-local" in out
    assert "name: kafka-to-local" in out  # the worked YAML content itself

def test_context_uses_server_registry_not_client_plugin_lists(monkeypatch):
    import tram.api.routers.ai_docs as ai_docs

    monkeypatch.setattr(ai_docs, "list_plugins", lambda: {
        "sources": ["snmp_poll"], "sinks": ["local"],
        "serializers": ["json"], "transforms": ["filter"],
    })
    context = build_ai_context("snmp to local", {"sources": ["invented"], "transforms": []})
    assert "invented" not in context
    assert "snmp_poll:" in context
    assert "filter:" in context
    assert "condition: str  # required" in context
    assert "filename_template:" in context


def test_context_explains_runtime_shapes_and_expression_literals(monkeypatch):
    import tram.api.routers.ai_docs as ai_docs

    monkeypatch.setattr(ai_docs, "list_plugins", lambda: {
        "sources": ["gnmi", "snmp_poll"], "sinks": ["local"],
        "serializers": ["json"], "transforms": ["aggregate", "select_from_list", "project"],
    })
    context = build_ai_context("gnmi and snmp to local", {})
    for guidance in (
        "_metrics.ifInOctets", "_labels.ifDescr", "_index", "shell-style globs",
        "schedule type interval", "schedule type stream", "'op:field'",
        "non-null values", "output: {item.path: output_field}",
        "existing null is preserved", "{field.device.name}", "\"'tokyo'\"",
    ):
        assert guidance in context



def test_context_loads_runtime_plugins_in_a_fresh_process():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", """
from tram.api.routers.ai_docs import build_ai_context
context = build_ai_context('SNMP SFTP gNMI polling', {
    'sources': [], 'sinks': [], 'transforms': [], 'serializers': [],
})
for schema in ('snmp_poll:', 'sftp:', 'gnmi:', 'json:', 'filter:'):
    assert schema in context, schema
assert 'filename_template:' in context
assert 'condition: str  # required' in context
"""],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
