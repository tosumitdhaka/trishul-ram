"""Tests for the TRAM_HTTP_ACCELERATED runtime selection + reporting (v1.7.0 Pilot A).

Covers: flag-off byte-for-byte today's uvicorn args, flag-on passing
``loop="uvloop"`` / ``http="httptools"`` when both packages import, the
WARNING + default-runtime fallback when either is missing, and the
runtime-reporting helpers that derive names from the ACTUAL loop/protocol
classes (never by echoing the config).
"""
from __future__ import annotations

import builtins
import logging
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tram.daemon.server import (
    _http_accel_kwargs,
    _http_runtime_name,
    _loop_runtime_name,
    _resolve_active_runtime,
    serve,
)

# ── Helpers (mirror test_daemon_server.py) ────────────────────────────────────


def _thread_factory(run_targets: list):
    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target
            self.name = name
            self.daemon = daemon
            self._alive = False

        def start(self):
            self._alive = True
            run_targets.append(self)
            if self._target is not None:
                self._target()
            self._alive = False

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return self._alive

    return FakeThread


def _make_config(*, tram_mode: str = "standalone", host: str = "0.0.0.0", port: int = 8765):
    """Return a minimal AppConfig instance (same shape as test_daemon_server.py)."""
    from tram.core.config import AppConfig

    return AppConfig(
        host=host,
        port=port,
        pipeline_dir="./pipelines",
        state_dir=None,
        api_url="http://localhost:8765",
        log_level="INFO",
        log_format="json",
        workers=1,
        reload_on_start=False,
        node_id="test-node",
        db_url="",
        shutdown_timeout=30,
        api_key="",
        rate_limit=0,
        rate_limit_window=60,
        tls_certfile="",
        tls_keyfile="",
        otel_endpoint="",
        otel_service="tram",
        watch_pipelines=False,
        mib_dir="/mibs",
        schema_dir="/schemas",
        schema_registry_url="",
        schema_registry_username="",
        schema_registry_password="",
        ui_dir="/ui",
        auth_users="",
        templates_dir="/tram-templates",
        tram_mode=tram_mode,
        manager_url="",
        stats_interval=30,
        worker_urls="",
        worker_replicas=0,
        worker_service="tram-worker",
        worker_namespace="default",
        worker_port=8766,
        worker_ingress_port=8767,
    )


def _import_guard(mock_importable: set[str], missing: set[str]):
    """builtins.__import__ side_effect: mock some module names, pass all else through."""
    real_import = builtins.__import__

    def _guard(name, *args, **kwargs):
        if name in missing:
            raise ImportError(f"No module named {name!r}")
        if name in mock_importable:
            return MagicMock()
        return real_import(name, *args, **kwargs)

    return _guard


# ── Flag-off (default): today's uvicorn args ──────────────────────────────────


class TestFlagOff:
    def test_manager_flag_off_passes_no_loop_or_http_kwargs(self, monkeypatch):
        """TRAM_HTTP_ACCELERATED off (default) → uvicorn gets today's args."""
        monkeypatch.delenv("TRAM_HTTP_ACCELERATED", raising=False)
        config = _make_config()

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"), \
             patch("signal.getsignal", return_value=signal.SIG_DFL):
            serve(config)

        _, kwargs = mock_run.call_args
        assert "loop" not in kwargs
        assert "http" not in kwargs

    def test_worker_flag_off_passes_no_loop_or_http_kwargs(self, monkeypatch):
        """Worker agent + webhook ingress also keep today's args when flag is off."""
        monkeypatch.delenv("TRAM_HTTP_ACCELERATED", raising=False)
        config = _make_config(tram_mode="worker")
        fake_agent_app = MagicMock()
        fake_agent_app.state = SimpleNamespace()
        created_threads = []

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("os.kill"), \
             patch("tram.agent.server.create_worker_app", return_value=fake_agent_app), \
             patch("tram.agent.server.create_worker_ingress_app", return_value=MagicMock()), \
             patch("threading.Thread", side_effect=_thread_factory(created_threads)):
            serve(config)

        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            assert "loop" not in call.kwargs
            assert "http" not in call.kwargs

    def test_flag_accessor_defaults_off(self, monkeypatch):
        """http_accelerated() reads the env each call and defaults to off."""
        monkeypatch.delenv("TRAM_HTTP_ACCELERATED", raising=False)
        assert _http_accel_kwargs() == {}
        monkeypatch.setenv("TRAM_HTTP_ACCELERATED", "1")
        # Guard the import: the dev venv does not install http_accel extras.
        guard = _import_guard(mock_importable={"uvloop", "httptools"}, missing=set())
        with patch("builtins.__import__", side_effect=guard):
            assert _http_accel_kwargs() == {"loop": "uvloop", "http": "httptools"}


# ── Flag on + both packages importable ────────────────────────────────────────


class TestFlagOnWithPackages:
    def test_manager_passes_loop_and_http(self, monkeypatch):
        """TRAM_HTTP_ACCELERATED=1 + both importable → uvloop/httptools passed."""
        monkeypatch.setenv("TRAM_HTTP_ACCELERATED", "1")
        guard = _import_guard(mock_importable={"uvloop", "httptools"}, missing=set())
        config = _make_config()

        with patch("tram.daemon.server.setup_logging"), \
             patch("builtins.__import__", side_effect=guard), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"), \
             patch("signal.getsignal", return_value=signal.SIG_DFL):
            serve(config)

        _, kwargs = mock_run.call_args
        assert kwargs["loop"] == "uvloop"
        assert kwargs["http"] == "httptools"

    def test_worker_agent_and_ingress_get_accel_kwargs(self, monkeypatch):
        """Flag on applies to BOTH worker servers (agent + webhook ingress)."""
        monkeypatch.setenv("TRAM_HTTP_ACCELERATED", "1")
        guard = _import_guard(mock_importable={"uvloop", "httptools"}, missing=set())
        config = _make_config(tram_mode="worker")
        fake_agent_app = MagicMock()
        fake_agent_app.state = SimpleNamespace()
        created_threads = []

        with patch("tram.daemon.server.setup_logging"), \
             patch("builtins.__import__", side_effect=guard), \
             patch("uvicorn.run") as mock_run, \
             patch("os.kill"), \
             patch("tram.agent.server.create_worker_app", return_value=fake_agent_app), \
             patch("tram.agent.server.create_worker_ingress_app", return_value=MagicMock()), \
             patch("threading.Thread", side_effect=_thread_factory(created_threads)):
            serve(config)

        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            assert call.kwargs["loop"] == "uvloop"
            assert call.kwargs["http"] == "httptools"


# ── Flag on + import failure: WARNING + default args ──────────────────────────


class TestFlagOnMissingPackage:
    @pytest.mark.parametrize("missing", ["uvloop", "httptools", ["uvloop", "httptools"]])
    def test_warns_and_uses_default_args(self, monkeypatch, caplog, missing):
        """Either package missing → one WARNING naming it + today's uvicorn args."""
        monkeypatch.setenv("TRAM_HTTP_ACCELERATED", "1")
        # Normalize once: the parametrize passes a bare string for single
        # packages — set("uvloop") would char-split. Mock the packages that
        # should be PRESENT (the dev venv installs neither), block the rest.
        missing_list = missing if isinstance(missing, list) else [missing]
        guard = _import_guard(mock_importable={"uvloop", "httptools"} - set(missing_list),
                              missing=set(missing_list))
        config = _make_config()

        with patch("tram.daemon.server.setup_logging"), \
             patch("builtins.__import__", side_effect=guard), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"), \
             patch("signal.getsignal", return_value=signal.SIG_DFL):
            with caplog.at_level(logging.WARNING, logger="tram.daemon.server"):
                serve(config)

        _, kwargs = mock_run.call_args
        assert "loop" not in kwargs
        assert "http" not in kwargs
        expected = " and ".join(missing if isinstance(missing, list) else [missing])
        assert f"TRAM_HTTP_ACCELERATED=1 but {expected} not installed" in caplog.text


# ── Runtime-reporting helpers inspect the ACTUAL classes ──────────────────────


class TestRuntimeReporting:
    def test_loop_runtime_name_uses_actual_loop_class(self):
        """Loop names come from the ACTUAL class module, not the config value."""
        uvloop_cls = type("Loop", (), {"__module__": "uvloop"})
        asyncio_cls = type("Loop", (), {"__module__": "asyncio.events"})

        assert _loop_runtime_name(uvloop_cls()) == "uvloop"
        assert _loop_runtime_name(uvloop_cls) == "uvloop"  # class also accepted
        assert _loop_runtime_name(asyncio_cls()) == "asyncio"

    def test_http_runtime_name_uses_actual_protocol_class(self):
        """Parser names come from the ACTUAL uvicorn protocol class module."""
        httptools_cls = type(
            "HttpToolsProtocol", (), {"__module__": "uvicorn.protocols.http.httptools_impl"}
        )
        h11_cls = type("H11Protocol", (), {"__module__": "uvicorn.protocols.http.h11_impl"})

        assert _http_runtime_name(httptools_cls) == "httptools"
        assert _http_runtime_name(h11_cls) == "h11"

    def test_resolve_active_runtime_explicit_asyncio_h11(self):
        """Explicit asyncio/h11 selection resolves to exactly those runtimes."""
        assert _resolve_active_runtime("asyncio", "h11") == ("asyncio", "h11")