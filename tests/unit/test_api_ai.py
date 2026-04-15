"""Tests for AI assist router — /api/ai/status, config, test, suggest."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tram.api.routers.ai import _call_ai, _get_ai_cfg, _strip_fences, router


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
        mock_ant.Anthropic.return_value.messages.create.return_value = mock_msg
        with patch.dict(sys.modules, {"anthropic": mock_ant}):
            result = _call_ai("system", "user", 100, self._cfg())
        assert result == "name: my-pipe"

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
            _call_ai("sys", "usr", 100, self._cfg(base_url="http://proxy/v1"))
        call_kwargs = mock_ant.Anthropic.call_args.kwargs
        assert call_kwargs.get("base_url") == "http://proxy"


class TestCallAiOpenAI:
    def _cfg(self, **kw):
        return {"provider": "openai", "api_key": "sk-key",
                "model": "", "base_url": "", **kw}

    def test_success(self):
        mock_oai = MagicMock()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_oai.OpenAI.return_value.chat.completions.create.return_value = mock_resp
        with patch.dict(sys.modules, {"openai": mock_oai}):
            result = _call_ai("system", "user", 100, self._cfg())
        assert result == "ok"

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
            _call_ai("sys", "usr", 100, self._cfg(base_url="http://my-proxy"))
        call_kwargs = mock_oai.OpenAI.call_args.kwargs
        assert call_kwargs.get("base_url") == "http://my-proxy"


class TestCallAiBedrock:
    def _cfg(self, **kw):
        return {"provider": "bedrock", "api_key": "key",
                "model": "", "base_url": "http://bedrock-proxy", **kw}

    def test_success(self):
        import json
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"content": [{"text": "pipeline yaml"}]}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _call_ai("sys", "usr", 100, self._cfg())
        assert result == "pipeline yaml"

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

    def test_clears_empty_keys(self):
        db = _make_db()
        app = _make_app(db=db)
        client = TestClient(app)
        r = client.post("/api/ai/config", json={"provider": "", "api_key": ""})
        assert r.status_code == 200
        db.delete_setting.assert_called()


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
        with patch("tram.api.routers.ai._call_ai", return_value="OK"):
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
        with patch("tram.api.routers.ai._call_ai", return_value="name: my-pipe"):
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
        with patch("tram.api.routers.ai._call_ai", return_value="The error means X"):
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
        with patch("tram.api.routers.ai._call_ai", return_value="name: fixed-pipe"):
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
        with patch("tram.api.routers.ai._call_ai", return_value="name: modified-pipe"):
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
