"""Unit tests for tram/daemon/server.py — serve() function."""
from __future__ import annotations

import signal
from dataclasses import dataclass
from unittest.mock import MagicMock, call, patch

import pytest

from tram.daemon.server import serve


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_config(
    *,
    tram_mode: str = "standalone",
    host: str = "0.0.0.0",
    port: int = 8765,
    workers: int = 1,
    node_id: str = "test-node",
    manager_url: str = "",
    tls_certfile: str = "",
    tls_keyfile: str = "",
    log_level: str = "INFO",
    log_format: str = "json",
):
    """Return a minimal AppConfig-like dataclass instance."""
    from tram.core.config import AppConfig
    # Build via from_env so we always get a real frozen dataclass;
    # override the fields we care about by constructing manually.
    import socket

    return AppConfig(
        host=host,
        port=port,
        pipeline_dir="./pipelines",
        state_dir=None,
        api_url="http://localhost:8765",
        log_level=log_level,
        log_format=log_format,
        workers=workers,
        reload_on_start=False,
        node_id=node_id,
        db_url="",
        shutdown_timeout=30,
        api_key="",
        rate_limit=0,
        rate_limit_window=60,
        tls_certfile=tls_certfile,
        tls_keyfile=tls_keyfile,
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
        manager_url=manager_url,
        worker_urls="",
        worker_replicas=0,
        worker_service="tram-worker",
        worker_namespace="default",
        worker_port=8766,
    )


# ── Worker branch ──────────────────────────────────────────────────────────────


class TestServeWorkerBranch:
    """Tests for the TRAM_MODE=worker path in serve()."""

    def test_worker_calls_create_worker_app(self):
        """serve() instantiates a worker app and passes it to uvicorn.run."""
        config = _make_config(tram_mode="worker", node_id="w1", manager_url="http://mgr:8765")
        fake_app = MagicMock()

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.agent.server.create_worker_app", return_value=fake_app) as mock_cwa:
            serve(config)

        mock_cwa.assert_called_once_with(worker_id="w1", manager_url="http://mgr:8765")
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] is fake_app

    def test_worker_default_port_8766(self, monkeypatch):
        """Worker defaults to port 8766 when TRAM_WORKER_PORT env var is absent."""
        config = _make_config(tram_mode="worker")
        monkeypatch.delenv("TRAM_WORKER_PORT", raising=False)

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.agent.server.create_worker_app", return_value=MagicMock()):
            serve(config)

        _, kwargs = mock_run.call_args
        assert kwargs["port"] == 8766

    def test_worker_custom_port_from_env(self, monkeypatch):
        """Worker reads port from TRAM_WORKER_PORT environment variable."""
        config = _make_config(tram_mode="worker")
        monkeypatch.setenv("TRAM_WORKER_PORT", "9000")

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.agent.server.create_worker_app", return_value=MagicMock()):
            serve(config)

        _, kwargs = mock_run.call_args
        assert kwargs["port"] == 9000

    def test_worker_uvicorn_kwargs_no_tls(self, monkeypatch):
        """Worker uvicorn call has expected kwargs when TLS is disabled."""
        config = _make_config(tram_mode="worker", host="127.0.0.1")
        monkeypatch.delenv("TRAM_WORKER_PORT", raising=False)

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.agent.server.create_worker_app", return_value=MagicMock()):
            serve(config)

        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["workers"] == 1
        assert kwargs["log_config"] is None
        assert kwargs["access_log"] is False
        assert "ssl_certfile" not in kwargs
        assert "ssl_keyfile" not in kwargs

    def test_worker_tls_kwargs_added(self, monkeypatch):
        """Worker uvicorn call includes ssl_ kwargs when TLS cert+key are set."""
        config = _make_config(
            tram_mode="worker",
            tls_certfile="/etc/tls/cert.pem",
            tls_keyfile="/etc/tls/key.pem",
        )
        monkeypatch.delenv("TRAM_WORKER_PORT", raising=False)

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.agent.server.create_worker_app", return_value=MagicMock()):
            serve(config)

        _, kwargs = mock_run.call_args
        assert kwargs["ssl_certfile"] == "/etc/tls/cert.pem"
        assert kwargs["ssl_keyfile"] == "/etc/tls/key.pem"

    def test_worker_tls_only_certfile_no_ssl_kwargs(self, monkeypatch):
        """Only certfile set (no keyfile) → TLS kwargs must NOT be added."""
        config = _make_config(
            tram_mode="worker",
            tls_certfile="/etc/tls/cert.pem",
            tls_keyfile="",  # keyfile missing
        )
        monkeypatch.delenv("TRAM_WORKER_PORT", raising=False)

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.agent.server.create_worker_app", return_value=MagicMock()):
            serve(config)

        _, kwargs = mock_run.call_args
        assert "ssl_certfile" not in kwargs
        assert "ssl_keyfile" not in kwargs

    def test_worker_returns_after_uvicorn(self, monkeypatch):
        """serve() returns (does not fall through to manager branch) after worker path."""
        config = _make_config(tram_mode="worker")
        monkeypatch.delenv("TRAM_WORKER_PORT", raising=False)

        mock_create_app = MagicMock()
        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run"), \
             patch("tram.agent.server.create_worker_app", return_value=MagicMock()):
            # Patch create_app so we can assert it's never called
            with patch("tram.api.app.create_app", mock_create_app):
                serve(config)

        mock_create_app.assert_not_called()


# ── Manager / standalone branch ────────────────────────────────────────────────


class TestServeManagerBranch:
    """Tests for the manager and standalone paths in serve()."""

    @pytest.mark.parametrize("mode", ["standalone", "manager"])
    def test_manager_calls_create_app(self, mode):
        """serve() calls create_app(config) and passes the result to uvicorn.run."""
        config = _make_config(tram_mode=mode)
        fake_app = MagicMock()

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.api.app.create_app", return_value=fake_app) as mock_ca, \
             patch("signal.signal"):
            serve(config)

        mock_ca.assert_called_once_with(config)
        args, kwargs = mock_run.call_args
        assert args[0] is fake_app

    @pytest.mark.parametrize("mode", ["standalone", "manager"])
    def test_manager_uvicorn_kwargs_no_tls(self, mode):
        """Manager uvicorn call has correct host/port/workers and no ssl kwargs."""
        config = _make_config(tram_mode=mode, host="0.0.0.0", port=8765, workers=2)

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"):
            serve(config)

        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "0.0.0.0"
        assert kwargs["port"] == 8765
        assert kwargs["workers"] == 2
        assert kwargs["log_config"] is None
        assert kwargs["access_log"] is False
        assert "ssl_certfile" not in kwargs
        assert "ssl_keyfile" not in kwargs

    def test_manager_tls_kwargs_added(self):
        """Manager uvicorn call includes ssl_ kwargs when both cert+key are set."""
        config = _make_config(
            tram_mode="standalone",
            tls_certfile="/etc/tls/cert.pem",
            tls_keyfile="/etc/tls/key.pem",
        )

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"):
            serve(config)

        _, kwargs = mock_run.call_args
        assert kwargs["ssl_certfile"] == "/etc/tls/cert.pem"
        assert kwargs["ssl_keyfile"] == "/etc/tls/key.pem"

    def test_manager_tls_only_certfile_no_ssl_kwargs(self):
        """Only certfile set → TLS kwargs must NOT be added for manager branch."""
        config = _make_config(
            tram_mode="standalone",
            tls_certfile="/etc/tls/cert.pem",
            tls_keyfile="",
        )

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"):
            serve(config)

        _, kwargs = mock_run.call_args
        assert "ssl_certfile" not in kwargs
        assert "ssl_keyfile" not in kwargs

    def test_manager_sigterm_handler_installed(self):
        """serve() installs a SIGTERM handler on the manager/standalone path."""
        config = _make_config(tram_mode="standalone")
        installed_signals = {}

        def fake_signal(signum, handler):
            installed_signals[signum] = handler

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run"), \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal", side_effect=fake_signal), \
             patch("signal.getsignal", return_value=signal.SIG_DFL):
            serve(config)

        assert signal.SIGTERM in installed_signals
        assert callable(installed_signals[signal.SIGTERM])

    def test_sigterm_handler_sends_sigint(self):
        """The installed SIGTERM handler forwards a SIGINT to the current process."""
        config = _make_config(tram_mode="standalone")
        captured_handler = {}

        def fake_signal(signum, handler):
            captured_handler[signum] = handler

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run"), \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal", side_effect=fake_signal), \
             patch("signal.getsignal", return_value=signal.SIG_DFL):
            serve(config)

        handler = captured_handler[signal.SIGTERM]

        with patch("os.kill") as mock_kill, \
             patch("os.getpid", return_value=12345):
            handler(signal.SIGTERM, None)

        mock_kill.assert_called_once_with(12345, signal.SIGINT)

    def test_sigterm_handler_restores_original(self):
        """The SIGTERM handler restores the original signal after firing."""
        config = _make_config(tram_mode="standalone")
        original_handler = MagicMock()
        installed: dict = {}

        def fake_signal(signum, handler):
            installed[signum] = handler

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run"), \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal", side_effect=fake_signal), \
             patch("signal.getsignal", return_value=original_handler), \
             patch("os.kill"), patch("os.getpid", return_value=1):
            serve(config)
            # Fire the handler inside the patch context so signal.signal is still mocked
            handler = installed[signal.SIGTERM]
            handler(signal.SIGTERM, None)

        # After the handler fires, signal.signal should have been called again
        # to restore the original; the restore call stores original_handler back.
        assert installed[signal.SIGTERM] is original_handler

    def test_worker_does_not_install_sigterm(self, monkeypatch):
        """Worker branch must NOT install a SIGTERM handler."""
        config = _make_config(tram_mode="worker")
        monkeypatch.delenv("TRAM_WORKER_PORT", raising=False)
        mock_signal = MagicMock()

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run"), \
             patch("tram.agent.server.create_worker_app", return_value=MagicMock()), \
             patch("signal.signal", mock_signal):
            serve(config)

        mock_signal.assert_not_called()


# ── Default config (config=None) ───────────────────────────────────────────────


class TestServeDefaultConfig:
    """Tests for the config=None code path."""

    def test_none_config_calls_from_env(self):
        """When config=None, serve() calls AppConfig.from_env() to build config."""
        fake_config = _make_config(tram_mode="standalone")

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run"), \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"), \
             patch("signal.getsignal", return_value=signal.SIG_DFL), \
             patch("tram.core.config.AppConfig.from_env", return_value=fake_config) as mock_fe:
            serve(None)

        mock_fe.assert_called_once()

    def test_none_config_uses_returned_config(self):
        """serve(None) uses the AppConfig returned by from_env for uvicorn kwargs."""
        fake_config = _make_config(tram_mode="standalone", port=9999, workers=3)

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run") as mock_run, \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"), \
             patch("signal.getsignal", return_value=signal.SIG_DFL), \
             patch("tram.core.config.AppConfig.from_env", return_value=fake_config):
            serve(None)

        _, kwargs = mock_run.call_args
        assert kwargs["port"] == 9999
        assert kwargs["workers"] == 3

    def test_explicit_config_bypasses_from_env(self):
        """When a config object is supplied, from_env() is never called."""
        config = _make_config(tram_mode="standalone")

        with patch("tram.daemon.server.setup_logging"), \
             patch("uvicorn.run"), \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"), \
             patch("signal.getsignal", return_value=signal.SIG_DFL), \
             patch("tram.core.config.AppConfig.from_env") as mock_fe:
            serve(config)

        mock_fe.assert_not_called()


# ── setup_logging integration ──────────────────────────────────────────────────


class TestServeLogging:
    def test_setup_logging_called_with_config_values(self):
        """serve() forwards log_level and log_format from config to setup_logging."""
        config = _make_config(tram_mode="standalone", log_level="DEBUG", log_format="text")

        with patch("tram.daemon.server.setup_logging") as mock_log, \
             patch("uvicorn.run"), \
             patch("tram.api.app.create_app", return_value=MagicMock()), \
             patch("signal.signal"), \
             patch("signal.getsignal", return_value=signal.SIG_DFL):
            serve(config)

        mock_log.assert_called_once_with(level="DEBUG", fmt="text")

    def test_setup_logging_called_for_worker_too(self, monkeypatch):
        """setup_logging is called even for the worker branch."""
        config = _make_config(tram_mode="worker")
        monkeypatch.delenv("TRAM_WORKER_PORT", raising=False)

        with patch("tram.daemon.server.setup_logging") as mock_log, \
             patch("uvicorn.run"), \
             patch("tram.agent.server.create_worker_app", return_value=MagicMock()):
            serve(config)

        mock_log.assert_called_once()
