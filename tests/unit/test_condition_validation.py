"""Issue #85 — registration-time filter/add_field condition validation (L015).

Pins the L015 rule: dry-run each filter/add_field expression against a sample
record so broken conditions (unbound names like ``record``) are caught at
registration instead of silently losing 100% of records at run time. The rule
is surfaced through ``tram validate`` (CLI), the registration endpoint, and
the dry-run endpoint.
"""

from __future__ import annotations

import logging
import textwrap
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import tram.cli.main as _cli_mod
from tram.cli.main import app
from tram.pipeline.linter import lint
from tram.pipeline.loader import load_pipeline_from_yaml

runner = CliRunner()


@pytest.fixture(autouse=True)
def _patch_console(monkeypatch, caplog):
    """Patch Rich Console (same reason as test_cli_main): CliRunner captures
    stdout and Rich's internal stream handling corrupts click's cleanup."""
    mock_console = MagicMock()
    monkeypatch.setattr(_cli_mod, "console", mock_console)
    monkeypatch.setattr(_cli_mod, "err_console", mock_console)
    with caplog.at_level(logging.WARNING):
        yield


def _load(yaml_body: str) -> object:
    return load_pipeline_from_yaml(textwrap.dedent(yaml_body))


def _config_with_transforms(transforms_block: str) -> object:
    """Wrap *transforms_block* (a YAML list at column 0) in a minimal pipeline."""
    block = textwrap.indent(textwrap.dedent(transforms_block), "    ")
    body = (
        "pipeline:\n"
        "  name: l015-test\n"
        "  source:\n"
        "    type: local\n"
        "    path: /tmp\n"
        "  serializer_in:\n"
        "    type: json\n"
        "  serializer_out:\n"
        "    type: json\n"
        "  transforms:\n"
        f"{block}"
        "  sink:\n"
        "    type: local\n"
        "    path: /out\n"
    )
    return load_pipeline_from_yaml(body)


def _l015(config) -> list:
    return [f for f in lint(config) if f.rule_id == "L015"]


class TestL015FilterConditions:
    def test_broken_template_class_record_get_is_error(self):
        """t1 as shipped (deploy-state.md): record.get('event_type') lost 100%."""
        config = _config_with_transforms("""\
- type: filter
  condition: "record.get('event_type') != 'EVENT'"
""")
        findings = _l015(config)
        assert len(findings) == 1
        assert findings[0].severity == "error"
        assert "record" in findings[0].message

    def test_broken_template_class_record_get_t5_style_is_error(self):
        """t5 as shipped: record.get('bytes_down') >= 0 lost 100%."""
        config = _config_with_transforms("""\
- type: filter
  condition: "record.get('bytes_down') >= 0"
""")
        findings = _l015(config)
        assert len(findings) == 1
        assert findings[0].severity == "error"

    def test_filter_pipeline_name_is_error(self):
        """filter never binds `pipeline` either — deterministic failure."""
        config = _config_with_transforms("""\
- type: filter
  condition: "pipeline.name == 'x'"
""")
        findings = _l015(config)
        assert len(findings) == 1
        assert findings[0].severity == "error"

    def test_valid_condition_passes_clean(self):
        """t1 fixed (templates-fixed/): event_type != 'EVENT'."""
        config = _config_with_transforms("""\
- type: filter
  condition: "event_type != 'EVENT'"
""")
        assert _l015(config) == []

    def test_valid_numeric_condition_passes_clean(self):
        """t5 fixed: bytes_down >= 0."""
        config = _config_with_transforms("""\
- type: filter
  condition: "bytes_down >= 0"
""")
        assert _l015(config) == []

    def test_dynamic_but_valid_expression_not_rejected(self):
        """Intentionally varying expressions must not hard-fail."""
        config = _config_with_transforms("""\
- type: filter
  condition: "direction in ('UP', 'DOWN') and event_type != 'EVENT'"
""")
        assert _l015(config) == []

    def test_len_expression_not_rejected(self):
        """String-oriented conditions pass via the string seed."""
        config = _config_with_transforms("""\
- type: filter
  condition: "len(msisdn) > 3"
""")
        assert _l015(config) == []

    def test_syntax_error_is_error(self):
        config = _config_with_transforms("""\
- type: filter
  condition: "bytes_down >="
""")
        findings = _l015(config)
        assert len(findings) == 1
        assert findings[0].severity == "error"

    def test_type_dependent_failure_is_warning(self):
        """A division-by-zero is data-dependent, not an unbound name — warning."""
        config = _config_with_transforms("""\
- type: filter
  condition: "bytes_down / 0 > 1"
""")
        findings = _l015(config)
        assert len(findings) == 1
        assert findings[0].severity == "warning"

    def test_t5_fixed_chain_passes_clean(self):
        """The full corrected t5 chain evaluates clean."""
        config = _config_with_transforms("""\
- type: rename
  fields:
    msisdn: subscriber
- type: cast
  fields:
    duration_s: int
- type: add_field
  fields:
    kb_down: "bytes_down / 1024"
    source_tag: "'perf-t5'"
- type: filter
  condition: "bytes_down >= 0"
- type: project
  fields:
    record_id: record_id
""")
        assert _l015(config) == []


class TestL015AddFieldConditions:
    def test_valid_add_field_passes_clean(self):
        config = _config_with_transforms("""\
- type: add_field
  fields:
    kb_down: "bytes_down / 1024"
""")
        assert _l015(config) == []

    def test_add_field_record_get_is_allowed(self):
        """add_field binds `record` explicitly — record.get(...) is valid there."""
        config = _config_with_transforms("""\
- type: add_field
  fields:
    tag: "record.get('event_type')"
""")
        assert _l015(config) == []

    def test_add_field_pipeline_name_is_allowed(self):
        config = _config_with_transforms("""\
- type: add_field
  fields:
    tag: "pipeline.name"
""")
        assert _l015(config) == []

    def test_add_field_type_dependent_failure_is_warning(self):
        config = _config_with_transforms("""\
- type: add_field
  fields:
    bad: "bytes_down / 0"
""")
        findings = _l015(config)
        assert len(findings) == 1
        assert findings[0].severity == "warning"


class TestL015SinkLevelTransforms:
    def test_sink_level_filter_unbound_record_is_error(self):
        config = _load("""
            pipeline:
              name: l015-sink
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
                transforms:
                  - type: filter
                    condition: "record.get('x') == 1"
        """)
        findings = _l015(config)
        assert len(findings) == 1
        assert findings[0].severity == "error"

    def test_no_expression_transforms_produce_no_l015(self):
        config = _load("""
            pipeline:
              name: l015-none
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              sink:
                type: local
                path: /out
        """)
        assert _l015(config) == []


class TestCLIValidateSurfacesL015:
    def test_validate_rejects_unbound_filter_condition(self, tmp_path):
        pipeline_file = tmp_path / "broken.yaml"
        pipeline_file.write_text(textwrap.dedent("""\
            pipeline:
              name: l015-cli
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              transforms:
                - type: filter
                  condition: "record.get('event_type') != 'EVENT'"
              sink:
                type: local
                path: /out
        """))
        result = runner.invoke(app, ["validate", str(pipeline_file)])
        assert result.exit_code == 1

    def test_validate_passes_valid_filter_condition(self, tmp_path):
        pipeline_file = tmp_path / "valid.yaml"
        pipeline_file.write_text(textwrap.dedent("""\
            pipeline:
              name: l015-cli-ok
              source:
                type: local
                path: /tmp
              serializer_in:
                type: json
              serializer_out:
                type: json
              transforms:
                - type: filter
                  condition: "event_type != 'EVENT'"
              sink:
                type: local
                path: /out
        """))
        result = runner.invoke(app, ["validate", str(pipeline_file)])
        assert result.exit_code == 0


class TestRegistrationAndDryRunLint:
    """Issue #85: registration-rejecting lint at the API surface."""

    def _make_app(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from tram.api.routers.pipelines import router

        app = FastAPI()
        app.include_router(router)
        mock_controller = MagicMock()
        app.state.manager = MagicMock()
        app.state.controller = mock_controller
        app.state.config = MagicMock()
        app.state.db = None
        app.state.stats_store = None
        return TestClient(app, raise_server_exceptions=False), mock_controller

    def _broken_yaml(self) -> str:
        return textwrap.dedent("""\
            name: l015-api
            schedule:
              type: manual
            source:
              type: local
              path: /tmp/in
            serializer_in:
              type: json
            transforms:
              - type: filter
                condition: "record.get('event_type') != 'EVENT'"
            sinks:
              - type: local
                path: /tmp/out
        """)

    def _valid_yaml(self) -> str:
        return textwrap.dedent("""\
            name: l015-api
            schedule:
              type: manual
            source:
              type: local
              path: /tmp/in
            serializer_in:
              type: json
            transforms:
              - type: filter
                condition: "event_type != 'EVENT'"
            sinks:
              - type: local
                path: /tmp/out
        """)

    def test_register_rejects_unbound_filter_condition(self):
        client, mock_controller = self._make_app()
        resp = client.post("/api/pipelines", json={"yaml_text": self._broken_yaml()})
        assert resp.status_code == 400
        assert "L015" in resp.json()["detail"]
        mock_controller.register.assert_not_called()

    def test_register_accepts_valid_condition(self):
        client, mock_controller = self._make_app()
        state = MagicMock()
        state.to_dict.return_value = {"name": "l015-api"}
        mock_controller.register.return_value = state
        resp = client.post("/api/pipelines", json={"yaml_text": self._valid_yaml()})
        assert resp.status_code == 201
        mock_controller.register.assert_called_once()

    def test_dry_run_surfaces_lint_errors(self):
        client, _mock_controller = self._make_app()
        resp = client.post("/api/pipelines/dry-run", json={"yaml_text": self._broken_yaml()})
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is False
        assert any("record" in issue for issue in body["issues"])

    def test_dry_run_clean_for_valid_condition(self):
        client, _mock_controller = self._make_app()
        resp = client.post("/api/pipelines/dry-run", json={"yaml_text": self._valid_yaml()})
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True