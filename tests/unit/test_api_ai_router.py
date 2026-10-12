"""Tests for the AI assist router."""
from __future__ import annotations

import os
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.ai import _AiResult, router


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


class TestAiStatus:
    def test_not_configured_when_no_key(self):
        app = _make_app()
        client = TestClient(app)
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("TRAM_AI_API_KEY", None)
            resp = client.get("/api/ai/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["provider"] is None

    def test_enabled_when_key_set(self):
        app = _make_app()
        client = TestClient(app)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}):
            resp = client.get("/api/ai/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["provider"] == "anthropic"  # default

    def test_openai_provider_reported(self):
        app = _make_app()
        client = TestClient(app)
        with patch.dict(os.environ, {
            "TRAM_AI_API_KEY": "test-key",
            "TRAM_AI_PROVIDER": "openai",
        }):
            resp = client.get("/api/ai/status")
        data = resp.json()
        assert data["provider"] == "openai"


class TestAiSuggest:
    def test_503_when_no_key(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        env = {k: v for k, v in os.environ.items() if k != "TRAM_AI_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            resp = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "test"})
        assert resp.status_code == 503

    def test_generate_mode_calls_ai_and_returns_yaml(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}), \
             patch("tram.api.routers.ai._call_ai", return_value=_AiResult("name: test\nschedule:\n  type: manual", None)):
            resp = client.post("/api/ai/suggest", json={
                "mode": "generate",
                "prompt": "create a test pipeline",
                "plugins": {"sources": ["sftp"], "sinks": ["local"], "transforms": [], "serializers": []},
            })
        assert resp.status_code == 200
        assert "yaml" in resp.json()

    def test_explain_mode_calls_ai_and_returns_explanation(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}), \
             patch("tram.api.routers.ai._call_ai", return_value=_AiResult("The error is in the sink config.", None)):
            resp = client.post("/api/ai/suggest", json={
                "mode": "explain",
                "yaml": "name: test",
                "error": "Missing table field",
            })
        assert resp.status_code == 200
        assert "explanation" in resp.json()

    def test_unknown_mode_returns_400(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}):
            resp = client.post("/api/ai/suggest", json={"mode": "unknown"})
        assert resp.status_code == 400

    def test_ai_error_returns_502(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}), \
             patch("tram.api.routers.ai._call_ai", side_effect=RuntimeError("API down")):
            resp = client.post("/api/ai/suggest", json={
                "mode": "generate",
                "prompt": "test",
            })
        assert resp.status_code == 502

    def test_generate_strips_markdown_fences(self):
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)
        fenced = "```yaml\nname: test\n```"
        with patch.dict(os.environ, {"TRAM_AI_API_KEY": "test-key"}), \
             patch("tram.api.routers.ai._call_ai", return_value=_AiResult(fenced, None)):
            resp = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "x"})
        assert resp.status_code == 200
        yaml_text = resp.json()["yaml"]
        assert "```" not in yaml_text


_LOCAL_YAML = """name: synthetic
schedule:
  type: manual
source:
  type: local
  path: /synthetic/input
serializer_in:
  type: json
serializer_out:
  type: ndjson
sinks:
  - type: local
    path: /synthetic/output
    filename_template: '{pipeline}.ndjson'
"""


def test_validation_checks_aggregate_constructor_and_filename_tokens():
    from tram.api.routers.ai import _validate_yaml

    aggregate = _LOCAL_YAML + """transforms:
  - type: aggregate
    group_by: [category]
    operations:
      total: sum(bytes)
"""
    assert any("transforms:" in issue for issue in _validate_yaml(aggregate))
    assert _validate_yaml(aggregate.replace("sum(bytes)", "sum:bytes")) == []
    assert any("ifType" in issue for issue in _validate_yaml(
        _LOCAL_YAML.replace("{pipeline}", "{ifType}"),
    ))
    assert _validate_yaml(_LOCAL_YAML.replace("{pipeline}", "{field.category}")) == []


def test_validation_checks_lint_and_cron_guidance():
    from tram.api.routers.ai import _PIPELINE_STRUCTURE, _validate_yaml

    assert 'cron: "*/5 * * * *"' in _PIPELINE_STRUCTURE
    assert "cron_expr" not in _PIPELINE_STRUCTURE
    assert _validate_yaml(_LOCAL_YAML.replace("type: manual", 'type: cron\n  cron: "0 * * * *"')) == []
    invalid = _LOCAL_YAML + "transforms:\n  - type: filter\n    condition: 'lenn(bytes) > 0'\n"
    assert _validate_yaml(invalid)


def test_validation_does_not_read_write_or_connect():
    from tram.api.routers.ai import _validate_yaml

    with patch("socket.socket.connect", side_effect=AssertionError("network I/O")), \
         patch("urllib.request.urlopen", side_effect=AssertionError("network I/O")), \
         patch("tram.connectors.local.source.LocalSource.read", side_effect=AssertionError("read")), \
         patch("tram.connectors.local.sink.LocalSink.write", side_effect=AssertionError("write")):
        assert _validate_yaml(_LOCAL_YAML) == []
        remote = _LOCAL_YAML.replace("type: local\n  path: /synthetic/input", """type: sftp
  host: example.invalid
  username: placeholder
  password: placeholder
  remote_path: /synthetic/input""")
        assert _validate_yaml(remote) == []


def test_yaml_retry_redacts_candidate_preserves_mode_and_bounds_calls():
    import asyncio
    from unittest.mock import AsyncMock

    from tram.api.routers.ai import _run_yaml_with_retry

    candidate = _LOCAL_YAML.replace("type: local\n  path: /synthetic/input", """type: sftp
  host: example.invalid
  username: placeholder
  password: candidate-private-value
  remote_path: /synthetic/input""") + """transforms:
  - type: aggregate
    operations:
      total: sum(bytes)
"""
    for mode in ("generate", "modify", "fix"):
        call = AsyncMock(side_effect=[_AiResult(candidate), _AiResult(_LOCAL_YAML)])
        with patch("tram.api.routers.ai._run_ai_call", call):
            out = asyncio.run(_run_yaml_with_retry(None, {}, "system", "instruction", mode=mode))
        assert out["valid"] is True
        assert out["retried"] is True
        assert out["attempts"] == 2
        assert call.await_count == 2
        retry = call.await_args_list[1]
        assert retry.args[1] == mode
        assert "sum(bytes)" in retry.args[3]
        assert "candidate-private-value" not in retry.args[3]
        assert "validation errors" in retry.args[3]
        assert retry.kwargs["retried"] is True
        assert 1024 < retry.kwargs["max_tokens"] <= 4096


def test_yaml_retry_truncation_and_malformed_candidate_fail_closed():
    import asyncio
    from unittest.mock import AsyncMock

    from tram.api.routers.ai import _run_yaml_with_retry

    for initial in (_AiResult(_LOCAL_YAML, "max_tokens"),
                    _AiResult('source: [password: private-unparseable')):
        call = AsyncMock(side_effect=[initial, _AiResult(_LOCAL_YAML, "length")])
        with patch("tram.api.routers.ai._run_ai_call", call):
            out = asyncio.run(_run_yaml_with_retry(None, {}, "system", "instruction", mode="generate"))
        assert out["valid"] is False
        assert any("truncated" in issue for issue in out["issues"])
        assert out["attempts"] == 2
        assert "private-unparseable" not in call.await_args_list[1].args[3]


def test_non_mapping_model_output_returns_validation_issues():
    from tram.api.routers.ai import _validate_yaml

    for candidate in ("42", "just a string", "[]"):
        assert _validate_yaml(candidate) == ["Pipeline YAML must be a mapping at the top level"]


def test_retry_does_not_forward_container_secrets_or_expanded_environment(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from tram.api.routers.ai import _run_yaml_with_retry

    marker = "[synthetic-private-env-marker]"
    monkeypatch.setenv("TRAM_REVIEW_SECRET", marker)
    for secret in ("[container-private-marker]", "'${TRAM_REVIEW_SECRET}'"):
        candidate = _LOCAL_YAML.replace("type: local\n  path: /synthetic/input", f"""type: sftp
  host: example.invalid
  username: placeholder
  password: {secret}
  remote_path: /synthetic/input""") + "\nunsupported_field: true\n"
        call = AsyncMock(side_effect=[_AiResult(candidate), _AiResult(_LOCAL_YAML)])
        with patch("tram.api.routers.ai._run_ai_call", call):
            out = asyncio.run(_run_yaml_with_retry(None, {}, "system", "instruction", mode="fix"))
        assert out["valid"] is True
        retry_user = call.await_args_list[1].args[3]
        assert "synthetic-private-env-marker" not in retry_user
        assert "container-private-marker" not in retry_user
        assert "${TRAM_REVIEW_SECRET}" not in retry_user


def test_retry_sanitizer_omits_environment_keys_and_masks_protocol_credentials(monkeypatch):
    import pytest

    from tram.api.routers.ai import _redact_retry_candidate, _validate_yaml

    monkeypatch.setenv("TRAM_REVIEW_SECRET", "[private-env-key-marker]")
    candidate = _LOCAL_YAML + "metadata:\n  '${TRAM_REVIEW_SECRET}': bad\n"
    with pytest.raises(ValueError, match="mapping keys"):
        _redact_retry_candidate(candidate)

    for source in (
        """type: snmp
  host: example.invalid
  community: private-community-marker
  auth_key: private-auth-marker
  priv_key: private-priv-marker""",
        """type: sql
  connection_url: postgresql://synthetic:private-url-marker@example.invalid/db
  query: SELECT 1""",
    ):
        candidate = _LOCAL_YAML.replace("type: local\n  path: /synthetic/input", source)
        sanitized = _redact_retry_candidate(candidate)
        feedback = "\n".join(_validate_yaml(sanitized))
        for secret in ("private-community-marker", "private-auth-marker", "private-priv-marker",
                       "private-url-marker"):
            assert secret not in sanitized + feedback


def test_malformed_collection_candidate_retries_without_forwarding_secrets():
    import asyncio
    from unittest.mock import AsyncMock

    from tram.api.routers.ai import _run_yaml_with_retry

    for malformed in ("sinks: 3", "transforms: true", "alerts: 7"):
        candidate = _LOCAL_YAML + malformed + "\nmetadata:\n  password: private-shape-marker\n"
        call = AsyncMock(side_effect=[_AiResult(candidate), _AiResult(_LOCAL_YAML)])
        with patch("tram.api.routers.ai._run_ai_call", call):
            out = asyncio.run(_run_yaml_with_retry(None, {}, "system", "instruction", mode="generate"))
        assert out["valid"] is True
        assert out["attempts"] == 2
        assert "private-shape-marker" not in call.await_args_list[1].args[3]
