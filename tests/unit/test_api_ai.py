"""Tests for AI assist router — /api/ai/status, config, test, suggest."""
from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, call, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.config_schema import schema_version
from tram.api.routers.ai import (
    _AiResult,
    _base_url_allowed,
    _base_url_problem,
    _build_triage_context,
    _call_ai,
    _get_ai_cfg,
    _group_skip_reasons,
    _redact_yaml,
    _strip_fences,
    _yaml_mode_result,
    router,
)
from tram.core.context import RunResult, RunStatus

# ── App factory ────────────────────────────────────────────────────────────


def _make_app(db=None, api_key_env=""):
    app = FastAPI()
    app.include_router(router)
    if db is not None:
        app.state.db = db
    return app


def _make_db(settings=None):
    db = MagicMock()
    settings = settings or {}
    db.get_setting.side_effect = lambda k: settings.get(k, "")
    return db


# ── _strip_fences ──────────────────────────────────────────────────────────


class TestStripFences:
    def test_no_fences(self):
        assert _strip_fences("hello") == "hello"

    def test_with_backtick_fences(self):
        text = "```yaml\nname: pipe\n```"
        result = _strip_fences(text)
        assert result == "name: pipe"

    def test_opening_fence_only(self):
        text = "```\nname: pipe"
        result = _strip_fences(text)
        assert result == "name: pipe"


# ── _get_ai_cfg ────────────────────────────────────────────────────────────


class TestGetAiCfg:
    def test_defaults_from_env(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_PROVIDER", "openai")
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-abc")
        monkeypatch.setenv("TRAM_AI_MODEL", "gpt-4")
        monkeypatch.delenv("TRAM_AI_BASE_URL", raising=False)
        cfg = _get_ai_cfg(None)
        assert cfg["provider"] == "openai"
        assert cfg["api_key"] == "sk-abc"
        assert cfg["model"] == "gpt-4"

    def test_db_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_PROVIDER", "openai")
        db = _make_db({"ai.provider": "anthropic", "ai.api_key": "ant-key"})
        cfg = _get_ai_cfg(db)
        assert cfg["provider"] == "anthropic"
        assert cfg["api_key"] == "ant-key"

    def test_default_provider_is_anthropic(self, monkeypatch):
        monkeypatch.delenv("TRAM_AI_PROVIDER", raising=False)
        cfg = _get_ai_cfg(None)
        assert cfg["provider"] == "anthropic"


# ── _call_ai ───────────────────────────────────────────────────────────────


class TestCallAiAnthropic:
    def _cfg(self, **kw):
        return {"provider": "anthropic", "api_key": "ant-key",
                "model": "", "base_url": "", **kw}

    def test_success(self):
        mock_ant = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="name: my-pipe")]
        mock_msg.stop_reason = "end_turn"
        mock_ant.Anthropic.return_value.messages.create.return_value = mock_msg
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            result = _call_ai("system", "user", 100, self._cfg())
        assert result.text == "name: my-pipe"
        assert result.stop_reason == "end_turn"

    def test_import_error(self):
        with patch.dict(sys.modules, {"anthropic": None}):
            with pytest.raises(RuntimeError, match="anthropic package not installed"):
                _call_ai("sys", "usr", 100, self._cfg())

    def test_auth_error(self):
        mock_ant = MagicMock()
        mock_ant.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_ant.APIConnectionError = type("APIConnectionError", (Exception,), {})
        mock_ant.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_ant.APIStatusError = type("APIStatusError", (Exception,), {})
        mock_ant.Anthropic.return_value.messages.create.side_effect = mock_ant.AuthenticationError()
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            with pytest.raises(RuntimeError, match="Invalid Anthropic API key"):
                _call_ai("sys", "usr", 100, self._cfg())

    def test_connection_error(self):
        mock_ant = MagicMock()
        mock_ant.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_ant.APIConnectionError = type("APIConnectionError", (Exception,), {})
        mock_ant.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_ant.APIStatusError = type("APIStatusError", (Exception,), {})
        mock_ant.Anthropic.return_value.messages.create.side_effect = mock_ant.APIConnectionError()
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            with pytest.raises(RuntimeError, match="Could not reach Anthropic"):
                _call_ai("sys", "usr", 100, self._cfg())

    def test_rate_limit_error(self):
        mock_ant = MagicMock()
        mock_ant.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_ant.APIConnectionError = type("APIConnectionError", (Exception,), {})
        mock_ant.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_ant.APIStatusError = type("APIStatusError", (Exception,), {})
        mock_ant.Anthropic.return_value.messages.create.side_effect = mock_ant.RateLimitError()
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            with pytest.raises(RuntimeError, match="rate limit"):
                _call_ai("sys", "usr", 100, self._cfg())

    def test_api_status_error(self):
        mock_ant = MagicMock()
        mock_ant.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_ant.APIConnectionError = type("APIConnectionError", (Exception,), {})
        mock_ant.RateLimitError = type("RateLimitError", (Exception,), {})
        # Create APIStatusError instance with required attributes
        exc_instance = MagicMock()
        exc_instance.status_code = 500
        exc_instance.message = "server error"
        APIStatusError = type("APIStatusError", (Exception,), {
            "status_code": 500, "message": "server error"
        })
        mock_ant.APIStatusError = APIStatusError
        mock_ant.Anthropic.return_value.messages.create.side_effect = exc_instance
        # Patch the exception handler to trigger the APIStatusError branch
        def raise_api_status(*a, **kw):
            e = APIStatusError("error")
            e.status_code = 500
            e.message = "server error"
            raise e
        mock_ant.Anthropic.return_value.messages.create.side_effect = raise_api_status
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            with pytest.raises(RuntimeError, match="Anthropic API error"):
                _call_ai("sys", "usr", 100, self._cfg())

    def test_base_url_strips_v1(self):
        mock_ant = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="ok")]
        mock_ant.Anthropic.return_value.messages.create.return_value = mock_msg
        mock_ant.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_ant.APIConnectionError = type("APIConnectionError", (Exception,), {})
        mock_ant.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_ant.APIStatusError = type("APIStatusError", (Exception,), {})
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            _call_ai("sys", "usr", 100, self._cfg(base_url="https://proxy/v1"))
        call_kwargs = mock_ant.Anthropic.call_args.kwargs
        assert call_kwargs.get("base_url") == "https://proxy"

    def test_timeout_passed_to_client(self):
        # A1: explicit 60 s timeout on the client, matching the Bedrock path.
        mock_ant = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="ok")]
        mock_ant.Anthropic.return_value.messages.create.return_value = mock_msg
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            _call_ai("sys", "usr", 100, self._cfg())
        assert mock_ant.Anthropic.call_args.kwargs.get("timeout") == 60.0


class TestCallAiOpenAI:
    def _cfg(self, **kw):
        return {"provider": "openai", "api_key": "sk-key",
                "model": "", "base_url": "", **kw}

    def test_success(self):
        mock_oai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="ok"), finish_reason="stop")]
        mock_oai.OpenAI.return_value.chat.completions.create.return_value = mock_resp
        with patch.dict(sys.modules, {"openai": mock_oai}):
            result = _call_ai("system", "user", 100, self._cfg())
        assert result.text == "ok"
        assert result.stop_reason == "stop"

    def test_import_error(self):
        with patch.dict(sys.modules, {"openai": None}):
            with pytest.raises(RuntimeError, match="openai package not installed"):
                _call_ai("sys", "usr", 100, self._cfg())

    def test_auth_error(self):
        mock_oai = MagicMock()
        mock_oai.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_oai.APIConnectionError = type("APIConnectionError", (Exception,), {})
        mock_oai.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_oai.APIStatusError = type("APIStatusError", (Exception,), {})
        mock_oai.OpenAI.return_value.chat.completions.create.side_effect = (
            mock_oai.AuthenticationError()
        )
        with patch.dict(sys.modules, {"openai": mock_oai}):
            with pytest.raises(RuntimeError, match="Invalid OpenAI API key"):
                _call_ai("sys", "usr", 100, self._cfg())

    def test_base_url_passed(self):
        mock_oai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_oai.OpenAI.return_value.chat.completions.create.return_value = mock_resp
        mock_oai.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_oai.APIConnectionError = type("APIConnectionError", (Exception,), {})
        mock_oai.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_oai.APIStatusError = type("APIStatusError", (Exception,), {})
        with patch.dict(sys.modules, {"openai": mock_oai}):
            _call_ai("sys", "usr", 100, self._cfg(base_url="https://my-proxy"))
        call_kwargs = mock_oai.OpenAI.call_args.kwargs
        assert call_kwargs.get("base_url") == "https://my-proxy"

    def test_timeout_passed_to_client(self):
        # A1: explicit 60 s timeout on the client, matching the Bedrock path.
        mock_oai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_oai.OpenAI.return_value.chat.completions.create.return_value = mock_resp
        with patch.dict(sys.modules, {"openai": mock_oai}):
            _call_ai("sys", "usr", 100, self._cfg())
        assert mock_oai.OpenAI.call_args.kwargs.get("timeout") == 60.0


class TestCallAiBedrock:
    def _cfg(self, **kw):
        return {"provider": "bedrock", "api_key": "key",
                "model": "", "base_url": "https://bedrock-proxy", **kw}

    def test_success(self):
        import json
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"content": [{"text": "pipeline yaml"}], "stop_reason": "max_tokens"}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _call_ai("sys", "usr", 100, self._cfg())
        assert result.text == "pipeline yaml"
        assert result.stop_reason == "max_tokens"

    def test_stop_reason_optional(self):
        # Bedrock proxies may omit stop_reason from the JSON body — report None.
        import json
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"content": [{"text": "yaml"}]}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _call_ai("sys", "usr", 100, self._cfg())
        assert result.text == "yaml"
        assert result.stop_reason is None

    def test_no_base_url_raises(self):
        with pytest.raises(RuntimeError, match="Base URL is required"):
            _call_ai("sys", "usr", 100, self._cfg(base_url=""))

    def test_401_error(self):
        import urllib.error
        exc = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        exc.read = lambda: b""
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(RuntimeError, match="Invalid Bedrock API key"):
                _call_ai("sys", "usr", 100, self._cfg())

    def test_404_error(self):
        import urllib.error
        exc = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        exc.read = lambda: b""
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(RuntimeError, match="Bedrock endpoint not found"):
                _call_ai("sys", "usr", 100, self._cfg())

    def test_general_exception(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("timeout")):
            with pytest.raises(RuntimeError, match="Bedrock request failed"):
                _call_ai("sys", "usr", 100, self._cfg())


class TestCallAiUnknownProvider:
    def test_unknown_provider_raises(self):
        cfg = {"provider": "llama", "api_key": "x", "model": "", "base_url": ""}
        with pytest.raises(ValueError, match="Unknown TRAM_AI_PROVIDER"):
            _call_ai("sys", "usr", 100, cfg)


# ── /api/ai/status endpoint ────────────────────────────────────────────────


class TestAiStatus:
    def test_not_configured(self, monkeypatch):
        monkeypatch.delenv("TRAM_AI_API_KEY", raising=False)
        app = _make_app()
        client = TestClient(app)
        r = client.get("/api/ai/status")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    def test_configured_via_env(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        monkeypatch.setenv("TRAM_AI_PROVIDER", "openai")
        app = _make_app()
        client = TestClient(app)
        r = client.get("/api/ai/status")
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is True
        assert data["provider"] == "openai"

    def test_default_model_shown_when_no_model_set(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        monkeypatch.delenv("TRAM_AI_MODEL", raising=False)
        monkeypatch.delenv("TRAM_AI_PROVIDER", raising=False)
        app = _make_app()
        client = TestClient(app)
        r = client.get("/api/ai/status")
        assert r.json()["model"] is not None

    def test_status_exposes_schema_version(self, monkeypatch):
        # Issue #24: /api/ai/status carries the schema identity token so the
        # UI can detect a schema change underneath a long-lived tab.
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        data = client.get("/api/ai/status").json()
        assert data["schema_version"] == schema_version()
        assert len(data["schema_version"]) == 12


# ── /api/ai/config endpoints ───────────────────────────────────────────────


class TestAiGetConfig:
    def test_no_key_api_key_hint_empty(self, monkeypatch):
        monkeypatch.delenv("TRAM_AI_API_KEY", raising=False)
        app = _make_app()
        client = TestClient(app)
        r = client.get("/api/ai/config")
        assert r.status_code == 200
        assert r.json()["api_key_set"] is False

    def test_short_key_hint_set(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "abc")
        app = _make_app()
        client = TestClient(app)
        r = client.get("/api/ai/config")
        data = r.json()
        assert data["api_key_hint"] == "set"

    def test_long_key_hint_masked(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-long-key-1234")
        app = _make_app()
        client = TestClient(app)
        r = client.get("/api/ai/config")
        assert "1234" in r.json()["api_key_hint"]

    def test_source_env_when_no_db(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        r = client.get("/api/ai/config")
        assert r.json()["source"] == "env"


class TestAiSaveConfig:
    def test_no_db_returns_503(self):
        app = _make_app()
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"api_key": "sk-new"})
        assert r.status_code == 503

    def test_saves_to_db(self):
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"provider": "openai", "api_key": "sk-new"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        db.set_setting.assert_called()

    def test_absent_api_key_preserves_stored_key(self):
        # A2: omitting api_key from the payload must not touch the stored key.
        db = _make_db({"ai.api_key": "stored-key"})
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"model": "gpt-4o"})
        assert r.status_code == 200
        assert all(call.args[0] != "ai.api_key" for call in db.set_setting.call_args_list)
        db.delete_setting.assert_not_called()
        assert db.get_setting("ai.api_key") == "stored-key"

    def test_blank_api_key_keeps_existing(self):
        # A2 chosen semantics: blank == absent == "no change". A blank api_key
        # must NOT delete the stored key (older UIs always sent api_key: "").
        db = _make_db({"ai.api_key": "stored-key"})
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"api_key": ""})
        assert r.status_code == 200
        db.delete_setting.assert_not_called()
        assert db.get_setting("ai.api_key") == "stored-key"

    # ── P2-1: explicit null means clear ──────────────────────────────────

    def test_null_api_key_clears_stored_key(self):
        # Deliberate clearing without re-opening the key-wipe hole: only an
        # explicit JSON null deletes; blank/absent still keep.
        db = _make_db({"ai.api_key": "stored-key"})
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"api_key": None})
        assert r.status_code == 200
        db.delete_setting.assert_called_once_with("ai.api_key")
        assert all(call.args[0] != "ai.api_key" for call in db.set_setting.call_args_list)

    def test_null_clears_provider_model_base_url(self):
        db = _make_db({"ai.provider": "openai", "ai.model": "gpt-4", "ai.base_url": "https://x"})
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"provider": None, "model": None, "base_url": None})
        assert r.status_code == 200
        assert db.delete_setting.call_args_list == [
            call("ai.provider"), call("ai.model"), call("ai.base_url"),
        ]

    def test_null_on_nonexistent_key_is_noop_success(self):
        db = _make_db()  # nothing stored
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": None})
        assert r.status_code == 200
        db.delete_setting.assert_called_once_with("ai.base_url")

    def test_null_one_field_sets_another(self):
        db = _make_db({"ai.api_key": "old-key"})
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"api_key": None, "model": "gpt-4o"})
        assert r.status_code == 200
        db.delete_setting.assert_called_once_with("ai.api_key")
        db.set_setting.assert_called_once_with("ai.model", "gpt-4o")

    def test_unknown_provider_rejected_with_400(self):
        # A8: reject bad provider strings at save time instead of a later 502.
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"provider": "anthrpic"})
        assert r.status_code == 400
        assert "provider" in r.json()["detail"].lower()
        db.set_setting.assert_not_called()

    def test_non_string_api_key_rejected_with_400(self):
        # Issue #43 ride-along: JSON booleans/numbers must be rejected, not
        # str()-coerced ("True"), and nothing may be persisted.
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"api_key": True})
        assert r.status_code == 400
        assert "api_key" in r.json()["detail"]
        db.set_setting.assert_not_called()
        db.delete_setting.assert_not_called()

    def test_later_field_rejection_leaves_earlier_fields_unsaved(self):
        # Issue #43 ride-along: the whole body is validated before persistence
        # — a 400 on a later field must not leave an earlier valid field saved.
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"provider": "openai", "api_key": True})
        assert r.status_code == 400
        db.set_setting.assert_not_called()
        db.delete_setting.assert_not_called()


# ── /api/ai/test endpoint ──────────────────────────────────────────────────


class TestAiTestEndpoint:
    def test_not_configured_returns_503(self, monkeypatch):
        monkeypatch.delenv("TRAM_AI_API_KEY", raising=False)
        app = _make_app()
        client = TestClient(app)
        r = client.post("/api/ai/test")
        assert r.status_code == 503

    def test_success(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("OK", None)):
            r = client.post("/api/ai/test")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_runtime_error_returns_502(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", side_effect=RuntimeError("bad key")):
            r = client.post("/api/ai/test")
        assert r.status_code == 502


# ── /api/ai/suggest endpoint ───────────────────────────────────────────────


class TestAiSuggestEndpoint:
    def test_not_configured_returns_503(self, monkeypatch):
        monkeypatch.delenv("TRAM_AI_API_KEY", raising=False)
        app = _make_app()
        client = TestClient(app)
        r = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "kafka to local"})
        assert r.status_code == 503

    def test_generate_mode(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("name: my-pipe", None)):
            r = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "read kafka"})
        assert r.status_code == 200
        assert "yaml" in r.json()

    def test_generate_mode_error_returns_502(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", side_effect=Exception("timeout")):
            r = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "x"})
        assert r.status_code == 502

    def test_explain_mode(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("The error means X", None)):
            r = client.post("/api/ai/suggest", json={
                "mode": "explain", "yaml": "name: p", "error": "source missing"
            })
        assert r.status_code == 200
        assert "explanation" in r.json()

    def test_explain_mode_error_returns_502(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", side_effect=Exception("err")):
            r = client.post("/api/ai/suggest", json={"mode": "explain"})
        assert r.status_code == 502

    def test_fix_mode(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("name: fixed-pipe", None)):
            r = client.post("/api/ai/suggest", json={
                "mode": "fix", "yaml": "name: p", "error": "missing source"
            })
        assert r.status_code == 200
        assert "yaml" in r.json()

    def test_fix_mode_error_returns_502(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", side_effect=Exception("err")):
            r = client.post("/api/ai/suggest", json={"mode": "fix"})
        assert r.status_code == 502

    def test_modify_mode(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("name: modified-pipe", None)):
            r = client.post("/api/ai/suggest", json={
                "mode": "modify", "yaml": "name: p", "instruction": "add a filter"
            })
        assert r.status_code == 200
        assert "yaml" in r.json()

    def test_modify_mode_error_returns_502(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", side_effect=Exception("err")):
            r = client.post("/api/ai/suggest", json={"mode": "modify"})
        assert r.status_code == 502

    def test_unknown_mode_returns_400(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        r = client.post("/api/ai/suggest", json={"mode": "bogus"})
        assert r.status_code == 400


# ── A3: model-output validation + truncation surfacing ─────────────────────

_VALID_PIPELINE_YAML = (
    "name: pipe\n"
    "schedule:\n  type: manual\n"
    "source:\n  type: local\n  path: /tmp/in\n"
    "serializer_in:\n  type: json\n"
    "sinks:\n  - type: local\n    path: /tmp/out\n"
).strip()


class TestYamlModeResult:
    def test_valid_yaml_returns_no_issues(self):
        result = _yaml_mode_result(_AiResult(_VALID_PIPELINE_YAML, "end_turn"))
        assert result["yaml"] == _VALID_PIPELINE_YAML
        assert result["valid"] is True
        assert result["issues"] == []

    def test_invalid_yaml_still_returns_yaml_with_issues(self):
        result = _yaml_mode_result(_AiResult("name: broken\n", "end_turn"))
        assert result["yaml"] == "name: broken"
        assert result["valid"] is False
        assert any("validation" in issue.lower() for issue in result["issues"])

    def test_yaml_syntax_error_reported(self):
        result = _yaml_mode_result(_AiResult("name: [unclosed\n", "end_turn"))
        assert result["valid"] is False
        assert any("parse" in issue.lower() for issue in result["issues"])

    def test_empty_output_reported(self):
        result = _yaml_mode_result(_AiResult("```\n```", "end_turn"))
        assert result["valid"] is False
        assert result["issues"] == ["Model returned empty YAML"]

    def test_anthropic_max_tokens_truncation_warning(self):
        # A3: stop_reason="max_tokens" (anthropic / bedrock) ⇒ truncation warning.
        result = _yaml_mode_result(_AiResult(_VALID_PIPELINE_YAML, "max_tokens"))
        assert result["valid"] is False
        assert any("truncat" in issue.lower() for issue in result["issues"])

    def test_openai_length_truncation_warning(self):
        # A3: finish_reason="length" (openai) ⇒ truncation warning.
        result = _yaml_mode_result(_AiResult(_VALID_PIPELINE_YAML, "length"))
        assert result["valid"] is False
        assert any("truncat" in issue.lower() for issue in result["issues"])

    def test_no_truncation_warning_for_end_turn(self):
        result = _yaml_mode_result(_AiResult(_VALID_PIPELINE_YAML, "end_turn"))
        assert not any("truncat" in issue.lower() for issue in result["issues"])

    def test_strips_fences_before_validation(self):
        fenced = f"```yaml\n{_VALID_PIPELINE_YAML}\n```"
        result = _yaml_mode_result(_AiResult(fenced, "end_turn"))
        assert result["yaml"] == _VALID_PIPELINE_YAML
        assert result["valid"] is True


class TestAiSuggestValidation:
    """Endpoint-level: generate/fix/modify return {yaml, valid, issues}."""

    def test_generate_returns_valid_and_issues(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai",
                   return_value=_AiResult(_VALID_PIPELINE_YAML, "end_turn")):
            r = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "x"})
        assert r.status_code == 200
        data = r.json()
        assert data["yaml"] == _VALID_PIPELINE_YAML
        assert data["valid"] is True
        assert data["issues"] == []

    def test_generate_invalid_yaml_returns_raw_yaml(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("name: broken\n", "end_turn")):
            r = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "x"})
        assert r.status_code == 200
        data = r.json()
        assert data["yaml"] == "name: broken"  # raw YAML still returned
        assert data["valid"] is False
        assert data["issues"]

    def test_fix_mode_returns_valid_and_issues(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai",
                   return_value=_AiResult(_VALID_PIPELINE_YAML, "end_turn")):
            r = client.post("/api/ai/suggest", json={"mode": "fix", "yaml": _VALID_PIPELINE_YAML, "error": "e"})
        data = r.json()
        assert data["yaml"] == _VALID_PIPELINE_YAML
        assert data["valid"] is True
        assert data["issues"] == []

    def test_modify_mode_returns_valid_and_issues(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai",
                   return_value=_AiResult(_VALID_PIPELINE_YAML, "end_turn")):
            r = client.post("/api/ai/suggest", json={"mode": "modify", "yaml": _VALID_PIPELINE_YAML, "instruction": "i"})
        data = r.json()
        assert data["yaml"] == _VALID_PIPELINE_YAML
        assert data["valid"] is True
        assert data["issues"] == []

    def test_explain_mode_unchanged(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("The error means X", None)):
            r = client.post("/api/ai/suggest", json={"mode": "explain", "yaml": "x", "error": "e"})
        assert r.status_code == 200
        assert set(r.json().keys()) == {"explanation"}


# ── A4: secret redaction in explain/fix/modify prompts ─────────────────────

_SECRET_BEARING_YAML = (
    "name: pipe\n"
    "schedule:\n  type: manual\n"
    "source:\n  type: sftp\n  host: example.com\n  username: op\n  password: supersecret123\n"
    "serializer_in:\n  type: json\n"
    "sinks:\n  - type: local\n    path: /tmp/out\n"
)


class TestRedactYaml:
    def test_masks_secret_fields(self):
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: sftp\n  host: example.com\n  username: op\n  password: s3cr3t\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n  - type: rest\n    url: http://x\n    token: abc123\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "s3cr3t" not in redacted
        assert "abc123" not in redacted
        assert "***redacted***" in redacted
        assert "example.com" in redacted   # non-secret fields untouched
        assert "op" in redacted

    def test_keeps_env_var_references(self):
        # ${VAR} refs are env references — the loader substitutes at runtime.
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: sftp\n  host: example.com\n  username: op\n  password: ${SFTP_PASSWORD}\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n  - type: kafka\n    brokers: [a:9092]\n    topic: t\n    sasl_password: ${KAFKA_PASS:-fallback}\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "${SFTP_PASSWORD}" in redacted
        assert "${KAFKA_PASS:-fallback}" in redacted
        assert "***redacted***" not in redacted

    def test_non_secret_fields_untouched(self):
        redacted = _redact_yaml(_SECRET_BEARING_YAML)
        assert "supersecret123" not in redacted
        assert "example.com" in redacted
        assert "username: op" in redacted
        assert "name: pipe" in redacted

    def test_masks_wrapped_pipeline_format(self):
        yaml_text = (
            "pipeline:\n"
            "  name: pipe\n"
            "  schedule:\n    type: manual\n"
            "  source:\n    type: rest\n    url: http://x\n    token: tok123\n"
            "  serializer_in:\n    type: json\n"
            "  sinks:\n    - type: local\n      path: /tmp\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "tok123" not in redacted
        assert "pipeline:" in redacted

    def test_unknown_connector_type_still_masked(self):
        # Connector types outside the schema cache (plugin connectors) are
        # covered by the password/token/secret name heuristic.
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: my_plugin_source\n  api_token: plugintok\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n  - type: local\n    path: /tmp\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "plugintok" not in redacted
        assert "***redacted***" in redacted

    def test_masks_nested_sink_blocks(self):
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: kafka\n  brokers: [a:9092]\n  topic: in\n  sasl_password: ksecret\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n"
            "  - type: rest\n    url: http://x\n    token: sinktok\n"
            "    serializer_out:\n      type: json\n"
            "    transforms:\n      - type: add_field\n        fields:\n          a: b\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "ksecret" not in redacted
        assert "sinktok" not in redacted
        assert "add_field" in redacted      # transforms survive masking
        assert "serializer_out" in redacted

    def test_unparseable_yaml_raises_value_error(self):
        # A4/Issue #43: redaction fails closed — unparseable YAML may still
        # hold live secrets mid-edit, so it must raise, never pass through.
        raw = "name: [unclosed\n  source:\n    type: sftp\n    password: keepme"
        with pytest.raises(ValueError, match="YAML syntax"):
            _redact_yaml(raw)

    def test_non_dict_top_level_raises_value_error(self):
        # A bare list can still carry secret-bearing mappings — fail closed.
        raw = "- name: pipe\n  source:\n    type: sftp\n    password: keepme"
        with pytest.raises(ValueError, match="mapping at the top level"):
            _redact_yaml(raw)

    def test_masks_api_key_connector_fields(self):
        # Issue #43: api_key fields on REST/ES sources and sinks were not
        # matched by the password/token/secret heuristic.
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: rest\n  url: https://es.example.com\n"
            "  api_key: rest-source-key\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n"
            "  - type: elasticsearch\n"
            "    url: https://es.example.com\n"
            "    api_key: es-sink-key\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "rest-source-key" not in redacted
        assert "es-sink-key" not in redacted
        assert "***redacted***" in redacted
        assert "url: https://es.example.com" in redacted   # non-secret intact

    def test_masks_singular_sink_block(self):
        # Backward-compat singular `sink:` (PipelineConfig.sink) carries the
        # same secret fields as a `sinks[]` entry — smtp has a password field.
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: local\n  path: /tmp/in\n"
            "serializer_in:\n  type: json\n"
            "sink:\n  type: smtp\n  host: mail.example.com\n  username: op\n  password: smtpsecret\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "smtpsecret" not in redacted
        assert "***redacted***" in redacted
        assert "mail.example.com" in redacted
        assert "username: op" in redacted

    # ── P2-2: dict-valued headers + alert webhook URLs ───────────────────

    def test_masks_all_header_dict_values(self):
        # headers/extra_headers values are masked wholesale (keys kept —
        # they are structural); ${VAR} env refs stay intact.
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: rest\n  url: http://x\n"
            "  headers:\n    Authorization: Bearer tok123\n    X-Api-Key: kkk\n    X-Env: ${MY_TOKEN}\n"
            "  extra_headers:\n    Content-Type: application/json\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n  - type: local\n    path: /tmp/out\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "tok123" not in redacted
        assert "kkk" not in redacted
        assert "application/json" not in redacted
        assert "Authorization" in redacted       # keys kept
        assert "X-Api-Key" in redacted
        assert "Content-Type" in redacted
        assert "${MY_TOKEN}" in redacted         # env refs intact
        assert "***redacted***" in redacted

    def test_masks_alert_webhook_url(self):
        # webhook URLs embed credentials in userinfo/query — the whole URL is
        # the secret-bearing value, so it is masked entirely.
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: local\n  path: /tmp/in\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n  - type: local\n    path: /tmp/out\n"
            "alerts:\n"
            "  - rule_name: r1\n"
            "    webhook_url: https://hooks.example.com/xyz/tok123\n"
            "    condition: records_out > 5\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "tok123" not in redacted
        assert "hooks.example.com" not in redacted
        assert "rule_name: r1" in redacted
        assert "condition" in redacted
        assert "***redacted***" in redacted

    def test_alert_webhook_env_ref_kept(self):
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: local\n  path: /tmp/in\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n  - type: local\n    path: /tmp/out\n"
            "alerts:\n"
            "  - rule_name: r1\n"
            "    webhook_url: ${ALERT_WEBHOOK_URL}\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "${ALERT_WEBHOOK_URL}" in redacted
        assert "***redacted***" not in redacted

    def test_non_header_dict_fields_not_touched(self):
        # No over-masking: arbitrary dict-valued fields are not header/secret
        # carriers and must pass through untouched.
        yaml_text = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: local\n  path: /tmp/in\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n  - type: local\n    path: /tmp/out\n"
            "    metadata:\n      team: ops\n      cost_center: 123\n"
        )
        redacted = _redact_yaml(yaml_text)
        assert "metadata" in redacted
        assert "team: ops" in redacted
        assert "cost_center: 123" in redacted
        assert "***redacted***" not in redacted

    def test_empty_yaml_returned_unchanged(self):
        assert _redact_yaml("") == ""
        assert _redact_yaml("   \n") == "   \n"


# ── N3 (v1.4.7): template examples are redacted before entering prompts ─────


class TestRedactTemplateExamples:
    def _template(self, name, yaml_text):
        return {"id": name, "name": name, "yaml": yaml_text}

    def test_secret_looking_template_field_is_masked(self):
        from tram.api.routers.ai import _redact_template_examples

        tpl = self._template("sftp-export", (
            "name: sftp-export\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: sftp\n"
            "  host: ne.example.com\n"
            "  username: collector\n"
            "  password: hunter2\n"
        ))
        out = _redact_template_examples([tpl])
        assert len(out) == 1
        assert "hunter2" not in out[0]["yaml"]
        assert "***redacted***" in out[0]["yaml"]

    def test_unredactable_template_is_dropped_fail_closed(self):
        from tram.api.routers.ai import _redact_template_examples

        good = self._template("good", "name: good\n")
        broken = self._template("broken", "source: [unclosed\n")
        out = _redact_template_examples([good, broken])
        assert len(out) == 1
        assert out[0]["id"] == "good"
        # The original template dicts are never mutated.
        assert "source: [unclosed" in broken["yaml"]

    def test_empty_and_blank_templates_pass_through(self):
        from tram.api.routers.ai import _redact_template_examples

        out = _redact_template_examples([self._template("empty", "")])
        assert len(out) == 1
        assert out[0]["yaml"] == ""


class TestAiPromptRedaction:
    """Endpoint-level: what reaches the provider has secrets masked."""

    def _post(self, client, payload):
        return client.post("/api/ai/suggest", json=payload)

    def _capture(self, monkeypatch, client, payload):
        captured = {}
        def fake_call_ai(system, user, max_tokens, cfg):
            captured["user"] = user
            captured["system"] = system
            return _AiResult(_VALID_PIPELINE_YAML, None)
        with patch("tram.api.routers.ai._call_ai", side_effect=fake_call_ai):
            r = self._post(client, payload)
        return r, captured

    def test_fix_mode_redacts_yaml_before_calling_ai(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        r, captured = self._capture(monkeypatch, client, {
            "mode": "fix", "yaml": _SECRET_BEARING_YAML, "error": "boom",
        })
        assert r.status_code == 200
        assert "supersecret123" not in captured["user"]
        assert "***redacted***" in captured["user"]
        assert "example.com" in captured["user"]   # non-secret intact
        assert "username: op" in captured["user"]

    def test_fix_mode_redacts_singular_sink_block(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        yaml_with_singular_sink = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: local\n  path: /tmp/in\n"
            "serializer_in:\n  type: json\n"
            "sink:\n  type: smtp\n  host: mail.example.com\n  username: op\n  password: smtpsecret\n"
        )
        r, captured = self._capture(monkeypatch, client, {
            "mode": "fix", "yaml": yaml_with_singular_sink, "error": "boom",
        })
        assert r.status_code == 200
        assert "smtpsecret" not in captured["user"]
        assert "***redacted***" in captured["user"]
        assert "mail.example.com" in captured["user"]

    def test_fix_mode_redacts_headers_and_alert_webhooks(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        yaml_with_headers = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: rest\n  url: http://x\n"
            "  headers:\n    Authorization: Bearer tok123\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n  - type: local\n    path: /tmp/out\n"
            "alerts:\n  - rule_name: r1\n    webhook_url: https://hooks.example.com/xyz/tok123\n"
        )
        r, captured = self._capture(monkeypatch, client, {
            "mode": "fix", "yaml": yaml_with_headers, "error": "boom",
        })
        assert r.status_code == 200
        assert "tok123" not in captured["user"]
        assert "hooks.example.com" not in captured["user"]
        assert "Authorization" in captured["user"]   # header key kept
        assert "***redacted***" in captured["user"]

    def test_explain_mode_redacts_yaml_before_calling_ai(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        r, captured = self._capture(monkeypatch, client, {
            "mode": "explain", "yaml": _SECRET_BEARING_YAML, "error": "boom",
        })
        assert r.status_code == 200
        assert "supersecret123" not in captured["user"]
        assert "***redacted***" in captured["user"]

    def test_modify_mode_redacts_yaml_before_calling_ai(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        r, captured = self._capture(monkeypatch, client, {
            "mode": "modify", "yaml": _SECRET_BEARING_YAML, "instruction": "add sink",
        })
        assert r.status_code == 200
        assert "supersecret123" not in captured["user"]
        assert "***redacted***" in captured["user"]

    def test_env_var_refs_survive_into_prompt(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        yaml_with_env = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: sftp\n  host: example.com\n  username: op\n  password: ${SFTP_PASSWORD}\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n  - type: local\n    path: /tmp/out\n"
        )
        r, captured = self._capture(monkeypatch, client, {
            "mode": "fix", "yaml": yaml_with_env, "error": "boom",
        })
        assert r.status_code == 200
        assert "${SFTP_PASSWORD}" in captured["user"]
        assert "***redacted***" not in captured["user"]

    def test_fix_mode_redacts_api_key_fields(self, monkeypatch):
        # Issue #43: api_key values in REST/ES connector configs must not
        # reach the provider prompt.
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        yaml_with_api_key = (
            "name: pipe\n"
            "schedule:\n  type: manual\n"
            "source:\n  type: rest\n  url: https://es.example.com\n"
            "  api_key: rest-source-key\n"
            "serializer_in:\n  type: json\n"
            "sinks:\n"
            "  - type: elasticsearch\n"
            "    url: https://es.example.com\n"
            "    api_key: es-sink-key\n"
        )
        r, captured = self._capture(monkeypatch, client, {
            "mode": "fix", "yaml": yaml_with_api_key, "error": "boom",
        })
        assert r.status_code == 200
        assert "rest-source-key" not in captured["user"]
        assert "es-sink-key" not in captured["user"]
        assert "***redacted***" in captured["user"]
        assert "https://es.example.com" in captured["user"]   # non-secret intact

    def test_unparseable_yaml_refused_with_400(self, monkeypatch):
        # Issue #43: redaction fails closed — unparseable YAML returns 400 and
        # never reaches the provider, even though it may hold live secrets.
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        raw = "name: [unclosed\n  source:\n    type: sftp\n    password: keepme"
        with patch("tram.api.routers.ai._call_ai") as mock_call:
            r = self._post(client, {"mode": "explain", "yaml": raw, "error": "boom"})
        assert r.status_code == 400
        assert "YAML" in r.json()["detail"]
        mock_call.assert_not_called()


# ── A11: base_url scheme enforcement + allowlist ────────────────────────────


class TestBaseUrlScheme:
    """A11: https always OK; http only for loopback/private hosts."""

    def test_https_public_host_accepted(self):
        assert _base_url_problem("https://llm.example.com") is None
        assert _base_url_problem("https://llm.example.com/v1") is None

    def test_http_localhost_accepted(self):
        assert _base_url_problem("http://localhost:11434") is None
        assert _base_url_problem("http://my-ollama.localhost:11434") is None

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:11434",
        "http://127.8.8.8:11434",      # whole 127.0.0.0/8 is loopback
        "http://10.0.0.5:8080",
        "http://172.16.0.3:11434",
        "http://192.168.1.10:11434",
        "http://[::1]:11434",          # IPv6 loopback
        "http://[fd00::1]:11434",      # IPv6 unique-local
    ])
    def test_http_loopback_private_accepted(self, url):
        assert _base_url_problem(url) is None

    @pytest.mark.parametrize("url", [
        "http://example.com",
        "http://llm.example.com:8080",
        "http://169.254.169.254",      # link-local / cloud metadata — NOT local
        "http://100.64.0.1",           # CGNAT shared space — NOT local
        "http://8.8.8.8",
    ])
    def test_http_public_host_rejected(self, url):
        assert _base_url_problem(url) is not None

    def test_missing_scheme_rejected(self):
        # urlsplit would parse "localhost:11434" as scheme="localhost"
        assert _base_url_problem("localhost:11434") is not None
        assert _base_url_problem("llm.example.com/v1") is not None

    def test_unknown_scheme_rejected(self):
        assert _base_url_problem("ftp://example.com") is not None

    def test_empty_base_url_ok(self):
        assert _base_url_problem("") is None
        assert _base_url_problem("   ") is None

    def test_https_accepted_at_save(self):
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com"})
        assert r.status_code == 200
        db.set_setting.assert_called_with("ai.base_url", "https://llm.example.com")

    def test_http_public_rejected_at_save(self):
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "http://example.com"})
        assert r.status_code == 400
        assert "http" in r.json()["detail"]
        db.set_setting.assert_not_called()

    def test_http_loopback_accepted_at_save(self):
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "http://localhost:11434"})
        assert r.status_code == 200
        db.set_setting.assert_called_with("ai.base_url", "http://localhost:11434")

    def test_http_public_rejected_at_call(self):
        # Defense-in-depth: env-var-configured base_urls skip the config
        # endpoint, so _call_ai re-checks before attaching the API key.
        cfg = {"provider": "openai", "api_key": "k", "model": "", "base_url": "http://example.com"}
        with pytest.raises(RuntimeError, match="http"):
            _call_ai("sys", "usr", 10, cfg)

    def test_http_localhost_accepted_at_call(self):
        mock_oai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_oai.OpenAI.return_value.chat.completions.create.return_value = mock_resp
        with patch.dict(sys.modules, {"openai": mock_oai}):
            result = _call_ai("sys", "usr", 10, {"provider": "openai", "api_key": "k",
                                                 "model": "", "base_url": "http://localhost:11434"})
        assert result.text == "ok"
        assert mock_oai.OpenAI.call_args.kwargs["base_url"] == "http://localhost:11434"


class TestBaseUrlAllowlist:
    """A11: TRAM_AI_ALLOWED_BASE_URLS — prefix match on normalized URLs."""

    def test_matching_prefix_accepted_at_save(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com,http://localhost:11434")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com/v1"})
        assert r.status_code == 200
        db.set_setting.assert_called_with("ai.base_url", "https://llm.example.com/v1")

    def test_matching_local_http_accepted_at_save(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com,http://localhost:11434")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "http://localhost:11434"})
        assert r.status_code == 200

    def test_non_matching_rejected_at_save(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://other.example.com"})
        assert r.status_code == 400
        assert "ALLOWED_BASE_URLS" in r.json()["detail"]
        db.set_setting.assert_not_called()

    def test_scheme_rule_still_enforced_with_allowlist(self, monkeypatch):
        # An http allowlist entry can't whitelist a PUBLIC host — the scheme
        # rule runs first and rejects it.
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "http://llm.example.com")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "http://llm.example.com"})
        assert r.status_code == 400
        assert "http" in r.json()["detail"]

    def test_no_allowlist_no_restriction(self, monkeypatch):
        monkeypatch.delenv("TRAM_AI_ALLOWED_BASE_URLS", raising=False)
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://anything.example.com"})
        assert r.status_code == 200

    def test_prefix_match_normalizes_case_and_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://LLM.Example.com")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com/v1/"})
        assert r.status_code == 200

    # ── Review fix: origin-exact + directory-boundary matching ────────────

    def test_sibling_domain_rejected(self, monkeypatch):
        # A sibling domain shares the hostname prefix but is a different origin.
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com.evil.io"})
        assert r.status_code == 400
        assert "ALLOWED_BASE_URLS" in r.json()["detail"]
        db.set_setting.assert_not_called()

    def test_path_boundary_enforced(self, monkeypatch):
        # /v1 must not match /v1anything — only exact or directory-boundary.
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com/v1")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com/v1anything"})
        assert r.status_code == 400
        db.set_setting.assert_not_called()

    def test_default_port_normalized(self, monkeypatch):
        # https://host:443 == https://host origin; http://host:80 == http://host.
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com,http://localhost:80")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com:443/v1"})
        assert r.status_code == 200
        r = client.post("/api/ai/config", json={"base_url": "http://localhost"})
        assert r.status_code == 200

    def test_explicit_non_default_port_not_normalized(self, monkeypatch):
        # A non-default port is part of the origin — https://host:8443 differs.
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com:8443"})
        assert r.status_code == 400

    def test_exact_origin_and_directory_path_match(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com/v1")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com/v1"})
        assert r.status_code == 200
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com/v1/foo"})
        assert r.status_code == 200

    def test_shorter_submitted_path_cannot_narrow_entry(self, monkeypatch):
        # Submitting a shorter path than the entry is not an exact/boundary match.
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com/v1/deep")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"base_url": "https://llm.example.com/v1"})
        assert r.status_code == 400

    def test_base_url_allowed_unit(self):
        # Direct unit coverage of the matcher semantics.
        allowed = ["https://llm.example.com/v1", "http://localhost:11434"]
        assert _base_url_allowed("https://llm.example.com/v1", allowed) is True
        assert _base_url_allowed("https://llm.example.com/v1/foo", allowed) is True
        assert _base_url_allowed("https://llm.example.com/v1anything", allowed) is False
        assert _base_url_allowed("https://llm.example.com.evil.io/v1", allowed) is False
        assert _base_url_allowed("https://llm.example.com/v2", allowed) is False
        assert _base_url_allowed("http://localhost:11434/", allowed) is True
        assert _base_url_allowed("http://localhost:11435", allowed) is False
        assert _base_url_allowed("http://localhost", allowed) is False
        assert _base_url_allowed("", allowed) is False

    def test_non_matching_rejected_at_call(self, monkeypatch):
        # Issue #43: the allowlist is enforced in _call_ai too — a base_url
        # that passed the scheme check but is not allowlisted must raise
        # before any SDK client is constructed (and the API key attached).
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com")
        mock_oai = MagicMock()
        cfg = {"provider": "openai", "api_key": "k", "model": "",
               "base_url": "https://attacker.example.com"}
        with patch.dict(sys.modules, {"openai": mock_oai}):
            with pytest.raises(RuntimeError, match="ALLOWED_BASE_URLS"):
                _call_ai("sys", "usr", 10, cfg)
        mock_oai.OpenAI.assert_not_called()

    def test_matching_accepted_at_call(self, monkeypatch):
        # Allowlisted base_urls still reach the provider at call time.
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com/v1")
        mock_oai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_oai.OpenAI.return_value.chat.completions.create.return_value = mock_resp
        mock_oai.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_oai.APIConnectionError = type("APIConnectionError", (Exception,), {})
        mock_oai.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_oai.APIStatusError = type("APIStatusError", (Exception,), {})
        with patch.dict(sys.modules, {"openai": mock_oai}):
            result = _call_ai("sys", "usr", 10, {"provider": "openai", "api_key": "k",
                                                 "model": "", "base_url": "https://llm.example.com/v1"})
        assert result.text == "ok"


class TestAllowlistDefaultEndpoint:
    """C6: with TRAM_AI_ALLOWED_BASE_URLS set, the EFFECTIVE endpoint must be
    allowlisted — base_url if set, else the provider's fixed default endpoint.
    Previously the check ran only when base_url was set, so an unset base_url
    bypassed the allowlist entirely."""

    def test_default_endpoint_not_listed_rejected_before_client(self, monkeypatch):
        # anthropic's default endpoint is not on the allowlist → RuntimeError
        # before any SDK client is constructed (and the API key attached).
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com")
        mock_ant = MagicMock()
        cfg = {"provider": "anthropic", "api_key": "k", "model": "", "base_url": ""}
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            with pytest.raises(RuntimeError, match="ALLOWED_BASE_URLS"):
                _call_ai("sys", "usr", 10, cfg)
        mock_ant.Anthropic.assert_not_called()

    def test_default_endpoint_listed_proceeds(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://api.anthropic.com")
        mock_ant = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text="ok")]
        mock_ant.Anthropic.return_value.messages.create.return_value = mock_msg
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            result = _call_ai("sys", "usr", 10,
                              {"provider": "anthropic", "api_key": "k", "model": "", "base_url": ""})
        assert result.text == "ok"

    def test_openai_default_endpoint_not_listed_rejected(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://api.anthropic.com")
        mock_oai = MagicMock()
        cfg = {"provider": "openai", "api_key": "k", "model": "", "base_url": ""}
        with patch.dict(sys.modules, {"openai": mock_oai}):
            with pytest.raises(RuntimeError, match="ALLOWED_BASE_URLS"):
                _call_ai("sys", "usr", 10, cfg)
        mock_oai.OpenAI.assert_not_called()

    def test_openai_default_endpoint_listed_proceeds(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://api.openai.com/v1")
        mock_oai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_oai.OpenAI.return_value.chat.completions.create.return_value = mock_resp
        with patch.dict(sys.modules, {"openai": mock_oai}):
            result = _call_ai("sys", "usr", 10,
                              {"provider": "openai", "api_key": "k", "model": "", "base_url": ""})
        assert result.text == "ok"

    def test_bedrock_without_base_url_rejected_naming_provider(self, monkeypatch):
        # Bedrock has no fixed default endpoint — an allowlisted base_url is
        # required when the allowlist is active.
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://api.anthropic.com")
        cfg = {"provider": "bedrock", "api_key": "k", "model": "", "base_url": ""}
        with pytest.raises(RuntimeError, match="bedrock"):
            _call_ai("sys", "usr", 10, cfg)

    def test_bedrock_with_allowlisted_base_url_proceeds(self, monkeypatch):
        import json

        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://bedrock-proxy")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"content": [{"text": "yaml"}]}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _call_ai("sys", "usr", 10, {
                "provider": "bedrock", "api_key": "k", "model": "",
                "base_url": "https://bedrock-proxy",
            })
        assert result.text == "yaml"

    def test_base_url_still_checked_when_set(self, monkeypatch):
        # Explicit base_urls keep the existing behavior: scheme + allowlist.
        monkeypatch.setenv("TRAM_AI_ALLOWED_BASE_URLS", "https://llm.example.com")
        mock_oai = MagicMock()
        cfg = {"provider": "openai", "api_key": "k", "model": "",
               "base_url": "https://other.example.com"}
        with patch.dict(sys.modules, {"openai": mock_oai}):
            with pytest.raises(RuntimeError, match="ALLOWED_BASE_URLS"):
                _call_ai("sys", "usr", 10, cfg)
        mock_oai.OpenAI.assert_not_called()


# ── A10: per-call audit log ─────────────────────────────────────────────────


class TestAiAuditLog:
    def test_success_log_line_has_all_fields(self, monkeypatch, caplog):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        monkeypatch.delenv("TRAM_AI_AUDIT", raising=False)
        app = _make_app()
        client = TestClient(app)
        with caplog.at_level(logging.INFO, logger="tram.ai"):
            with patch("tram.api.routers.ai._call_ai",
                       return_value=_AiResult("name: my-pipe", None, tokens_in=10, tokens_out=25)):
                r = client.post("/api/ai/suggest", json={"mode": "generate", "prompt": "x"})
        assert r.status_code == 200
        records = [rec for rec in caplog.records if rec.name == "tram.ai"]
        assert len(records) == 1
        rec = records[0]
        assert rec.getMessage() == "AI call completed"
        assert rec.mode == "generate"
        assert rec.client == "testclient"
        assert rec.provider == "anthropic"
        assert rec.model == "claude-haiku-4-5-20251001"
        assert rec.tokens_in == 10
        assert rec.tokens_out == 25
        assert rec.ok is True
        assert rec.duration_s >= 0

    def test_error_outcome_logged(self, monkeypatch, caplog):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with caplog.at_level(logging.WARNING, logger="tram.ai"):
            with patch("tram.api.routers.ai._call_ai", side_effect=RuntimeError("boom")):
                r = client.post("/api/ai/suggest", json={"mode": "explain", "yaml": "x", "error": "e"})
        assert r.status_code == 502
        records = [rec for rec in caplog.records if rec.name == "tram.ai"]
        assert len(records) == 1
        rec = records[0]
        assert rec.getMessage() == "AI call failed"
        assert rec.mode == "explain"
        assert rec.ok is False
        assert rec.tokens_in is None
        assert rec.tokens_out is None

    def test_ai_test_endpoint_logs_mode_test(self, monkeypatch, caplog):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()
        client = TestClient(app)
        with caplog.at_level(logging.INFO, logger="tram.ai"):
            with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("OK", None)):
                r = client.post("/api/ai/test")
        assert r.status_code == 200
        records = [rec for rec in caplog.records if rec.name == "tram.ai"]
        assert len(records) == 1
        assert records[0].mode == "test"
        assert records[0].ok is True


# ── A10: ai_usage persistence (gated by TRAM_AI_AUDIT) ──────────────────────


class TestAiUsagePersistence:
    def test_row_appended_when_audit_enabled(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        monkeypatch.setenv("TRAM_AI_AUDIT", "1")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai",
                   return_value=_AiResult("name: p", None, tokens_in=3, tokens_out=7)):
            r = client.post("/api/ai/suggest", json={"mode": "modify", "yaml": "x", "instruction": "i"})
        assert r.status_code == 200
        assert db.append_ai_usage.call_count == 1
        kwargs = db.append_ai_usage.call_args.kwargs
        assert kwargs["mode"] == "modify"
        assert kwargs["client"] == "testclient"
        assert kwargs["provider"] == "anthropic"
        assert kwargs["model"] == "claude-haiku-4-5-20251001"
        assert kwargs["tokens_in"] == 3
        assert kwargs["tokens_out"] == 7
        assert kwargs["ok"] is True
        # Issue #24: the usage row carries the schema identity the prompt was
        # built against.
        assert kwargs["schema_version"] == schema_version()

    def test_row_absent_when_audit_disabled(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        monkeypatch.setenv("TRAM_AI_AUDIT", "0")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("OK", None)):
            r = client.post("/api/ai/test")
        assert r.status_code == 200
        db.append_ai_usage.assert_not_called()

    def test_error_outcome_recorded_with_ok_false(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        monkeypatch.setenv("TRAM_AI_AUDIT", "1")
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", side_effect=RuntimeError("boom")):
            r = client.post("/api/ai/suggest", json={"mode": "fix", "yaml": "x", "error": "e"})
        assert r.status_code == 502
        assert db.append_ai_usage.call_count == 1
        kwargs = db.append_ai_usage.call_args.kwargs
        assert kwargs["ok"] is False
        assert kwargs["tokens_in"] is None
        assert kwargs["tokens_out"] is None

    def test_no_db_does_not_crash_audit(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app()  # app.state.db unset
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("OK", None)):
            r = client.post("/api/ai/test")
        assert r.status_code == 200

    def test_usage_persistence_failure_swallowed(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        monkeypatch.setenv("TRAM_AI_AUDIT", "1")
        db = _make_db()
        db.append_ai_usage.side_effect = RuntimeError("db down")
        app = _make_app(db=db)
        client = TestClient(app)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("OK", None)):
            r = client.post("/api/ai/test")
        assert r.status_code == 200  # audit failure must not break the AI call


# ── A6: template-grounded generation ─────────────────────────────────────────


class TestTemplateGroundedGeneration:
    """A6: generate-mode few-shot grounding from the bundled template library."""

    def _template(self, name, source, sinks, yaml_text):
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

    def _capture_system(self, client, payload):
        captured = {}

        def fake_call_ai(system, user, max_tokens, cfg):
            captured["system"] = system
            return _AiResult(_VALID_PIPELINE_YAML, "end_turn")

        with patch("tram.api.routers.ai._call_ai", side_effect=fake_call_ai):
            r = client.post("/api/ai/suggest", json=payload)
        return r, captured

    def test_generate_embeds_best_matching_template(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        templates = [
            self._template("kafka-to-local", "kafka", ["local"], "name: kafka-to-local\n..."),
            self._template("sftp-to-s3", "sftp", ["s3"], "name: sftp-to-s3\n..."),
        ]

        async def fake_templates(request):
            return templates

        with patch("tram.api.routers.ai._bundled_templates", side_effect=fake_templates):
            r, captured = self._capture_system(client, {
                "mode": "generate", "prompt": "read from kafka write to local",
            })
        assert r.status_code == 200
        assert r.json()["valid"] is True
        assert "WORKED TEMPLATE EXAMPLES" in captured["system"]
        assert "### Template: kafka-to-local" in captured["system"]
        assert "### Template: sftp-to-s3" not in captured["system"]  # no tag match

    def test_generate_without_templates_is_ungrounded(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        # No app.state.config → _bundled_templates returns [] → no grounding,
        # and the call still succeeds (best-effort).
        client = TestClient(_make_app())
        r, captured = self._capture_system(client, {"mode": "generate", "prompt": "kafka to local"})
        assert r.status_code == 200
        assert "WORKED TEMPLATE EXAMPLES" not in captured["system"]

    def test_generate_grounding_is_bounded(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        big = "name: big\n" + ("# pad\n" * 800)  # ~4 KB per template
        templates = [self._template(f"t{i}", "kafka", ["local"], big) for i in range(6)]

        async def fake_templates(request):
            return templates

        with patch("tram.api.routers.ai._bundled_templates", side_effect=fake_templates):
            r, captured = self._capture_system(client, {"mode": "generate", "prompt": "kafka local"})
        assert r.status_code == 200
        assert "WORKED TEMPLATE EXAMPLES" in captured["system"]
        # count capped at 3, per-template content truncated
        assert captured["system"].count("### Template:") <= 3
        assert "… (truncated)" in captured["system"]


# ── A7: fix-mode validate-and-retry loop ─────────────────────────────────────


class TestFixRetryLoop:
    """A7: fix mode validates the model output and retries at most once."""

    _INVALID = "name: broken\n"  # parses but fails load_pipeline_from_yaml

    def _client(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        return TestClient(_make_app())

    def _post(self, client):
        return client.post("/api/ai/suggest", json={
            "mode": "fix", "yaml": _VALID_PIPELINE_YAML, "error": "sink write failed",
        })

    def test_valid_first_attempt_does_not_retry(self, monkeypatch):
        client = self._client(monkeypatch)
        with patch("tram.api.routers.ai._call_ai",
                   return_value=_AiResult(_VALID_PIPELINE_YAML, "end_turn")) as mock_call:
            r = self._post(client)
        assert r.status_code == 200
        data = r.json()
        assert data["valid"] is True
        assert data["retried"] is False
        assert data["attempts"] == 1
        mock_call.assert_called_once()

    def test_invalid_first_attempt_retries_once_and_recovers(self, monkeypatch):
        client = self._client(monkeypatch)
        calls = []

        def fake_call_ai(system, user, max_tokens, cfg):
            calls.append(user)
            return _AiResult(_VALID_PIPELINE_YAML if len(calls) == 2 else self._INVALID, "end_turn")

        with patch("tram.api.routers.ai._call_ai", side_effect=fake_call_ai):
            r = self._post(client)
        assert r.status_code == 200
        data = r.json()
        assert data["valid"] is True
        assert data["retried"] is True
        assert data["attempts"] == 2
        assert len(calls) == 2
        # the retry prompt feeds the validation error back into the model
        assert "failed TRAM validation" in calls[1]
        assert "validation errors" in calls[1]

    def test_retry_still_invalid_surfaces_final_issues(self, monkeypatch):
        client = self._client(monkeypatch)
        with patch("tram.api.routers.ai._call_ai",
                   return_value=_AiResult(self._INVALID, "end_turn")) as mock_call:
            r = self._post(client)
        assert r.status_code == 200
        data = r.json()
        assert data["valid"] is False
        assert data["retried"] is True
        assert data["attempts"] == 2
        assert data["issues"]
        assert mock_call.call_count == 2  # never loops more than once

    def test_retry_attempt_is_audited_with_retried_flag(self, monkeypatch, caplog):
        client = self._client(monkeypatch)
        calls = []

        def fake_call_ai(system, user, max_tokens, cfg):
            calls.append(user)
            return _AiResult(_VALID_PIPELINE_YAML if len(calls) == 2 else self._INVALID, "end_turn")

        with caplog.at_level(logging.INFO, logger="tram.ai"):
            with patch("tram.api.routers.ai._call_ai", side_effect=fake_call_ai):
                r = self._post(client)
        assert r.status_code == 200
        records = [rec for rec in caplog.records if rec.name == "tram.ai"]
        assert len(records) == 2
        assert [rec.retried for rec in records] == [False, True]
        assert all(rec.mode == "fix" for rec in records)
        assert all(rec.ok is True for rec in records)


# ── B1: run-failure triage mode ──────────────────────────────────────────────


def _failed_run(**kw):
    """A realistic failed RunResult for triage tests."""
    defaults = dict(
        run_id="run-abc",
        pipeline_name="kafka-pipe",
        status=RunStatus.FAILED,
        started_at=datetime(2026, 9, 24, 1, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 24, 1, 0, 45, tzinfo=UTC),
        records_in=50000,
        records_out=100,
        records_skipped=49800,
        bytes_in=512000,
        bytes_out=4096,
        error="Sink write failed: connection reset by peer",
        dlq_count=120,
        errors=[
            "Invalid record: bad timestamp",
            "Invalid record: bad timestamp",
            "Invalid record: bad timestamp",
            "Transform failed: division by zero",
        ],
    )
    defaults.update(kw)
    return RunResult(**defaults)


class TestGroupSkipReasons:
    def test_groups_and_counts_descending(self):
        errors = ["a", "b", "a", "a", "b", "c"]
        assert _group_skip_reasons(errors) == [("a", 3), ("b", 2), ("c", 1)]

    def test_caps_groups_and_truncates_reasons(self):
        errors = [f"reason-{i}-" + "x" * 500 for i in range(10)]
        groups = _group_skip_reasons(errors, max_groups=3, max_reason_chars=10)
        assert len(groups) == 3
        assert all(len(reason) <= 10 for reason, _ in groups)

    def test_non_string_errors_coerced(self):
        assert _group_skip_reasons([1, 1, "a"]) == [("1", 2), ("a", 1)]


class TestBuildTriageContext:
    def test_includes_counters_error_and_redacted_yaml(self):
        run = _failed_run().to_dict()
        ctx = _build_triage_context(run, "name: kafka-pipe\n")
        assert "Pipeline: kafka-pipe" in ctx
        assert "Status: failed" in ctx
        assert "Records skipped: 49800" in ctx
        assert "DLQ count: 120" in ctx
        assert "Sink write failed: connection reset by peer" in ctx
        assert "3x Invalid record: bad timestamp" in ctx
        assert "1x Transform failed: division by zero" in ctx
        assert "Pipeline YAML (secrets redacted):\nname: kafka-pipe" in ctx

    def test_omits_yaml_when_none(self):
        run = _failed_run().to_dict()
        ctx = _build_triage_context(run, None)
        assert "Pipeline YAML" not in ctx
        assert "Records skipped: 49800" in ctx


class TestTriageMode:
    """B1: mode='triage' explains a failed run from its run-history row."""

    def _make_controller(self, run=None, pipeline_yaml=None, pipeline_registered=True):
        controller = MagicMock()
        controller.get_run.return_value = run
        if pipeline_registered:
            state = MagicMock()
            state.yaml_text = pipeline_yaml
            controller.get.return_value = state
        else:
            controller.get.return_value = None
        return controller

    def _client(self, monkeypatch, controller, db=None):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        app = _make_app(db=db)
        app.state.controller = controller
        return TestClient(app)

    def _capture(self, client, payload):
        captured = {}

        def fake_call_ai(system, user, max_tokens, cfg):
            captured["system"] = system
            captured["user"] = user
            return _AiResult("The sink connection failed, so records were skipped.", None)

        with patch("tram.api.routers.ai._call_ai", side_effect=fake_call_ai):
            r = client.post("/api/ai/suggest", json=payload)
        return r, captured

    def test_missing_run_id_returns_400(self, monkeypatch):
        client = self._client(monkeypatch, self._make_controller(run=None))
        r = client.post("/api/ai/suggest", json={"mode": "triage"})
        assert r.status_code == 400
        assert "run_id" in r.json()["detail"]

    def test_unknown_run_id_returns_404(self, monkeypatch):
        client = self._client(monkeypatch, self._make_controller(run=None))
        r = client.post("/api/ai/suggest", json={"mode": "triage", "run_id": "nope"})
        assert r.status_code == 404
        assert "nope" in r.json()["detail"]

    def test_no_controller_returns_503(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())  # app.state.controller unset
        r = client.post("/api/ai/suggest", json={"mode": "triage", "run_id": "run-abc"})
        assert r.status_code == 503

    def test_builds_context_and_redacts_yaml(self, monkeypatch):
        controller = self._make_controller(
            run=_failed_run(), pipeline_yaml=_SECRET_BEARING_YAML,
        )
        client = self._client(monkeypatch, controller)
        r, captured = self._capture(client, {"mode": "triage", "run_id": "run-abc"})
        assert r.status_code == 200
        data = r.json()
        assert data["explanation"]
        assert data["run_id"] == "run-abc"
        assert data["pipeline"] == "kafka-pipe"
        assert data["status"] == "failed"
        user = captured["user"]
        assert "Records skipped: 49800" in user
        assert "DLQ count: 120" in user
        assert "Sink write failed: connection reset by peer" in user
        assert "3x Invalid record: bad timestamp" in user
        assert "1x Transform failed: division by zero" in user
        # A4 redaction discipline: pipeline secrets never reach the provider
        assert "supersecret123" not in user
        assert "***redacted***" in user
        assert "example.com" in user  # non-secret config intact

    def test_unregistered_pipeline_omits_yaml_still_triages(self, monkeypatch):
        controller = self._make_controller(
            run=_failed_run(), pipeline_yaml=None, pipeline_registered=False,
        )
        client = self._client(monkeypatch, controller)
        r, captured = self._capture(client, {"mode": "triage", "run_id": "run-abc"})
        assert r.status_code == 200
        assert "Pipeline YAML" not in captured["user"]
        assert "Records skipped: 49800" in captured["user"]

    def test_unredactable_yaml_omitted_fails_closed(self, monkeypatch):
        controller = self._make_controller(
            run=_failed_run(), pipeline_yaml="name: [unclosed\n  password: secret\n",
        )
        client = self._client(monkeypatch, controller)
        r, captured = self._capture(client, {"mode": "triage", "run_id": "run-abc"})
        assert r.status_code == 200
        assert "Pipeline YAML" not in captured["user"]  # never sent unredacted
        assert "password" not in captured["user"]

    def test_audit_row_records_mode_triage(self, monkeypatch):
        monkeypatch.setenv("TRAM_AI_AUDIT", "1")
        db = _make_db()
        controller = self._make_controller(
            run=_failed_run(), pipeline_yaml=None, pipeline_registered=False,
        )
        client = self._client(monkeypatch, controller, db=db)
        with patch("tram.api.routers.ai._call_ai", return_value=_AiResult("ctx", None)):
            r = client.post("/api/ai/suggest", json={"mode": "triage", "run_id": "run-abc"})
        assert r.status_code == 200
        assert db.append_ai_usage.call_count == 1
        kwargs = db.append_ai_usage.call_args.kwargs
        assert kwargs["mode"] == "triage"
        assert kwargs["ok"] is True
        assert kwargs["schema_version"] == schema_version()

    def test_provider_error_returns_502(self, monkeypatch):
        controller = self._make_controller(
            run=_failed_run(), pipeline_yaml=None, pipeline_registered=False,
        )
        client = self._client(monkeypatch, controller)
        with patch("tram.api.routers.ai._call_ai", side_effect=RuntimeError("API down")):
            r = client.post("/api/ai/suggest", json={"mode": "triage", "run_id": "run-abc"})
        assert r.status_code == 502
