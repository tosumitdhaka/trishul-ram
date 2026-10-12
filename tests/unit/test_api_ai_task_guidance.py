"""Runtime checks for task-specific examples and their final prompt placement."""

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from pydantic import TypeAdapter

from tram.api.routers.ai_docs import build_task_guidance
from tram.models.pipeline import SourceConfig, TransformConfig
from tram.registry.registry import get_transform


def _example(text: str) -> dict:
    return yaml.safe_load(re.search(r"```yaml\n(.*?)\n```", text, re.DOTALL).group(1))


def _apply_example(example: dict, records: list[dict]) -> list[dict]:
    import tram.transforms  # noqa: F401

    for raw in example["transforms"]:
        config = TypeAdapter(TransformConfig).validate_python(raw)
        records = get_transform(config.type)(config.model_dump()).apply(records)
    return records


def test_selection_example_defaults_missing_and_null_without_changing_zero():
    example = _example(build_task_guidance("select nested list with a zero default"))
    records = [
        {"interfaces": [
            {"name": "other", "stats": {"rx": 99}},
            {"name": "eth0", "stats": {"rx": 7}}, {"name": "eth0", "stats": {"rx": 99}},
        ]},
        {"interfaces": [{"name": "other", "stats": {"rx": 99}}]},
        {"interfaces": [{"name": "eth0"}]},
        {"interfaces": [{"name": "eth0", "stats": {"rx": None}}]},
        {"interfaces": [{"name": "eth0", "stats": {"rx": 0}}]},
    ]
    assert _apply_example(example, records) == [
        {"rx": 7}, {"rx": 0}, {"rx": 0}, {"rx": 0}, {"rx": 0},
    ]


def test_projection_default_preserves_existing_null():
    example = {"transforms": [{
        "type": "project", "fields": {"rx": {"source": "rx", "default": 0}},
    }]}
    assert _apply_example(example, [{"rx": None}, {}]) == [{"rx": None}, {"rx": 0}]


def test_record_count_example_groups_a_local_json_array_through_executor(tmp_path):
    import json

    from tram.core.context import RunStatus
    from tram.pipeline.executor import PipelineExecutor
    from tram.pipeline.loader import load_pipeline_from_yaml

    example = _example(build_task_guidance("group and sum bytes and count records"))
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    records = [
        {"device": "interface", "bytes": 10}, {"device": "interface", "bytes": None},
        {"device": "interface", "bytes": 7}, {"device": "endpoint", "bytes": None},
        {"device": "interface"},
    ]
    (input_dir / "fixture.json").write_text(json.dumps(records))
    output_dir = tmp_path / "output"
    example.update({
        "name": "guidance-aggregate", "schedule": {"type": "manual"},
        "source": {"type": "local", "path": str(input_dir), "file_pattern": "*.json"},
        "serializer_in": {"type": "json"},
    })
    example["sinks"][0].update({"path": str(output_dir), "filename_template": "fixture.ndjson"})
    config = load_pipeline_from_yaml(yaml.safe_dump(example))
    result = PipelineExecutor().batch_run(config)
    assert result.status == RunStatus.SUCCESS, result.error
    rows = [json.loads(line) for line in (output_dir / "fixture.ndjson").read_text().splitlines()]
    assert rows == [
        {"device": "interface", "bytes_total": 17, "records": 4},
        {"device": "endpoint", "bytes_total": None, "records": 1},
    ]


def test_snmp_example_explicitly_loads_mibs_for_symbolic_labels():
    from tram.connectors.snmp.source import SNMPPollSource

    example = _example(build_task_guidance("SNMP interface type categories"))
    config = TypeAdapter(SourceConfig).validate_python(example["source"])
    source = SNMPPollSource(config.model_dump())
    sentinel = object()
    with patch("tram.connectors.snmp.mib_utils.get_mib_view", return_value=sentinel) as load:
        assert source._load_mib_view() is sentinel
    load.assert_called_once_with(["/mibs"], ["IF-MIB", "IANAifType-MIB"])
    assert source.index_depth == 1
    assert source.yield_rows and source.classify
    with patch("tram.connectors.snmp.source.prepend_system_mib_dirs", side_effect=lambda dirs: dirs):
        ungrounded = SNMPPollSource({
            **config.model_dump(), "mib_modules": [], "mib_dirs": [],
        })
    assert ungrounded.resolve_oids is True
    assert ungrounded._load_mib_view() is None



def test_snmp_enum_and_explicit_label_patterns_have_different_matching_values():
    from tram.connectors.snmp.source import SNMPPollSource

    bindings = {"ifType": ("6", "Integer32")}
    classified = SNMPPollSource._classify_bindings(
        bindings, enum_names={"ifType": "ethernetCsmacd"},
    )
    assert classified["_labels"]["ifType"] == "ethernetCsmacd (6)"
    forced = SNMPPollSource._classify_bindings(
        bindings, label_patterns=("ifType",), enum_names={"ifType": "ethernetCsmacd"},
    )
    assert forced["_labels"]["ifType"] == "6"


def test_task_guidance_is_relevant_and_bounded():
    plain = build_task_guidance("copy kafka messages to local")
    assert "```yaml" not in plain
    assert "Nested selection" in build_task_guidance("select nested list")
    assert "Aggregation and record counts" in build_task_guidance("group and count")
    assert "SNMP symbolic" in build_task_guidance("ifType category")
    assert len(build_task_guidance("snmp nested list project default count group sum")) < 4500


@pytest.mark.parametrize("mode", ["generate", "fix", "modify"])
def test_task_guidance_is_last_in_each_yaml_mode(monkeypatch, mode):
    from tram.api.routers import ai

    task = "snmp kafka nested list default group count"
    pipeline_yaml = """
name: existing
source: {type: local, path: /input}
serializer_in: {type: json}
sinks: [{type: local, path: /output}]
"""
    body = {
        "mode": mode, "prompt": task, "instruction": task, "error": task,
        "yaml": pipeline_yaml, "plugins": {"sources": [], "transforms": []},
    }
    captured = {}

    async def request_json():
        return body

    async def templates(_request):
        return [{
            "name": "kafka-example", "source_type": "kafka", "sink_types": ["local"],
            "yaml": pipeline_yaml,
        }]

    async def retry(_request, _cfg, system, _user, **_kwargs):
        captured["system"] = system
        return {"yaml": pipeline_yaml}

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=None)), json=request_json,
    )
    monkeypatch.setattr(ai, "_get_ai_cfg", lambda _db: {"api_key": "synthetic"})
    monkeypatch.setattr(ai, "_bundled_templates", templates)
    monkeypatch.setattr(ai, "_run_yaml_with_retry", retry)
    monkeypatch.setattr(ai, "_run_fix_with_retry", retry)
    asyncio.run(ai.ai_suggest(request))

    system = captured["system"]
    assert system.endswith(build_task_guidance(task if mode == "generate" else pipeline_yaml + task))
    assert "snmp_poll:" in system  # empty client lists cannot hide runtime schemas
    if mode == "generate":
        assert system.index("WORKED TEMPLATE EXAMPLES") < system.index("FINAL TASK GUIDANCE")
