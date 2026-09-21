"""Tests for AI assist router — /api/ai/status, config, test, suggest."""
from __future__ import annotations

import logging
import sys
from unittest.mock import MagicMock, call, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.ai import (
    _AiResult,
    _base_url_allowed,
    _base_url_problem,
    _call_ai,
    _get_ai_cfg,
    _redact_yaml,
    _strip_fences,
    _yaml_mode_result,
    router,
)

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

    def test_unparseable_yaml_returned_unchanged(self):
        raw = "name: [unclosed\n  source:\n    type: sftp\n    password: keepme"
        assert _redact_yaml(raw) == raw

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

    def test_unparseable_yaml_sent_unchanged(self, monkeypatch):
        # Mid-edit YAML that won't parse is sent as-is so explain/fix still work.
        monkeypatch.setenv("TRAM_AI_API_KEY", "sk-test")
        client = TestClient(_make_app())
        raw = "name: [unclosed\n  source:\n    type: sftp\n    password: keepme"
        r, captured = self._capture(monkeypatch, client, {
            "mode": "explain", "yaml": raw, "error": "boom",
        })
        assert r.status_code == 200
        assert "keepme" in captured["user"]


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
